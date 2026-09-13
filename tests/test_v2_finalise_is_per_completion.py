"""Finalise is once per COMPLETION, not once per turn.

🔴 MEASURED, cid 9c825ca5 (rev 01085-9fc), a follow-up question:

    round 1  complete=true   on 4 memory-carried facts, ZERO fresh retrieval
             -> finalise fired, spending the turn's ONE communicate round
    round 2  roles=communicate,validate — and react IGNORED the role:
             "LEARNED: the previous rag call failed ... I will call rag again"
    round 3  complete=true   no communicate round left
    round 4  complete=true   no communicate round left
    round 5  complete=true   no communicate round left
    -> 97.1s against a 95s promise, five rounds for a two-round question.

Two defects, one symptom. The premature finalise spent the round; the
once-per-TURN latch meant no later completion could end the turn.
"""
import ast
import pathlib

SRC = pathlib.Path("app/pipeline/react_loop.py").read_text()


def _code_only() -> str:
    """Comments stripped IN THE HELPER — six gates in this repo have matched
    the prose describing the code rather than the code."""
    return "\n".join(ln.split("#")[0] for ln in SRC.splitlines())


def test_reopening_rearms_the_communicate_round():
    """THE LATCH. Without this, rounds 3-5 each said complete=true and none
    could end the turn."""
    code = _code_only()
    assert "ctx._v2_finalised = False" in code, (
        "nothing ever re-arms finalise — a completion that did not hold "
        "permanently costs the turn its ability to end")
    rearm = code.index("ctx._v2_finalised = False")
    guard = code.rindex("not is_complete", 0, rearm)
    assert guard > 0, "the re-arm must be guarded on react having REOPENED"


def test_the_rearm_cannot_fire_on_a_completion():
    """A re-arm on a COMPLETE round would loop: finalise, complete, re-arm,
    finalise. The guard is the whole safety of this change."""
    tree = ast.parse(SRC)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        for stmt in node.body:
            if (isinstance(stmt, ast.Assign)
                    and any(isinstance(t, ast.Attribute)
                            and t.attr == "_v2_finalised" for t in stmt.targets)
                    and isinstance(stmt.value, ast.Constant)
                    and stmt.value.value is False):
                found.append(ast.dump(node.test))
    assert found, "no guarded assignment of _v2_finalised = False"
    for test in found:
        assert "is_complete" in test, (
            "the re-arm is not conditioned on is_complete — it could fire on "
            "a completed round and loop")


def test_a_round_that_learned_nothing_does_not_spend_the_communicate_round():
    """THE PREMATURE FINALISE. Round 1's complete=true rested entirely on
    carried memory. Requiring `_earned` stops the turn spending its one
    delivery round before there is anything new to deliver."""
    code = _code_only()
    assert "_earned" in code
    # Anchored on _has_fresh's DEFINITION, not on _earned's use — the first
    # version windowed below the definition and failed on a correct fix.
    i = code.index("_has_fresh =")
    window = code[i:i + 500]
    assert "_v2_preloaded" in window and "tool_results" in window, (
        "freshness must consider BOTH preloaded evidence and this turn's tool "
        "results — either one is real retrieval")
    # ...and must still allow a memory-only turn with nothing outstanding.
    assert "gaps" in window, (
        "a completion with no open gaps must still be allowed to finalise, or "
        "a question answerable from memory can never end")


def test_it_reads_a_reference_that_is_always_bound():
    """_v2_resp is assigned inside a conditional 500 lines above this decision.
    Reading it here is the UnboundLocalError shape that has produced five
    separate bugs in this file tonight — and it would raise on exactly the
    paths where the contract failed to parse."""
    code = _code_only()
    i = code.index("_earned =")
    window = code[max(0, i - 600):i + 200]
    assert "_v2_last_contract" in window
    assert "_v2_resp" not in window, (
        "reads a name bound inside a conditional far above; use the ctx-carried "
        "contract")


def test_the_budget_guards_are_untouched():
    """The re-arm must not become an unbounded loop wearing a role name: the
    round and ceiling checks are what bound it."""
    code = _code_only()
    i = code.index("_earned")
    window = code[i:i + 600]
    assert "rn < max_it" in window
    assert "react_hard_ceiling_s" in window


def test_the_earned_guard_is_actually_IN_the_finalise_condition():
    """🔴 CAUGHT BY A MUTATION CHECK THAT REFUSED TO FAIL.

    Deleting `and _earned` from the finalise condition left all five tests
    green: they asserted the variable was DEFINED with the right ingredients
    and never that it was USED. A gate with no caller — in the test for a gate
    with no caller.

    Asserts on the parsed condition, so the guard cannot be defined and then
    quietly dropped from the branch it exists for.
    """
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.dump(node.test)
        if "_v2_finalised" in test_src and "is_complete" in test_src \
                and "react_hard_ceiling_s" in test_src:
            assert "_earned" in test_src, (
                "the finalise condition no longer consults _earned — a round "
                "that learned nothing can spend the turn's communicate round "
                "again")
            return
    raise AssertionError("could not find the finalise condition")
