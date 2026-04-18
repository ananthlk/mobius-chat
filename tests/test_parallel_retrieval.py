"""Phase B.4 — parallel retrieval (search_corpus + lazy-RAG fan-out).

When the planner picks ``search_corpus`` and the thread has instant_rag
uploads, the ReAct dispatcher fans out parallel ``lazy_rag_search`` calls
against each upload and merges results. Rationale (2026-04-17): the
planner was correctly picking search_corpus for "what does Sunshine say
about H0036?" even when the user had a Sunshine doc attached, because
the payer keyword dominated tool selection. Fan-out removes the binary
choice — the integrator gets both curated corpus AND upload chunks in
one retrieval round, no extra planner round.

Boundaries locked in here:
  1. No uploads: behaves exactly like pre-B.4 (no fan-out, no extra calls)
  2. With uploads: both paths run concurrently; merged sources include both
  3. The other direction (search_uploaded_document → search_corpus) is
     intentionally NOT fanned out (user's "my doc" intent is scoped)
  4. Upload failures are isolated — one bad doc doesn't kill the corpus result
  5. Corpus failure still returns upload chunks (not a hard block)
  6. Cap at 3 upload fan-outs per turn + 15 merged chunks total
  7. Tool name stays "search_corpus" in the return dict so the 0.19 retry
     guard and per-tool observability still work
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────


def _mock_ctx(
    thread_id: str = "t-test",
    message: str = "what does sunshine say about h0036",
    active: dict | None = None,
) -> SimpleNamespace:
    """Minimal PipelineContext shape for the dispatcher."""
    return SimpleNamespace(
        thread_id=thread_id,
        correlation_id="c-test",
        message=message,
        effective_message=message,
        merged_state={"active": active or {}},
        chat_mode="copilot",
        usages=[],
    )


def _patch_corpus(return_value):
    """Patch the answer_non_patient retriever used inside react_loop.

    react_loop imports answer_non_patient at module top, so we patch the
    attribute on the react_loop module (not the source module).
    """
    return patch("app.pipeline.react_loop.answer_non_patient", return_value=return_value)


def _patch_lazy(return_value_or_side_effect):
    """Patch lazy_rag_search used inside react_loop. Note react_loop uses
    a deferred import inside the search_corpus branch (``from
    app.services.instant_rag_search import lazy_rag_search``) so we have
    to patch the source module."""
    from unittest.mock import patch as _patch
    return _patch(
        "app.services.instant_rag_search.lazy_rag_search",
        return_value_or_side_effect if not callable(return_value_or_side_effect) else None,
        side_effect=return_value_or_side_effect if callable(return_value_or_side_effect) else None,
    )


def _rag_filters_empty(*args, **kwargs):
    return {}


# ── Guard rail: no uploads → no fan-out ──────────────────────────────────


class TestNoUploadsBehavesLikePreB4:
    """When the thread has no instant_rag uploads, search_corpus must run
    exactly once and the return shape must match the pre-B.4 contract
    (no 'fanned_out_to' key, or an empty list)."""

    def test_only_corpus_runs_when_no_uploads(self):
        from app.pipeline.react_loop import _execute_tool

        corpus_result = ("some corpus answer long enough " * 20, [{"id": "c1", "text": "chunk"}], None, "corpus_only")

        lazy_mock = MagicMock()
        with _patch_corpus(corpus_result), \
             patch("app.pipeline.react_loop.rag_filters_from_active", _rag_filters_empty), \
             patch("app.services.instant_rag_search.lazy_rag_search", lazy_mock):
            ctx = _mock_ctx(active={"uploaded_files": []})
            out = _execute_tool("search_corpus", {"query": "q"}, ctx, emitter=None)

        assert out["tool"] == "search_corpus"
        assert out["success"] is True
        lazy_mock.assert_not_called(), (
            "lazy_rag_search must not run when no instant_rag uploads are on the thread."
        )
        assert out.get("fanned_out_to", []) == []
        assert out.get("upload_chunks_total", 0) == 0

    def test_roster_upload_does_not_trigger_fanout(self):
        """Roster-reconciliation uploads have no document_id and no
        chunks; they must be filtered out of the fan-out candidate pool."""
        from app.pipeline.react_loop import _execute_tool

        corpus_result = ("corpus long answer " * 20, [{"id": "c1"}], None, "corpus_only")
        lazy_mock = MagicMock()
        with _patch_corpus(corpus_result), \
             patch("app.pipeline.react_loop.rag_filters_from_active", _rag_filters_empty), \
             patch("app.services.instant_rag_search.lazy_rag_search", lazy_mock):
            ctx = _mock_ctx(active={
                "uploaded_files": [
                    {"upload_id": "r-1", "purpose": "roster_reconciliation", "filename": "roster.csv"},
                ],
            })
            _execute_tool("search_corpus", {"query": "q"}, ctx, emitter=None)
        lazy_mock.assert_not_called()

    def test_instant_rag_without_document_id_filtered(self):
        """Partial upload that never got a document_id is unsearchable —
        must not trigger the fan-out."""
        from app.pipeline.react_loop import _execute_tool

        corpus_result = ("corpus long answer " * 20, [{"id": "c1"}], None, "corpus_only")
        lazy_mock = MagicMock()
        with _patch_corpus(corpus_result), \
             patch("app.pipeline.react_loop.rag_filters_from_active", _rag_filters_empty), \
             patch("app.services.instant_rag_search.lazy_rag_search", lazy_mock):
            ctx = _mock_ctx(active={
                "uploaded_files": [
                    {"upload_id": "u-1", "purpose": "instant_rag"},  # no document_id
                ],
            })
            _execute_tool("search_corpus", {"query": "q"}, ctx, emitter=None)
        lazy_mock.assert_not_called()


# ── Happy path: fan-out merges corpus + upload sources ───────────────────


class TestFanOutMergesSources:
    def _active_with_one_upload(self):
        return {
            "uploaded_files": [
                {
                    "upload_id": "u-1",
                    "document_id": "doc-abc",
                    "filename": "Sunshine-Manual.pdf",
                    "purpose": "instant_rag",
                },
            ],
        }

    def test_both_retrievers_run(self):
        from app.pipeline.react_loop import _execute_tool

        corpus_result = ("corpus spine answer " * 20, [{"id": "c1", "text": "corpus c1"}], None, "corpus_only")
        lazy_mock = MagicMock(return_value=(
            "upload snippet",
            [{"id": "u-s1", "text": "upload s1"}, {"id": "u-s2", "text": "upload s2"}],
            None,
            "corpus_only",
        ))

        with _patch_corpus(corpus_result), \
             patch("app.pipeline.react_loop.rag_filters_from_active", _rag_filters_empty), \
             patch("app.services.instant_rag_search.lazy_rag_search", lazy_mock):
            ctx = _mock_ctx(active=self._active_with_one_upload())
            out = _execute_tool("search_corpus", {"query": "q"}, ctx, emitter=None)

        # Both called exactly once.
        lazy_mock.assert_called_once()
        # Lazy called with the right document_id.
        call_kwargs = lazy_mock.call_args.kwargs
        assert call_kwargs.get("document_id") == "doc-abc"

        # Merged sources include both sides.
        src_ids = [s.get("id") for s in out["sources"]]
        assert "c1" in src_ids, "corpus source dropped in merge"
        assert "u-s1" in src_ids and "u-s2" in src_ids, "upload sources dropped in merge"

        assert "u-1" in out["fanned_out_to"]
        assert out["upload_chunks_total"] == 2
        assert out["success"] is True

    def test_multiple_uploads_fanned_out(self):
        from app.pipeline.react_loop import _execute_tool

        active = {
            "uploaded_files": [
                {"upload_id": f"u-{i}", "document_id": f"doc-{i}",
                 "filename": f"f{i}.pdf", "purpose": "instant_rag"}
                for i in range(5)
            ],
        }
        corpus_result = ("corpus answer long enough " * 20, [], None, "no_sources")
        lazy_mock = MagicMock(return_value=("snippet", [{"id": "s"}], None, "corpus_only"))

        with _patch_corpus(corpus_result), \
             patch("app.pipeline.react_loop.rag_filters_from_active", _rag_filters_empty), \
             patch("app.services.instant_rag_search.lazy_rag_search", lazy_mock):
            ctx = _mock_ctx(active=active)
            out = _execute_tool("search_corpus", {"query": "q"}, ctx, emitter=None)

        # Cap at 3 parallel upload fan-outs (contract documented in the code).
        assert lazy_mock.call_count == 3, (
            f"Expected 3 parallel lazy_rag_search calls (cap), got {lazy_mock.call_count}. "
            "Relaxing this cap risks diluting the integrator's context budget."
        )
        # First three upload_ids should be in fanned_out_to (order preserved).
        assert out["fanned_out_to"] == ["u-0", "u-1", "u-2"]

    def test_merged_sources_capped_at_15(self):
        from app.pipeline.react_loop import _execute_tool

        corpus_sources = [{"id": f"c{i}", "text": f"c{i}"} for i in range(12)]
        upload_sources = [{"id": f"u{i}", "text": f"u{i}"} for i in range(8)]
        corpus_result = ("corpus long answer " * 20, corpus_sources, None, "corpus_only")
        lazy_mock = MagicMock(return_value=("snippet", upload_sources, None, "corpus_only"))

        with _patch_corpus(corpus_result), \
             patch("app.pipeline.react_loop.rag_filters_from_active", _rag_filters_empty), \
             patch("app.services.instant_rag_search.lazy_rag_search", lazy_mock):
            ctx = _mock_ctx(active=self._active_with_one_upload())
            out = _execute_tool("search_corpus", {"query": "q"}, ctx, emitter=None)

        assert len(out["sources"]) <= 15, (
            f"Merged sources exceeded cap: got {len(out['sources'])}, limit 15. "
            "Over-cap dilutes integrator context budget."
        )


# ── Partial failure semantics ─────────────────────────────────────────────


class TestPartialFailureHandling:
    def _active(self):
        return {
            "uploaded_files": [
                {"upload_id": "u-1", "document_id": "doc-ok",
                 "filename": "ok.pdf", "purpose": "instant_rag"},
                {"upload_id": "u-2", "document_id": "doc-bad",
                 "filename": "bad.pdf", "purpose": "instant_rag"},
            ],
        }

    def test_one_upload_failure_does_not_kill_corpus(self):
        """Exception raised from one lazy_rag_search call must be swallowed
        so the corpus result + other uploads still reach the integrator."""
        from app.pipeline.react_loop import _execute_tool

        def lazy_side_effect(**kwargs):
            if kwargs.get("document_id") == "doc-bad":
                raise RuntimeError("Chroma down")
            return ("snippet", [{"id": "ok-s1"}], None, "corpus_only")

        corpus_result = ("corpus long answer " * 20, [{"id": "c1"}], None, "corpus_only")

        with _patch_corpus(corpus_result), \
             patch("app.pipeline.react_loop.rag_filters_from_active", _rag_filters_empty), \
             patch("app.services.instant_rag_search.lazy_rag_search", side_effect=lazy_side_effect):
            ctx = _mock_ctx(active=self._active())
            out = _execute_tool("search_corpus", {"query": "q"}, ctx, emitter=None)

        # The good one contributed; the bad one silently dropped.
        assert out["success"] is True
        assert "ok-s1" in [s.get("id") for s in out["sources"]]
        # fanned_out_to should only list the upload that actually returned sources.
        assert out["fanned_out_to"] == ["u-1"]

    def test_corpus_failure_still_returns_upload_chunks(self):
        """If the main corpus retriever raises, uploaded-doc chunks must
        still reach the integrator. Partial retrieval beats empty."""
        from app.pipeline.react_loop import _execute_tool

        corpus_mock = MagicMock(side_effect=RuntimeError("retriever_backend down"))
        lazy_mock = MagicMock(return_value=("upload only", [{"id": "u-s"}], None, "corpus_only"))

        with patch("app.pipeline.react_loop.answer_non_patient", corpus_mock), \
             patch("app.pipeline.react_loop.rag_filters_from_active", _rag_filters_empty), \
             patch("app.services.instant_rag_search.lazy_rag_search", lazy_mock):
            ctx = _mock_ctx(active={
                "uploaded_files": [
                    {"upload_id": "u-1", "document_id": "doc-abc",
                     "filename": "f.pdf", "purpose": "instant_rag"},
                ],
            })
            out = _execute_tool("search_corpus", {"query": "q"}, ctx, emitter=None)

        assert out["upload_chunks_total"] == 1
        assert out["success"] is True, (
            "With upload chunks still present, the turn must succeed even "
            "when corpus failed — otherwise the user gets a refusal despite "
            "usable evidence in hand."
        )
        # signal must not be no_sources when we have upload chunks.
        assert out["signal"] != "no_sources"

    def test_both_empty_returns_no_sources(self):
        from app.pipeline.react_loop import _execute_tool

        corpus_result = ("", [], None, "no_sources")
        lazy_mock = MagicMock(return_value=("", [], None, "no_sources"))

        with _patch_corpus(corpus_result), \
             patch("app.pipeline.react_loop.rag_filters_from_active", _rag_filters_empty), \
             patch("app.services.instant_rag_search.lazy_rag_search", lazy_mock):
            ctx = _mock_ctx(active={
                "uploaded_files": [
                    {"upload_id": "u-1", "document_id": "doc-abc",
                     "filename": "f.pdf", "purpose": "instant_rag"},
                ],
            })
            out = _execute_tool("search_corpus", {"query": "q"}, ctx, emitter=None)

        assert out["success"] is False
        assert out["signal"] == "no_sources", (
            "Both paths empty must set signal=no_sources so the 0.19 retry "
            "guard records a failed attempt."
        )


# ── Concurrency: parallel execution (not sequential) ─────────────────────


class TestActuallyParallel:
    """If fan-out ran sequentially, total latency = corpus_latency +
    upload_latency. Parallel should be approximately max(corpus, upload).
    Verify by instrumenting timing."""

    def test_parallel_not_sequential(self):
        from app.pipeline.react_loop import _execute_tool

        CORPUS_DELAY = 0.30
        UPLOAD_DELAY = 0.30

        def slow_corpus(**kwargs):
            time.sleep(CORPUS_DELAY)
            return ("corpus answer long " * 20, [{"id": "c1"}], None, "corpus_only")

        def slow_lazy(**kwargs):
            time.sleep(UPLOAD_DELAY)
            return ("snippet", [{"id": "u1"}], None, "corpus_only")

        with patch("app.pipeline.react_loop.answer_non_patient", side_effect=slow_corpus), \
             patch("app.pipeline.react_loop.rag_filters_from_active", _rag_filters_empty), \
             patch("app.services.instant_rag_search.lazy_rag_search", side_effect=slow_lazy):
            ctx = _mock_ctx(active={
                "uploaded_files": [
                    {"upload_id": "u-1", "document_id": "doc-abc",
                     "filename": "f.pdf", "purpose": "instant_rag"},
                ],
            })
            t0 = time.perf_counter()
            _execute_tool("search_corpus", {"query": "q"}, ctx, emitter=None)
            elapsed = time.perf_counter() - t0

        # Sequential would be ≥ 0.60s. Parallel should be closer to 0.30s.
        # Allow generous slack for CI variance but assert we're nowhere
        # near sequential.
        assert elapsed < (CORPUS_DELAY + UPLOAD_DELAY) * 0.85, (
            f"Retrieval took {elapsed:.2f}s — looks sequential "
            f"(both would be ~{CORPUS_DELAY + UPLOAD_DELAY:.2f}s). "
            f"ThreadPoolExecutor is supposed to run them in parallel."
        )


# ── Contract: tool name stays "search_corpus" ─────────────────────────────


class TestRetryGuardAndObservabilityContract:
    """The Phase 0.19 retry guard records attempts by tool name.
    Changing the tool name to something new would break the guard's
    same-signature block and the exhaustion counter. B.4 fans out
    internally but keeps the outer contract."""

    def test_tool_name_unchanged(self):
        from app.pipeline.react_loop import _execute_tool

        corpus_result = ("corpus answer long " * 20, [{"id": "c1"}], None, "corpus_only")
        lazy_mock = MagicMock(return_value=("snippet", [{"id": "u1"}], None, "corpus_only"))

        with _patch_corpus(corpus_result), \
             patch("app.pipeline.react_loop.rag_filters_from_active", _rag_filters_empty), \
             patch("app.services.instant_rag_search.lazy_rag_search", lazy_mock):
            ctx = _mock_ctx(active={
                "uploaded_files": [
                    {"upload_id": "u-1", "document_id": "doc-abc",
                     "filename": "f.pdf", "purpose": "instant_rag"},
                ],
            })
            out = _execute_tool("search_corpus", {"query": "q"}, ctx, emitter=None)

        assert out["tool"] == "search_corpus", (
            "B.4 must keep the tool name as 'search_corpus' so the 0.19 "
            "retry guard + _TOOL_STAGE_FOR_USAGE mapping + per-tool metrics "
            "all continue to work."
        )

    def test_observability_fields_present(self):
        """B.4 adds fanned_out_to + upload_chunks_total so ops can tell
        from the tool_result dict whether parallel retrieval happened."""
        from app.pipeline.react_loop import _execute_tool

        corpus_result = ("corpus answer long " * 20, [{"id": "c1"}], None, "corpus_only")
        lazy_mock = MagicMock(return_value=("snippet", [{"id": "u1"}], None, "corpus_only"))

        with _patch_corpus(corpus_result), \
             patch("app.pipeline.react_loop.rag_filters_from_active", _rag_filters_empty), \
             patch("app.services.instant_rag_search.lazy_rag_search", lazy_mock):
            ctx = _mock_ctx(active={
                "uploaded_files": [
                    {"upload_id": "u-1", "document_id": "doc-abc",
                     "filename": "f.pdf", "purpose": "instant_rag"},
                ],
            })
            out = _execute_tool("search_corpus", {"query": "q"}, ctx, emitter=None)

        assert "fanned_out_to" in out
        assert "upload_chunks_total" in out
        assert out["fanned_out_to"] == ["u-1"]
        assert out["upload_chunks_total"] == 1
