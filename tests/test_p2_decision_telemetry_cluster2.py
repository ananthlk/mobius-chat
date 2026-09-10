"""P2 telemetry gap — cluster 2: the four small silent nodes.

The rule for this pass is "instrument the DECISION, not the functions". These
four were chosen because a decision was hiding in each, and in two cases the
decision was a NEGATIVE one that nothing recorded — which is how
make_tool_failed made tool_failed structurally impossible while health stayed
green.
"""
from __future__ import annotations

import io
import logging

import pytest


def _cap(name):
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    lg = logging.getLogger(name)
    lg.addHandler(h); lg.setLevel(logging.INFO)
    return buf, lg, h


# ── retrieval_budget: the decision is whether the FLOOR BOUND ───────
def test_floor_binding_is_distinguishable_from_a_computed_budget():
    """Both paths return an int and the caller cannot tell which. When the floor
    binds, the computed budget was REJECTED — history ate the window and RAG
    gets a minimum payload rather than a sized one. Materially different turn."""
    from app.services import retrieval_budget as rb
    buf, lg, h = _cap("app.services.retrieval_budget")
    try:
        class _Ctx: pass
        rb.compute_token_budget_for_retrieval(_Ctx())
    finally:
        lg.removeHandler(h)
    out = buf.getvalue()
    assert "[retrieval_budget]" in out
    assert ("floor_bound" in out) or ("computed" in out)
    for field in ("computed=", "floor=", "context_window=", "history_tokens="):
        assert field in out, f"{field} missing — cannot assert why the floor bound"


# ── personalization: four causes collapsed into one return ──────────
@pytest.mark.parametrize("profile,sensitive,expected_mode,expected_reason", [
    (None, False, "confirm_first", "no_profile"),
    ({"autonomy": "not-a-dict"}, False, "confirm_first", "no_autonomy_block"),
    ({"autonomy": {"routine_tasks": "automatic"}}, False, "automatic", "declared"),
    ({"autonomy": {"routine_tasks": "zzz"}}, False, "confirm_first", "unrecognised_value"),
    ({"autonomy": {"routine_tasks": ""}}, False, "confirm_first", "unset"),
])
def test_autonomy_reason_is_recorded_not_just_the_mode(profile, sensitive,
                                                       expected_mode, expected_reason):
    """autonomy_for gates whether a tool AUTO-EXECUTES or asks first. Three of
    the four confirm_first causes are upstream DEFECTS and one is a legitimate
    default — collapsed, a broken profile looked exactly like a cautious user."""
    from app.pipeline import personalization as pz
    buf, lg, h = _cap("app.pipeline.personalization")
    try:
        got = pz.autonomy_for(profile, sensitive=sensitive)
    finally:
        lg.removeHandler(h)
    assert got == expected_mode, "behaviour changed — this pass is telemetry only"
    assert f"reason={expected_reason!r}" in buf.getvalue()


# ── active_context: a TTL that is decremented and never enforced ────
def test_expired_context_is_still_served_and_now_says_so():
    """expires_after_turns is read and decremented in this module and compared
    NOWHERE in app/. At ttl == 0 the guard stops counting and the context is
    returned anyway, so it never expires.

    Deliberately asserts the CURRENT behaviour: fixing it changes what the model
    sees on a later turn, which needs a ruling, not a telemetry pass. The test
    exists so the day someone fixes it, this fails and they find the note."""
    from app.pipeline.active_context import load_active_context
    buf, lg, h = _cap("app.pipeline.active_context")
    try:
        out = load_active_context({"active_context": {"k": 1, "expires_after_turns": 0}})
    finally:
        lg.removeHandler(h)
    assert out == {"k": 1, "expires_after_turns": 0}, "TTL now expires — see the note in this test"
    assert "expired_but_served=True" in buf.getvalue()


def test_the_absence_branch_is_recorded_too():
    """A node that only emits when it finds something is indistinguishable from
    one that is never called."""
    from app.pipeline.active_context import load_active_context
    buf, lg, h = _cap("app.pipeline.active_context")
    try:
        assert load_active_context({}, []) is None
    finally:
        lg.removeHandler(h)
    assert "none_available" in buf.getvalue()


# ── the shared shape ────────────────────────────────────────────────
def test_record_decision_never_raises():
    """Telemetry that can break a turn is worse than none."""
    from app.telemetry.spans import record_decision
    class _Boom:
        def info(self, *a, **k): raise RuntimeError("logger exploded")
    record_decision("n", "d", logger_=_Boom(), x=1)          # must not raise
    record_decision("n", "d", logger_=None)
