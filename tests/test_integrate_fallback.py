"""Tests for integrate stage: try-again stub when response is unparseable."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.pipeline.context import PipelineContext
from app.planner.schemas import Plan, SubQuestion
from app.stages.integrate import run_integrate

FALLBACK_TRY_AGAIN = "Something went wrong. Please try again, or start a new chat."


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
