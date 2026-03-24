"""Tests for responder invalid-JSON fallback (Day 2 gate: no 500 on invalid integrator output)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.planner.schemas import Plan, SubQuestion
from app.responder.final import _parse_answer_card, format_response


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

    # Patch where LLM is used (final imports get_llm_provider from app.services.llm_provider inside format_response)
    with patch("app.services.llm_provider.get_llm_provider") as p:
        mock_provider = p.return_value
        mock_provider.generate_with_usage = AsyncMock(return_value=("{ invalid json from llm }", {}))
        # Repair path may be tried; mock it to also return invalid so we hit minimal wrap
        with patch("app.responder.final._repair_json", return_value=""):
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
