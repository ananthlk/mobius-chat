"""§8.1 — deterministic resolve-by-ID on fetch_document.

Contract (DOWNLOAD_AGENT_CORE_REQUIREMENTS.md §2/§8.1):
- optional `document_id` input; when present, skip ALL fuzzy tiers
  (thread uploads, name match, corpus_search, web registry);
- resolve the exact row by primary key → exactly one SourceRef;
- the single-match content-attachment path is GUARANTEED to fire;
- malformed / unknown id → clean `no_sources` envelope, never an
  exception;
- either query OR document_id is required.
"""
from __future__ import annotations

from types import SimpleNamespace

import app.skills.builtin.fetch_document as fd
from app.skills.registry import SkillCall


_ROW = {
    "document_id": "06dcfff8-87d2-41be-91cc-83fcc7e574b0",
    "document_display_name": "Sunshine Health Provider Manual",
    "document_filename": "Provider_Manual.pdf",
    "document_payer": "Sunshine Health",
    "document_state": "FL",
    "document_program": "Medicaid",
    "document_authority_level": "payer_manual",
    "updated_at": "2026-03-01T00:00:00Z",
}


def _call(document_id=None, query=None, ctx=None):
    inputs = {}
    if document_id is not None:
        inputs["document_id"] = document_id
    if query is not None:
        inputs["query"] = query
    return SkillCall(
        name="fetch_document",
        inputs=inputs,
        question=query or "",
        pipeline_ctx=ctx,
    )


def _guard_no_fuzzy(monkeypatch):
    """Make every fuzzy tier explode — proves the id path never touches them."""
    def _boom(*a, **k):
        raise AssertionError("fuzzy tier called on a document_id request")
    monkeypatch.setattr(fd, "_thread_upload_matches", _boom)
    monkeypatch.setattr(fd, "_fetch_candidates", _boom)
    monkeypatch.setattr(fd, "_corpus_search_resolve", _boom)
    monkeypatch.setattr(fd, "_web_registry_resolve", _boom)
    # Attachment fetch hits RAG over HTTP — stub it out (None = no attach).
    monkeypatch.setattr(fd, "_maybe_fetch_attachment", lambda *a, **k: None)


def test_resolve_by_id_returns_single_card_and_skips_fuzzy(monkeypatch):
    _guard_no_fuzzy(monkeypatch)
    monkeypatch.setattr(fd, "_resolve_by_id", lambda did: dict(_ROW) if did == _ROW["document_id"] else None)
    monkeypatch.setenv("RAG_API_BASE", "https://rag.example")
    ctx = SimpleNamespace()

    env = fd._run_fetch_document(_call(document_id=_ROW["document_id"], ctx=ctx))

    assert env.signal == "ok"
    assert env.extra["resolved_via"] == "document_id"
    assert env.extra["match_count"] == 1
    assert len(env.sources) == 1
    src = env.sources[0]
    assert src.document_id == _ROW["document_id"]
    assert src.extra["download_url"].endswith(f"/documents/{_ROW['document_id']}/file")
    # Guaranteed single-match: the attachment path was reached (golden opt-out set).
    assert env.extra["golden"] is False
    # Structured payload for the FE / react context.
    payload = ctx.react_document_download_data["documents"][0]
    assert payload["document_id"] == _ROW["document_id"]
    assert payload["resolved_via"] == "document_id"


def test_resolve_by_id_attachment_fires_on_single_match(monkeypatch):
    _guard_no_fuzzy(monkeypatch)
    monkeypatch.setattr(fd, "_resolve_by_id", lambda did: dict(_ROW))
    # Prove the GUARANTEED attachment path is taken for the id resolve.
    seen = {}
    def _attach(doc_id, filename):
        seen["doc_id"] = doc_id
        return {"data_b64": "QUJD", "mime": "application/pdf", "filename": filename}
    monkeypatch.setattr(fd, "_maybe_fetch_attachment", _attach)
    monkeypatch.setenv("RAG_API_BASE", "https://rag.example")

    env = fd._run_fetch_document(_call(document_id=_ROW["document_id"]))

    assert seen["doc_id"] == _ROW["document_id"]
    assert env.extra["attachment"]["mime"] == "application/pdf"


def test_unknown_id_returns_no_sources_not_exception(monkeypatch):
    _guard_no_fuzzy(monkeypatch)
    monkeypatch.setattr(fd, "_resolve_by_id", lambda did: None)  # valid-shape but not found

    env = fd._run_fetch_document(_call(document_id="11111111-2222-3333-4444-555555555555"))

    assert env.signal == "no_sources"
    assert not env.sources


def test_document_id_wins_over_query(monkeypatch):
    # Both provided → id path taken, query fuzzy tiers never called.
    _guard_no_fuzzy(monkeypatch)
    monkeypatch.setattr(fd, "_resolve_by_id", lambda did: dict(_ROW))
    monkeypatch.setattr(fd, "_maybe_fetch_attachment", lambda *a, **k: None)
    monkeypatch.setenv("RAG_API_BASE", "https://rag.example")

    env = fd._run_fetch_document(_call(document_id=_ROW["document_id"], query="something totally different"))

    assert env.extra["resolved_via"] == "document_id"
    assert env.sources[0].document_id == _ROW["document_id"]


def test_neither_query_nor_id_is_no_sources():
    env = fd._run_fetch_document(_call())
    assert env.signal == "no_sources"


# ── _resolve_by_id unit: UUID guard + row normalization ─────────────


def test_resolve_by_id_rejects_malformed_uuid_without_db(monkeypatch):
    # Malformed id must NOT hit the DB (no Postgres uuid-cast error).
    import app.db_client as db_client
    def _boom(*a, **k):
        raise AssertionError("db_query hit for a malformed uuid")
    monkeypatch.setattr(db_client, "db_query", _boom)

    assert fd._resolve_by_id("not-a-uuid") is None
    assert fd._resolve_by_id("") is None
    assert fd._resolve_by_id(None) is None


def test_resolve_by_id_queries_by_pk_and_normalizes(monkeypatch):
    import app.db_client as db_client
    captured = {}
    def _fake_db_query(sql, db, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return {"rows": [dict(_ROW)]}
    monkeypatch.setattr(db_client, "db_query", _fake_db_query)

    row = fd._resolve_by_id(_ROW["document_id"])
    assert row["document_id"] == _ROW["document_id"]
    assert captured["params"] == {"doc_id": _ROW["document_id"]}
    assert "WHERE document_id = %(doc_id)s" in captured["sql"]


def test_resolve_by_id_empty_result_is_none(monkeypatch):
    import app.db_client as db_client
    monkeypatch.setattr(db_client, "db_query", lambda *a, **k: {"rows": []})
    assert fd._resolve_by_id(_ROW["document_id"]) is None


def test_resolve_by_id_normalizes_columnar_shape(monkeypatch):
    # db_query psycopg2 fallback shape: {columns, rows:[[...]]}.
    import app.db_client as db_client
    cols = list(_ROW.keys())
    monkeypatch.setattr(
        db_client, "db_query",
        lambda *a, **k: {"columns": cols, "rows": [[_ROW[c] for c in cols]]},
    )
    row = fd._resolve_by_id(_ROW["document_id"])
    assert row["document_filename"] == "Provider_Manual.pdf"
