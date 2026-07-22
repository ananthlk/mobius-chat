"""Tests for the section_hint pipeline: capture → ctx → integrator payload.

Verifies that analytics-tool section hints flow from _execute_tool through
ctx.react_tool_results into _finalize_response and ultimately reach the
consolidator JSON payload via _build_consolidator_input_json.

All tests are pure-function / unit level — no LLM, no DB, no external services.
"""
from __future__ import annotations

import json
import types
from unittest.mock import MagicMock, patch

import pytest

from app.planner.schemas import Plan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_hint(
    section_format="table",
    section_title="Rate Gap",
    table_headers=None,
    rows=None,
) -> dict:
    h: dict = {"section_format": section_format, "section_title": section_title}
    if table_headers is not None:
        h["table_headers"] = table_headers
    if rows is not None:
        h["rows"] = rows
    return h


def _make_ctx(**kwargs):
    """Return a SimpleNamespace that mimics the ctx fields _finalize_response reads."""
    defaults = dict(
        chat_mode=None,
        system_context=None,
        usages=[],
        seed_tool_results=None,
        react_tool_results=None,
        # fields _make_react_plan / _sync_extra_out_to_context need
        effective_message="test query",
        message="test query",
        correlation_id="cid",
        thread_id="t1",
        extra_out=None,
        # fields _execute_tool needs
        merged_state=None,
        user_profile=None,
    )
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# a) _execute_tool sets _tr["section_hint"] when env.extra has section_format
# ---------------------------------------------------------------------------

def test_section_hint_captured_in_tr():
    """_execute_tool must copy env.extra fields into _tr['section_hint']."""
    from app.pipeline.react_loop import _execute_tool

    envelope_extra = {
        "section_format": "table",
        "section_title": "Rate Gap",
        "table_headers": ["A", "B"],
        "table_rows": [["x", "y"]],
    }

    mock_env = MagicMock()
    mock_env.text = "some result text"
    mock_env.extra = envelope_extra
    mock_env.signal = "corpus_only"
    mock_env.sources = []
    mock_env.golden = False

    ctx = _make_ctx(correlation_id="test-cid", thread_id="t1")

    # _execute_tool does `from app.skills import registry as _skill_registry` locally,
    # so we patch the module object's attributes rather than a module-level name.
    import app.skills.registry as _skills_reg_mod
    original_has = getattr(_skills_reg_mod, "has", None)
    original_dispatch = getattr(_skills_reg_mod, "dispatch", None)
    original_SkillCall = getattr(_skills_reg_mod, "SkillCall", None)

    mock_call_instance = MagicMock()
    mock_SkillCall = MagicMock(return_value=mock_call_instance)
    _skills_reg_mod.has = MagicMock(return_value=True)
    _skills_reg_mod.dispatch = MagicMock(return_value=mock_env)
    _skills_reg_mod.SkillCall = mock_SkillCall
    try:
        tr = _execute_tool(
            tool="get_org_rate_gap",
            inputs={"org_id": "123"},
            ctx=ctx,
            emitter=None,
        )
    finally:
        if original_has is not None:
            _skills_reg_mod.has = original_has
        if original_dispatch is not None:
            _skills_reg_mod.dispatch = original_dispatch
        if original_SkillCall is not None:
            _skills_reg_mod.SkillCall = original_SkillCall

    assert "section_hint" in tr, "section_hint must be set when env.extra has section_format"
    sh = tr["section_hint"]
    assert sh["section_format"] == "table"
    assert sh["section_title"] == "Rate Gap"
    assert sh["table_headers"] == ["A", "B"]
    # table_rows must be normalized to rows
    assert sh["rows"] == [["x", "y"]], "table_rows should be normalized to rows"
    assert "table_rows" not in sh, "original table_rows key should not be in section_hint"


# ---------------------------------------------------------------------------
# b) tr_entry preserves section_hint when appending to tool_results
# ---------------------------------------------------------------------------

def test_tr_entry_preserves_section_hint():
    """When a result dict has section_hint, the appended tr_entry must carry it."""
    hint = _make_hint(rows=[["x", "y"]])
    result = {
        "success": True,
        "result": "some result",
        "section_hint": hint,
    }

    # Mimic the append logic at ~line 2581-2592
    tr_entry: dict = {
        "tool": "get_org_rate_gap",
        "success": result.get("success", False),
        "result": result.get("result", ""),
    }
    if result.get("section_hint"):
        tr_entry["section_hint"] = result["section_hint"]

    assert tr_entry["section_hint"] == hint


# ---------------------------------------------------------------------------
# c) _finalize_response sets ctx.tool_section_hints from react_tool_results
# ---------------------------------------------------------------------------

def test_finalize_response_sets_tool_section_hints():
    """_finalize_response must collect section_hint entries from ctx.react_tool_results."""
    from app.pipeline.react_loop import _finalize_response

    hint = _make_hint(table_headers=["A", "B"], rows=[["x", "y"]])
    ctx = _make_ctx(
        correlation_id="cid",
        thread_id="t1",
        react_tool_results=[
            {
                "tool": "get_org_rate_gap",
                "success": True,
                "result": "some text",
                "section_hint": hint,
            }
        ],
    )

    _finalize_response(ctx, "answer text", [], "no_sources", "get_org_rate_gap", None)

    assert hasattr(ctx, "tool_section_hints"), "ctx.tool_section_hints must be set"
    assert ctx.tool_section_hints == [hint]


# ---------------------------------------------------------------------------
# d) _finalize_response leaves tool_section_hints unset when no hints exist
# ---------------------------------------------------------------------------

def test_finalize_response_no_hints_when_no_section_hint():
    """When no tool result carries section_hint, ctx.tool_section_hints must not be set."""
    from app.pipeline.react_loop import _finalize_response

    ctx = _make_ctx(
        correlation_id="cid",
        thread_id="t1",
        react_tool_results=[
            {
                "tool": "search_corpus",
                "success": True,
                "result": "some plain text",
                # no section_hint
            }
        ],
    )

    _finalize_response(ctx, "answer", [], "corpus_only", "search_corpus", None)

    assert getattr(ctx, "tool_section_hints", None) is None


# ---------------------------------------------------------------------------
# e) _build_consolidator_input_json includes tool_section_hints when provided
# ---------------------------------------------------------------------------

def test_build_consolidator_includes_tool_section_hints():
    """Consolidator JSON payload must include tool_section_hints when passed."""
    from app.responder.final import _build_consolidator_input_json

    plan = Plan(subquestions=[])
    hints = [{"section_format": "table", "section_title": "Rate Gap", "rows": [["x", "y"]]}]

    raw = _build_consolidator_input_json(
        plan,
        [],
        "What is the rate gap?",
        tool_section_hints=hints,
    )
    payload = json.loads(raw)

    assert "tool_section_hints" in payload
    assert payload["tool_section_hints"] == hints


# ---------------------------------------------------------------------------
# f) _build_consolidator_input_json omits tool_section_hints when None
# ---------------------------------------------------------------------------

def test_build_consolidator_omits_hints_when_none():
    """Consolidator JSON payload must NOT include tool_section_hints when not provided."""
    from app.responder.final import _build_consolidator_input_json

    plan = Plan(subquestions=[])

    raw = _build_consolidator_input_json(
        plan,
        [],
        "What is the rate gap?",
        tool_section_hints=None,
    )
    payload = json.loads(raw)

    assert "tool_section_hints" not in payload


# ---------------------------------------------------------------------------
# g) table_rows is normalized to rows; table_rows key absent from section_hint
# ---------------------------------------------------------------------------

def test_table_rows_normalized_to_rows():
    """_execute_tool must normalize env.extra['table_rows'] → section_hint['rows']."""
    from app.pipeline.react_loop import _execute_tool

    envelope_extra = {
        "section_format": "table",
        "section_title": "Rate Table",
        "table_headers": ["Code", "Rate"],
        "table_rows": [["H2019", "$10.50"]],
        # no 'rows' key — only table_rows
    }

    mock_env = MagicMock()
    mock_env.text = "result"
    mock_env.extra = envelope_extra
    mock_env.signal = "corpus_only"
    mock_env.sources = []
    mock_env.golden = False

    ctx = _make_ctx(correlation_id="cid2", thread_id="t2")

    import app.skills.registry as _skills_reg_mod
    mock_call_instance = MagicMock()
    mock_SkillCall = MagicMock(return_value=mock_call_instance)
    _orig_has = getattr(_skills_reg_mod, "has", None)
    _orig_dispatch = getattr(_skills_reg_mod, "dispatch", None)
    _orig_SC = getattr(_skills_reg_mod, "SkillCall", None)
    _skills_reg_mod.has = MagicMock(return_value=True)
    _skills_reg_mod.dispatch = MagicMock(return_value=mock_env)
    _skills_reg_mod.SkillCall = mock_SkillCall
    try:
        tr = _execute_tool(
            tool="get_org_rate_gap",
            inputs={},
            ctx=ctx,
            emitter=None,
        )
    finally:
        if _orig_has is not None:
            _skills_reg_mod.has = _orig_has
        if _orig_dispatch is not None:
            _skills_reg_mod.dispatch = _orig_dispatch
        if _orig_SC is not None:
            _skills_reg_mod.SkillCall = _orig_SC

    sh = tr.get("section_hint", {})
    assert "rows" in sh, "rows must be present after normalization"
    assert sh["rows"] == [["H2019", "$10.50"]]
    assert "table_rows" not in sh, "table_rows must not leak into section_hint"


# ---------------------------------------------------------------------------
# h) _normalize_answer_card converts ui_blocks → format/data
# ---------------------------------------------------------------------------

def test_normalize_answer_card_converts_ui_blocks_table():
    """LLM may emit ui_blocks instead of format+data; normalizer must fix it."""
    from app.responder.final import _parse_answer_card

    raw_card = json.dumps({
        "mode": "BLENDED",
        "direct_answer": "See table.",
        "sections": [
            {
                "intent": "process",
                "label": "Rate Gap",
                "bullets": [],
                "ui_blocks": [
                    {
                        "type": "table",
                        "title": "Rate Gap",
                        "headers": ["Service Line", "Org PPC", "Peer P50"],
                        "rows": [["Psych Rehab", "$124.46", "$77.27"]],
                    }
                ],
            }
        ],
        "takeaways": [],
        "next_steps": [],
        "gaps": [],
        "next_questions_for_user": [],
        "thread_summary": "Rate gap",
        "suggested_actions": [],
    })

    card = _parse_answer_card(raw_card)
    assert card is not None, "_parse_answer_card should succeed"
    sec = card["sections"][0]
    assert sec.get("format") == "table", f"expected format=table, got {sec.get('format')!r}"
    assert "ui_blocks" not in sec, "ui_blocks must be removed after normalization"
    assert sec["data"]["headers"] == ["Service Line", "Org PPC", "Peer P50"]
    assert sec["data"]["rows"] == [["Psych Rehab", "$124.46", "$77.27"]]


# ---------------------------------------------------------------------------
# i) pre_built_sections generated from table hints
# ---------------------------------------------------------------------------

def test_pre_built_sections_generated_from_table_hint():
    """When tool_section_hints has a table hint, pre_built_sections must be populated."""
    from app.responder.final import _build_consolidator_input_json

    hints = [
        {
            "section_format": "table",
            "section_title": "Rate Gap Analysis",
            "table_headers": ["Service Line", "Org PPC", "Peer P50"],
            "rows": [["Psych Rehab", "$124.46", "$77.27"]],
        }
    ]
    payload = json.loads(
        _build_consolidator_input_json(
            Plan(subquestions=[]), [], "rate gap?", tool_section_hints=hints
        )
    )
    assert "pre_built_sections" in payload, "pre_built_sections must be present"
    pbs = payload["pre_built_sections"]
    assert len(pbs) == 1
    assert pbs[0]["format"] == "table"
    assert pbs[0]["label"] == "Rate Gap Analysis"
    assert pbs[0]["data"]["headers"] == ["Service Line", "Org PPC", "Peer P50"]
    assert pbs[0]["data"]["rows"] == [["Psych Rehab", "$124.46", "$77.27"]]


def test_pre_built_sections_omitted_when_no_hints():
    """Without hints, pre_built_sections must not appear in payload."""
    from app.responder.final import _build_consolidator_input_json

    payload = json.loads(
        _build_consolidator_input_json(Plan(subquestions=[]), [], "what is prior auth?")
    )
    assert "pre_built_sections" not in payload


# ---------------------------------------------------------------------------
# k) extra.sections[] multi-section path
# ---------------------------------------------------------------------------

def test_execute_tool_multi_section_sets_section_hints():
    """extra.sections[] → _tr['section_hints'] (plural) with both entries."""
    from app.pipeline.react_loop import _execute_tool

    multi_extra = {
        "sections": [
            {
                "section_format": "table",
                "section_title": "Service-Line Rate Gap",
                "table_headers": ["Service Line", "Org PPC"],
                "table_rows": [["Psych Rehab", "$124.46"]],
            },
            {
                "section_format": "table",
                "section_title": "Rate Gap by MSA",
                "table_headers": ["MSA", "Org PPC", "MSA P50"],
                "table_rows": [["Naples", "$124.46", "$80.00"]],
            },
        ]
    }

    mock_env = MagicMock()
    mock_env.text = "Rate gap analysis..."
    mock_env.extra = multi_extra
    mock_env.sources = []
    mock_env.signal = "no_sources"

    import app.skills.registry as _skills_reg_mod
    _orig_has = getattr(_skills_reg_mod, "has", None)
    _orig_dispatch = getattr(_skills_reg_mod, "dispatch", None)
    _orig_SC = getattr(_skills_reg_mod, "SkillCall", None)

    _skills_reg_mod.has = lambda name: True
    _skills_reg_mod.dispatch = lambda call: mock_env
    _skills_reg_mod.SkillCall = MagicMock(return_value=MagicMock())

    ctx = _make_ctx(correlation_id="test-multi")

    try:
        tr = _execute_tool("get_org_rate_gap", {"include_msa_cut": True}, ctx, None)
    finally:
        if _orig_has is not None: _skills_reg_mod.has = _orig_has
        if _orig_dispatch is not None: _skills_reg_mod.dispatch = _orig_dispatch
        if _orig_SC is not None: _skills_reg_mod.SkillCall = _orig_SC

    assert "section_hints" in tr, "section_hints (plural) must be set for multi-section extra"
    assert "section_hint" not in tr, "section_hint (singular) must NOT be set for multi-section path"
    hints = tr["section_hints"]
    assert len(hints) == 2
    assert hints[0]["section_format"] == "table"
    assert hints[0]["section_title"] == "Service-Line Rate Gap"
    assert hints[0]["rows"] == [["Psych Rehab", "$124.46"]]
    assert hints[1]["section_title"] == "Rate Gap by MSA"
    assert hints[1]["rows"] == [["Naples", "$124.46", "$80.00"]]


def test_finalize_response_collects_plural_section_hints():
    """_finalize_response must collect section_hints (plural) into ctx.tool_section_hints."""
    from app.pipeline.react_loop import _finalize_response

    hints = [
        {"section_format": "table", "section_title": "A", "rows": [["x"]]},
        {"section_format": "table", "section_title": "B", "rows": [["y"]]},
    ]
    ctx = _make_ctx(
        correlation_id="test",
        react_tool_results=[
            {"tool": "get_org_rate_gap", "success": True, "result": "...", "section_hints": hints}
        ],
    )

    _finalize_response(ctx, "answer", [], "no_sources", "get_org_rate_gap", None)

    assert hasattr(ctx, "tool_section_hints")
    assert len(ctx.tool_section_hints) == 2
    assert ctx.tool_section_hints[0]["section_title"] == "A"
    assert ctx.tool_section_hints[1]["section_title"] == "B"
