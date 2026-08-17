"""Task #94 (2026-08-12, Chat Master): Instant RAG (search_uploaded_document)
never populated ctx._rag_call_rounds, so make_react_trace's rag_call_rounds
stayed empty on Instant RAG turns even though the corpus path (search_corpus)
already recorded one entry per real RAG HTTP call via _record_rag_round.
Turns completed normally ("First pass · N rounds") but the Diagnostics panel
showed no Call N blocks -- the react_trace plumbing itself was fine, only
this tool's calls were invisible to it.
"""
from __future__ import annotations

from unittest.mock import patch

from app.pipeline.context import PipelineContext


def _make_ctx(**extra) -> PipelineContext:
    ctx = PipelineContext(correlation_id="c-instant-rag-rounds", thread_id="t1", message="What does this say?")
    ctx.merged_state = {
        "active": {
            "uploaded_files": [
                {"upload_id": "u1", "filename": "policy.pdf", "purpose": "instant_rag", "document_id": "doc-1"},
            ],
        },
    }
    for k, v in extra.items():
        setattr(ctx, k, v)
    return ctx


class TestInstantRagRecordsCallRounds:
    def test_immediate_success_records_one_round(self):
        """The common case -- doc already indexed, first probe finds
        sources. Exactly one round, not zero."""
        from app.pipeline.react_loop import _execute_tool

        ctx = _make_ctx()
        with patch(
            "app.services.instant_rag_search.lazy_rag_search",
            return_value=("Found it.", [{"document_name": "policy.pdf", "index": 1, "text": "x"}], None, "corpus_only"),
        ):
            result = _execute_tool("search_uploaded_document", {"upload_id": "u1", "query": "What does this say?"}, ctx, emitter=lambda s: None)

        assert result["success"] is True
        rounds = getattr(ctx, "_rag_call_rounds", None)
        assert rounds, "search_uploaded_document must populate ctx._rag_call_rounds -- Task #94"
        assert len(rounds) == 1
        assert rounds[0]["round_n"] == 1
        assert rounds[0]["terminal_action"] == "corpus_only"
        assert rounds[0]["module_trace"]["tool"] == "search_uploaded_document"
        assert rounds[0]["module_trace"]["phase"] == "initial_probe"
        assert isinstance(rounds[0]["latency_ms"], float)

    def test_indexing_poll_records_a_round_per_attempt_including_misses(self):
        """Doc not ready on the first probe -- polls a couple times before
        landing. Each attempt (including the earlier no_sources misses) is
        its own round, in order -- rag_call_rounds isn't gated on success."""
        from app.pipeline.react_loop import _execute_tool

        ctx = _make_ctx()
        _calls = [
            ("", [], None, "no_sources"),
            ("", [], None, "no_sources"),
            ("Found it.", [{"document_name": "policy.pdf", "index": 1, "text": "x"}], None, "corpus_only"),
        ]

        def _fake(*a, **kw):
            return _calls.pop(0)

        with patch("app.services.instant_rag_search.lazy_rag_search", side_effect=_fake), \
             patch("time.sleep", return_value=None):
            result = _execute_tool("search_uploaded_document", {"upload_id": "u1", "query": "What does this say?"}, ctx, emitter=lambda s: None)

        assert result["success"] is True
        rounds = ctx._rag_call_rounds
        assert len(rounds) == 3
        assert [r["round_n"] for r in rounds] == [1, 2, 3]
        assert rounds[0]["module_trace"]["phase"] == "initial_probe"
        assert rounds[1]["module_trace"]["phase"] == "indexing_poll"
        assert rounds[2]["module_trace"]["phase"] == "indexing_poll"
        assert rounds[0]["terminal_action"] == "no_sources"
        assert rounds[1]["terminal_action"] == "no_sources"
        assert rounds[-1]["terminal_action"] == "corpus_only"
