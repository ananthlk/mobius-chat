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
    assert types[0] == "mode_badge"
    assert types[1] == "tool_attribution"
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


def test_build_envelope_pipeline_human_gate_after_tool_attribution():
    gate = {
        "run_id": "550e8400-e29b-41d4-a716-446655440000",
        "pending_step_id": "ensure_benchmarks",
        "phase": "awaiting_validation",
        "draft_output": {"step_id": "ensure_benchmarks", "status": "done"},
        "mode": "copilot",
        "org_name": "Acme",
        "plan_kind": "credentialing_copilot",
        "thread_id": "thread-1",
    }
    env = build_assistant_envelope_v1(
        answer_card={"mode": "FACTUAL", "direct_answer": "Co-pilot started.", "sections": []},
        ui_blocks_raw=None,
        tool_fired="run_credentialing_report",
        response_sources=[],
        next_steps=[],
        next_questions_for_user=[],
        roster_report_final_md=None,
        has_roster_pdf=False,
        pipeline_human_gate=gate,
    )
    blocks = env["blocks"]
    assert blocks[0].get("type") == "mode_badge"
    assert blocks[1].get("type") == "tool_attribution"
    assert blocks[2].get("type") == "pipeline_human_gate"
    assert blocks[2].get("gate", {}).get("run_id") == gate["run_id"]


# ── Full collapse (2026-08-10, Chat Master spec, Ananth-approved) ──────────
# The envelope becomes the COMPLETE render source, replacing the FE's
# dual-read of the raw AnswerCard JSON alongside the envelope (the
# triple-print mechanism). Adds typed section blocks, tldr, first_pass,
# mode_badge, and moves correction to the end of block ordering.

def _minimal_kwargs(**overrides):
    base = dict(
        ui_blocks_raw=None,
        tool_fired="search_corpus",
        response_sources=[],
        next_steps=[],
        next_questions_for_user=[],
        roster_report_final_md=None,
        has_roster_pdf=False,
    )
    base.update(overrides)
    return base


class TestModeBadge:
    def test_mode_badge_from_card_mode(self):
        env = build_assistant_envelope_v1(
            answer_card={"mode": "CANONICAL", "direct_answer": "Hi", "sections": []},
            **_minimal_kwargs(),
        )
        badge = next(b for b in env["blocks"] if b["type"] == "mode_badge")
        assert badge["mode"] == "CANONICAL"

    def test_no_mode_badge_when_answer_card_none(self):
        env = build_assistant_envelope_v1(answer_card=None, **_minimal_kwargs())
        assert not any(b["type"] == "mode_badge" for b in env["blocks"])


class TestTldrAndFirstPass:
    def test_tldr_block_from_tldr_summary(self):
        env = build_assistant_envelope_v1(
            answer_card={"mode": "FACTUAL", "direct_answer": "Hi", "sections": [], "tldr_summary": "Short version."},
            **_minimal_kwargs(),
        )
        tldr = next(b for b in env["blocks"] if b["type"] == "tldr")
        assert tldr["markdown"] == "Short version."

    def test_no_tldr_block_when_absent(self):
        env = build_assistant_envelope_v1(
            answer_card={"mode": "FACTUAL", "direct_answer": "Hi", "sections": []},
            **_minimal_kwargs(),
        )
        assert not any(b["type"] == "tldr" for b in env["blocks"])

    def test_first_pass_from_react_draft_and_reasoning_trace(self):
        env = build_assistant_envelope_v1(
            answer_card={
                "mode": "FACTUAL", "direct_answer": "Hi", "sections": [],
                "react_draft": "Raw draft text.",
                "reasoning_trace": [{"round": 1, "running_answer": "..."}],
            },
            **_minimal_kwargs(),
        )
        fp = next(b for b in env["blocks"] if b["type"] == "first_pass")
        assert fp["draft_markdown"] == "Raw draft text."
        assert fp["trace_rounds"] == [{"round": 1, "running_answer": "..."}]
        assert fp["collapsed_default"] is True

    def test_no_first_pass_when_both_absent(self):
        env = build_assistant_envelope_v1(
            answer_card={"mode": "FACTUAL", "direct_answer": "Hi", "sections": []},
            **_minimal_kwargs(),
        )
        assert not any(b["type"] == "first_pass" for b in env["blocks"])


class TestTypedSectionBlocks:
    def test_table_section_becomes_table_block(self):
        env = build_assistant_envelope_v1(
            answer_card={
                "mode": "FACTUAL", "direct_answer": "Hi",
                "sections": [{"format": "table", "label": "Rates", "data": {"headers": ["Code", "Rate"], "rows": [["90834", "$85"]]}}],
            },
            **_minimal_kwargs(),
        )
        tb = next(b for b in env["blocks"] if b["type"] == "table")
        assert tb["headers"] == ["Code", "Rate"]
        assert tb["rows"] == [["90834", "$85"]]
        assert tb["label"] == "Rates"

    def test_stats_section_becomes_stats_block(self):
        env = build_assistant_envelope_v1(
            answer_card={
                "mode": "FACTUAL", "direct_answer": "Hi",
                "sections": [{"format": "stats", "data": {"items": [{"label": "Deadline", "value": "180 days"}]}}],
            },
            **_minimal_kwargs(),
        )
        sb = next(b for b in env["blocks"] if b["type"] == "stats")
        assert sb["items"] == [{"label": "Deadline", "value": "180 days"}]

    def test_bullets_section_from_top_level_bullets_key(self):
        env = build_assistant_envelope_v1(
            answer_card={
                "mode": "FACTUAL", "direct_answer": "Hi",
                "sections": [{"format": "bullets", "bullets": ["A", "B"]}],
            },
            **_minimal_kwargs(),
        )
        bb = next(b for b in env["blocks"] if b["type"] == "bullets")
        assert bb["items"] == ["A", "B"]

    def test_steps_section_becomes_steps_block(self):
        env = build_assistant_envelope_v1(
            answer_card={
                "mode": "FACTUAL", "direct_answer": "Hi",
                "sections": [{"format": "steps", "data": {"items": [{"label": "Do this first"}]}}],
            },
            **_minimal_kwargs(),
        )
        sb = next(b for b in env["blocks"] if b["type"] == "steps")
        assert sb["items"] == [{"label": "Do this first"}]

    def test_bars_and_conditions_pass_through_items(self):
        env = build_assistant_envelope_v1(
            answer_card={
                "mode": "FACTUAL", "direct_answer": "Hi",
                "sections": [
                    {"format": "bars", "data": {"items": [{"label": "X", "weight": 0.5}]}},
                    {"format": "conditions", "data": {"items": [{"condition": "if A", "result": "then B"}]}},
                ],
            },
            **_minimal_kwargs(),
        )
        types = [b["type"] for b in env["blocks"]]
        assert "bars" in types and "conditions" in types

    def test_appeals_playbook_becomes_domain_card(self):
        env = build_assistant_envelope_v1(
            answer_card={
                "mode": "FACTUAL", "direct_answer": "Hi",
                "sections": [{"format": "appeals_playbook", "data": {"deadline": "90d"}}],
            },
            **_minimal_kwargs(),
        )
        dc = next(b for b in env["blocks"] if b["type"] == "domain_card")
        assert dc["variant"] == "appeals_playbook"
        assert dc["data"] == {"deadline": "90d"}

    def test_sections_render_in_order(self):
        env = build_assistant_envelope_v1(
            answer_card={
                "mode": "FACTUAL", "direct_answer": "Hi",
                "sections": [
                    {"format": "stats", "data": {"items": [{"label": "A", "value": "1"}]}},
                    {"format": "bullets", "bullets": ["X"]},
                ],
            },
            **_minimal_kwargs(),
        )
        section_types = [b["type"] for b in env["blocks"] if b["type"] in ("stats", "bullets")]
        assert section_types == ["stats", "bullets"]

    def test_malformed_typed_section_dropped_not_crashed(self):
        """A table section missing headers/rows can't be trusted -- dropped,
        not guessed at, and must not raise."""
        env = build_assistant_envelope_v1(
            answer_card={
                "mode": "FACTUAL", "direct_answer": "Hi",
                "sections": [{"format": "table", "data": {"headers": ["A"]}}],
            },
            **_minimal_kwargs(),
        )
        assert not any(b["type"] == "table" for b in env["blocks"])

    def test_table_row_with_wrong_cell_count_dropped_keeps_valid_rows(self):
        """2026-08-12 live finding (cid=997193e2): the model generated a
        syntactically valid but structurally malformed row (2 cells
        against a 4-column header, e.g. ["Sunshine Health", "**18"]) --
        must be dropped while valid rows survive, not shipped broken."""
        env = build_assistant_envelope_v1(
            answer_card={
                "mode": "FACTUAL", "direct_answer": "Hi",
                "sections": [{
                    "format": "table",
                    "data": {
                        "headers": ["Payer", "Participating", "Non-Participating", "COB"],
                        "rows": [
                            ["Sunshine Health", "**18"],  # malformed -- 2 cells, not 4
                            ["Aetna", "180 days", "365 days", "90 days"],  # valid
                        ],
                    },
                }],
            },
            **_minimal_kwargs(),
        )
        tb = next(b for b in env["blocks"] if b["type"] == "table")
        assert tb["rows"] == [["Aetna", "180 days", "365 days", "90 days"]]

    def test_table_with_all_rows_malformed_drops_whole_section(self):
        """If every row is malformed, drop the section entirely -- a
        table with zero valid rows isn't a table, and direct_answer/
        react_draft already carries the same info in prose."""
        env = build_assistant_envelope_v1(
            answer_card={
                "mode": "FACTUAL", "direct_answer": "Hi",
                "sections": [{
                    "format": "table",
                    "data": {
                        "headers": ["Payer", "Deadline"],
                        "rows": [["Sunshine Health", "**18", "extra"], ["Aetna"]],
                    },
                }],
            },
            **_minimal_kwargs(),
        )
        assert not any(b["type"] == "table" for b in env["blocks"])

    def test_unrecognized_format_falls_back_to_detail(self):
        env = build_assistant_envelope_v1(
            answer_card={
                "mode": "FACTUAL", "direct_answer": "Hi",
                "sections": [{"format": "nonsense_format_xyz", "label": "Odd", "body": "Some prose."}],
            },
            **_minimal_kwargs(),
        )
        assert not any(b["type"] == "nonsense_format_xyz" for b in env["blocks"])
        detail = next(b for b in env["blocks"] if b["type"] == "detail")
        assert "Some prose." in detail["markdown"]


class TestCorrectionOrderedLast:
    def test_correction_is_the_final_block(self):
        env = build_assistant_envelope_v1(
            answer_card={
                "mode": "FACTUAL", "direct_answer": "Hi", "sections": [],
                "correction": {"original": "90 days", "corrected": "180 days"},
            },
            **_minimal_kwargs(),
        )
        assert env["blocks"][-1]["type"] == "correction"
        assert env["blocks"][-1]["original"] == "90 days"
        assert env["blocks"][-1]["corrected"] == "180 days"
