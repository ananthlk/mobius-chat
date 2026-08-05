"""Mid-loop truncation recovery checkpoint (Task #29).

docs/MIDTURN_TRUNCATION_RECOVERY_SPEC.md §1: if a timeout kills a turn
before react_loop.py returns, orchestrator.py's append_draft_answer() call
(which runs AFTER run_react() returns) never fires — a truncated turn has
nothing to hand off for a "Continue" retry. _checkpoint_best_evidence()
closes that gap by writing the best available tool result to the SAME
draft_ready channel after every round, so worker/run.py's checkpoint-read
(LLM Agent's side of this task) finds something regardless of whether the
turn finished react_loop or was killed mid-round.

These tests lock in: the pure selection logic (best successful result,
short-circuits below a length floor), and that it's actually called once
per round in a real (scripted) run_react() turn — not just that the
function exists.
"""

from __future__ import annotations

from unittest.mock import patch

from app.pipeline.context import PipelineContext
from app.pipeline.react_loop import _checkpoint_best_evidence, run_react

_SEARCH_SUCCESS = {
    "tool": "search_corpus", "success": True,
    "result": "a real, sufficiently long, useful snippet about the policy in question",
    "signal": "corpus_only", "sources": [], "usage": None,
}


def _make_ctx(chat_mode: str = "agentic") -> PipelineContext:
    ctx = PipelineContext(correlation_id="checkpoint-test", thread_id=None, message="q")
    ctx.effective_message = ctx.message
    ctx.merged_state = {}
    ctx.last_turns = []
    ctx.chat_mode = chat_mode
    ctx.thinking_chunks = []
    return ctx


class TestCheckpointBestEvidenceSelection:
    def test_no_successful_results_is_a_noop(self):
        ctx = _make_ctx()
        with patch("app.storage.progress.append_draft_answer") as mock_append:
            _checkpoint_best_evidence(ctx, [{"tool": "search_corpus", "success": False, "result": "nothing found"}])
        mock_append.assert_not_called()

    def test_short_result_below_length_floor_is_a_noop(self):
        ctx = _make_ctx()
        with patch("app.storage.progress.append_draft_answer") as mock_append:
            _checkpoint_best_evidence(ctx, [{"tool": "search_corpus", "success": True, "result": "too short"}])
        mock_append.assert_not_called()

    def test_uses_most_recent_successful_result(self):
        ctx = _make_ctx()
        tool_results = [
            {"tool": "search_corpus", "success": True, "result": "the OLDER, first successful result found here"},
            {"tool": "web_scrape", "success": False, "result": "HTTP 403 — this one failed"},
            {"tool": "search_corpus", "success": True, "result": "the NEWER, most recent successful result found here"},
        ]
        with patch("app.storage.progress.append_draft_answer") as mock_append:
            _checkpoint_best_evidence(ctx, tool_results)
        mock_append.assert_called_once()
        args, _ = mock_append.call_args
        assert args[0] == "checkpoint-test"
        assert "NEWER" in args[1]
        assert "OLDER" not in args[1]

    def test_falls_back_to_result_summary_when_result_too_short(self):
        ctx = _make_ctx()
        tool_results = [{
            "tool": "search_corpus", "success": True, "result": "short",
            "result_summary": "a sufficiently long fallback summary of what was actually found in this round",
        }]
        with patch("app.storage.progress.append_draft_answer") as mock_append:
            _checkpoint_best_evidence(ctx, tool_results)
        mock_append.assert_called_once()
        args, _ = mock_append.call_args
        assert "fallback summary" in args[1]

    def test_a_broken_progress_write_never_raises(self):
        """Checkpointing must never break the turn it's protecting."""
        ctx = _make_ctx()
        tool_results = [{"tool": "search_corpus", "success": True, "result": "a real, sufficiently long useful snippet"}]
        with patch("app.storage.progress.append_draft_answer", side_effect=RuntimeError("boom")):
            _checkpoint_best_evidence(ctx, tool_results)  # must not raise


def test_checkpoint_fires_once_per_round_in_a_real_turn():
    """End-to-end through run_react(): a multi-round turn checkpoints
    after each successful round, and the checkpoint content it writes
    matches the round's actual tool result -- not just that the call
    happens, but that it carries real, current evidence each time."""
    checkpoint_calls = []

    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        if stage in ("react_1", "react_2"):
            return '{"thought": "search", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'
        return '{"thought": "done", "tool": null, "inputs": {}, "is_complete": true, "answer": "a real, sufficiently long final answer here", "confidence": "high"}'

    round_n = {"n": 0}

    def fake_exec_retry(tool, inputs, ctx, rn, emit, tool_emitter, skip_retry=False):
        round_n["n"] += 1
        return {
            "tool": "search_corpus", "success": True,
            "result": f"round {round_n['n']} evidence: a real, sufficiently long snippet found this round",
            "signal": "corpus_only", "sources": [], "usage": None,
        }

    def fake_append_draft_answer(correlation_id, text, mode_hint=None):
        checkpoint_calls.append((correlation_id, text))

    ctx = _make_ctx("agentic")
    with patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", side_effect=fake_exec_retry), \
         patch("app.storage.progress.append_draft_answer", side_effect=fake_append_draft_answer):
        run_react(ctx, emitter=None)

    assert len(checkpoint_calls) == 2, f"expected one checkpoint per successful round, got {checkpoint_calls}"
    assert checkpoint_calls[0][0] == "checkpoint-test"
    assert "round 1 evidence" in checkpoint_calls[0][1]
    assert "round 2 evidence" in checkpoint_calls[1][1]
