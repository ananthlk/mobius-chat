"""Unit tests for pipeline orchestrator error boundaries."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from app.pipeline.context import PipelineContext
from app.pipeline.orchestrator import run_pipeline, _publish_failed, _emit_model_summary

USE_REACT = os.environ.get("MOBIUS_USE_REACT", "").lower() in ("1", "true", "yes")


def test_publish_failed_produces_structured_payload():
    """_publish_failed always produces a structured payload with required keys."""
    payload_keys = {
        "status",
        "message",
        "plan",
        "thinking_log",
        "response_source",
        "model_used",
        "llm_error",
        "tokens_used",
        "usage_breakdown",
        "cost_usd",
        "sources",
        "source_confidence_strip",
        "cited_source_indices",
        "thread_id",
    }
    with patch("app.pipeline.orchestrator.get_queue") as mock_q:
        with patch("app.pipeline.orchestrator.clear_progress"):
            with patch("app.pipeline.orchestrator.store_response"):
                _publish_failed(
                    "test-cid",
                    "test message",
                    None,
                    ["chunk1"],
                    ValueError("test error"),
                )
    # Verify structured payload was passed to publish_response
    mock_q.return_value.publish_response.assert_called_once()
    call_args = mock_q.return_value.publish_response.call_args
    assert call_args[0][0] == "test-cid"
    payload = call_args[0][1]
    assert payload["status"] == "failed"
    assert payload["llm_error"] == "test error"
    assert payload["thinking_log"] == ["chunk1"]
    assert payload_keys.issubset(payload.keys())


def test_publish_failed_handles_none_thinking_chunks():
    """_publish_failed handles None thinking_chunks."""
    with patch("app.pipeline.orchestrator.get_queue") as mock_q:
        with patch("app.pipeline.orchestrator.clear_progress"):
            with patch("app.pipeline.orchestrator.store_response"):
                _publish_failed(
                    "test-cid",
                    "msg",
                    None,
                    None,
                    RuntimeError("oops"),
                )
    payload = mock_q.return_value.publish_response.call_args[0][1]
    assert payload["thinking_log"] == []


@pytest.mark.skipif(USE_REACT, reason="ReAct path skips clarify stage; test applies to legacy pipeline only")
def test_clarify_stage_error_publishes_failed():
    """When run_clarify raises, pipeline publishes failed response (no crash)."""
    from app.planner.schemas import Plan, SubQuestion

    def _set_plan(ctx, **_):
        ctx.plan = Plan(subquestions=[SubQuestion(id="sq1", text="x", kind="non_patient")])
        ctx.refined_query = "x"
        ctx.blueprint = [{"agent": "RAG"}]

    with patch.dict(os.environ, {"MOBIUS_USE_REACT": "0"}, clear=False):
        with patch("app.pipeline.orchestrator.run_plan", side_effect=_set_plan):
            with patch("app.pipeline.orchestrator.run_clarify") as mock_clarify:
                mock_clarify.side_effect = RuntimeError("clarify crash")
                with patch("app.pipeline.orchestrator.get_queue") as mock_q:
                    with patch("app.pipeline.orchestrator.clear_progress"):
                        with patch("app.pipeline.orchestrator.store_response"):
                            run_pipeline("test-clarify-fail", "test msg", None)
    mock_q.return_value.publish_response.assert_called_once()
    payload = mock_q.return_value.publish_response.call_args[0][1]
    assert payload["status"] == "failed"
    assert "clarify crash" in payload["llm_error"]


def test_emit_model_summary_no_emitter():
    """_emit_model_summary with emitter=None does nothing."""
    ctx = PipelineContext(correlation_id="c", thread_id="t", message="m")
    ctx.usages = [{"model": "gemini-2.5-flash", "provider": "vertex", "latency_s": 1.5}]
    _emit_model_summary(ctx, 2.0, None)


def test_emit_model_summary_with_usages():
    """_emit_model_summary with usages emits model + latency."""
    ctx = PipelineContext(correlation_id="c", thread_id="t", message="m")
    ctx.usages = [{"model": "gemini-2.5-flash", "provider": "vertex", "latency_s": 1.5}]
    emitted = []
    _emit_model_summary(ctx, 2.0, emitted.append)
    assert len(emitted) == 1
    assert "Gemini Flash" in emitted[0]
    assert "1.5s" in emitted[0]


def test_emit_model_summary_answered_from_report():
    """_emit_model_summary with no usages but active_skill_reference emits report line."""
    ctx = PipelineContext(correlation_id="c", thread_id="t", message="m")
    ctx.usages = []
    ctx.active_skill_reference = True
    emitted = []
    _emit_model_summary(ctx, 0.2, emitted.append)
    assert len(emitted) == 1
    assert "Answered from report" in emitted[0]
