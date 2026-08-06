"""EvidenceLedger phase 1 (Task #48, Chat Architecture spec, 2026-08-06).

Code-computed, no LLM inference: gap_status is a mechanical function of
ctx._rag_call_history (dispatch_path/chosen_slot/status), rendered
unconditionally in build_reasoning_context's [Evidence Ledger] block.
Subsumes Task #50 -- gap_status=="stagnant" IS the reframe signal,
replacing the old `if not success:` gate that never fired when rag
returned real-but-wrong chunks (confirmed live: Amerigroup case, 3 rounds
of identical dispatch_path/chosen_slot/status, success=True throughout
since chunks were non-empty).
"""

from __future__ import annotations

from app.pipeline.context import PipelineContext
from app.pipeline.react.prompts import build_reasoning_context
from app.pipeline.react_loop import _compute_gap_status


def _call(dispatch_path="bayesian", chosen_slot="direct_answer", status="partial", **overrides):
    d = {"dispatch_path": dispatch_path, "chosen_slot": chosen_slot, "status": status}
    d.update(overrides)
    return d


class TestComputeGapStatus:
    def test_empty_history_is_fresh(self):
        assert _compute_gap_status([]) == "fresh"

    def test_single_call_is_progressing(self):
        assert _compute_gap_status([_call()]) == "progressing"

    def test_two_identical_calls_are_stagnant(self):
        """The Amerigroup case: same dispatch_path/chosen_slot/status twice
        in a row, real (non-empty) status -- this is the exact live
        scenario that motivated the fix."""
        history = [_call(), _call()]
        assert _compute_gap_status(history) == "stagnant"

    def test_three_identical_calls_still_stagnant(self):
        history = [_call(), _call(), _call()]
        assert _compute_gap_status(history) == "stagnant"

    def test_different_dispatch_path_is_progressing(self):
        history = [_call(dispatch_path="optimizer"), _call(dispatch_path="bayesian")]
        assert _compute_gap_status(history) == "progressing"

    def test_different_chosen_slot_is_progressing(self):
        history = [_call(chosen_slot="direct_answer"), _call(chosen_slot="supplement")]
        assert _compute_gap_status(history) == "progressing"

    def test_status_none_does_not_count_as_stagnant(self):
        """A genuinely empty/no-op result shouldn't double-fire the
        stagnant signal -- that's the relax-then-reframe protocol's
        territory (rule 1b), not this function's."""
        history = [_call(status=None), _call(status=None)]
        assert _compute_gap_status(history) == "progressing"

    def test_status_empty_does_not_count_as_stagnant(self):
        history = [_call(status="empty"), _call(status="empty")]
        assert _compute_gap_status(history) == "progressing"

    def test_only_last_two_calls_matter(self):
        """A stagnant pair followed by a genuinely different third call
        should read as progressing -- gap_status looks at the trend, not
        the whole history."""
        history = [_call(), _call(), _call(dispatch_path="optimizer")]
        assert _compute_gap_status(history) == "progressing"


def _make_ctx() -> PipelineContext:
    ctx = PipelineContext(correlation_id="ledger-test", thread_id=None, message="does it matter")
    ctx.effective_message = ctx.message
    ctx.merged_state = {}
    ctx.last_turns = []
    ctx.chat_mode = "agentic"
    ctx.thinking_chunks = []
    return ctx


class TestEvidenceLedgerRendering:
    def test_ledger_block_appears_with_history(self):
        history = [_call(), _call()]
        out = build_reasoning_context(
            _make_ctx(), [], 3, max_iterations=10,
            gap_status="stagnant", rag_call_history=history,
        )
        assert "[Evidence Ledger]" in out
        assert 'gap_status: "stagnant"' in out
        assert "round: 3" in out

    def test_ledger_block_absent_without_history(self):
        out = build_reasoning_context(
            _make_ctx(), [], 1, max_iterations=10,
            gap_status="fresh", rag_call_history=[],
        )
        assert "[Evidence Ledger]" not in out

    def test_strategy_tried_reflects_citable_required(self):
        history = [_call(citable_required=True), _call(citable_required=False)]
        out = build_reasoning_context(
            _make_ctx(), [], 2, max_iterations=10,
            gap_status="progressing", rag_call_history=history,
        )
        assert "citable retrieval" in out
        assert "relaxed retrieval" in out

    def test_falls_back_to_ctx_when_history_not_passed(self):
        """Legacy callers (existing tests, no gap_status/rag_call_history
        kwargs) must keep working -- falls back to ctx._rag_call_history,
        empty when unset, no [Evidence Ledger] block rendered."""
        ctx = _make_ctx()
        out = build_reasoning_context(ctx, [], 1)
        assert "[Evidence Ledger]" not in out

    def test_stagnant_instruction_mentions_lookup_authoritative_sources(self):
        history = [_call(), _call()]
        out = build_reasoning_context(
            _make_ctx(), [], 3, max_iterations=10,
            gap_status="stagnant", rag_call_history=history,
        )
        assert "lookup_authoritative_sources" in out
