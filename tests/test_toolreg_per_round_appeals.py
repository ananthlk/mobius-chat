"""Per-round execution through Tool Manifest — the appeals cut.

🔴 THE POINT OF THESE TESTS IS THAT THE THREE OUTCOMES DO NOT COLLAPSE.

toolreg distinguishes `evidence`, `empty` (a claim about the CORPUS, earned) and
`could_not_run` (a claim about OUR CODE). React's result dict has ONE boolean,
so the distinction has to survive in `signal` and `error`. If it does not, react
is told the corpus is silent when the truth is that the call failed — which is
the confusion the executor exists to prevent, arriving through the seam built to
prevent it.
"""
import json
import types
from unittest import mock

import pytest

from app.pipeline import react_loop


def _res(outcome, payload=None, sources=(), reason=""):
    return types.SimpleNamespace(tool="t", outcome=outcome, payload=payload,
                                 sources=list(sources), effects={}, notes=[],
                                 duration_ms=12, route="mcp", reason=reason,
                                 ok=outcome == "evidence", auto_retried=False)


class _Ctx:
    correlation_id = "cid"


def _run(outcome, **kw):
    import toolreg.v2 as v2
    with mock.patch.object(v2, "execute_tool", lambda *a, **k: _res(outcome, **kw)):
        return react_loop._execute_via_toolreg(
            "appeals_lookup_rules", {"carc": "197"}, _Ctx(), lambda *_a: None)


# The appeals group as declared by its owner. The routed set must stay a
# SUBSET of this: shrinking is an operational decision (a tool whose route is
# broken in the deployed catalogue belongs back on chat's branch, immediately,
# without a test change blocking it), while GROWING is a migration that needs
# its own evidence.
_APPEALS_GROUP = frozenset({
    "appeals_find_carc", "appeals_get_playbook", "appeals_lookup_rules",
    "appeals_validate_claim", "appeals_assemble_letter"})


def test_only_appeals_tools_are_routed():
    """🔴 The cut is deliberately narrow, and the ASYMMETRY is the point.

    The original form pinned the set at exactly five. That is right against a
    quiet addition and wrong against an urgent removal: on 2026-09-15
    appeals_find_carc came back "no executable route declared" from the
    deployed catalogue and had to go back to chat's branch within minutes —
    and this test failed the fix, not the fault. A gate that blocks the
    rollback is a gate pointed the wrong way.

    Subset, so pulling a broken tool is never blocked; non-empty, so the cut
    cannot be silently emptied and read as passing.
    """
    routed = react_loop._TOOLREG_OWNED
    assert routed, "the routed set is empty — the cut is off entirely"
    extra = routed - _APPEALS_GROUP
    assert not extra, (
        f"{sorted(extra)} routed through toolreg but not part of the appeals "
        f"cut — widening is a migration with its own evidence, not a quiet edit"
    )


def test_a_tool_pulled_for_a_broken_route_is_recorded_not_just_deleted():
    """A removal must leave a reason in the source. Otherwise the next person
    re-adds it, hits the same broken route, and rediscovers it live."""
    import inspect
    src = inspect.getsource(react_loop)
    i = src.index("_TOOLREG_OWNED: frozenset[str] = frozenset({")
    block = src[i:i + 1600]
    for missing in sorted(_APPEALS_GROUP - react_loop._TOOLREG_OWNED):
        assert missing in block, (
            f"{missing} was removed from the cut with no note saying why"
        )


def test_evidence_is_a_success_with_no_absence_signal():
    out = _run("evidence", payload={"rules": [1, 2]}, sources=[{"document_id": "d"}])
    assert out["success"] is True
    assert out["signal"] is None
    assert out["sources"]
    assert json.loads(out["result"])["rules"] == [1, 2]


def test_an_earned_empty_carries_the_no_sources_signal():
    """`empty` IS an absence claim — the tool looked and the corpus is silent.
    That is the one case where the no-sources signal is truthful."""
    out = _run("empty", payload={"rules": []})
    assert out["success"] is False
    assert out["signal"] == react_loop.RETRIEVAL_SIGNAL_NO_SOURCES


def test_could_not_run_is_NOT_reported_as_an_absence():
    """🔴 THE LOAD-BEARING ONE. A failed call must not reach react as 'the
    corpus holds nothing' — react would reason correctly to a wrong
    conclusion."""
    out = _run("could_not_run", reason="mcp server unreachable")
    assert out["success"] is False
    assert out["signal"] is None, (
        "a call that could not run was given the no-sources signal, which tells "
        "react the corpus is silent")
    assert "COULD NOT RUN" in out["result"]


def test_a_recoverable_failure_reaches_the_existing_retry():
    """The retry wrapper acts on error_code. Layer A stays chat's for now, so a
    recoverable failure must still be visible to it."""
    out = _run("could_not_run", reason="http 503 from appeals")
    assert out["error"]["schema_name"] == "error_envelope"
    assert out["error"]["error_code"] == "provider_error"
    out2 = _run("could_not_run", reason="the tool reported its own failure: timeout")
    assert out2["error"]["error_code"] == "timeout"


def test_an_UNRECOVERABLE_failure_does_not_get_a_retry_envelope():
    """Retrying a consequence refusal or a missing argument spends the promise
    to fail identically. The envelope is attached only when a retry could
    plausibly change the answer."""
    for reason in ("refused for SPECULATIVE execution: direction=outward",
                   "input rejected before calling: missing required ['carc']"):
        out = _run("could_not_run", reason=reason)
        assert "error" not in out, f"{reason!r} was made retry-eligible"


def test_the_interception_is_inside_the_retry_wrapper_not_before_it():
    """🔴 STRUCTURAL. Intercepting at the :9025 call site would take execution
    AND silently drop the retry, the promise-fit guard and _capture_rendered.
    This asserts the branch lives inside _run_once, so a later 'simplification'
    that hoists it out fails here rather than in production."""
    import inspect
    src = inspect.getsource(react_loop._execute_tool_with_retry)
    assert "_TOOLREG_OWNED" in src, (
        "the toolreg branch is no longer inside _execute_tool_with_retry — "
        "appeals has lost the retry and the promise-fit guard")
    assert "_capture_rendered" in src


# ───────────────────────────── through the wrapper, not the helper

def test_an_appeals_tool_reaches_toolreg_THROUGH_the_retry_wrapper():
    """🔴 EVERY TEST ABOVE CALLS _execute_via_toolreg DIRECTLY, AND THAT PROVED
    NOTHING ABOUT ROUTING.

    Caught by mutation: disabling the branch (`if False:`) left six of seven
    green, because six exercise the helper and the helper was still perfect.
    Only the structural test noticed. That is the fourth time in one session I
    have tested a helper and called it a surface.

    This one calls _execute_tool_with_retry — the real per-round entry point —
    and asserts the appeals tool reached toolreg and did NOT reach
    _execute_tool.
    """
    seen = {}

    def _spy(tool, inputs, *a, **k):
        seen["toolreg"] = tool
        return _res("evidence", payload={"rules": [1]})

    def _boom(*a, **k):
        seen["legacy"] = True
        raise AssertionError("_execute_tool was called for an appeals tool")

    import toolreg.v2 as v2
    with mock.patch.object(v2, "execute_tool", _spy), \
         mock.patch.object(react_loop, "_execute_tool", _boom):
        out = react_loop._execute_tool_with_retry(
            "appeals_lookup_rules", {"carc": "197"}, _Ctx(), 1,
            lambda *_a: None, None, skip_retry=True)

    assert seen.get("toolreg") == "appeals_lookup_rules", (
        "the appeals tool did not reach toolreg through the retry wrapper")
    assert "legacy" not in seen
    assert out["success"] is True


def test_a_NON_appeals_tool_still_goes_to_the_legacy_dispatch():
    """The cut must not widen silently. Everything outside the five keeps
    chat's own 2,522-line dispatch."""
    seen = {}

    def _legacy(tool, *a, **k):
        seen["legacy"] = tool
        return {"tool": tool, "success": True, "result": "ok", "sources": []}

    def _never(*a, **k):
        raise AssertionError("toolreg was called for a tool outside the cut")

    import toolreg.v2 as v2
    with mock.patch.object(v2, "execute_tool", _never), \
         mock.patch.object(react_loop, "_execute_tool", _legacy):
        react_loop._execute_tool_with_retry(
            "payor_fact", {"payor": "x"}, _Ctx(), 1,
            lambda *_a: None, None, skip_retry=True)

    assert seen.get("legacy") == "payor_fact"
