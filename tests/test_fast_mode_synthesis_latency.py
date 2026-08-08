"""Tests for _fast_mode_synthesize_answer's fast-model routing signals
(2026-08-08, Chat Master: "same pattern you used for the parallel
integrator... there's no reason to run it on Pro"). Confirmed live this
session: a real turn's trace showed this exact call landing on Gemini Pro
at 12.61s for a single lightweight max_tokens=350 synthesis pass -- no
reasoning_depth/latency_budget_ms had ever been threaded through to the
bandit for this stage."""
from __future__ import annotations

from unittest.mock import patch

from app.pipeline.context import PipelineContext  # noqa: F401 -- import order avoids a circular import
from app.pipeline.react_loop import _fast_mode_synthesize_answer


def _make_ctx():
    return PipelineContext(
        correlation_id="test-cid", thread_id="t1", message="q",
        plan=None, answers=[], sources=[], usages=[], retrieval_signals=[],
    )


def test_passes_fast_reasoning_depth_and_latency_budget():
    ctx = _make_ctx()
    with patch("app.pipeline.react_loop._call_llm_json") as mock_call:
        mock_call.return_value = "Synthesized answer."
        _fast_mode_synthesize_answer("What is X?", "raw evidence text", ctx, stage="react_1_fast_synthesis")

    kwargs = mock_call.call_args.kwargs
    assert kwargs.get("reasoning_depth") == "fast"
    assert kwargs.get("latency_budget_ms") == 2000


def test_still_returns_synthesized_answer():
    ctx = _make_ctx()
    with patch("app.pipeline.react_loop._call_llm_json") as mock_call:
        mock_call.return_value = "  Synthesized answer.  "
        result = _fast_mode_synthesize_answer("What is X?", "raw evidence text", ctx, stage="react_1_fast_synthesis")
    assert result == "Synthesized answer."


def test_returns_none_on_empty_response():
    ctx = _make_ctx()
    with patch("app.pipeline.react_loop._call_llm_json") as mock_call:
        mock_call.return_value = ""
        result = _fast_mode_synthesize_answer("What is X?", "raw evidence text", ctx, stage="react_1_fast_synthesis")
    assert result is None


def test_returns_none_on_exception_not_raises():
    ctx = _make_ctx()
    with patch("app.pipeline.react_loop._call_llm_json", side_effect=RuntimeError("boom")):
        result = _fast_mode_synthesize_answer("What is X?", "raw evidence text", ctx, stage="react_1_fast_synthesis")
    assert result is None


def test_max_tokens_stays_350():
    """Regression guard: the fast-model signals are additive -- must not
    accidentally change the existing max_tokens budget."""
    ctx = _make_ctx()
    with patch("app.pipeline.react_loop._call_llm_json") as mock_call:
        mock_call.return_value = "x"
        _fast_mode_synthesize_answer("What is X?", "raw evidence text", ctx, stage="react_1_fast_synthesis")
    assert mock_call.call_args.kwargs.get("max_tokens") == 350
