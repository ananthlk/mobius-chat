"""fetch_document download resolution + document_download envelope block."""
from __future__ import annotations

from types import SimpleNamespace

import app.skills.builtin.fetch_document as fd
from app.communication.assistant_envelope import _validate_ui_block
from app.skills.registry import SkillCall


_ROWS = [
    {
        "document_id": "11111111-1111-1111-1111-111111111111",
        "document_display_name": "Sunshine Health Provider Manual",
        "document_filename": "sunshine_provider_manual_2026.pdf",
        "document_payer": "Sunshine Health",
        "document_state": "FL",
        "document_program": "Medicaid",
        "document_authority_level": "payer_manual",
        "updated_at": "2026-01-01T00:00:00Z",
    },
    {
        "document_id": "22222222-2222-2222-2222-222222222222",
        "document_display_name": "FL.UM.87 Utilization Management",
        "document_filename": "FL.UM.87.pdf",
        "document_payer": "Sunshine Health",
        "document_state": "FL",
        "document_program": "Medicaid",
        "document_authority_level": "policy",
        "updated_at": "2026-02-01T00:00:00Z",
    },
]


def _call(query: str, ctx=None) -> SkillCall:
    return SkillCall(
        name="fetch_document",
        inputs={"query": query},
        question=query,
        pipeline_ctx=ctx,
    )


def test_name_match_uses_original_file_url_with_pdf_fallback(monkeypatch):
    monkeypatch.setattr(fd, "_fetch_candidates", lambda q: list(_ROWS))
    monkeypatch.setenv("RAG_API_BASE", "https://rag.example")
    ctx = SimpleNamespace()

    env = fd._run_fetch_document(_call("send me the Sunshine Provider Manual", ctx))

    assert env.signal == "ok"
    src = env.sources[0]
    doc_id = _ROWS[0]["document_id"]
    assert src.extra["download_url"] == f"https://rag.example/documents/{doc_id}/file"
    assert src.extra["fallback_download_url"] == (
        f"https://rag.example/documents/{doc_id}/download/pdf"
    )
    # structured payload attached for the envelope block
    payload = ctx.react_document_download_data
    assert payload["documents"][0]["document_id"] == doc_id
    assert payload["documents"][0]["resolved_via"] == "name_match"
    assert env.extra["document_download_payload"]["documents"]


def test_corpus_search_fallback_when_name_match_fails(monkeypatch):
    monkeypatch.setattr(fd, "_fetch_candidates", lambda q: list(_ROWS))
    monkeypatch.setattr(
        fd,
        "_corpus_search_resolve",
        lambda q, limit=3: [
            {
                "document_id": _ROWS[1]["document_id"],
                "document_display_name": _ROWS[1]["document_display_name"],
                "document_filename": _ROWS[1]["document_filename"],
            }
        ],
    )
    ctx = SimpleNamespace()

    env = fd._run_fetch_document(_call("the policy about telehealth visits", ctx))

    assert env.signal == "ok"
    assert env.extra["resolved_via"] == "corpus_search"
    doc = ctx.react_document_download_data["documents"][0]
    assert doc["document_id"] == _ROWS[1]["document_id"]
    # payer/state enriched from metadata rows by document_id
    assert doc["payer"] == "Sunshine Health"
    assert doc["state"] == "FL"


def test_corpus_search_wrong_rule_number_filtered_out(monkeypatch):
    # Regression: querying "Florida rule 59G-4.029" must NOT return 59G-4.127
    # via corpus_search when 59G-4.029 isn't in the corpus.  The vector search
    # finds related rules but they have a DIFFERENT rule number; the caller
    # ends up looping on the wrong download card for 12 rounds.
    wrong_rule = {
        "document_id": "44444444-4444-4444-4444-444444444444",
        "document_display_name": "59G-4.127 Mental Health Coverage Policy",
        "document_filename": "59G-4.127_MH_Coverage_Policy.pdf",
        "document_payer": "",
        "document_state": "FL",
        "document_program": "Medicaid",
        "document_authority_level": "state_rule",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(fd, "_fetch_candidates", lambda q: [wrong_rule])
    monkeypatch.setattr(
        fd,
        "_corpus_search_resolve",
        lambda q, limit=3: [
            {
                "document_id": wrong_rule["document_id"],
                "document_display_name": wrong_rule["document_display_name"],
                "document_filename": wrong_rule["document_filename"],
            }
        ],
    )
    monkeypatch.setattr(fd, "_web_registry_resolve", lambda q, limit=3: [])
    ctx = SimpleNamespace()

    env = fd._run_fetch_document(_call("Florida rule 59G-4.029", ctx))

    # Must NOT return the wrong rule — should signal no_sources (not in corpus)
    assert env.signal == "no_sources", (
        f"Expected no_sources, got {env.signal!r}; "
        "wrong-numbered document from corpus_search should be filtered out"
    )


def test_corpus_search_correct_rule_number_accepted(monkeypatch):
    # When the document EXISTS in the candidate pool with the rule number in its
    # name, _rank_matches finds it directly (name_match) and corpus_search is
    # never invoked.  This verifies the filter doesn't interfere with that path.
    right_rule = {
        "document_id": "55555555-5555-5555-5555-555555555555",
        "document_display_name": "59G-4.029 Behavioral Health Coverage Policy",
        "document_filename": "59G-4.029_BH_Coverage_Policy.pdf",
        "document_payer": "",
        "document_state": "FL",
        "document_program": "Medicaid",
        "document_authority_level": "state_rule",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(fd, "_fetch_candidates", lambda q: [right_rule])
    # corpus_search should never be called when name_match succeeds
    cs_called = []
    monkeypatch.setattr(fd, "_corpus_search_resolve", lambda q, limit=3: cs_called.append(q) or [])
    ctx = SimpleNamespace()

    env = fd._run_fetch_document(_call("Florida rule 59G-4.029", ctx))

    # Resolves via name_match directly — no corpus_search needed
    assert env.signal == "ok"
    assert env.extra["resolved_via"] == "name_match"
    assert not cs_called, "corpus_search should not be called when name_match succeeds"
    assert ctx.react_document_download_data["documents"][0]["document_id"] == right_rule["document_id"]


def test_payer_column_counts_toward_match(monkeypatch):
    # Regression: "Sunshine provider manual" must beat a doc whose NAME
    # contains sunshine+health when the manual's payer column carries it.
    rows = list(_ROWS) + [{
        "document_id": "33333333-3333-3333-3333-333333333333",
        "document_display_name": "",
        "document_filename": "Provider_Manual.pdf",
        "document_payer": "Sunshine Health",
        "document_state": "FL",
        "document_program": "Medicaid",
        "document_authority_level": "payer_manual",
        "updated_at": "2026-03-01T00:00:00Z",
    }]
    monkeypatch.setattr(fd, "_fetch_candidates", lambda q: rows)
    ctx = SimpleNamespace()

    env = fd._run_fetch_document(_call("send me the Sunshine Health provider manual", ctx))

    assert env.signal == "ok"
    top = ctx.react_document_download_data["documents"][0]
    assert top["document_id"] == "33333333-3333-3333-3333-333333333333"


def _call_with_uploads(query: str, ctx, files):
    return SkillCall(
        name="fetch_document",
        inputs={"query": query},
        question=query,
        active_context={"uploaded_files": files},
        pipeline_ctx=ctx,
    )


_UPLOADS = [
    {"document_id": "aaaa-1", "upload_id": "u1", "filename": "sunshine_claims_export.pdf"},
    {"document_id": "aaaa-2", "upload_id": "u2", "filename": "roster_march.xlsx"},
]


def test_tier0_returns_thread_upload_on_filename_match(monkeypatch):
    monkeypatch.setattr(fd, "_fetch_candidates", lambda q: list(_ROWS))
    ctx = SimpleNamespace()

    env = fd._run_fetch_document(
        _call_with_uploads("download the roster march spreadsheet", ctx, list(_UPLOADS))
    )

    assert env.extra["resolved_via"] == "thread_upload"
    doc = ctx.react_document_download_data["documents"][0]
    assert doc["document_id"] == "aaaa-2"
    assert doc["download_url"] == "/chat/uploads/aaaa-2/download"


def test_tier0_single_stray_token_does_not_hijack_corpus_ask(monkeypatch):
    # "Sunshine provider manual" overlaps sunshine_claims_export.pdf on
    # one token — must fall through to corpus resolution, not tier 0.
    monkeypatch.setattr(fd, "_fetch_candidates", lambda q: list(_ROWS))
    ctx = SimpleNamespace()

    env = fd._run_fetch_document(
        _call_with_uploads("send me the Sunshine Health provider manual", ctx, list(_UPLOADS))
    )

    assert env.extra["resolved_via"] == "name_match"


def test_tier0_upload_intent_words_with_single_upload(monkeypatch):
    monkeypatch.setattr(fd, "_fetch_candidates", lambda q: list(_ROWS))
    ctx = SimpleNamespace()

    env = fd._run_fetch_document(
        _call_with_uploads("send me back the file I uploaded", ctx, [_UPLOADS[0]])
    )

    assert env.extra["resolved_via"] == "thread_upload"
    assert ctx.react_document_download_data["documents"][0]["document_id"] == "aaaa-1"


def test_web_registry_tier3_when_corpus_misses(monkeypatch):
    monkeypatch.setattr(fd, "_fetch_candidates", lambda q: list(_ROWS))
    monkeypatch.setattr(fd, "_corpus_search_resolve", lambda q, limit=3: [])
    monkeypatch.setattr(
        fd,
        "_web_registry_resolve",
        lambda q, limit=3: [{
            "web_url": "https://www.sunshinehealth.com/content/dam/plan-forms/appeal-form.pdf",
            "host": "www.sunshinehealth.com",
            "filename": "appeal-form.pdf",
            "title": "Appeal Form",
            "payer": "Sunshine Health",
            "state": "FL",
            "authority_level": "payer_form",
            "ingested": False,
        }],
    )
    ctx = SimpleNamespace()

    env = fd._run_fetch_document(_call("download the sunshine appeal form", ctx))

    assert env.signal == "ok"
    assert env.extra["resolved_via"] == "web_registry"
    doc = ctx.react_document_download_data["documents"][0]
    # Primary = same-origin proxy; fallback = the direct source URL.
    assert doc["download_url"].startswith("/chat/download-proxy?url=https%3A%2F%2Fwww.sunshinehealth.com")
    assert doc["fallback_download_url"].startswith("https://www.sunshinehealth.com/")
    assert doc["host"] == "www.sunshinehealth.com"
    assert env.sources[0].source_type == "web"


def test_no_match_when_fallback_also_empty(monkeypatch):
    monkeypatch.setattr(fd, "_fetch_candidates", lambda q: list(_ROWS))
    monkeypatch.setattr(fd, "_corpus_search_resolve", lambda q, limit=3: [])
    monkeypatch.setattr(fd, "_web_registry_resolve", lambda q, limit=3: [])

    env = fd._run_fetch_document(_call("zzz qqq nonexistent"))

    assert env.signal == "no_sources"
    assert not env.sources


def test_validate_document_download_block():
    block = {
        "type": "document_download",
        "query": "sunshine manual",
        "documents": [
            {
                "document_id": "abc",
                "title": "Sunshine Health Provider Manual",
                "download_url": "https://rag.example/documents/abc/file",
                "fallback_download_url": "https://rag.example/documents/abc/download/pdf",
                "filename": "manual.pdf",
                "payer": "Sunshine Health",
                "resolved_via": "name_match",
            },
            {"title": "missing id and url — dropped"},
        ],
    }
    out = _validate_ui_block(block, max_source_index=0)
    assert out is not None
    assert out["type"] == "document_download"
    assert out["query"] == "sunshine manual"
    assert len(out["documents"]) == 1
    doc = out["documents"][0]
    assert doc["download_url"].endswith("/documents/abc/file")
    assert doc["fallback_download_url"].endswith("/download/pdf")
    assert doc["payer"] == "Sunshine Health"


def test_validate_document_download_block_rejects_empty():
    assert _validate_ui_block({"type": "document_download", "documents": []}, max_source_index=0) is None
    assert _validate_ui_block({"type": "document_download"}, max_source_index=0) is None
