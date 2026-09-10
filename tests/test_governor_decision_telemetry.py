"""The governor decides whether a turn gets another round, and said nothing.

It had zero logger calls and zero structured signal — and the reason it read as
"zero logger calls" is that the module had NO logger object at all. The absence
was not a style choice; there was nothing to call.

Its span already existed and measures 0ms, which is exactly the trap this phase
is about: a node can be instantaneous and still be choosing wrongly, so timing
told us nothing. What was missing is the DECISION.
"""
from __future__ import annotations

import io
import logging

import pytest

from app.pipeline.react.governor import (
    RoundState, default_contract_for_mode, evaluate, _evaluate,
)


def _state(**kw):
    base = dict(proposes_complete=False, self_reported_confidence="low",
                critic_verdict=None, groundedness_passed=None, elapsed_s=1.0,
                base_rounds_remaining=3, extension_rounds_available=1)
    base.update(kw)
    return RoundState(**base)


def _capture(contract, state):
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    lg = logging.getLogger("app.pipeline.react.governor")
    lg.addHandler(h); lg.setLevel(logging.INFO)
    try:
        out = evaluate(contract, state)
    finally:
        lg.removeHandler(h)
    return out, buf.getvalue()


def test_the_module_has_a_logger_at_all():
    """The log line lives inside `except Exception: pass`. Without a logger
    object it would raise NameError into that handler and log nothing, forever,
    while the node counted as instrumented — a swallowed failure added by the
    very change meant to remove swallowed failures."""
    from app.pipeline.react import governor
    assert isinstance(getattr(governor, "logger", None), logging.Logger)


def test_decision_is_recorded_with_every_input_that_could_have_changed_it():
    """Assertability: "the governor granted an extension" cannot be checked
    against anything. The branch plus its inputs can."""
    c = default_contract_for_mode("agentic")
    (directive, _reason), logged = _capture(
        c, _state(base_rounds_remaining=0, extension_rounds_available=1, elapsed_s=8.2))
    assert directive == "extend"
    for field in ("proposes_complete=", "confidence=", "critic=", "grounded=",
                  "elapsed=", "soft_target=", "hard_ceiling=", "rounds_left=",
                  "ext_avail=", "bar="):
        assert field in logged, f"{field} missing — the decision is not assertable without it"


def test_the_pure_function_stayed_pure():
    """`_evaluate` is the decision; `evaluate` is the decision plus recording.
    Keeping them separate is what lets the logic be tested without capturing
    logs, and stops the recording from being able to change the outcome."""
    c = default_contract_for_mode("agentic")
    s = _state(elapsed_s=8.2, base_rounds_remaining=0, extension_rounds_available=1)
    assert _evaluate(c, s) == evaluate(c, s)


@pytest.mark.parametrize("kw,expected", [
    (dict(elapsed_s=10_000.0), "finalize"),                       # hard stop by time
    (dict(base_rounds_remaining=0, extension_rounds_available=0), "finalize"),
    # agentic's bar is "high", so `complete` additionally requires the
    # groundedness floor to have PASSED — proposing completion is not enough.
    # My first version of this case omitted that and expected "complete"; the
    # governor correctly returned "search". The test was wrong, not the code.
    (dict(proposes_complete=True, self_reported_confidence="high",
          groundedness_passed=True), "complete"),
    (dict(base_rounds_remaining=0, extension_rounds_available=1, elapsed_s=8.2), "extend"),
])
def test_each_branch_records_its_own_directive(kw, expected):
    """Counts are grouped by directive, so a shift in the decision mix is
    visible without parsing anything. If two branches shared a target that
    would be invisible."""
    c = default_contract_for_mode("agentic")
    (directive, _), logged = _capture(c, _state(**kw))
    assert directive == expected
    assert f"[governor] {expected} " in logged


def test_decision_kind_is_registered():
    from app.telemetry.spans import KIND_DECISION, _KINDS
    assert KIND_DECISION == "decision"
    assert KIND_DECISION in _KINDS
