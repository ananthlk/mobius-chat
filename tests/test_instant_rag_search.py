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

    def test_where_clause_scopes_to_document_id_and_instant_rag(self, monkeypatch):
        from app.services import instant_rag_search as m

        captured: dict = {}

        class FakeColl:
            def query(self, *, query_embeddings, n_results, where, include):
                captured["n_results"] = n_results
                captured["where"] = where
                captured["include"] = include
                return {
                    "ids":       [["c1"]],
                    "documents": [["the chunk text"]],
                    "metadatas": [[{"document_id": "doc-abc", "page_number": 1}]],
                    "distances": [[0.3]],
                }

        class FakeClient:
            def __init__(self, path):
                self.path = path

            def get_or_create_collection(self, name, metadata=None):
                return FakeColl()

        fake_chromadb = MagicMock()
        fake_chromadb.PersistentClient = FakeClient

        monkeypatch.setitem(
            __import__("sys").modules, "chromadb", fake_chromadb,
        )
        monkeypatch.setattr(
            "app.chat_config.get_chat_config",
            lambda: _fake_cfg(),
            raising=True,
        )
        monkeypatch.setattr(
            "app.services.embedding_provider.get_query_embedding",
            lambda q: [0.1] * 8,
            raising=True,
        )

        answer, sources, usage, signal = m.lazy_rag_search(
            document_id="doc-abc",
            question="what does it say about X",
            k=5,
        )

        # Where clause must AND document_id + instant_rag=true.
        assert captured["where"] == {
            "$and": [
                {"document_id": "doc-abc"},
                {"instant_rag": "true"},
            ]
        }, (
            "Lazy RAG must scope strictly to (document_id AND instant_rag=true) "
            "so it can't accidentally surface corpus chunks. The tool is a "
            "dedicated path for uploaded docs only."
        )
        assert captured["n_results"] == 5
        # Cosine distance 0.3 → similarity ≈ 0.85.
        assert sources[0]["rerank_score"] == pytest.approx(1.0 - 0.3 / 2.0)
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
        from app.services import instant_rag_search as m

        class FakeColl:
            def query(self, **kw):
                return {
                    "ids":       [["c1", "c2", "c3"]],
                    "documents": [["alpha", "beta", "gamma"]],
                    "metadatas": [[{}, {}, {}]],
                    "distances": [[0.1, 0.2, 0.3]],
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

        answer, sources, _, _ = m.lazy_rag_search(document_id="doc-abc", question="q")
        assert len(sources) == 3
        assert answer == "alpha\n\n---\n\nbeta\n\n---\n\ngamma", (
            "Chunks must be joined with a clear separator so the integrator "
            "can see boundaries."
        )


# ── ReAct tool wiring — manifest + capability presence ─────────────────────


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
