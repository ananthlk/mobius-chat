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


def test_the_row_reader_is_the_shared_helper():
    """🔴 I wrote res.get("rows") and it silently returned nothing -- the
    codebase reads SELECTs through _rows_as_dicts. A wrong reader does not
    raise; it yields None, which looks exactly like 'no priced calls'."""
    import inspect

    import app.services.post_run_adjudication as A

    src = inspect.getsource(A)
    assert "_rows_as_dicts" in src
    assert '.get("rows")' not in src, "hand-rolled row read is back"
