"""The promise has three terms and only one was ever attested.

Measured 2026-09-14: 741 turn_attestations rows -- 657 delivered_latency_s,
ZERO delivered_cost_c, ZERO delivered_quality. The columns carried the comment
"step 3 -- NULL in step 1"; step 3 never happened, and nothing failed, because
a NULL in a column nobody reads looks like a column that is not due yet.

Meanwhile llm_calls held 681 rows with cost_usd for those same turns, and the
post-run adjudicator had scored essentially all of them into qc_audit. Both
producers ran. Neither consumer existed.
"""
import types

import app.pipeline.react.promise as P


def test_cost_is_summed_in_CENTS_not_dollars(monkeypatch):
    """promised_cost_c is cents (fast 16, normal 45, thinking 81). A delivered
    figure in dollars against a promise in cents reads as a 100x saving --
    worse than no figure at all."""
    monkeypatch.setattr(P, "db_execute", None, raising=False)
    monkeypatch.setattr("app.db_client.db_execute",
                        lambda *a, **k: {"rows": [{"usd": 0.0304}]})
    assert P._delivered_cost_c("cid-1") == 3.04


def test_no_priced_call_is_None_not_zero():
    """A turn that made no priced call and a turn whose cost we could not
    determine are different facts. Zero asserts the first."""
    import app.db_client as C
    orig = C.db_execute
    try:
        C.db_execute = lambda *a, **k: {"rows": [{"usd": None}]}
        assert P._delivered_cost_c("cid-1") is None
    finally:
        C.db_execute = orig


def test_a_failed_lookup_never_fails_the_turn():
    import app.db_client as C
    orig = C.db_execute
    try:
        def _boom(*a, **k):
            raise RuntimeError("db down")
        C.db_execute = _boom
        assert P._delivered_cost_c("cid-1") is None
    finally:
        C.db_execute = orig


def test_close_promise_attaches_the_cost(monkeypatch):
    from datetime import UTC, datetime
    monkeypatch.setattr(P, "_delivered_cost_c", lambda cid: 4.5)
    a = P.close_promise(P.open_promise("copilot", datetime.now(UTC)),
                        correlation_id="cid-1", outcome="completed",
                        now=datetime.now(UTC))
    assert a.delivered_cost_c == 4.5
    assert a.promised_cost_c is not None, "nothing to compare it against"


def test_quality_is_written_by_the_adjudicator_not_at_close():
    """Ordering: the adjudicator runs AFTER publish, so at close the score does
    not exist. An attestation written without it is correct; one that never
    gains it is the defect. Guards that the UPDATE is wired and scoped."""
    import ast
    import inspect

    import app.services.post_run_adjudication as A

    src = inspect.getsource(A)
    tree = ast.parse(src)
    consts = {n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    sql = [c for c in consts if "turn_attestations" in c]
    assert sql, "the adjudicator never touches turn_attestations"
    stmt = " ".join(sql)
    assert "delivered_quality" in stmt
    # UPDATE, not INSERT: close_promise owns the row. Creating one here would
    # hide a missing attestation instead of surfacing it.
    assert "UPDATE" in stmt and "INSERT" not in stmt
    # Scoped so a re-run cannot overwrite a score already attested.
    assert "IS NULL" in stmt
