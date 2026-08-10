"""Unit tests for pipeline orchestrator error boundaries."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from app.pipeline.context import PipelineContext
from app.pipeline.orchestrator import (
    run_pipeline,
    _publish_failed,
    _publish_clarification_or_refinement,
    _emit_model_summary,
)

USE_REACT = os.environ.get("MOBIUS_USE_REACT", "").lower() in ("1", "true", "yes")


def test_publish_failed_produces_structured_payload():
    """_publish_failed always produces a structured payload with required keys.

    Sprint A.1 commit 3: _publish_failed now also appends a
    turn_failed envelope dict to thinking_log before publishing.
    So thinking_log is no longer the bare input list; it carries
    the original chunks PLUS the structured failure event. Test
    updated to reflect the new shape."""
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
        with patch("app.pipeline.orchestrator.store_response"):
            _publish_failed(
                "test-cid-structured-payload",
                "test message",
                None,
                ["chunk1"],
                ValueError("test error"),
            )
    # Verify structured payload was passed to publish_response
    mock_q.return_value.publish_response.assert_called_once()
    call_args = mock_q.return_value.publish_response.call_args
    assert call_args[0][0] == "test-cid-structured-payload"
    payload = call_args[0][1]
    assert payload["status"] == "failed"
    assert payload["llm_error"] == "test error"
    # thinking_log now starts with the caller's chunks, followed by a
    # turn_failed envelope dict that _publish_failed appended.
    log = payload["thinking_log"]
    assert log[0] == "chunk1"
    # The appended envelope has the expected shape:
    assert len(log) == 2
    env = log[1]
    assert isinstance(env, dict) and env.get("signal") == "turn_failed"
    assert env.get("data", {}).get("error_class") == "ValueError"
    assert payload_keys.issubset(payload.keys())


def test_publish_failed_includes_assistant_envelope():
    """Ananth ruling (2026-08-09): early-exit turns must emit
    assistant_envelope so the FE renders a typed UX envelope, not a plain
    bubble -- same call as integrate.py's full-answer path, wired minimally
    at this early-exit site too."""
    with patch("app.pipeline.orchestrator.get_queue") as mock_q:
        with patch("app.pipeline.orchestrator.store_response"):
            _publish_failed(
                "test-cid-envelope",
                "test message",
                None,
                ["chunk1"],
                ValueError("test error"),
            )
    payload = mock_q.return_value.publish_response.call_args[0][1]
    env = payload.get("assistant_envelope")
    assert isinstance(env, dict)
    assert "blocks" in env


def test_publish_failed_envelope_failure_does_not_break_the_failure_response():
    """Building the envelope must never take down the already-failing
    turn's response -- wrapped in its own try/except."""
    with patch("app.pipeline.orchestrator.get_queue") as mock_q:
        with patch("app.pipeline.orchestrator.store_response"):
            with patch(
                "app.communication.assistant_envelope.build_assistant_envelope_v1",
                side_effect=RuntimeError("boom"),
            ):
                _publish_failed(
                    "test-cid-envelope-fail",
                    "test message",
                    None,
                    ["chunk1"],
                    ValueError("test error"),
                )
    mock_q.return_value.publish_response.assert_called_once()
    payload = mock_q.return_value.publish_response.call_args[0][1]
    assert payload["status"] == "failed"


def test_publish_failed_handles_none_thinking_chunks():
    """_publish_failed handles None thinking_chunks.

    Sprint A.1 commit 3: even when called with None chunks, the
    turn_failed envelope still gets appended, so thinking_log is
    a 1-element list carrying just the failure event."""
    with patch("app.pipeline.orchestrator.get_queue") as mock_q:
        with patch("app.pipeline.orchestrator.store_response"):
            _publish_failed(
                "test-cid-none-chunks",
                "msg",
                None,
                None,
                RuntimeError("oops"),
            )
    payload = mock_q.return_value.publish_response.call_args[0][1]
    # Starts empty; _publish_failed appends the turn_failed envelope.
    log = payload["thinking_log"]
    assert len(log) == 1
    assert log[0].get("signal") == "turn_failed"
    assert log[0].get("data", {}).get("error_class") == "RuntimeError"


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


# ── RAG post-synthesis grading callback (2026-08-06, Phase 1 cutover) ──
#
# _fire_rag_grade_callbacks re-keyed from rag_agent_id to correlation_id
# (see app/skills/builtin/corpus_search.py's module docstring for the
# full history: RAG's new /api/retriever/answer response has no
# agent_id-equivalent field, and Retriever confirmed the grade endpoint
# actually filters WHERE correlation_id = :cid on the DB).


def test_fire_rag_grade_callbacks_patches_correlation_id_url():
    from app.pipeline.orchestrator import _fire_rag_grade_callbacks

    ctx = PipelineContext(correlation_id="turn-cid-123", thread_id="t", message="m")
    ctx.final_message = "The timely filing deadline is 180 days."
    ctx.pending_rag_grade_calls = [{
        "base_url": "https://mobius-rag-ortabkknqa-uc.a.run.app",
        "correlation_id": "turn-cid-123",
        "query": "What is the timely filing deadline?",
        "chunks": [{"text": "180 days"}],
    }]

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        import json as _json
        captured["body"] = _json.loads(req.data.decode())
        class _Resp:
            def read(self):
                return b"{}"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        return _Resp()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        _fire_rag_grade_callbacks(ctx)
        import time as _time
        _time.sleep(0.05)  # fire-and-forget thread — give it a moment

    assert captured["url"] == "https://mobius-rag-ortabkknqa-uc.a.run.app/api/observe/decisions/turn-cid-123/grade"
    assert captured["method"] == "PATCH"
    assert captured["body"]["answer"] == "The timely filing deadline is 180 days."
    assert captured["body"]["query"] == "What is the timely filing deadline?"


def test_fire_rag_grade_callbacks_noop_when_no_pending():
    from app.pipeline.orchestrator import _fire_rag_grade_callbacks

    ctx = PipelineContext(correlation_id="c", thread_id="t", message="m")
    ctx.final_message = "an answer"
    ctx.pending_rag_grade_calls = []

    with patch("urllib.request.urlopen") as mock_urlopen:
        _fire_rag_grade_callbacks(ctx)
    mock_urlopen.assert_not_called()


def test_fire_rag_grade_callbacks_noop_when_no_final_answer():
    from app.pipeline.orchestrator import _fire_rag_grade_callbacks

    ctx = PipelineContext(correlation_id="c", thread_id="t", message="m")
    ctx.final_message = ""
    ctx.pending_rag_grade_calls = [{
        "base_url": "https://rag.example.com", "correlation_id": "cid",
        "query": "q", "chunks": [],
    }]

    with patch("urllib.request.urlopen") as mock_urlopen:
        _fire_rag_grade_callbacks(ctx)
    mock_urlopen.assert_not_called()


# ── assistant_envelope on early-exit publish paths (Ananth ruling, 2026-08-09) ──
# 6 anchors in orchestrator.py were rendering as plain bubbles instead of typed
# UX envelopes because they never called build_assistant_envelope_v1() (the
# full-answer path at integrate.py:1711 already did). Minimal fix: wire the
# same call at each early-exit site. Covers the 3 branches of
# _publish_clarification_or_refinement + _publish_failed directly-testable
# here; the other 2 (ReAct task-mode reply, status-only bypass, both inside
# run_pipeline's react branch) are verified live post-deploy instead --
# mocking the full pipeline just to reach those two lines isn't worth the
# fixture weight for a change this mechanical.

def _publish_and_capture(ctx: PipelineContext) -> dict:
    with patch("app.pipeline.orchestrator.get_persistence") as mock_persist:
        mock_persist.return_value.save_turn_with_messages = lambda **kw: None
        mock_persist.return_value.save_turn = lambda **kw: None
        with patch("app.pipeline.orchestrator.get_queue") as mock_q:
            with patch("app.pipeline.orchestrator.store_response"):
                with patch("app.pipeline.orchestrator.save_state_full"):
                    with patch("app.pipeline.orchestrator.register_open_slots"):
                        _publish_clarification_or_refinement(ctx, 0.0)
    mock_q.return_value.publish_response.assert_called_once()
    return mock_q.return_value.publish_response.call_args[0][1]


def test_route_clarification_includes_assistant_envelope():
    ctx = PipelineContext(correlation_id="c-route", thread_id="t", message="m")
    ctx.needs_route_clarification = True
    ctx.route_clarification_choices = [{"id": "web", "label": "Web"}, {"id": "rag", "label": "Policy materials"}]

    payload = _publish_and_capture(ctx)
    assert payload["status"] == "clarification"
    env = payload.get("assistant_envelope")
    assert isinstance(env, dict) and "blocks" in env


def test_slot_clarification_includes_assistant_envelope():
    ctx = PipelineContext(correlation_id="c-slot", thread_id="t", message="m")
    ctx.needs_clarification = True
    ctx.clarification_message = "Which state?"
    ctx.missing_slots = ["jurisdiction"]

    payload = _publish_and_capture(ctx)
    assert payload["status"] == "clarification"
    env = payload.get("assistant_envelope")
    assert isinstance(env, dict) and "blocks" in env


def test_refinement_ask_includes_assistant_envelope():
    ctx = PipelineContext(correlation_id="c-refine", thread_id="t", message="m")
    ctx.needs_clarification = False
    ctx.refinement_suggestions = ["Try being more specific about the payer."]

    payload = _publish_and_capture(ctx)
    assert payload["status"] == "refinement_ask"
    env = payload.get("assistant_envelope")
    assert isinstance(env, dict) and "blocks" in env
