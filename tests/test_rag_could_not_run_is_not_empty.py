"""A rag self-reported failure must not reach react as an empty corpus.

Measured 2026-09-14: 8 concurrent calls to mobius-rag returned HTTP 200 with
contract.status="timeout" and chunks=[]. corpus_search collapsed that to
signal="no_sources" -- byte-identical to a genuine empty corpus -- so react
reported "not mentioned in anything returned" about material it never searched.

These tests assert the PROPERTY (a self-reported failure is distinguishable and
retryable), not the wording of any emit line, so rephrasing the message cannot
satisfy them and neither can editing the guard's own comments.
"""
import inspect
import textwrap

from app.pipeline import react_loop


def test_could_not_run_set_is_drawn_from_rags_real_enum():
    """rag's status is a CLOSED enum, verified in mobius-rag contract.py
    `_derive_status`: {no_retrieval, filled_no_synthesis, empty, partial, ok,
    timeout}.

    The first version of this set was {timeout, error, failed, cancelled} --
    three values rag never emits. It would have passed a test that only
    checked "is timeout in the set", so this test pins the WHOLE partition:
    every member must be a real rag status, and every real status must be
    consciously on one side or the other.
    """
    rag_enum = {"no_retrieval", "filled_no_synthesis", "empty",
                "partial", "ok", "timeout"}
    s = set(react_loop._RAG_COULD_NOT_RUN_STATUSES)

    unknown = s - rag_enum
    assert not unknown, (
        f"{unknown} are not values mobius-rag ever sets -- a guard matching "
        f"a string the other system never emits is not a guard"
    )

    # could-not-run: no basis to say anything about the corpus.
    assert "timeout" in s
    assert "filled_no_synthesis" in s

    # NOT could-not-run, each for its own reason:
    #   ok/partial -- real results ("partial" as failure discarded good
    #                 chunks once already, fixed 2026-08-06)
    #   empty      -- rag looked and found nothing; that IS a corpus answer
    #   no_retrieval -- a real terminal CLARIFY/DECLINE outcome, and it has
    #                 its own branch that this guard sits above
    for real_outcome in ("ok", "partial", "empty", "no_retrieval"):
        assert real_outcome not in s, (
            f"{real_outcome} is a real rag outcome, not a failure"
        )


def test_no_retrieval_still_reaches_its_own_clarify_branch():
    """The guard sits ABOVE the clarify branch. If no_retrieval were ever
    added to the could-not-run set, clarify would become dead code -- the
    exact ordering defect this file's other test guards in the opposite
    direction."""
    assert "no_retrieval" not in react_loop._RAG_COULD_NOT_RUN_STATUSES
    src = inspect.getsource(react_loop._execute_tool)
    assert '_status == "no_retrieval"' in src, \
        "the clarify branch that reads no_retrieval is gone"


def _branch_src():
    src = inspect.getsource(react_loop._execute_tool)
    i = src.index("_RAG_COULD_NOT_RUN_STATUSES")
    return src[i:i + 2000]


def test_failure_branch_returns_a_retryable_error_envelope():
    """Layer A retries on error_envelope + a recoverable error_code.

    Without the envelope the timeout is invisible to the ONE mechanism built
    to recover from it -- which was the whole defect.
    """
    b = _branch_src()
    assert '"error"' in b, "must attach a typed envelope at result['error']"
    assert "ErrorEnvelope" in b, "envelope must come from the contract, not a hand-rolled dict"
    # The code must be one _execute_tool_with_retry actually retries on.
    recoverable = {"rate_limit", "timeout", "provider_error", "scrape_failed"}
    assert any(f'"{c}"' in b for c in recoverable), \
        "error_code must be in the recoverable set or the retry never fires"


def test_failure_branch_does_not_report_no_sources():
    b = _branch_src()
    assert "RETRIEVAL_SIGNAL_NO_SOURCES" not in b, \
        "reporting no_sources is precisely the bug this branch exists to fix"
    assert '"could_not_run"' in b


def test_failure_branch_precedes_every_evidence_based_branch():
    """Branch exists != branch reached.

    Every branch below this one -- clarify, relax, reframe, google fallback --
    reasons FROM returned evidence. If any of them returns first, this guard is
    dead code on exactly the turns it exists for.

    Checked against the AST, not string position: the first version of this test
    searched for the substring "google_search" and failed on a TELEMETRY LABEL
    that returns nothing. A name appearing earlier in the source is not a branch
    taken earlier -- only a `return` is.
    """
    import ast
    src = inspect.getsource(react_loop._execute_tool)
    tree = ast.parse(textwrap.dedent(src))
    fn = tree.body[0]

    guard_line = None
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and "_RAG_COULD_NOT_RUN_STATUSES" in ast.dump(node.test):
            guard_line = node.lineno
            break
    assert guard_line is not None, "the could-not-run guard is gone"

    # Where does _status -- the value the guard reads -- become available?
    status_line = min(
        (n.lineno for n in ast.walk(fn)
         if isinstance(n, ast.Assign)
         and any(getattr(t, "id", None) == "_status" for t in n.targets)),
        default=None,
    )
    assert status_line is not None and status_line < guard_line

    # Nothing may return between the two. A return in that window would take
    # the turn before the guard is ever evaluated.
    between = [n.lineno for n in ast.walk(fn)
               if isinstance(n, ast.Return) and status_line < n.lineno < guard_line]
    assert not between, (
        f"return statement(s) at {between} run before the could-not-run guard, "
        f"making it unreachable on a failed retrieval"
    )


def test_rag_round_history_records_rags_own_status():
    """Dropped until 2026-09-14, which is why every downstream reader of this
    history -- the reframe decision and the model's own view of it in
    react/prompts.py -- could not tell a timeout from an empty corpus."""
    src = inspect.getsource(react_loop)
    i = src.index("def _record_rag_round")
    body = src[i:i + 1200]
    assert '"status": telemetry.get("status")' in body, \
        "rag's own verdict on whether it ran must survive into the round history"
