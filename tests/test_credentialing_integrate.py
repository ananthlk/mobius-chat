"""
Tests that credentialing report Q&A flows through the integrator to the final response.

The final response is always routed through the integrator (format_response).
We verify: tool answer → ctx.answers → integrate stage → ctx.final_message and response_payload.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.pipeline.context import PipelineContext
from app.planner.schemas import Plan, SubQuestion
from app.stages.integrate import run_integrate
from app.stages.resolve import run_resolve
from app.services.doc_assembly import RETRIEVAL_SIGNAL_ROSTER_COMPLETE


# Credentialing tool answer as would be returned by _ask_credentialing_report
CREDENTIALING_ANSWER = (
    "The report shows 12 providers in Section A (ready for PML), "
    "$2M at-risk revenue in Section B, and 3 providers in Section C needing attention."
)


def _make_credentialing_plan() -> Plan:
    return Plan(
        subquestions=[
            SubQuestion(
                id="sq1",
                text="What is the latest credentialing report for David Lawrence Center?",
                kind="tool",
                question_intent="factual",
                intent_score=0.8,
            )
        ]
    )


def _make_ctx_with_credentialing_answer() -> PipelineContext:
    plan = _make_credentialing_plan()
    ctx = PipelineContext(
        correlation_id="test-corr-1",
        thread_id="thread-1",
        message="What is the latest report for David Lawrence Center?",
    )
    ctx.plan = plan
    ctx.answers = [CREDENTIALING_ANSWER]
    ctx.sources = [
        {
            "index": 1,
            "document_name": "Credentialing report",
            "text": CREDENTIALING_ANSWER[:300],
            "source_type": "external",
        }
    ]
    ctx.retrieval_signals = [RETRIEVAL_SIGNAL_ROSTER_COMPLETE]
    ctx.answer_set = {
        "sq1": {
            "answer": CREDENTIALING_ANSWER,
            "source": "tool",
            "status": "answered",
            "layer_used": 2,
        }
    }
    return ctx


def test_credentialing_answer_passed_to_integrator():
    """Integrate stage receives credentialing tool answer and passes it to format_response."""
    ctx = _make_ctx_with_credentialing_answer()
    # AnswerCard JSON as the integrator would produce from the credentialing answer
    expected_direct = "Based on the latest credentialing report for David Lawrence Center: " + CREDENTIALING_ANSWER
    final_json = json.dumps({
        "mode": "FACTUAL",
        "direct_answer": expected_direct,
        "sections": [],
    })

    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (final_json, None)
        run_integrate(ctx, emitter=None)

    mock_format.assert_called_once()
    call_args = mock_format.call_args
    assert call_args[0][0] == ctx.plan
    assert call_args[0][1] == ctx.answers
    assert call_args[1]["user_message"] == ctx.message
    assert ctx.answers[0] == CREDENTIALING_ANSWER

    assert ctx.final_message == final_json
    parsed = json.loads(ctx.final_message)
    assert parsed.get("mode") == "FACTUAL"
    assert CREDENTIALING_ANSWER in parsed.get("direct_answer", "")


def test_credentialing_final_response_parsed_into_payload():
    """After integrate, final_message is valid AnswerCard JSON and response_payload has status and message."""
    ctx = _make_ctx_with_credentialing_answer()
    final_json = json.dumps({
        "mode": "FACTUAL",
        "direct_answer": "Summary: " + CREDENTIALING_ANSWER,
        "sections": [{"title": "Report summary", "label": "Report summary", "content": CREDENTIALING_ANSWER[:200]}],
    })

    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (final_json, None)
        run_integrate(ctx, emitter=None)

    assert ctx.final_message == final_json
    assert ctx.response_payload is not None
    assert ctx.response_payload.get("status") == "completed"
    # Payload message is the AnswerCard JSON (display_message) shown to the user
    message = ctx.response_payload.get("message") or ""
    assert message
    parsed = json.loads(message)
    assert parsed.get("mode") == "FACTUAL"
    assert CREDENTIALING_ANSWER in parsed.get("direct_answer", "")


def test_credentialing_integrate_sets_final_message():
    """run_integrate sets ctx.final_message from format_response return."""
    ctx = _make_ctx_with_credentialing_answer()
    expected = '{"mode": "FACTUAL", "direct_answer": "The credentialing report shows 12 providers in Section A.", "sections": []}'

    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (expected, None)
        run_integrate(ctx, emitter=None)

    assert ctx.final_message == expected
    data = json.loads(ctx.final_message)
    assert data.get("direct_answer", "").find("credentialing") >= 0 or data.get("direct_answer", "").find("Section A") >= 0


def test_credentialing_resolve_then_integrate_final_response():
    """Full path: resolve (tool returns credentialing answer) → integrate → final response in payload."""
    plan = _make_credentialing_plan()
    blueprint = [
        {
            "agent": "tool",
            "text": plan.subquestions[0].text,
            "reframed_text": plan.subquestions[0].text,
        }
    ]
    ctx = PipelineContext(
        correlation_id="test-corr-resolve",
        thread_id=None,
        message="What is the latest report for David Lawrence Center?",
    )
    ctx.plan = plan
    ctx.blueprint = blueprint
    ctx.merged_state = {}

    tool_answer = CREDENTIALING_ANSWER
    tool_sources = [{"index": 1, "document_name": "Credentialing report", "text": tool_answer[:300], "source_type": "external"}]

    with patch("app.stages.resolve.answer_tool") as mock_tool:
        mock_tool.return_value = (tool_answer, tool_sources, None, RETRIEVAL_SIGNAL_ROSTER_COMPLETE)
        run_resolve(ctx, emitter=None)

    assert len(ctx.answers) == 1
    assert ctx.answers[0] == tool_answer
    assert RETRIEVAL_SIGNAL_ROSTER_COMPLETE in ctx.retrieval_signals

    final_json = json.dumps({
        "mode": "FACTUAL",
        "direct_answer": "Based on the latest credentialing report for David Lawrence Center: " + tool_answer,
        "sections": [],
    })
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (final_json, None)
        run_integrate(ctx, emitter=None)

    assert ctx.final_message == final_json
    assert ctx.response_payload is not None
    assert ctx.response_payload.get("status") == "completed"
    msg = ctx.response_payload.get("message", "")
    assert msg
    parsed = json.loads(msg)
    assert CREDENTIALING_ANSWER in parsed.get("direct_answer", "")
