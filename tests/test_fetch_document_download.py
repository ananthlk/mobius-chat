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
    assert doc["download_url"].startswith("https://www.sunshinehealth.com/")
    assert "fallback_download_url" not in doc
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
