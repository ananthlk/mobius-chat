"""assistant_envelope v1 builder and source enrichment."""
from __future__ import annotations

import os
from urllib.parse import unquote

from app.communication.assistant_envelope import (
    ENVELOPE_VERSION,
    build_assistant_envelope_v1,
    enrich_sources_open_hrefs,
    resolve_tool_fired,
)
from app.pipeline.context import PipelineContext


def test_enrich_sources_template():
    os.environ["MOBIUS_DOCUMENT_VIEWER_URL_TEMPLATE"] = "https://viewer.example/doc/{document_id}?p={page}"
    src = [
        {
            "index": 1,
            "document_id": "abc",
            "document_name": "Manual",
            "page_number": 3,
            "text": "x",
        }
    ]
    out = enrich_sources_open_hrefs(src)
    assert out[0]["open_href"] == "https://viewer.example/doc/abc?p=3"
    assert out[0]["open_kind"] == "corpus"
    del os.environ["MOBIUS_DOCUMENT_VIEWER_URL_TEMPLATE"]


def test_enrich_sources_rag_app_public_url_deep_link():
    os.environ["MOBIUS_RAG_APP_PUBLIC_URL"] = "http://localhost:5173"
    src = [{"document_id": "doc-uuid-1", "page_number": 12}]
    out = enrich_sources_open_hrefs(src)
    href = out[0]["open_href"]
    assert out[0]["open_kind"] == "corpus"
    assert "tab=read" in href
    assert "documentId=doc-uuid-1" in href
    assert "pageNumber=12" in href
    del os.environ["MOBIUS_RAG_APP_PUBLIC_URL"]


def test_enrich_sources_template_overrides_public_url():
    os.environ["MOBIUS_RAG_APP_PUBLIC_URL"] = "http://wrong.example"
    os.environ["MOBIUS_DOCUMENT_VIEWER_URL_TEMPLATE"] = "https://viewer.example/doc/{document_id}?p={page}"
    src = [{"document_id": "abc", "page_number": 2}]
    out = enrich_sources_open_hrefs(src)
    assert out[0]["open_href"] == "https://viewer.example/doc/abc?p=2"
    del os.environ["MOBIUS_RAG_APP_PUBLIC_URL"]
    del os.environ["MOBIUS_DOCUMENT_VIEWER_URL_TEMPLATE"]


def test_enrich_sources_appends_cite_text_query():
    os.environ["MOBIUS_DOCUMENT_VIEWER_URL_TEMPLATE"] = "https://viewer.example/doc/{document_id}?p={page}"
    src = [
        {
            "document_id": "abc",
            "page_number": 2,
            "cite_text": "member must submit within 60 days",
        }
    ]
    out = enrich_sources_open_hrefs(src)
    href = out[0]["open_href"]
    assert "citeText=" in href
    assert "member must submit" in unquote(href)
    del os.environ["MOBIUS_DOCUMENT_VIEWER_URL_TEMPLATE"]


def test_build_envelope_supplemental_detail_when_sections_empty():
    env = build_assistant_envelope_v1(
        answer_card={
            "mode": "FACTUAL",
            "direct_answer": "Yes.",
            "sections": [],
            "confidence_note": "Based on provider manual section 3.2.",
        },
        ui_blocks_raw=None,
        tool_fired="search_corpus",
        response_sources=[],
        next_steps=[],
        next_questions_for_user=[],
        roster_report_final_md=None,
        has_roster_pdf=False,
    )
    detail_blocks = [b for b in env["blocks"] if b.get("type") == "detail"]
    assert len(detail_blocks) == 1
    assert "confidence" in detail_blocks[0]["markdown"].lower()
    assert "3.2" in detail_blocks[0]["markdown"]
    assert detail_blocks[0].get("collapsed_default") is True


def test_build_envelope_merges_llm_detail_into_card_detail():
    env = build_assistant_envelope_v1(
        answer_card={
            "mode": "BLENDED",
            "direct_answer": "OK",
            "sections": [{"label": "Steps", "bullets": ["Do A", "Do B"]}],
            "confidence_note": "Verify with your plan.",
        },
        ui_blocks_raw=[{"type": "detail", "markdown": "Extra from model.", "collapsed_default": True}],
        tool_fired="search_corpus",
        response_sources=[],
        next_steps=[],
        next_questions_for_user=[],
        roster_report_final_md=None,
        has_roster_pdf=False,
    )
    details = [b for b in env["blocks"] if b.get("type") == "detail"]
    assert len(details) == 1
    md = details[0]["markdown"]
    assert "Steps" in md
    assert "confidence" in md.lower()
    assert "Extra from model" in md


def test_build_envelope_resolutions_add_detail():
    env = build_assistant_envelope_v1(
        answer_card={
            "mode": "FACTUAL",
            "direct_answer": "Summary.",
            "sections": [],
        },
        ui_blocks_raw=None,
        tool_fired="search_corpus",
        response_sources=[],
        next_steps=[],
        next_questions_for_user=[],
        roster_report_final_md=None,
        has_roster_pdf=False,
        resolutions=[
            {"sq_id": "sq1", "question": "Is PA required?", "resolution": "Yes for DME.", "source": "rag"},
        ],
    )
    details = [b for b in env["blocks"] if b.get("type") == "detail"]
    assert len(details) == 1
    assert "PA required" in details[0]["markdown"]
    assert "DME" in details[0]["markdown"]


def test_build_envelope_section_body_not_only_bullets():
    env = build_assistant_envelope_v1(
        answer_card={
            "mode": "FACTUAL",
            "direct_answer": "Short.",
            "sections": [
                {"intent": "references", "label": "Context", "body": "Full paragraph from the manual."},
            ],
        },
        ui_blocks_raw=None,
        tool_fired="search_corpus",
        response_sources=[],
        next_steps=[],
        next_questions_for_user=[],
        roster_report_final_md=None,
        has_roster_pdf=False,
    )
    details = [b for b in env["blocks"] if b.get("type") == "detail"]
    assert len(details) == 1
    assert "Full paragraph" in details[0]["markdown"]


def test_build_envelope_minimal():
    env = build_assistant_envelope_v1(
        answer_card={"mode": "FACTUAL", "direct_answer": "Hello", "sections": []},
        ui_blocks_raw=None,
        tool_fired="search_corpus",
        response_sources=[],
        next_steps=["Do X"],
        next_questions_for_user=["Ask Y?"],
        roster_report_final_md=None,
        has_roster_pdf=False,
    )
    assert env["version"] == ENVELOPE_VERSION
    types = [b["type"] for b in env["blocks"]]
    assert types[0] == "tool_attribution"
    assert "direct_answer" in types
    assert "sources" in types
    assert "next_steps" in types
    assert "suggested_questions" in types
    ns = next(b for b in env["blocks"] if b.get("type") == "next_steps")
    sq = next(b for b in env["blocks"] if b.get("type") == "suggested_questions")
    assert ns.get("collapsed_default") is True
    assert sq.get("collapsed_default") is True


def test_build_envelope_followups_expanded_when_authoritative():
    env = build_assistant_envelope_v1(
        answer_card={"mode": "FACTUAL", "direct_answer": "Hello", "sections": []},
        ui_blocks_raw=None,
        tool_fired="search_corpus",
        response_sources=[],
        next_steps=["Do X"],
        next_questions_for_user=["Ask Y?"],
        roster_report_final_md=None,
        has_roster_pdf=False,
        source_confidence_strip="approved_authoritative",
    )
    ns = next(b for b in env["blocks"] if b.get("type") == "next_steps")
    assert ns.get("collapsed_default") is False


def test_resolve_tool_fired_react():
    ctx = PipelineContext(correlation_id="c1", thread_id=None, message="hi")
    ctx.react_last_tool = "google_search"
    assert resolve_tool_fired(ctx) == "google_search"


def test_validate_chart_drops_oversized_b64():
    huge = "x" * 2_000_000
    env = build_assistant_envelope_v1(
        answer_card={"mode": "FACTUAL", "direct_answer": "Hi", "sections": []},
        ui_blocks_raw=[{"type": "chart", "image_base64": huge, "title": "T"}],
        tool_fired="unknown",
        response_sources=[],
        next_steps=[],
        next_questions_for_user=[],
        roster_report_final_md=None,
        has_roster_pdf=False,
    )
    assert not any(b.get("type") == "chart" for b in env["blocks"])
