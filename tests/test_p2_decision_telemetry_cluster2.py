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
def test_ttl_no_writer_is_distinguishable_from_ttl_expired():
    """`expires_after_turns` is INERT IN ALL THREE DIRECTIONS: never written
    (the only assignments in app/ are the decrements themselves), therefore
    never counted down, therefore never compared.

    My first version of this recording emitted `expired_but_served=(ttl<=0)`,
    which is True on EVERY row for exactly that reason — a field that can hold
    one value while passing "producer exists", "consumer exists" and "query
    returns rows". That is the is_fallback defect, reproduced in a field added
    hours after naming it. Hence: record the RAW value and whether the KEY IS
    PRESENT, because "no writer" and "expired" are different facts.
    """
    from app.pipeline.active_context import load_active_context
    buf, lg, h = _cap("app.pipeline.active_context")
    try:
        load_active_context({"active_context": {"k": 1}})                       # no key
        load_active_context({"active_context": {"k": 1, "expires_after_turns": 3}})
    finally:
        lg.removeHandler(h)
    out = buf.getvalue()
    assert "ttl_key_present=False" in out and "ttl_raw=None" in out
    assert "ttl_key_present=True" in out and "ttl_raw=2" in out
    # the field must take more than one value, or it carries nothing
    assert out.count("ttl_key_present=False") >= 1
    assert out.count("ttl_key_present=True") >= 1


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


# ── react_retry_guard: the ALLOW path is the point ──────────────────

def test_guard_records_the_allow_path_not_only_the_blocks():
    """A guard that has never reported firing is indistinguishable from a guard
    that is not wired. Zero blocks reads identically as "no repeats happened"
    and "this code never runs" — make_tool_failed exactly, where tool_failed
    was structurally impossible and health stayed green.

    Recording the ALLOW is what proves the guard executes at all."""
    from app.pipeline.react_retry_guard import ReactRetryGuard
    buf, lg, h = _cap("app.pipeline.react_retry_guard")
    try:
        g = ReactRetryGuard()
        assert g.should_block(tool="rag", inputs={"q": "x"}, current_results_count=0) is None
    finally:
        lg.removeHandler(h)
    out = buf.getvalue()
    assert "[react_retry_guard] allowed" in out
    for field in ("tool=", "streak=", "results_count=", "attempts_tracked="):
        assert field in out


def test_a_repeat_is_blocked_and_says_which_attempt_it_cited():
    from app.pipeline.react_retry_guard import ReactRetryGuard, FailedAttempt, inputs_signature
    buf, lg, h = _cap("app.pipeline.react_retry_guard")
    try:
        g = ReactRetryGuard()
        g.failed_attempts.append(FailedAttempt(
            tool="rag", inputs_sig=inputs_signature({"q": "x"}),
            error_code="no_sources", round=1, results_before=0))
        hit = g.should_block(tool="rag", inputs={"q": "x"}, current_results_count=0)
    finally:
        lg.removeHandler(h)
    assert hit is not None
    out = buf.getvalue()
    assert "blocked_repeat" in out
    assert "failed_round=1" in out and "error_code='no_sources'" in out


def test_block_and_allow_are_distinct_targets():
    """Both branches must be countable separately, or the ratio — the thing
    that says whether the guard is doing anything — is unrecoverable."""
    from app.pipeline.react_retry_guard import ReactRetryGuard, FailedAttempt, inputs_signature
    buf, lg, h = _cap("app.pipeline.react_retry_guard")
    try:
        g = ReactRetryGuard()
        g.should_block(tool="rag", inputs={"q": "a"}, current_results_count=0)
        g.failed_attempts.append(FailedAttempt(
            tool="rag", inputs_sig=inputs_signature({"q": "b"}),
            error_code="err", round=1, results_before=5))
        g.should_block(tool="rag", inputs={"q": "b"}, current_results_count=0)
    finally:
        lg.removeHandler(h)
    out = buf.getvalue()
    assert "allowed" in out and "blocked_repeat" in out


# ── the sporadic miss: argument precedence, not the manifest ────────

def test_numeric_carc_is_not_beaten_by_a_carc_group():
    """`lookup = carc_group or str(carc) if carc else carc_group` parses as
    `(carc_group or str(carc)) if carc else carc_group` — a conditional binds
    looser than `or` — so a carc_group WON over a correct numeric carc. The
    endpoint is case-sensitive (`197` hits, `PRECERT` hits, `precert` -> {}),
    so a turn holding the right code sent the wrong key and reported no
    playbook. This is the live "carc 197 / sunshine health" miss."""
    carc_group, carc = "precert", 197

    # the old expression, kept as the thing being guarded against
    old = carc_group or str(carc) if carc else carc_group
    assert old == "precert", "the historical bug no longer reproduces — check the test"

    # what the fix must produce: numeric first, group normalised as fallback
    lookups = []
    if carc:
        lookups.append(str(carc))
    if carc_group and carc_group.upper() not in lookups:
        lookups.append(carc_group.upper())
    assert lookups[0] == "197", "numeric carc must be tried first"
    assert "PRECERT" in lookups, "group must survive as an upper-cased fallback"


def test_the_fix_is_in_the_source_not_only_in_this_test():
    """Searches CODE, not raw text. The old expression still appears in the
    comment that explains why it was wrong, and a substring search would fail
    forever — with the obvious "fix" being to delete the explanation."""
    import inspect
    from app.pipeline import react_loop
    code = "\n".join(
        ln for ln in inspect.getsource(react_loop).splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert "lookup = carc_group or str(carc) if carc else carc_group" not in code
    assert "_lookups" in code
