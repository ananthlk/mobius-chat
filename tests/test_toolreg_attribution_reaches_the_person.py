"""Whose executor ran the tool, said where a person can read it.

🔴 THIS FILE EXISTS BECAUSE THE EMIT WAS THE ONLY THING NOT UNDER TEST.

`_preload_runner_toolreg` accepted `emitter` and never called it, so every
tool that ran through Tool Manifest was SILENT in the turn's notes while the
native path emitted normally. A parameter taken and never used is the same
defect as a column written and never read: wired from both ends, connected in
neither.

The directed path did emit, but only on failure, and the line read
"<tool> could not run" — naming the tool and hiding the executor. Through
toolreg a tool can be `refused` (a consequence declaration declined it, on
purpose, and nothing is broken), `not_wired` (no route was ever declared, also
not a tool fault, and NOT retryable), or `could_not_run` (tried, broke — the
only one that is a fault). Collapsing those into one sentence about chat's tool
sends a reader to chat's handler to debug a missing row in a route table. That
is not hypothetical: every chat tool but list_tasks returned could_not_run in
DIRECTED mode and looked broken while being merely unrouted.

Mutation-measured before committing: deleting the attribution from
`_execute_via_toolreg` leaves the 778 v2/react_loop tests green.
"""
import types

import pytest

from app.pipeline import react_loop


class _Ctx:
    correlation_id = "testcid0"


def _res(**kw):
    base = dict(tool="t", outcome="answered", payload={"value": "v"},
                sources=[], effects={}, notes=[], duration_ms=12,
                route="POST http://x", reason="", ok=True, auto_retried=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _run_directed(monkeypatch, **result):
    import toolreg.v2 as _v2
    monkeypatch.setattr(_v2, "execute_tool", lambda *a, **k: _res(**result))
    seen: list[str] = []
    out = react_loop._execute_via_toolreg("sometool", {}, _Ctx(), seen.append)
    return out, seen


def test_a_tool_that_ran_says_whose_executor_ran_it(monkeypatch):
    out, seen = _run_directed(monkeypatch, outcome="answered",
                              sources=[{"a": 1}])
    assert out["success"] is True
    assert seen, "a tool ran through Tool Manifest and the person was told nothing"
    assert any("Tool Manifest" in m for m in seen), seen


def test_a_refusal_is_not_reported_as_a_failure(monkeypatch):
    """`refused` means a gate declined. Nothing was asked and nothing broke."""
    _out, seen = _run_directed(monkeypatch, outcome="refused",
                               reason="refused for SPECULATIVE execution")
    line = " ".join(seen)
    assert "refused" in line.lower(), seen
    assert "could not run" not in line.lower(), (
        "a deliberate refusal was reported as a fault", seen)


def test_a_missing_route_is_not_reported_as_a_failure(monkeypatch):
    """`not_wired` means nobody ever declared a route. Not retryable."""
    _out, seen = _run_directed(monkeypatch, outcome="not_wired",
                               reason="no executable route declared")
    line = " ".join(seen)
    assert "no route" in line.lower(), seen
    assert "could not run" not in line.lower(), seen


@pytest.mark.parametrize("canon", ["refused", "not_wired"])
def test_refused_and_not_wired_never_carry_a_retry_code(monkeypatch, canon):
    """Neither is retryable, and that must not rest on another module's prose.

    The retry code is derived by substring-matching the reason text. Today no
    refusal reason happens to contain "timeout", so the guarantee holds by
    accident; a reworded reason would start retrying a consequence refusal
    against a promise it cannot satisfy.
    """
    out, _seen = _run_directed(monkeypatch, outcome=canon,
                               reason="upstream timeout while refusing")
    assert out.get("error") is None, (
        f"{canon} was given a retryable error envelope", out.get("error"))


def test_chats_deployed_branches_still_see_the_legacy_three(monkeypatch):
    """The bridge asks for five words and must hand this module three.

    react_loop branches on "evidence" / "empty" / "could_not_run" as string
    literals. Emitting the canonical vocabulary into those branches would
    retire them silently — a branch that still exists and is no longer reached.
    """
    from toolreg.outcomes import canonical, to_legacy
    assert to_legacy(canonical("answered")) == "evidence"
    assert to_legacy(canonical("empty")) == "empty"
    for canon in ("refused", "not_wired", "could_not_run"):
        assert to_legacy(canonical(canon)) == "could_not_run"

    out, _ = _run_directed(monkeypatch, outcome="empty", payload="", sources=[])
    assert out["signal"] == react_loop.RETRIEVAL_SIGNAL_NO_SOURCES


def test_an_injected_legacy_runner_is_normalised_not_downgraded(monkeypatch):
    """A runner is injectable and may speak the OLD vocabulary.

    to_legacy() knows canonical keys only. Feeding it an injected "evidence"
    raised, and the defensive fallback turned a SUCCESS into could_not_run.
    Four tests caught it. canonical() must run first.
    """
    out, seen = _run_directed(monkeypatch, outcome="evidence",
                              sources=[{"a": 1}])
    assert out["success"] is True, (
        "a legacy-spelled runner outcome was downgraded to a failure", seen)
