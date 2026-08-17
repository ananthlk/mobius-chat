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


# ── reader-for-RAG: corpus-text fallback when the PDF is too big ─────


def test_corpus_text_reader_when_whole_file_unavailable(monkeypatch):
    # Whole-file PDF is too big / unavailable and no pages were asked for.
    # Instead of dead-ending at a link, the doc's already-parsed corpus
    # text is attached so react can READ it this round.
    monkeypatch.setattr(fd, "_resolve_by_id", lambda did: dict(_ROW))
    monkeypatch.setattr(fd, "_maybe_fetch_attachment", lambda *a, **k: None)
    monkeypatch.setattr(
        fd, "_fetch_corpus_text_attachment",
        lambda doc_id, base: {"mime_type": "text/markdown", "data_b64": "eA==",
                              "filename": f"{base}_text.md", "truncated": False})
    # A complete text read means the planner does NOT need the size hint.
    monkeypatch.setattr(fd, "_document_page_count", lambda did: (_ for _ in ()).throw(
        AssertionError("page_count fetched despite a complete corpus-text read")))

    env = fd._run_fetch_document(fd.SkillCall(
        name="fetch_document", inputs={"document_id": _ROW["document_id"]},
        question="", pipeline_ctx=SimpleNamespace()))

    assert env.extra["read_mode"] == "corpus_text"
    assert env.extra["attachment"]["mime_type"] == "text/markdown"
    assert "page_count" not in env.extra
    assert "read_truncated" not in env.extra


def test_corpus_text_truncated_surfaces_page_count(monkeypatch):
    # A doc bigger than the text cap: partial read + page_count so react
    # can page for the rest.
    monkeypatch.setattr(fd, "_resolve_by_id", lambda did: dict(_ROW))
    monkeypatch.setattr(fd, "_maybe_fetch_attachment", lambda *a, **k: None)
    monkeypatch.setattr(
        fd, "_fetch_corpus_text_attachment",
        lambda doc_id, base: {"mime_type": "text/markdown", "data_b64": "eA==",
                              "filename": "x_text.md", "truncated": True})
    monkeypatch.setattr(fd, "_document_page_count", lambda did: 261)

    env = fd._run_fetch_document(fd.SkillCall(
        name="fetch_document", inputs={"document_id": _ROW["document_id"]},
        question="", pipeline_ctx=SimpleNamespace()))

    assert env.extra["read_mode"] == "corpus_text"
    assert env.extra["read_truncated"] is True
    assert env.extra["page_count"] == 261


def test_pages_take_precedence_over_corpus_text(monkeypatch):
    # An explicit page range must use the page path, never the corpus-text
    # or whole-file readers.
    monkeypatch.setattr(fd, "_resolve_by_id", lambda did: dict(_ROW))
    monkeypatch.setattr(
        fd, "_fetch_pages_attachment",
        lambda doc_id, pages, base: {"mime_type": "text/markdown", "data_b64": "cGFnZQ==",
                                     "filename": f"{base}_pages.md"})
    monkeypatch.setattr(fd, "_fetch_corpus_text_attachment", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("corpus-text reader called when pages were requested")))
    monkeypatch.setattr(fd, "_maybe_fetch_attachment", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("whole-file attach called when pages were requested")))

    env = fd._run_fetch_document(fd.SkillCall(
        name="fetch_document", inputs={"document_id": _ROW["document_id"], "pages": "20-24"},
        question="", pipeline_ctx=SimpleNamespace()))

    assert env.extra["read_mode"] == "pages"
    assert env.extra["attached_pages"] == "20-24"


def test_corpus_text_assembly_orders_and_labels(monkeypatch):
    # _fetch_corpus_text_attachment concatenates chunk text in the order
    # the SQL returns (page, paragraph) and labels the attachment.
    import app.db_client as db_client
    ordered = [
        {"text": "page1 para0", "page_number": 1, "paragraph_index": 0},
        {"text": "page1 para1", "page_number": 1, "paragraph_index": 1},
        {"text": "page2 para0", "page_number": 2, "paragraph_index": 0},
    ]
    monkeypatch.setattr(
        db_client, "db_query",
        lambda sql, db, params=None, max_rows=2000: {"rows": ordered})

    att = fd._fetch_corpus_text_attachment("doc-x", "Foo.pdf")
    import base64
    body = base64.b64decode(att["data_b64"]).decode("utf-8")
    assert body == "page1 para0\n\npage1 para1\n\npage2 para0"
    assert att["mime_type"] == "text/markdown"
    assert att["filename"] == "Foo_text.md"
    assert att["truncated"] is False


def test_corpus_text_none_when_no_chunk_text(monkeypatch):
    import app.db_client as db_client
    monkeypatch.setattr(db_client, "db_query", lambda *a, **k: {"rows": []})
    assert fd._fetch_corpus_text_attachment("doc-x", "Foo.pdf") is None


# ── exact-match short-circuit + multi-match id exposure (spec §4) ────

_EXACT = {
    "document_id": "517b8626-bb7d-4ab3-b02f-c98522b7be91",
    "document_display_name": "59G-4.150 Inpatient Hospital Services Coverage Policy",
    "document_filename": "59G-4.150_Inpatient_Hospital_Services_Coverage_Policy_Final.pdf",
    "document_payer": "AHCA", "document_state": "FL", "document_program": "Medicaid",
    "document_authority_level": "payer_manual", "updated_at": "2026-03-01T00:00:00Z",
}
_SIBLING = {
    "document_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "document_display_name": "59G-4.252 Diabetic Supply Services Coverage Policy",
    "document_filename": "59G-4.252_Diabetic_Supply_Services_Coverage_Policy_FINAL.pdf",
    "document_payer": "AHCA", "document_state": "FL", "document_program": "Medicaid",
    "document_authority_level": "payer_manual", "updated_at": "2026-03-01T00:00:00Z",
}


def _no_io(monkeypatch):
    # Keep _corpus_match_envelope's single-match path off the network/DB.
    monkeypatch.setattr(fd, "_maybe_fetch_attachment", lambda *a, **k: None)
    monkeypatch.setattr(fd, "_fetch_corpus_text_attachment", lambda *a, **k: None)
    monkeypatch.setattr(fd, "_document_page_count", lambda *a, **k: None)
    monkeypatch.setattr(fd, "_thread_upload_matches", lambda *a, **k: [])


def test_exact_filename_shortcircuits_over_near_duplicate_siblings(monkeypatch):
    # Spec case #2: user names the exact filename; a near-duplicate sibling
    # shares tokens. Exact-match must win as a SINGLE resolve — no pick-list.
    _no_io(monkeypatch)
    monkeypatch.setattr(fd, "_fetch_candidates", lambda q, **k: [dict(_EXACT), dict(_SIBLING)])
    q = "59G-4.150_Inpatient_Hospital_Services_Coverage_Policy_Final.pdf"

    env = fd._run_fetch_document(_call(query=q, ctx=SimpleNamespace()))

    assert env.extra["match_count"] == 1
    assert env.extra["resolved_via"] == "exact_name_match"
    assert env.sources[0].document_id == _EXACT["document_id"]
    # sibling never surfaced as a candidate to the model
    assert _SIBLING["document_id"] not in (env.text or "")


def test_exact_match_ignores_extension_and_separators(monkeypatch):
    _no_io(monkeypatch)
    monkeypatch.setattr(fd, "_fetch_candidates", lambda q, **k: [dict(_EXACT), dict(_SIBLING)])
    # No extension, spaces instead of underscores, different case.
    env = fd._run_fetch_document(_call(
        query="59g-4.150 inpatient hospital services coverage policy final",
        ctx=SimpleNamespace()))
    assert env.extra["resolved_via"] == "exact_name_match"
    assert env.sources[0].document_id == _EXACT["document_id"]


def test_fuzzy_multimatch_text_exposes_document_ids(monkeypatch):
    # Spec case #3 + 2a: genuinely ambiguous (no exact match) → pick-list,
    # but the text now carries each candidate's document_id so a follow-up
    # round can resolve deterministically instead of re-searching the name.
    _no_io(monkeypatch)
    a = dict(_ROW, document_id="11111111-1111-1111-1111-111111111111",
             document_display_name="Sunshine Provider Manual 2024", document_filename="pm_2024.pdf")
    b = dict(_ROW, document_id="22222222-2222-2222-2222-222222222222",
             document_display_name="Sunshine Provider Manual 2025", document_filename="pm_2025.pdf")
    monkeypatch.setattr(fd, "_fetch_candidates", lambda q, **k: [a, b])

    env = fd._run_fetch_document(_call(query="sunshine provider manual", ctx=SimpleNamespace()))

    assert env.extra["match_count"] == 2
    assert "11111111-1111-1111-1111-111111111111" in env.text
    assert "22222222-2222-2222-2222-222222222222" in env.text
    assert "document_id" in env.text


def test_duplicate_exact_names_fall_through_to_disambiguation(monkeypatch):
    # Spec open-Q2: two DISTINCT docs share an exact filename → do NOT pick
    # one arbitrarily; fall through to the multi-candidate path.
    _no_io(monkeypatch)
    dup1 = dict(_EXACT, document_id="10000000-0000-0000-0000-000000000001")
    dup2 = dict(_EXACT, document_id="20000000-0000-0000-0000-000000000002")
    monkeypatch.setattr(fd, "_fetch_candidates", lambda q, **k: [dup1, dup2])
    q = "59G-4.150_Inpatient_Hospital_Services_Coverage_Policy_Final.pdf"

    env = fd._run_fetch_document(_call(query=q, ctx=SimpleNamespace()))

    assert env.extra["match_count"] == 2
    assert env.extra["resolved_via"] != "exact_name_match"


def test_normalize_name_folds_extension_case_separators():
    n = fd._normalize_name
    assert n("59G-4.150_Inpatient_Hospital.pdf") == n("59g-4.150 inpatient hospital")
    assert n("Provider_Manual.PDF") == "provider manual"
    assert n("a/b-c_d.docx") == "a b c d"
    # policy-id dots are preserved (only the trailing extension is stripped)
    assert "4.150" in n("59G-4.150.pdf")


# ── follow-up bug: octet-stream mime_type must not leak (spec §6) ────


class _FakeResp:
    def __init__(self, data: bytes, content_type: str | None):
        self._data = data
        self.headers = {}
        if content_type is not None:
            self.headers["Content-Type"] = content_type

    def read(self, n=-1):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(monkeypatch, content_type):
    import urllib.request
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResp(b"%PDF-1.7 fake", content_type))


def test_attachment_octet_stream_is_corrected_to_pdf(monkeypatch):
    # RAG file endpoint returns a generic type → must NOT leak to Gemini
    # (which 400s on application/octet-stream). Fall back to the filename.
    _patch_urlopen(monkeypatch, "application/octet-stream")
    att = fd._maybe_fetch_attachment("doc-x", "Provider_Manual.pdf")
    assert att["mime_type"] == "application/pdf"


def test_attachment_binary_octet_stream_is_corrected(monkeypatch):
    _patch_urlopen(monkeypatch, "binary/octet-stream")
    att = fd._maybe_fetch_attachment("doc-x", "policy.pdf")
    assert att["mime_type"] == "application/pdf"


def test_attachment_absent_content_type_uses_guess(monkeypatch):
    _patch_urlopen(monkeypatch, None)
    att = fd._maybe_fetch_attachment("doc-x", "file.pdf")
    assert att["mime_type"] == "application/pdf"


def test_attachment_specific_content_type_is_preserved(monkeypatch):
    # A real, specific type from the endpoint must pass through unchanged.
    _patch_urlopen(monkeypatch, "application/pdf; charset=binary")
    att = fd._maybe_fetch_attachment("doc-x", "whatever.bin")
    assert att["mime_type"] == "application/pdf"


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


def test_page_count_fetched_lazily_when_no_content_available(monkeypatch):
    # Whole-file too big AND no corpus text (e.g. needs_ocr) AND no pages →
    # the model has NO content, so page_count fires so react can page next.
    monkeypatch.setattr(fd, "_resolve_by_id", lambda did: dict(_ROW))
    monkeypatch.setattr(fd, "_maybe_fetch_attachment", lambda doc_id, fname: None)  # too big / failed
    monkeypatch.setattr(fd, "_fetch_corpus_text_attachment", lambda doc_id, base: None)  # no parsed text
    monkeypatch.setattr(fd, "_document_page_count", lambda did: 261)

    env = fd._run_fetch_document(fd.SkillCall(name="fetch_document", inputs={"document_id": _ROW["document_id"]}, question="", pipeline_ctx=SimpleNamespace()))
    assert "attachment" not in env.extra
    assert "read_mode" not in env.extra
    assert env.extra["page_count"] == 261
