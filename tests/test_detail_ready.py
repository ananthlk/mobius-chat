"""Tests for the "detailed answer" tab feature (Chat Master spec, 2026-08-05):
append_detail_answer() (progress.py) and its call site in run_integrate()
(integrate.py), which fires a detail_ready SSE event as soon as the
integrator's AnswerCard JSON parses, carrying display_summary (as
``content``) + output_intent.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.pipeline.context import PipelineContext
from app.planner.schemas import Plan, SubQuestion
from app.stages.integrate import run_integrate
from app.storage.progress import append_detail_answer, _progress, _lock


def _make_ctx():
    plan = Plan(subquestions=[SubQuestion(id="sq1", text="What is X?", kind="non_patient")])
    return PipelineContext(
        correlation_id="test-detail-cid",
        thread_id="test-thread",
        message="What is X?",
        plan=plan,
        answers=["Some answer"],
        sources=[],
        usages=[],
        retrieval_signals=[],
    )


def test_append_detail_answer_fires_detail_ready_event():
    cid = "detail-event-test"
    with _lock:
        _progress[cid] = {"events": []}
    try:
        with patch("app.storage.progress._publish_progress_event") as mock_publish:
            append_detail_answer(cid, "full formatted answer text", output_intent="report")
        with _lock:
            events = list(_progress[cid]["events"])
        assert len(events) == 1
        assert events[0]["event"] == "detail_ready"
        assert events[0]["data"]["content"] == "full formatted answer text"
        assert events[0]["data"]["output_intent"] == "report"
        mock_publish.assert_called_once()
    finally:
        with _lock:
            _progress.pop(cid, None)


def test_append_detail_answer_omits_output_intent_when_absent():
    cid = "detail-event-test-2"
    with _lock:
        _progress[cid] = {"events": []}
    try:
        with patch("app.storage.progress._publish_progress_event"):
            append_detail_answer(cid, "content only")
        with _lock:
            events = list(_progress[cid]["events"])
        assert "output_intent" not in events[0]["data"]
    finally:
        with _lock:
            _progress.pop(cid, None)


def test_run_integrate_fires_detail_ready_from_display_summary():
    """End-to-end through run_integrate(): when the integrator's AnswerCard
    JSON includes display_summary + output_intent, detail_ready fires with
    that exact content -- not a re-derivation, not the tldr_summary."""
    ctx = _make_ctx()
    card = {
        "mode": "FACTUAL",
        "direct_answer": "One sentence backup.",
        "sections": [],
        "output_intent": "report",
        "display_summary": "This is the full, detailed, formatted answer the user should see.",
        "tldr_summary": "Short TL;DR of the answer.",
    }

    with patch("app.stages.integrate.format_response") as mock_format, \
         patch("app.storage.progress.append_detail_answer") as mock_detail:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    mock_detail.assert_called_once()
    args, kwargs = mock_detail.call_args
    assert args[0] == "test-detail-cid"
    assert args[1] == "This is the full, detailed, formatted answer the user should see."
    assert kwargs.get("output_intent") == "report"


def test_run_integrate_skips_detail_ready_when_display_summary_absent():
    """Legacy/fallback AnswerCards without display_summary must not fire
    detail_ready with garbage -- silently skip."""
    ctx = _make_ctx()
    card = {"mode": "FACTUAL", "direct_answer": "backup", "sections": []}

    with patch("app.stages.integrate.format_response") as mock_format, \
         patch("app.storage.progress.append_detail_answer") as mock_detail:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    mock_detail.assert_not_called()


def test_run_integrate_preserves_tldr_summary_in_client_payload():
    """tldr_summary must survive the allowlist filter into the client-facing
    card, same as output_intent/display_summary already do."""
    ctx = _make_ctx()
    card = {
        "mode": "FACTUAL",
        "direct_answer": "backup",
        "sections": [],
        "output_intent": "read",
        "display_summary": "detail content",
        "tldr_summary": "the tl;dr",
    }

    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert payload.get("tldr_summary") == "the tl;dr"


# ── suggest_escalate (Chat Master spec, 2026-08-05) ─────────────────────────


def test_suggest_escalate_true_on_no_path_forward_non_agentic():
    ctx = _make_ctx()
    ctx.chat_mode = "copilot"
    ctx.react_unfinished_reason = "no_path_forward"
    card = {"mode": "FACTUAL", "direct_answer": "backup", "sections": []}

    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert payload.get("suggest_escalate") is True


def test_suggest_escalate_true_on_groundedness_failed_non_agentic():
    ctx = _make_ctx()
    ctx.chat_mode = "quick"
    ctx.react_groundedness_passed = False
    card = {"mode": "FACTUAL", "direct_answer": "backup", "sections": []}

    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert payload.get("suggest_escalate") is True


def test_suggest_escalate_absent_when_already_agentic():
    """Never suggested when already in the deepest-reasoning mode --
    nowhere further to escalate to."""
    ctx = _make_ctx()
    ctx.chat_mode = "agentic"
    ctx.react_unfinished_reason = "no_path_forward"
    ctx.react_groundedness_passed = False
    card = {"mode": "FACTUAL", "direct_answer": "backup", "sections": []}

    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert "suggest_escalate" not in payload


def test_suggest_escalate_absent_when_not_stalled():
    """Normal, successful turn -- no unfinished_reason, groundedness
    either None (critic didn't run) or True."""
    ctx = _make_ctx()
    ctx.chat_mode = "copilot"
    ctx.react_unfinished_reason = None
    ctx.react_groundedness_passed = None
    card = {"mode": "FACTUAL", "direct_answer": "backup", "sections": []}

    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert "suggest_escalate" not in payload


def test_suggest_escalate_absent_when_groundedness_none_not_false():
    """Critic never ran (None) must NOT be treated as a failure -- only
    an explicit False triggers the groundedness leg of the condition."""
    ctx = _make_ctx()
    ctx.chat_mode = "copilot"
    ctx.react_unfinished_reason = None
    ctx.react_groundedness_passed = None
    card = {"mode": "FACTUAL", "direct_answer": "backup", "sections": []}

    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert "suggest_escalate" not in payload


def test_suggest_escalate_true_with_other_non_agentic_modes():
    """task mode is also a valid non-agentic caller_mode that should
    still get the suggestion when stalled."""
    ctx = _make_ctx()
    ctx.chat_mode = "task"
    ctx.react_unfinished_reason = "no_path_forward"
    card = {"mode": "FACTUAL", "direct_answer": "backup", "sections": []}

    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert payload.get("suggest_escalate") is True
