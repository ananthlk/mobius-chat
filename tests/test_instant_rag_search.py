"""Phase B.1 — lazy-RAG (instant-RAG) search tests.

Covers two surfaces:

1. ``_resolve_upload_document_id`` in react_loop — thread-state lookup
   that maps a user-facing ``upload_id`` to the ``document_id`` stored
   with the chunks. This is the only logic that has to stay correct
   for the tool to ever find the right doc.

2. ``lazy_rag_search`` in app.services.instant_rag_search — the dedicated
   Chroma vector-search path that skips the main RAG pipeline's J/P/D
   tagger + tag-match rerank + confidence filter. The tests here mock
   Chroma so we exercise the shaping logic (query construction, score
   conversion, return-tuple shape) without a live vector store. A live
   integration test belongs in the manual smoke suite.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_skills_core_chroma_cache():
    """Reset the shared Chroma collection cache between tests.

    lazy_rag + corpus_search in mobius-skills-core keep a module-level
    cache of chromadb collection handles to avoid the HNSW-index load
    cost on every query. Tests that monkey-patch chromadb need the
    cache cleared so a stale mock from a previous test doesn't leak.
    """
    try:
        from mobius_skills_core.skills.corpus_search import _reset_chroma_cache
        _reset_chroma_cache()
    except ImportError:
        pass
    yield
    try:
        from mobius_skills_core.skills.corpus_search import _reset_chroma_cache
        _reset_chroma_cache()
    except ImportError:
        pass


# ── helper: thread-state upload_id → document_id resolution ────────────────


class TestResolveUploadDocumentId:
    """Covers _resolve_upload_document_id (react_loop.py).

    The tool trusts this function to translate the user-facing upload_id
    to the internal document_id that indexes Chroma. Getting this wrong
    either makes the tool silently search the wrong doc or never match.
    """

    def _helper(self):
        from app.pipeline.react_loop import _resolve_upload_document_id
        return _resolve_upload_document_id

    def test_returns_document_id_when_matched(self):
        resolve = self._helper()
        active = {
            "uploaded_files": [
                {"upload_id": "u-1", "document_id": "doc-abc", "purpose": "instant_rag"},
            ]
        }
        assert resolve(active, "u-1") == "doc-abc"

    def test_returns_none_when_no_match(self):
        resolve = self._helper()
        active = {"uploaded_files": [{"upload_id": "u-1", "document_id": "doc-abc"}]}
        assert resolve(active, "u-999") is None

    def test_returns_none_for_empty_upload_id(self):
        resolve = self._helper()
        active = {"uploaded_files": [{"upload_id": "u-1", "document_id": "doc-abc"}]}
        assert resolve(active, "") is None
        assert resolve(active, "  ") is None

    def test_skips_records_without_document_id(self):
        """Roster-reconciliation uploads get an upload_id but no
        document_id (no searchable chunks). Those must be skipped — if
        the user passes a roster upload_id here, we don't pretend to
        resolve it."""
        resolve = self._helper()
        active = {
            "uploaded_files": [
                {"upload_id": "u-1", "purpose": "roster_reconciliation"},  # no document_id
                {"upload_id": "u-1", "document_id": "doc-abc"},
            ]
        }
        # First matching record has no document_id — keep scanning.
        assert resolve(active, "u-1") == "doc-abc"

    def test_empty_document_id_string_skipped(self):
        resolve = self._helper()
        active = {"uploaded_files": [{"upload_id": "u-1", "document_id": ""}]}
        assert resolve(active, "u-1") is None

    def test_non_dict_entries_ignored(self):
        resolve = self._helper()
        active = {
            "uploaded_files": [
                "not-a-dict",
                None,
                {"upload_id": "u-1", "document_id": "doc-abc"},
            ]
        }
        assert resolve(active, "u-1") == "doc-abc"

    def test_empty_active_state(self):
        resolve = self._helper()
        assert resolve({}, "u-1") is None
        assert resolve({"uploaded_files": []}, "u-1") is None


# ── lazy_rag_search — argument validation ──────────────────────────────────


class TestLazyRagSearchArgumentHandling:
    def test_missing_document_id_returns_no_sources(self):
        from app.services.instant_rag_search import lazy_rag_search
        answer, sources, usage, signal = lazy_rag_search("", "hello")
        assert sources == []
        assert signal == "no_sources"

    def test_missing_question_returns_no_sources(self):
        from app.services.instant_rag_search import lazy_rag_search
        answer, sources, usage, signal = lazy_rag_search("doc-abc", "   ")
        assert sources == []
        assert signal == "no_sources"


# ── lazy_rag_search — the Chroma round-trip ────────────────────────────────


def _fake_cfg(chroma_persist_dir: str = "/tmp/chroma-test", collection: str = "test_coll"):
    rag = SimpleNamespace(
        chroma_persist_dir=chroma_persist_dir,
        chroma_collection=collection,
        vector_store="chroma",
    )
    return SimpleNamespace(rag=rag)


class TestLazyRagSearchChromaQuery:
    """Mocks Chroma to assert the query shape + return contract."""

    def test_request_scopes_to_document_id(self, monkeypatch):
        """RAG /api/query request body must include document_id so pgvector
        search is scoped to the uploaded document — not the whole corpus."""
        import io
        import json
        import urllib.request
        from app.services import instant_rag_search as m

        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data)
            payload = json.dumps({
                "chunks": [
                    {
                        "source_id": "c1",
                        "text": "the chunk text",
                        "document_id": "doc-abc",
                        "page_number": 1,
                        "similarity": 0.85,
                    }
                ]
            }).encode()
            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            resp.read = lambda: payload
            return resp

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setenv("MOBIUS_RAG_URL", "http://rag-test")

        answer, sources, usage, signal = m.lazy_rag_search(
            document_id="doc-abc",
            question="what does it say about X",
            k=5,
        )

        assert captured["body"]["document_id"] == "doc-abc", (
            "Lazy RAG must send document_id in the request body so pgvector "
            "scopes the search to just the uploaded document."
        )
        assert captured["body"]["k"] == 5
        assert captured["url"].endswith("/api/query")
        assert sources[0]["rerank_score"] == pytest.approx(0.85)
        assert sources[0]["text"] == "the chunk text"
        assert signal == "corpus_only"
        assert answer == "the chunk text"

    def test_empty_chroma_result_returns_no_sources_signal(self, monkeypatch):
        from app.services import instant_rag_search as m

        class FakeColl:
            def query(self, **kw):
                return {
                    "ids": [[]], "documents": [[]],
                    "metadatas": [[]], "distances": [[]],
                }

        class FakeClient:
            def __init__(self, path): pass
            def get_or_create_collection(self, name, metadata=None): return FakeColl()

        monkeypatch.setitem(__import__("sys").modules, "chromadb", MagicMock(PersistentClient=FakeClient))
        monkeypatch.setattr("app.chat_config.get_chat_config", lambda: _fake_cfg(), raising=True)
        monkeypatch.setattr(
            "app.services.embedding_provider.get_query_embedding",
            lambda q: [0.0] * 8, raising=True,
        )

        answer, sources, usage, signal = m.lazy_rag_search(
            document_id="doc-empty", question="anything",
        )
        assert sources == []
        assert signal == "no_sources", (
            "Empty chroma result must set signal=no_sources so the ReAct "
            "retry guard records a failed attempt for this tool — otherwise "
            "Phase 0.19 can't detect exhaustion."
        )

    def test_embedding_failure_is_swallowed_as_no_sources(self, monkeypatch):
        from app.services import instant_rag_search as m

        monkeypatch.setattr("app.chat_config.get_chat_config", lambda: _fake_cfg(), raising=True)
        monkeypatch.setattr(
            "app.services.embedding_provider.get_query_embedding",
            MagicMock(side_effect=RuntimeError("provider down")),
            raising=True,
        )

        answer, sources, usage, signal = m.lazy_rag_search(
            document_id="doc-abc", question="anything",
        )
        # Must NOT raise; the retry guard will record a failed attempt
        # and the planner pivots on the next round.
        assert sources == []
        assert signal == "no_sources"

    def test_no_chroma_persist_dir_returns_no_sources(self, monkeypatch):
        from app.services import instant_rag_search as m

        monkeypatch.setattr(
            "app.chat_config.get_chat_config",
            lambda: _fake_cfg(chroma_persist_dir=""),
            raising=True,
        )
        answer, sources, usage, signal = m.lazy_rag_search(
            document_id="doc-abc", question="anything",
        )
        assert sources == []
        assert signal == "no_sources"

    def test_no_llm_synthesis_no_usage(self, monkeypatch):
        """The tool must NOT burn LLM tokens — synthesis happens once at
        the integrator. If this test ever fails because usage is non-None,
        something is wired to call an LLM from within the retrieval path."""
        from app.services import instant_rag_search as m

        class FakeColl:
            def query(self, **kw):
                return {
                    "ids": [["c1"]],
                    "documents": [["text"]],
                    "metadatas": [[{}]],
                    "distances": [[0.1]],
                }

        class FakeClient:
            def __init__(self, path): pass
            def get_or_create_collection(self, name, metadata=None): return FakeColl()

        monkeypatch.setitem(__import__("sys").modules, "chromadb", MagicMock(PersistentClient=FakeClient))
        monkeypatch.setattr("app.chat_config.get_chat_config", lambda: _fake_cfg(), raising=True)
        monkeypatch.setattr(
            "app.services.embedding_provider.get_query_embedding",
            lambda q: [0.0] * 8, raising=True,
        )

        _, _, usage, _ = m.lazy_rag_search(document_id="doc-abc", question="q")
        assert usage is None

    def test_multiple_chunks_joined_with_separator(self, monkeypatch):
        import json
        import urllib.request
        from app.services import instant_rag_search as m

        def fake_urlopen(req, timeout=None):
            payload = json.dumps({
                "chunks": [
                    {"source_id": "c1", "text": "alpha", "document_id": "doc-abc"},
                    {"source_id": "c2", "text": "beta",  "document_id": "doc-abc"},
                    {"source_id": "c3", "text": "gamma", "document_id": "doc-abc"},
                ]
            }).encode()
            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            resp.read = lambda: payload
            return resp

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setenv("MOBIUS_RAG_URL", "http://rag-test")

        answer, sources, _, _ = m.lazy_rag_search(document_id="doc-abc", question="q")
        assert len(sources) == 3
        assert answer == "alpha\n\nbeta\n\ngamma", (
            "Chunks must be joined with double newline so the integrator "
            "can see boundaries."
        )


# ── ReAct tool wiring — manifest + capability presence ─────────────────────


class TestReasoningContextSurfacesUploads:
    """Regression for the 2026-04-17 planner-is-blind bug.

    When a thread has instant_rag uploads in active.uploaded_files[], the
    reasoning context passed to the planner LLM MUST mention them, along
    with a nudge to prefer search_uploaded_document over search_corpus
    for self-referential questions. Otherwise the user uploads a doc,
    asks "what is in this document", and the planner goes hunting in
    the main corpus — returns "I was unable to find information about
    the document."
    """

    def _build_ctx(self, active: dict) -> "Any":
        """Minimal PipelineContext-shaped object for build_reasoning_context."""
        from types import SimpleNamespace
        return SimpleNamespace(
            merged_state={"active": active},
            active_context=None,
            failed_query=None,
            last_turns=None,
            effective_message="what is in this document",
            message="what is in this document",
        )

    def test_planner_sees_upload_when_one_exists(self):
        from app.pipeline.react_loop import build_reasoning_context
        active = {
            "uploaded_files": [
                {
                    "upload_id": "u-1",
                    "document_id": "doc-abc",
                    "filename": "Sunshine-Provider-Manual.pdf",
                    "purpose": "instant_rag",
                    "row_count": 287,
                },
            ],
        }
        ctx_reasoning = build_reasoning_context(self._build_ctx(active), [], iteration=1)
        assert "Documents attached to this thread" in ctx_reasoning, (
            "Reasoning context doesn't mention uploaded docs — the planner "
            "won't know search_uploaded_document is relevant. This is the "
            "2026-04-17 blind-planner regression."
        )
        assert "Sunshine-Provider-Manual.pdf" in ctx_reasoning, (
            "Filename not surfaced — planner can't discriminate when multiple uploads exist."
        )
        assert "search_uploaded_document" in ctx_reasoning, (
            "Reasoning context must tell the planner which tool to pick; "
            "otherwise it defaults to search_corpus and misses."
        )
        assert "search_corpus does not find" in ctx_reasoning, (
            "Must explicitly warn that search_corpus misses these. Without "
            "the negative nudge, planner may still pick search_corpus first."
        )

    def test_no_upload_section_when_none_attached(self):
        """Don't noise up the reasoning context when there are no uploads."""
        from app.pipeline.react_loop import build_reasoning_context
        active = {"uploaded_files": []}
        ctx_reasoning = build_reasoning_context(self._build_ctx(active), [], iteration=1)
        assert "Documents attached to this thread" not in ctx_reasoning

    def test_roster_uploads_skipped(self):
        """Only instant_rag uploads are searchable via the tool. Roster-
        reconciliation uploads have no document_id (no chunks) — surfacing
        them would make the planner pick search_uploaded_document and fail."""
        from app.pipeline.react_loop import build_reasoning_context
        active = {
            "uploaded_files": [
                {"upload_id": "r-1", "purpose": "roster_reconciliation", "filename": "roster.csv"},
            ],
        }
        ctx_reasoning = build_reasoning_context(self._build_ctx(active), [], iteration=1)
        assert "Documents attached to this thread" not in ctx_reasoning

    def test_only_records_with_document_id_surfaced(self):
        """An instant_rag record without a document_id is unsearchable —
        likely a partial upload that never completed ingest. Don't advertise it."""
        from app.pipeline.react_loop import build_reasoning_context
        active = {
            "uploaded_files": [
                {"upload_id": "u-bad", "purpose": "instant_rag", "filename": "broken.pdf"},  # no document_id
                {"upload_id": "u-ok", "purpose": "instant_rag", "document_id": "doc-ok", "filename": "good.pdf"},
            ],
        }
        ctx_reasoning = build_reasoning_context(self._build_ctx(active), [], iteration=1)
        assert "good.pdf" in ctx_reasoning
        assert "broken.pdf" not in ctx_reasoning

    def test_more_than_10_uploads_caps_at_10(self):
        """Threads with many uploads shouldn't fill the reasoning context
        with the whole list — the first 10 is enough to cue the planner."""
        from app.pipeline.react_loop import build_reasoning_context
        active = {
            "uploaded_files": [
                {"upload_id": f"u-{i}", "purpose": "instant_rag",
                 "document_id": f"doc-{i}", "filename": f"file-{i}.pdf"}
                for i in range(25)
            ],
        }
        ctx_reasoning = build_reasoning_context(self._build_ctx(active), [], iteration=1)
        # First 10 present:
        for i in range(10):
            assert f"file-{i}.pdf" in ctx_reasoning
        # 11th onwards NOT present:
        assert "file-15.pdf" not in ctx_reasoning


class TestUploadPersistenceIsSynchronous:
    """2026-04-17 race-condition fix. The instant-rag upload handler used
    to fire the thread-state save on a daemon thread and return
    immediately. The frontend's sendMessage() could win the race and
    start a chat turn before the uploaded_files[] record was written —
    then the ReAct loop would read empty thread state and the planner
    never saw the upload.

    Lock in that the persistence is synchronous by grepping the source
    for the anti-pattern.
    """

    def test_instant_rag_upload_does_not_use_daemon_thread(self):
        from pathlib import Path
        import re

        main_py = Path(__file__).parent.parent / "app" / "main.py"
        text = main_py.read_text()

        # Find the _handle_instant_rag_upload function body — we need to
        # scope the check to that function specifically, since other
        # upload paths legitimately use daemon threads for bg work.
        match = re.search(
            r"def _handle_instant_rag_upload\b.*?(?=\n(?:def |@app\.)|\Z)",
            text,
            re.DOTALL,
        )
        assert match, "_handle_instant_rag_upload function not found in main.py"
        body = match.group(0)

        # The append_uploaded_file_record call must NOT be in a daemon
        # thread within this function. Pre-fix pattern was:
        #   _threading.Thread(target=_persist, ...).start()
        assert "_threading.Thread" not in body or "append_uploaded_file_record" not in body.split("_threading.Thread")[0] + body.split("_threading.Thread")[-1], (
            "instant-rag upload handler wraps append_uploaded_file_record "
            "in a daemon thread — that's the race condition that made the "
            "chat turn fire before the upload was in thread state."
        )

        # Stronger positive check: synchronous call to
        # append_uploaded_file_record must be present.
        assert "append_uploaded_file_record(" in body, (
            "instant-rag upload handler doesn't persist the upload record "
            "at all — the next chat turn won't see it."
        )


class TestToolRegistration:
    """The tool is inert unless the planner LLM sees it listed in the
    manifest and capabilities. These tests lock in that registration."""

    def test_manifest_lists_search_uploaded_document(self):
        from app.pipeline.tool_manifest import TOOL_MANIFEST
        assert "search_uploaded_document" in TOOL_MANIFEST, (
            "search_uploaded_document isn't in TOOL_MANIFEST — the planner "
            "LLM won't know to pick it. Re-add the entry in tool_manifest.py."
        )

    def test_capabilities_entry_exists(self):
        from app.stages.agents.capabilities import TOOL_CAPABILITIES
        assert "search_uploaded_document" in TOOL_CAPABILITIES

    def test_tool_stage_mapping_present(self):
        """Usage emissions use _TOOL_STAGE_FOR_USAGE to route metrics.
        Missing mapping means usage rows get a generic 'tool_' prefix and
        the LLM-performance UI can't group them."""
        from app.pipeline.react_loop import _TOOL_STAGE_FOR_USAGE
        assert _TOOL_STAGE_FOR_USAGE.get("search_uploaded_document") == "rag"
