"""A round must not be authorised when the work it will do cannot fit.

Measured 2026-09-14: agentic promises 95s; two live runs took 147s and 233s.
The governor was correct about every number it held -- it checked time ALREADY
SPENT and never whether the next rag call (25-94s, rag runs min=max=1) would
fit in what remained. A round starting at t=80s against a 95s target has
already missed.

v1 must stay bit-identical for the A/B, so the gate is the DATA: v1 never
populates expected_tool_cost_s and therefore cannot reach the new branch.
"""
import pytest

from app.pipeline.react.governor import RoundState, _evaluate, default_contract_for_mode


def _contract():
    return default_contract_for_mode("agentic")


def _state(**kw):
    base = dict(
        proposes_complete=False, self_reported_confidence=None, critic_verdict=None,
        groundedness_passed=None, elapsed_s=10.0,
        base_rounds_remaining=5, extension_rounds_available=1,
    )
    base.update(kw)
    return RoundState(**base)


def test_absent_estimate_behaves_exactly_as_before():
    """v1 passes nothing. Its directive must not move."""
    c = _contract()
    s = _state(elapsed_s=c.soft_target_s - 30.0)
    assert s.expected_tool_cost_s is None
    assert _evaluate(c, s)[0] == "search"


def test_a_call_that_does_not_fit_consolidates_instead():
    c = _contract()
    # 30s left, next call historically costs 60s -> it cannot fit.
    s = _state(elapsed_s=c.soft_target_s - 30.0, expected_tool_cost_s=60.0)
    directive, reason = _evaluate(c, s)
    assert directive == "consolidate"
    assert "would not fit" in reason


def test_a_call_that_fits_is_still_authorised():
    """The guard must not become a blanket stop -- that would trade the
    promise for the answer, which is the opposite trade."""
    c = _contract()
    s = _state(elapsed_s=c.soft_target_s - 60.0, expected_tool_cost_s=10.0)
    assert _evaluate(c, s)[0] == "search"


def test_guard_cannot_authorise_work_or_override_an_earlier_branch():
    """It sits immediately before `search`, so it can only ever turn a
    would-be search into a consolidate. A cheap next call must not rescue a
    turn whose time or rounds are already exhausted."""
    c = _contract()
    spent = _state(elapsed_s=c.hard_ceiling_s + 100.0, expected_tool_cost_s=0.1)
    assert _evaluate(c, spent)[0] == "finalize"
    no_rounds = _state(base_rounds_remaining=0, extension_rounds_available=0,
                       expected_tool_cost_s=0.1)
    assert _evaluate(c, no_rounds)[0] == "finalize"


def test_estimator_is_v2_only_and_ignores_failed_calls():
    """A call that could not run reports the CEILING, not the cost of working
    retrieval. Including it would predict every future call at the timeout and
    stop the loop permanently -- a self-inflicted absorbing state."""
    from app.pipeline import react_loop

    class Ctx:
        orchestrator_version = "v2"
        _rag_call_rounds = [
            {"latency_ms": 30000, "status": "ok"},
            {"latency_ms": 120000, "status": "timeout"},   # must be ignored
        ]
    assert react_loop._v2_expected_tool_cost_s(Ctx()) == pytest.approx(30.0)

    class V1(Ctx):
        orchestrator_version = "v1"
    assert react_loop._v2_expected_tool_cost_s(V1()) is None

    class Fresh:
        orchestrator_version = "v2"
        _rag_call_rounds = []
    assert react_loop._v2_expected_tool_cost_s(Fresh()) is None


def test_estimator_takes_the_max_not_the_mean():
    """Asymmetric cost: under-predicting misses the promise, over-predicting
    consolidates one round early with evidence already in hand."""
    from app.pipeline import react_loop

    class Ctx:
        orchestrator_version = "v2"
        _rag_call_rounds = [{"latency_ms": 10000, "status": "ok"},
                            {"latency_ms": 90000, "status": "ok"}]
    assert react_loop._v2_expected_tool_cost_s(Ctx()) == pytest.approx(90.0)
