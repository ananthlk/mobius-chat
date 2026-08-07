"""Tests for responder invalid-JSON fallback (Day 2 gate: no 500 on invalid integrator output)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.planner.schemas import Plan, SubQuestion
from app.responder.final import _fallback_message, _parse_answer_card, format_response


def test_parse_answer_card_invalid_returns_none():
    """Invalid or non-JSON integrator output should parse to None."""
    assert _parse_answer_card("") is None
    assert _parse_answer_card("   ") is None
    assert _parse_answer_card("not json at all") is None
    assert _parse_answer_card("{ invalid }") is None
    assert _parse_answer_card('{"mode": "FACTUAL"}') is None  # missing direct_answer, sections
    assert _parse_answer_card('{"mode": "FACTUAL", "direct_answer": "x"}') is None  # missing sections
    assert _parse_answer_card('{"mode": "OTHER", "direct_answer": "x", "sections": []}') is None  # invalid mode


def test_parse_answer_card_valid_returns_dict():
    """Valid AnswerCard JSON should parse to dict with mode, direct_answer, sections."""
    out = _parse_answer_card('{"mode": "FACTUAL", "direct_answer": "Yes.", "sections": []}')
    assert out is not None
    assert out.get("mode") == "FACTUAL"
    assert out.get("direct_answer") == "Yes."
    assert out.get("sections") == []


def test_invalid_integrator_json_produces_fallback_no_500():
    """When integrator returns invalid JSON, format_response returns valid AnswerCard (minimal or fallback), never raises."""
    plan = Plan(subquestions=[SubQuestion(id="sq1", text="What is X?", kind="non_patient")])
    stub_answers = ["Answer one."]

    # Patch where LLM is used (final imports get_llm_provider from app.services.llm_provider inside format_response).
    # The ``_repair_json`` LLM-based repair tier was deleted in Phase 0.16b
    # (see comment in app/responder/final.py around line 195). The two
    # remaining parse tiers — ``json.loads`` then ``json_repair.loads`` —
    # both fail on "{ invalid json from llm }", so we hit the minimal-wrap
    # fallback path without needing to mock any repair function.
    with patch("app.services.llm_provider.get_llm_provider") as p:
        mock_provider = p.return_value
        mock_provider.generate_with_usage = AsyncMock(return_value=("{ invalid json from llm }", {}))
        message, usage = format_response(plan, stub_answers, "What is X?")
    assert message is not None
    assert isinstance(message, str)
    # Must be valid JSON (no 500, frontend can parse)
    parsed = json.loads(message)
    assert "mode" in parsed
    assert "direct_answer" in parsed
    assert "sections" in parsed
    assert parsed["mode"] in ("FACTUAL", "CANONICAL", "BLENDED")
    assert isinstance(parsed["sections"], list)


class TestFallbackMessageValidCard:
    """2026-08-07: _fallback_message used to return raw joined text with no
    card structure. On a tool-heavy turn, stub_answers can hold a tool's raw
    JSON result verbatim (e.g. appeals_validate_claim output) -- with no
    wrapping, that raw tool JSON leaked to the user as the entire "answer"
    when the integrator LLM call failed (surfaced by a genuine Vertex
    gemini-2.5-pro timeout in production). Now wraps as a minimal valid
    AnswerCard so it passes the downstream validator instead of getting
    replaced by a second, diverging try-again stub."""

    def test_returns_valid_json_not_raw_text(self):
        plan = Plan(subquestions=[SubQuestion(id="sq1", text="q", kind="non_patient")])
        out = _fallback_message(plan, ["Answer text."])
        parsed = json.loads(out)  # must not raise
        assert parsed["mode"] == "FACTUAL"
        assert parsed["direct_answer"] == "Answer text."
        assert parsed["sections"] == []

    def test_raw_tool_json_stub_answer_stays_a_string_value_not_top_level(self):
        """The exact live regression: a raw tool result as the stub answer
        must land INSIDE direct_answer (a string), never become top-level
        card structure itself."""
        plan = Plan(subquestions=[SubQuestion(id="sq1", text="q", kind="non_patient")])
        raw_tool_json = '{"raw_text": "COB.R001: Medicaid Payor of Last Resort", "rules_validated": 4}'
        out = _fallback_message(plan, [raw_tool_json])
        parsed = json.loads(out)
        assert parsed["direct_answer"] == raw_tool_json
        assert "raw_text" not in parsed  # not hoisted to top level
        assert parsed["mode"] == "FACTUAL"
        assert parsed["sections"] == []

    def test_no_subquestions_uses_bleed_fallback_text(self):
        """No subquestions at all -> parts is empty -> falls back to the
        standard bleed-fallback text rather than an empty direct_answer."""
        from app.communication.json_display_sanitize import DEFAULT_BLEED_FALLBACK

        plan = Plan(subquestions=[])
        out = _fallback_message(plan, [])
        parsed = json.loads(out)
        assert parsed["direct_answer"] == DEFAULT_BLEED_FALLBACK

    def test_missing_stub_answer_for_subquestion_uses_placeholder(self):
        """A subquestion with no corresponding stub_answers entry still gets
        SOME direct_answer text (the existing "[No answer yet]" placeholder,
        unchanged behavior) -- not an empty string."""
        plan = Plan(subquestions=[SubQuestion(id="sq1", text="q", kind="non_patient")])
        out = _fallback_message(plan, [])
        parsed = json.loads(out)
        assert parsed["direct_answer"] == "[No answer yet]"

    def test_multiple_subquestions_joined_into_single_direct_answer(self):
        plan = Plan(subquestions=[
            SubQuestion(id="sq1", text="q1", kind="non_patient"),
            SubQuestion(id="sq2", text="q2", kind="non_patient"),
        ])
        out = _fallback_message(plan, ["Answer one.", "Answer two."])
        parsed = json.loads(out)
        assert "Answer one." in parsed["direct_answer"]
        assert "Answer two." in parsed["direct_answer"]

    def test_passes_the_normal_answercard_validator(self):
        """Downstream integrate.py validator requires: dict, mode in the
        known set, direct_answer key present, sections a list (or valid
        RECITAL) -- confirm the wrapped fallback satisfies all of these so
        it doesn't get replaced by a SECOND, diverging try-again stub."""
        plan = Plan(subquestions=[SubQuestion(id="sq1", text="q", kind="non_patient")])
        out = _fallback_message(plan, ["some content"])
        check = json.loads(out)
        _mode = check.get("mode")
        _recital_valid = _mode == "RECITAL"
        is_invalid = (
            not isinstance(check, dict)
            or _mode not in ("FACTUAL", "CANONICAL", "BLENDED", "RECITAL")
            or "direct_answer" not in check
            or (not _recital_valid and not isinstance(check.get("sections"), list))
        )
        assert not is_invalid


def test_integrator_llm_exception_produces_valid_card_not_raw_leak():
    """End-to-end: when the integrator LLM call raises (timeout, etc.),
    format_response's except-path fallback must return a valid AnswerCard,
    not raw stub_answers text -- the exact live regression (Vertex
    gemini-2.5-pro timeout -> raw appeals tool JSON shown as the answer).
    Patched at generate_sync (what format_response actually calls) rather
    than the provider layer -- provider-level mocks can get bypassed by
    llm_manager's own multi-provider fallback/retry, which would silently
    make a real API call instead of exercising the exception path."""
    plan = Plan(subquestions=[SubQuestion(id="sq1", text="q", kind="non_patient")])
    raw_tool_json = '{"raw_text": "leaked tool output", "rules_validated": 4}'

    with patch("app.services.llm_manager.generate_sync", side_effect=TimeoutError("abandoned after deadline")):
        message, usage = format_response(plan, [raw_tool_json], "q")

    parsed = json.loads(message)
    assert parsed["direct_answer"] == raw_tool_json
    assert "raw_text" not in parsed
    assert usage is None
