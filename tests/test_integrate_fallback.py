"""Tests for integrate stage: try-again stub when response is unparseable."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.communication.json_display_sanitize import DEFAULT_BLEED_FALLBACK
from app.pipeline.context import PipelineContext
from app.planner.schemas import Plan, SubQuestion
from app.stages.integrate import run_integrate

# Keep this test aligned with the production-code constant so copy
# changes to the fallback message don't require test churn. The
# 2026-04-18 rewrite moved from "Something went wrong. Please try
# again, or start a new chat." to a softer "I had trouble formatting
# the answer. Please try rephrasing your question." — see the comment
# in app/communication/json_display_sanitize.py for rationale.
FALLBACK_TRY_AGAIN = DEFAULT_BLEED_FALLBACK


def test_unparseable_final_message_produces_try_again_card():
    """When format_response returns plain text (e.g. integrator exception), integrate sends try-again AnswerCard."""
    plan = Plan(subquestions=[SubQuestion(id="sq1", text="What is X?", kind="non_patient")])
    ctx = PipelineContext(
        correlation_id="test-cid",
        thread_id="test-thread",
        message="What is X?",
        plan=plan,
        answers=["Some answer"],
        sources=[],
        usages=[],
        retrieval_signals=[],
    )

    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = ("Plain text from LLM error or fallback.", None)
        run_integrate(ctx)

    assert ctx.response_payload is not None
    msg = ctx.response_payload.get("message", "")
    assert isinstance(msg, str)
    parsed = json.loads(msg)
    assert parsed.get("mode") == "FACTUAL"
    assert "direct_answer" in parsed
    assert parsed["direct_answer"] == FALLBACK_TRY_AGAIN
    assert parsed.get("sections") == []


def test_invalid_json_final_message_produces_try_again_card():
    """When final_message is invalid JSON (not AnswerCard), integrate sends try-again AnswerCard."""
    plan = Plan(subquestions=[SubQuestion(id="sq1", text="What is X?", kind="non_patient")])
    ctx = PipelineContext(
        correlation_id="test-cid",
        thread_id="test-thread",
        message="What is X?",
        plan=plan,
        answers=["Some answer"],
        sources=[],
        usages=[],
        retrieval_signals=[],
    )

    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = ("{ invalid json }", None)
        run_integrate(ctx)

    assert ctx.response_payload is not None
    msg = ctx.response_payload.get("message", "")
    parsed = json.loads(msg)
    assert parsed.get("direct_answer") == FALLBACK_TRY_AGAIN
    assert parsed.get("sections") == []
