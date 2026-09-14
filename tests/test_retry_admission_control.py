"""A retry is a spend the governor never sees.

The round loop asks the governor whether another ROUND fits the promise. A
retry happens inside one round, so before this guard it spent the promise with
nothing deciding whether it could afford to.

Reachable as of cd85a5b, which correctly began classifying rag timeouts as
recoverable: a 120s rag timeout plus its retry is 240s against a 95s promise.
That one change made correctness better and latency worse; this is the other
half of it.
"""
import time
import types

import pytest

from app.pipeline import react_loop


def _recoverable_result():
    return {
        "tool": "search_corpus", "success": False, "result": "timed out",
        "sources": [],
        "error": {"schema_name": "error_envelope", "error_code": "timeout",
                  "retry_after_seconds": 3, "user_facing_message": "timed out"},
    }


class _Ctx:
    correlation_id = "cid-test"
    chat_mode = "agentic"
    orchestrator_version = "v2"
    react_soft_target_s = 95.0
    _rag_call_rounds = [{"latency_ms": 120000, "status": "ok"}]  # 120s calls

    def __init__(self, spent_s):
        self.react_turn_start_monotonic = time.monotonic() - spent_s


def _run(ctx, monkeypatch):
    calls = {"n": 0}
    lines = []

    def fake_execute(tool, inputs, c, emitter=None, open_gaps=None, **kw):
        calls["n"] += 1
        return _recoverable_result()

    monkeypatch.setattr(react_loop, "_execute_tool", fake_execute)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    react_loop._execute_tool_with_retry(
        "search_corpus", {"query": "q"}, ctx, 2,
        lines.append, lambda *_a, **_k: None,
    )
    return calls["n"], lines


def test_retry_is_refused_when_it_cannot_fit_the_promise(monkeypatch):
    """80s spent, ~120s expected, 95s promise -> the retry cannot fit."""
    n, lines = _run(_Ctx(spent_s=80.0), monkeypatch)
    assert n == 1, "the call ran once and must NOT have been retried"
    assert any("NOT retrying" in l for l in lines)


def test_retry_still_happens_when_there_is_room(monkeypatch):
    """The guard must not become a blanket no-retry -- that would trade
    recovery for the promise, which is the opposite trade."""
    ctx = _Ctx(spent_s=1.0)
    ctx._rag_call_rounds = [{"latency_ms": 5000, "status": "ok"}]
    n, _ = _run(ctx, monkeypatch)
    assert n == 2, "a retry that fits must still run"


def test_v1_retry_is_never_conditioned(monkeypatch):
    """v1 must stay bit-identical for the A/B, even when it is over budget."""
    ctx = _Ctx(spent_s=10_000.0)
    ctx.orchestrator_version = "v1"
    n, _ = _run(ctx, monkeypatch)
    assert n == 2, "v1's retry must not be gated by the promise"


def test_no_contract_means_no_basis_to_refuse(monkeypatch):
    """Governor off -> no soft target -> the guard must not invent one.
    A missing budget is not a zero budget."""
    ctx = _Ctx(spent_s=10_000.0)
    ctx.react_soft_target_s = None
    n, _ = _run(ctx, monkeypatch)
    assert n == 2


def test_missing_clock_means_no_basis_to_refuse(monkeypatch):
    ctx = _Ctx(spent_s=10_000.0)
    ctx.react_turn_start_monotonic = None
    n, _ = _run(ctx, monkeypatch)
    assert n == 2
