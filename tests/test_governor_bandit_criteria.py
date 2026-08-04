"""Model-bandit selection criteria derivations (governor.py, 2026-08-04).

Ananth's react-prep ask: the model bandit should be able to select on
latency/reasoning-depth per call, not just session-level chat_mode. Scoped
with Chat Architecture, LLM Agent, and Eval (calibration risk) before
building — LLM Agent owns the actual `router.select()` mechanics
(model_registry.py/bandit_weights.py); these two functions are ReAct's
side: pure derivations from state react already has, producing the two
kwargs LLM Agent's design defines (`reasoning_depth`, `latency_budget_ms`).

Unit-tested here in isolation (no LLM/DB calls needed — these are pure
functions of already-known values) rather than only indirectly through a
full run_react() scripted turn.
"""

from __future__ import annotations

from unittest.mock import patch

from app.pipeline.context import PipelineContext
from app.pipeline.react.governor import (
    FINALIZE_MARGIN_S,
    ProductPromiseContract,
    agent_role_to_reasoning_depth,
    latency_budget_ms,
)
from app.pipeline.react_loop import run_react


def _contract(hard_ceiling_s: float = 300.0) -> ProductPromiseContract:
    return ProductPromiseContract(
        max_rounds=3, max_extension_rounds=1, confidence_bar="medium",
        soft_target_s=12.0, hard_ceiling_s=hard_ceiling_s,
    )


# ── agent_role_to_reasoning_depth ────────────────────────────────────────


def test_explore_maps_to_fast():
    assert agent_role_to_reasoning_depth("explore") == "fast"


def test_synthesize_maps_to_thinking():
    assert agent_role_to_reasoning_depth("synthesize") == "thinking"


def test_draft_maps_to_thinking():
    """draft is the completing round, not a cheap lookup -- must NOT be
    lumped with explore's "fast" just because it's structurally similar
    to a "final" round; it's real synthesis work."""
    assert agent_role_to_reasoning_depth("draft") == "thinking"


def test_none_agent_role_maps_to_none():
    """Governor off (or role not yet resolved this round) -> no override,
    the bandit's own mode-derived default applies unchanged."""
    assert agent_role_to_reasoning_depth(None) is None


def test_unrecognized_agent_role_maps_to_none():
    assert agent_role_to_reasoning_depth("some_future_role") is None


# ── latency_budget_ms ─────────────────────────────────────────────────


def test_none_when_directive_is_search():
    """Most rounds are unconstrained -- only consolidate/finalize set a
    latency ceiling, per LLM Agent's "most rounds unconstrained" design."""
    assert latency_budget_ms(_contract(), elapsed_s=5.0, directive="search") is None


def test_none_when_directive_is_extend_or_complete():
    assert latency_budget_ms(_contract(), elapsed_s=5.0, directive="extend") is None
    assert latency_budget_ms(_contract(), elapsed_s=5.0, directive="complete") is None


def test_none_when_directive_is_none():
    assert latency_budget_ms(_contract(), elapsed_s=5.0, directive=None) is None


def test_computes_remaining_budget_on_consolidate():
    contract = _contract(hard_ceiling_s=300.0)
    result = latency_budget_ms(contract, elapsed_s=100.0, directive="consolidate")
    expected_s = 300.0 - 100.0 - FINALIZE_MARGIN_S
    assert result == int(expected_s * 1000)
    assert result == 195000


def test_computes_remaining_budget_on_finalize():
    contract = _contract(hard_ceiling_s=300.0)
    result = latency_budget_ms(contract, elapsed_s=280.0, directive="finalize")
    expected_s = 300.0 - 280.0 - FINALIZE_MARGIN_S
    assert result == int(expected_s * 1000)
    assert result == 15000


def test_none_when_already_past_the_margin():
    """If remaining time is already at/past FINALIZE_MARGIN_S, the
    governor's own hard-stop path handles it -- a latency filter on the
    NEXT model pick isn't the right mechanism at that point."""
    contract = _contract(hard_ceiling_s=300.0)
    assert latency_budget_ms(contract, elapsed_s=296.0, directive="consolidate") is None
    assert latency_budget_ms(contract, elapsed_s=300.0, directive="finalize") is None


def test_reuses_the_same_margin_constant_the_governor_hard_stop_uses():
    """Not an independently-invented number -- must move in lockstep with
    FINALIZE_MARGIN_S if that constant ever changes, so the model-
    selection deadline and the governor's own emit-now deadline can't
    drift apart as two different formulas."""
    contract = _contract(hard_ceiling_s=100.0)
    result = latency_budget_ms(contract, elapsed_s=50.0, directive="consolidate")
    assert result == int((100.0 - 50.0 - FINALIZE_MARGIN_S) * 1000)


# ── End-to-end wiring through react_loop.py ──────────────────────────
#
# The pure-function tests above proved the math is right in isolation.
# These lock in the ACTUAL call-site wiring — this is exactly where a
# real bug was caught during implementation: the first version derived
# reasoning_depth from the composition-selection block's `_agent_role`
# var, which only populates when MOBIUS_PROMPT_SOURCE=composition is
# ALSO set (an unrelated flag), silently leaving reasoning_depth None
# whenever that flag was off even with the governor on. Caught by
# actually inspecting the kwargs _call_llm_json received, not by
# re-reading the code and assuming it was right.

_SEARCH_RESULT = {
    "tool": "search_corpus", "success": True, "result": "some text",
    "signal": "corpus_only", "sources": [], "usage": None,
}


def _make_ctx(chat_mode: str = "copilot"):
    ctx = PipelineContext(correlation_id="bandit-wiring-test", thread_id=None, message="q")
    ctx.effective_message = ctx.message
    ctx.merged_state = {}
    ctx.last_turns = []
    ctx.chat_mode = chat_mode
    ctx.thinking_chunks = []
    return ctx


def test_reasoning_depth_reaches_call_llm_json_without_composition_flag():
    """The exact regression: governor on, MOBIUS_PROMPT_SOURCE NOT set to
    composition -- reasoning_depth must still populate from the
    directive, not silently stay None."""
    calls = []

    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        calls.append((stage, kwargs.get("reasoning_depth")))
        if stage == "react_1":
            return '{"thought": "search", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'
        return '{"thought": "done", "tool": null, "inputs": {}, "is_complete": true, "answer": "a real long enough answer here", "confidence": "high"}'

    ctx = _make_ctx()
    with patch.dict("os.environ", {"MOBIUS_PRODUCT_PROMISE_ENABLED": "1", "MOBIUS_PROMPT_SOURCE": ""}), \
         patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
        run_react(ctx, emitter=None)

    react_calls = [c for c in calls if c[0].startswith("react_")]
    assert react_calls, "expected at least one react_N call"
    assert all(depth == "fast" for _, depth in react_calls), (
        f"expected reasoning_depth='fast' (directive=search->explore) on every react_N call, got {react_calls}"
    )


def test_reasoning_depth_and_latency_budget_none_when_governor_off():
    """Fail-soft floor: flag off -> both kwargs stay None, exactly today's
    pre-existing call shape (no behavior change for the bandit)."""
    calls = []

    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        calls.append((stage, kwargs.get("reasoning_depth"), kwargs.get("latency_budget_ms")))
        return '{"thought": "done", "tool": null, "inputs": {}, "is_complete": true, "answer": "a real long enough answer here", "confidence": "high"}'

    ctx = _make_ctx()
    with patch.dict("os.environ", {"MOBIUS_PRODUCT_PROMISE_ENABLED": ""}), \
         patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
        run_react(ctx, emitter=None)

    react_calls = [c for c in calls if c[0].startswith("react_")]
    assert react_calls
    assert all(depth is None and budget is None for _, depth, budget in react_calls)


def test_latency_budget_populates_on_consolidate_directive():
    """Real time-pressure scenario: soft_target_s forced tiny so
    consolidate fires on round 2, confirms latency_budget_ms actually
    reaches the call (not just reasoning_depth)."""
    calls = []

    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        calls.append((stage, kwargs.get("reasoning_depth"), kwargs.get("latency_budget_ms")))
        n = int(stage.split("_")[1]) if stage.startswith("react_") else None
        if n is not None and n < 3:
            return '{"thought": "search", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'
        return '{"thought": "done", "tool": null, "inputs": {}, "is_complete": true, "answer": "a real long enough answer here", "confidence": "high"}'

    ctx = _make_ctx()
    tiny_contract = ProductPromiseContract(
        max_rounds=3, max_extension_rounds=1, confidence_bar="medium",
        soft_target_s=0.0, hard_ceiling_s=300.0,
    )
    with patch.dict("os.environ", {"MOBIUS_PRODUCT_PROMISE_ENABLED": "1"}), \
         patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react.governor.default_contract_for_mode", return_value=tiny_contract), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
        run_react(ctx, emitter=None)

    react_calls = [c for c in calls if c[0].startswith("react_")]
    # round 1 is always unconstrained (elapsed_s ~0 < soft_target_s check
    # happens before any time has passed); round 2+ should be consolidating
    # given soft_target_s=0.0.
    later_rounds = [c for c in react_calls if int(c[0].split("_")[1]) >= 2]
    assert later_rounds, "expected at least a round 2"
    assert all(budget is not None and budget > 0 for _, _depth, budget in later_rounds), (
        f"expected a positive latency_budget_ms once consolidate fires, got {later_rounds}"
    )
