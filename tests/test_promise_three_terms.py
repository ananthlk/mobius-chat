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


def test_close_promise_does_NOT_guess_the_cost():
    """🔴 MY FIRST VERSION SUMMED COST AT CLOSE and the comment claimed the
    llm_calls rows were "all present by the outermost finally". Measured cid
    3b4d896d: calls ran 03:42:28 -> 03:44:22, the attestation closed 03:43:36.
    The adjudicator makes its OWN priced call after publish, so closing there
    sees a partial bill -- and a silently short cost reads as a cheaper turn."""
    from datetime import UTC, datetime
    a = P.close_promise(P.open_promise("copilot", datetime.now(UTC)),
                        correlation_id="cid-1", outcome="completed",
                        now=datetime.now(UTC))
    assert a.delivered_cost_c is None, (
        "cost is being computed at close, where the bill is not complete"
    )
    assert a.promised_cost_c is not None, "the promise side must still be set"


def test_both_terms_are_written_after_adjudication():
    """Cost AND quality land in one UPDATE, in the only place both exist."""
    import ast
    import inspect

    import app.services.post_run_adjudication as A

    tree = ast.parse(inspect.getsource(A))
    consts = {n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    upd = " ".join(c for c in consts if "turn_attestations" in c)
    assert upd, "the adjudicator never touches turn_attestations"
    assert "delivered_quality" in upd and "delivered_cost_c" in upd
    # UPDATE, not INSERT: close_promise owns the row. Creating one here would
    # hide a missing attestation rather than surface it.
    assert "UPDATE" in upd and "INSERT" not in upd
    # COALESCE so a re-run cannot overwrite an already-attested term.
    assert "COALESCE" in upd

    sums = " ".join(c for c in consts if "llm_calls" in c)
    assert "SUM(cost_usd)" in sums, "cost is not summed from llm_calls"


def test_cost_is_read_through_llm_calls_OWN_pool():
    """🔴 THREE ATTEMPTS, TWO SILENT FAILURES.

    1. res.get("rows") -- wrong shape, returned nothing.
    2. db_execute("chat", ...) + _rows_as_dicts -- llm_calls is written
       through llm_analytics' asyncpg pool, while db_execute routes via the
       manifest-gated db-agent. The refusal became [] and read as "no priced
       calls". Measured: cid 9ccb537a logged cost_c=None while llm_calls held
       6 rows summing $0.0338 for that turn.

    Neither failure logged anything, which is why it took three passes. Read
    the table the way the table is written."""
    import inspect

    import app.services.post_run_adjudication as A

    src = inspect.getsource(A)
    assert "_acquire_conn" in src, "cost no longer reads llm_calls' own pool"
    assert "fetchval" in src
    assert '.get("rows")' not in src, "hand-rolled row read is back"
    assert 'db_execute(\n                "SELECT SUM(cost_usd)' not in src

    # And the failure must be loud -- the two silent ones are the reason.
    i = src.index("cost sum FAILED")
    assert "logger.warning" in src[max(0, i - 200):i]
