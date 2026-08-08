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


def test_max_tokens_has_real_headroom():
    """2026-08-08: widened from 350 to 2048 after live truncation (mid-word/
    mid-number cutoffs on gemini-2.5-flash -- the installed SDK exposes no
    thinking_config to control Gemini 2.5's default thinking-token
    consumption directly, so max_tokens needs real headroom for the
    visible answer on top of whatever thinking uses)."""
    ctx = _make_ctx()
    with patch("app.pipeline.react_loop._call_llm_json") as mock_call:
        mock_call.return_value = "x"
        _fast_mode_synthesize_answer("What is X?", "raw evidence text", ctx, stage="react_1_fast_synthesis")
    assert mock_call.call_args.kwargs.get("max_tokens") == 2048


# ── Streaming (Chat Master addendum, same commit) ──────────────────────────
# "stream its output the same way the regular exit path does" -- reuses
# final.py's _emit_integrator_chunks directly (same chunking as
# format_response's own streaming) rather than re-implementing it, piping
# each chunk to append_message_chunk keyed on ctx.correlation_id.

def test_streams_answer_via_message_chunks():
    ctx = _make_ctx()
    with (
        patch("app.pipeline.react_loop._call_llm_json") as mock_call,
        patch("app.responder.final._emit_integrator_chunks") as mock_chunks,
    ):
        mock_call.return_value = "Synthesized answer text."
        result = _fast_mode_synthesize_answer("What is X?", "raw evidence text", ctx, stage="react_1_fast_synthesis")

    assert result == "Synthesized answer text."
    mock_chunks.assert_called_once()
    streamed_text = mock_chunks.call_args.args[0]
    assert streamed_text == "Synthesized answer text."


def test_streaming_callback_forwards_to_append_message_chunk():
    ctx = _make_ctx()
    with (
        patch("app.pipeline.react_loop._call_llm_json") as mock_call,
        patch("app.storage.progress.append_message_chunk") as mock_append,
    ):
        mock_call.return_value = "A longer synthesized answer for chunking."
        _fast_mode_synthesize_answer("What is X?", "raw evidence text", ctx, stage="react_1_fast_synthesis")

    assert mock_append.called
    # every call keyed on this turn's correlation_id
    for call in mock_append.call_args_list:
        assert call.args[0] == ctx.correlation_id
    # chunks concatenate back to the full answer
    rebuilt = "".join(call.args[1] for call in mock_append.call_args_list)
    assert rebuilt == "A longer synthesized answer for chunking."


def test_streaming_failure_does_not_break_answer_return():
    """Streaming is cosmetic -- if append_message_chunk/_emit_integrator_chunks
    itself raises, the synthesized answer must still come back."""
    ctx = _make_ctx()
    with (
        patch("app.pipeline.react_loop._call_llm_json") as mock_call,
        patch("app.responder.final._emit_integrator_chunks", side_effect=RuntimeError("boom")),
    ):
        mock_call.return_value = "Synthesized answer."
        result = _fast_mode_synthesize_answer("What is X?", "raw evidence text", ctx, stage="react_1_fast_synthesis")
    assert result == "Synthesized answer."


def test_no_streaming_when_answer_empty():
    ctx = _make_ctx()
    with (
        patch("app.pipeline.react_loop._call_llm_json") as mock_call,
        patch("app.responder.final._emit_integrator_chunks") as mock_chunks,
    ):
        mock_call.return_value = ""
        result = _fast_mode_synthesize_answer("What is X?", "raw evidence text", ctx, stage="react_1_fast_synthesis")
    assert result is None
    mock_chunks.assert_not_called()
