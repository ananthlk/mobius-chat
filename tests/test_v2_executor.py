"""The narrow v2 executor — the decision varies, nothing else does.

These assert the two properties that make an arm comparable: every posture
produces an action (an executor may not decline), and the executor's vocabulary
is exactly react_loop's — verified against react_loop.py itself, not against a
copy of the strings.
"""

import ast
import pathlib

import pytest

from app.pipeline.v2 import executor as ex
from app.pipeline.v2.posture import Decision, ExitMode, Posture


# ── purity, the same bar posture.py is held to ──────────────────────────────

def test_the_executor_is_pure():
    """No clock, no DB, no env. An arm that reads a global cannot be replayed
    from its stored row, which is the only way a disputed result is settled
    without re-running traffic that no longer exists."""
    tree = ast.parse(pathlib.Path("app/pipeline/v2/executor.py").read_text())
    banned = {"time", "datetime", "os", "random", "requests", "psycopg2"}
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    assert not (imported & banned), imported & banned


# ── totality: an executor may not decline ───────────────────────────────────

@pytest.mark.parametrize("posture", list(Posture))
def test_every_posture_produces_a_real_directive(posture):
    """shadow.map_directive may return None; this may not. The turn is running
    and something has to happen next."""
    a = ex.decide(Decision(posture, "because"))
    assert a.directive in ex.V1_DIRECTIVES, (posture, a.directive)


def test_an_unmapped_posture_ships_and_SAYS_SO():
    """A posture the table doesn't cover must not be a silent `complete`.
    Shipping the turn is right; hiding that the table is behind the machine is
    not."""
    class Fake(str):
        value = "reframe_and_pivot"
    a = ex.decide(Decision(Fake("x"), "because"))
    assert a.directive == ex.COMPLETE
    assert "UNMAPPED POSTURE" in a.because


# ── the COMMUNICATE split ───────────────────────────────────────────────────

def test_budget_exit_ships_with_the_notice_and_complete_ships_clean():
    d = Decision(Posture.COMMUNICATE, "done")
    assert ex.decide(d, ExitMode.COMPLETE).directive == ex.COMPLETE
    assert ex.decide(d, ExitMode.BUDGET).directive == ex.FINALIZE


def test_CAPABILITY_never_takes_the_finalize_path():
    """THE load-bearing test of this module.

    v1's finalize path appends "these claims could not be verified" AND is the
    path that offers continuation. A CAPABILITY exit means no amount of waiting
    produces the answer. Routing it through finalize would attach a quality
    warning to a scope limit and then invite the person to wait for it.

    It is one enum row away at all times, which is why this is a test and not
    a comment.
    """
    a = ex.decide(Decision(Posture.COMMUNICATE, "no tool reaches this"),
                  ExitMode.CAPABILITY)
    assert a.directive == ex.COMPLETE
    assert a.directive != ex.FINALIZE


# ── the known-wrong prompt, declared rather than discovered ─────────────────

def test_the_prompt_mismatch_is_RECORDED_not_hidden():
    """EXPLORE and ALTERNATIVES get v1's remediation prompt because v1 has no
    other extend prompt at this branch. That is reproduced deliberately —
    fixing it would vary the prompt, and then a divergence could not be
    attributed to the decision. It must arrive on the row."""
    assert ex.decide(Decision(Posture.EXPLORE, "thin")).prompt_mismatch
    assert ex.decide(Decision(Posture.ALTERNATIVES, "stuck")).prompt_mismatch
    # NARROW is what the remediation prompt is actually for — no mismatch.
    assert ex.decide(Decision(Posture.NARROW, "named claims")).prompt_mismatch is None
    # and a non-extend action can never carry one
    assert ex.decide(Decision(Posture.COMMUNICATE, "done")).prompt_mismatch is None


# ── the vocabulary is react_loop's, checked against react_loop ──────────────

def test_the_three_directives_are_the_ones_react_loop_actually_branches_on():
    """Read from react_loop.py's own comparisons, over the AST.

    A copy of three strings in a test proves the copy matches itself. If the
    loop grows a fourth branch or renames one, this fails loudly instead of the
    executor emitting a directive nothing acts on — a producer with no
    consumer, dressed as a decision.
    """
    tree = ast.parse(pathlib.Path("app/pipeline/react_loop.py").read_text())
    compared = set()
    for n in ast.walk(tree):
        if not isinstance(n, ast.Compare) or not isinstance(n.left, ast.Name):
            continue
        if n.left.id != "_pp_directive":
            continue
        for c in n.comparators:
            if isinstance(c, ast.Constant) and isinstance(c.value, str):
                compared.add(c.value)
    assert compared, "no _pp_directive comparisons found — did the branch move?"
    assert compared <= set(ex.V1_DIRECTIVES), compared - set(ex.V1_DIRECTIVES)


def test_extend_is_the_only_directive_that_continues():
    assert ex.decide(Decision(Posture.EXPLORE, "x")).continues
    assert not ex.decide(Decision(Posture.COMMUNICATE, "x")).continues
    assert not ex.decide(Decision(Posture.COMMUNICATE, "x"), ExitMode.BUDGET).continues


def test_overran_and_gap_ride_through_to_the_action():
    """Decided-and-discarded is a defect this program has already shipped once:
    `overran` was computed and never reached a column."""
    d = Decision(Posture.EXPLORE, "one more", gap_targeted="G3", overran=True)
    a = ex.decide(d)
    assert a.overran is True and a.gap_targeted == "G3"


# ── the exit mode dominates EVERY posture ───────────────────────────────────

@pytest.mark.parametrize("posture", [Posture.EXPLORE, Posture.NARROW,
                                     Posture.VALIDATE, Posture.ALTERNATIVES])
def test_no_posture_can_extend_past_a_budget_or_capability_exit(posture):
    """Found by printing the whole table, not by reading the code.

    `EXPLORE + BUDGET -> extend` spends a round the budget says is not there.
    `EXPLORE + CAPABILITY -> extend` chases an answer no tool can reach. Both
    were live until the table was printed.

    "select() would never return EXPLORE when the budget is gone" is true today
    and is exactly the reasoning that produced FRAME.
    """
    d = Decision(posture, "one more would close it")
    for mode in (ExitMode.BUDGET, ExitMode.ERROR, ExitMode.CAPABILITY):
        a = ex.decide(d, mode)
        assert not a.continues, (posture, mode, a.directive)


@pytest.mark.parametrize("posture", list(Posture))
def test_capability_ships_clean_from_every_posture(posture):
    """The scope limit must never take the path that appends a quality warning
    and offers continuation — from ANY posture, not just COMMUNICATE."""
    a = ex.decide(Decision(posture, "no tool reaches this"), ExitMode.CAPABILITY)
    assert a.directive == ex.COMPLETE, (posture, a.directive)


def test_a_posture_exit_contradiction_is_recorded_on_the_row():
    """A disagreement inside my own module. Stopping wins, and the loser is
    written down — an overridden decision that leaves no trace is
    indistinguishable from one that was never made."""
    a = ex.decide(Decision(Posture.EXPLORE, "one more"), ExitMode.BUDGET)
    assert "overrode it" in a.because and "explore" in a.because


def test_the_two_branches_react_loop_names_are_both_still_there():
    """`complete` is react_loop's fall-through and has no comparison; extend
    and finalize are explicit. A rename of either must fail here rather than
    leave the executor emitting a directive nothing acts on."""
    tree = ast.parse(pathlib.Path("app/pipeline/react_loop.py").read_text())
    compared = {c.value for n in ast.walk(tree)
                if isinstance(n, ast.Compare) and isinstance(n.left, ast.Name)
                and n.left.id == "_pp_directive"
                for c in n.comparators
                if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    assert {ex.EXTEND, ex.FINALIZE} <= compared, compared
