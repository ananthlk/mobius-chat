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


# ── misroute recovery: a filename dumped into document_id (2026-08-17) ──


def test_filename_in_document_id_recovers_via_name_match(monkeypatch):
    # The exact live bug: react passed a *.pdf filename as document_id.
    # A non-UUID id must NOT dead-end — it falls through to the fuzzy
    # name-match pipeline using the filename as the query.
    filename = "59G-4.150_Inpatient_Hospital_Services_Coverage_Policy_Final.pdf"

    # resolve_by_id must never be consulted for a non-UUID value.
    monkeypatch.setattr(fd, "_resolve_by_id", lambda did: (_ for _ in ()).throw(
        AssertionError("_resolve_by_id called for a non-UUID document_id")))
    # No thread uploads; name-match tier returns the corpus row.
    monkeypatch.setattr(fd, "_thread_upload_matches", lambda *a, **k: [])
    seen = {}
    def _fake_candidates(q, *, limit=30):
        seen["query"] = q
        return [dict(_ROW)]
    monkeypatch.setattr(fd, "_fetch_candidates", _fake_candidates)
    monkeypatch.setattr(fd, "_rank_matches", lambda q, cands: list(cands))
    monkeypatch.setattr(fd, "_maybe_fetch_attachment", lambda *a, **k: None)
    monkeypatch.setenv("RAG_API_BASE", "https://rag.example")

    env = fd._run_fetch_document(_call(document_id=filename))

    # The filename became the query and drove the name-match tier.
    assert seen["query"] == filename
    assert env.signal == "ok"
    assert env.extra["resolved_via"] == "name_match"
    assert env.sources[0].document_id == _ROW["document_id"]


def test_explicit_query_wins_over_misrouted_filename_id(monkeypatch):
    # If a real query is ALSO present, it takes precedence over the
    # filename-in-id; the id value is discarded, not appended.
    monkeypatch.setattr(fd, "_resolve_by_id", lambda did: (_ for _ in ()).throw(
        AssertionError("_resolve_by_id called for a non-UUID document_id")))
    monkeypatch.setattr(fd, "_thread_upload_matches", lambda *a, **k: [])
    seen = {}
    def _fake_candidates(q, *, limit=30):
        seen["query"] = q
        return [dict(_ROW)]
    monkeypatch.setattr(fd, "_fetch_candidates", _fake_candidates)
    monkeypatch.setattr(fd, "_rank_matches", lambda q, cands: list(cands))
    monkeypatch.setattr(fd, "_maybe_fetch_attachment", lambda *a, **k: None)
    monkeypatch.setenv("RAG_API_BASE", "https://rag.example")

    env = fd._run_fetch_document(_call(document_id="some_file.pdf", query="sunshine provider manual"))

    assert seen["query"] == "sunshine provider manual"
    assert env.extra["resolved_via"] == "name_match"


def test_looks_like_uuid_discriminates():
    assert fd._looks_like_uuid(_ROW["document_id"]) is True
    assert fd._looks_like_uuid("59G-4.150_Inpatient.pdf") is False
    assert fd._looks_like_uuid("sunshine provider manual") is False
    assert fd._looks_like_uuid("") is False
    assert fd._looks_like_uuid(None) is False


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


# ── §4 page-range extraction + size surfacing ───────────────────────


def test_page_range_attaches_requested_pages(monkeypatch):
    monkeypatch.setattr(fd, "_resolve_by_id", lambda did: dict(_ROW))
    monkeypatch.setattr(fd, "_document_page_count", lambda did: 261)
    captured = {}
    def _fake_pages(doc_id, pages, base_name):
        captured["pages"] = pages
        return {"mime_type": "text/markdown", "data_b64": "cGFnZQ==", "filename": f"{base_name}_pages.md"}
    monkeypatch.setattr(fd, "_fetch_pages_attachment", _fake_pages)
    # whole-file attach must NOT be called when pages requested
    monkeypatch.setattr(fd, "_maybe_fetch_attachment", lambda *a, **k: (_ for _ in ()).throw(AssertionError("whole-file attach called for a page-range request")))

    env = fd._run_fetch_document(SkillCall(
        name="fetch_document", inputs={"document_id": _ROW["document_id"], "pages": "20-24"},
        question="", pipeline_ctx=SimpleNamespace()))

    assert captured["pages"] == [20, 21, 22, 23, 24]
    assert env.extra["attachment"]["mime_type"] == "text/markdown"
    assert env.extra["attached_pages"] == "20-24"
    # page_count is fetched LAZILY — pages were requested + attached, so the
    # size hint isn't needed and no RAG /status round-trip is paid.
    assert "page_count" not in env.extra


def test_no_pages_attached_whole_file_skips_page_count(monkeypatch):
    # Small doc attaches whole → planner has the content → NO size round-trip.
    monkeypatch.setattr(fd, "_resolve_by_id", lambda did: dict(_ROW))
    monkeypatch.setattr(fd, "_document_page_count", lambda did: (_ for _ in ()).throw(AssertionError("page_count fetched when the whole file already attached")))
    monkeypatch.setattr(fd, "_maybe_fetch_attachment", lambda doc_id, fname: {"mime_type": "application/pdf", "data_b64": "eA==", "filename": fname})

    env = fd._run_fetch_document(fd.SkillCall(name="fetch_document", inputs={"document_id": _ROW["document_id"]}, question="", pipeline_ctx=SimpleNamespace()))
    assert env.extra["attachment"]["mime_type"] == "application/pdf"
    assert "page_count" not in env.extra
    assert "attached_pages" not in env.extra


def test_page_count_fetched_lazily_when_whole_file_did_not_attach(monkeypatch):
    # Oversized/failed whole-file attach + no pages → THIS is when the planner
    # needs the size (to request a page range next round), so page_count fires.
    monkeypatch.setattr(fd, "_resolve_by_id", lambda did: dict(_ROW))
    monkeypatch.setattr(fd, "_maybe_fetch_attachment", lambda doc_id, fname: None)  # too big / failed
    monkeypatch.setattr(fd, "_document_page_count", lambda did: 261)

    env = fd._run_fetch_document(fd.SkillCall(name="fetch_document", inputs={"document_id": _ROW["document_id"]}, question="", pipeline_ctx=SimpleNamespace()))
    assert "attachment" not in env.extra
    assert env.extra["page_count"] == 261
