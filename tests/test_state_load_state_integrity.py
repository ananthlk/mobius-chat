"""state_load / chat_state integrity — P3 work order.

Coverage for this node was ABSENT: nothing in the suite called run_state_load,
so there was nothing to regress against and the defect below sat in a live path
unobserved.

THE DEFECT: get_state returned None for a failed read and for a genuinely new
thread alike (its docstring named only the second), so every caller's
`get_state(tid) or {}` built a ThreadState from DEFAULT_STATE and handed it to
save_state_full — "replace state entirely (no merge)". One transient read error
plus one delta-bearing message replaced the whole conversation with defaults,
and state_version incremented on that write exactly as a healthy turn would, so
nothing afterwards could distinguish the two.

WHAT THESE TESTS ASSERT: the STORED ROW, never a return value. A function can
return whatever it likes while still having destroyed the row — that gap is
where this bug lived. The fake below is a real store: it holds rows, applies
writes, and bumps state_version, so a write that should not happen is caught by
observing that the row did not change.
"""
from __future__ import annotations

import json

import pytest

from app.pipeline.context import PipelineContext


class FakeChatState:
    """Minimal stateful stand-in for the chat_state table."""

    def __init__(self, thread_id: str, state: dict, version: int = 11):
        self.rows: dict[str, tuple[str, int]] = {thread_id: (json.dumps(state), version)}
        self.fail_reads = False
        self.writes_attempted = 0

    # -- db_query / db_execute surface -------------------------------
    def query(self, sql: str, db: str, params: dict | None = None):
        params = params or {}
        if "FROM chat_state" in sql:
            if self.fail_reads:
                return {"error": {"code": "query_error", "message": "simulated read failure"}}
            row = self.rows.get(params.get("tid"))
            return {"rows": [], "columns": []} if row is None else {
                "rows": [[row[0], row[1]]], "columns": ["state_json", "state_version"]}
        return {"rows": [], "columns": []}

    def execute(self, sql: str, db: str, params: dict | None = None):
        params = params or {}
        if "chat_state" in sql:
            self.writes_attempted += 1
            tid = params.get("tid")
            if "UPDATE chat_state" in sql:  # compare-and-set
                cur = self.rows.get(tid)
                if cur is None or cur[1] != params.get("ev"):
                    return {"rows_affected": 0}
                self.rows[tid] = (params["state_json"], cur[1] + 1)
                return {"rows_affected": 1}
            cur = self.rows.get(tid)
            self.rows[tid] = (params["state_json"], (cur[1] + 1) if cur else 1)
            return {"rows_affected": 1}
        return {"rows_affected": 0}

    def stored(self, tid: str) -> tuple[dict, int]:
        raw, ver = self.rows[tid]
        return json.loads(raw), ver


@pytest.fixture
def store(monkeypatch):
    import app.storage.threads as th
    prior = {"active": {"payer": "Aetna", "state": "FL"}, "open_slots": [], "resolved_slots": {}}
    fake = FakeChatState("t-1", prior, version=11)
    monkeypatch.setattr(th, "db_query", fake.query)
    monkeypatch.setattr(th, "db_execute", fake.execute)
    monkeypatch.setattr(th, "ensure_thread", lambda *a, **k: None)
    # Keep the test on state_load's own behaviour, not its neighbours' I/O.
    import app.stages.state_load as sl
    monkeypatch.setattr(sl, "get_last_turn_messages", lambda *a, **k: [])
    monkeypatch.setattr(sl, "get_thread_rolling_summary", lambda *a, **k: None)
    monkeypatch.setattr(sl, "get_last_turn_sources", lambda *a, **k: [])
    monkeypatch.setattr(sl, "get_prior_resolved_entities", lambda *a, **k: [])
    monkeypatch.setattr(sl, "build_context_pack", lambda *a, **k: "")
    monkeypatch.setattr(sl, "route_context", lambda *a, **k: "CONTINUATION")
    monkeypatch.setattr(sl, "clear_tool_results", lambda *a, **k: None)
    return fake


def _run(msg: str = "what about Cigna in Texas"):
    from app.stages.state_load import run_state_load
    ctx = PipelineContext(correlation_id="c-1", thread_id="t-1", message=msg)
    run_state_load(ctx)
    return ctx


# ── the core guarantee ───────────────────────────────────────────────
def test_failed_read_does_not_overwrite_stored_state(store):
    """A read failure plus a delta-bearing message must leave the row alone."""
    before, before_ver = store.stored("t-1")
    store.fail_reads = True

    ctx = _run()

    after, after_ver = store.stored("t-1")
    assert after == before, "stored state was replaced after a FAILED read"
    assert after_ver == before_ver == 11, "state_version advanced on a turn that read nothing"
    assert store.writes_attempted == 0, "a write was attempted from state that was never read"
    assert ctx.state_read_failed is True


def test_healthy_read_still_persists_the_delta(store):
    """The guard must not be a blanket 'never write' — that would pass the test
    above while breaking the product."""
    ctx = _run()
    after, after_ver = store.stored("t-1")
    assert ctx.state_read_failed is False
    assert after_ver == 12, "a healthy delta-bearing turn should advance state_version"
    assert after != {}, "state was replaced with defaults on a healthy read"


def test_error_and_absence_are_distinguishable(store):
    """The root cause: one value meant two opposite things."""
    from app.storage.threads import StateUnavailable, get_state
    assert get_state("no-such-thread") is None      # genuine absence
    store.fail_reads = True
    with pytest.raises(StateUnavailable):            # failure
        get_state("t-1")


def test_undecodable_row_raises_rather_than_looking_new(store):
    """A row that exists but does not decode used to return None — so the
    recovery was to overwrite the row we had just failed to read."""
    from app.storage.threads import StateUnavailable, get_state
    store.rows["t-1"] = ("\"not-an-object\"", 11)
    with pytest.raises(StateUnavailable):
        get_state("t-1")


# ── the concurrency guard (state_version was write-only) ─────────────
def test_compare_and_set_refuses_to_clobber_a_concurrent_write(store):
    """Two turns on one thread were a lost update: state_version was
    incremented on every write and compared on none."""
    from app.storage.threads import get_state_with_version, save_state_full
    _state, ver = get_state_with_version("t-1")          # turn A reads v11
    store.rows["t-1"] = (json.dumps({"active": {"payer": "United"}}), 12)  # turn B writes
    ok = save_state_full("t-1", {"active": {"payer": "Aetna"}}, expected_version=ver)
    assert ok is False, "stale write was applied over a newer one"
    assert store.stored("t-1")[0] == {"active": {"payer": "United"}}


def test_missing_rowcount_is_not_read_as_a_miss(store, monkeypatch):
    """Two backends answer db_execute and disagree on the key name. Absence must
    mean 'unknown', not '0 rows matched' — the latter would silently stop
    persisting state entirely, which is worse than the race being guarded."""
    import app.storage.threads as th
    monkeypatch.setattr(th, "db_execute", lambda *a, **k: {})   # no count reported
    save_ok = th.save_state_full("t-1", {"active": {}}, expected_version=11)
    assert save_ok is True
