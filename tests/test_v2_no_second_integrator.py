"""The v1 integrator is not for v2.

Ananth: "why is it not as simple as integrator v1 is not for v2".

🔴 TRACED IN FULL, cid 3628f1f2 (rev 01092-gpk):

    +73.1s  v2 integrator done — assemble ok, critique/next_steps skipped
    +73.2s  [integrate] integrator_mode=P
    +73.3s  THREE vertex calls at once: 35,890 / 27,885 / 29,656 chars
    +80.1s    returned  6.8s
    +85.3s    returned 12.0s
    +97.9s    returned 24.6s     <- the card waits for the slowest
    +98.2s  AnswerCard

25 seconds AFTER all reasoning ended, because the legacy parallel integrator
ran a second time over an answer v2 had already assembled.

It was REACHED because the deterministic pass is refused when gaps are open
and that turn had gaps=6 — for v1 an open gap means the answer is unfinished,
for v2 the gap ledger is a feature and role_communicate is instructed to name
what is still open. So v2 took the slow path essentially always.
"""
import ast
import pathlib

SRC = pathlib.Path("app/stages/integrate.py").read_text()


def _deterministic_branch():
    """The `if` that chooses the deterministic card, from the parsed source."""
    for node in ast.walk(ast.parse(SRC)):
        if isinstance(node, ast.If) and "_dyn_enrich_used" in ast.dump(node):
            if "_integ_path" in ast.dump(node.test):
                return ast.dump(node.test)
    raise AssertionError("could not find the deterministic-path branch")


def test_the_v2_arm_takes_the_deterministic_card():
    """THE RULE, stated plainly: v2 does not run v1's integrator."""
    test = _deterministic_branch()
    assert "_v2_arm" in test, (
        "the v2 arm still has to satisfy v1's sufficiency formula to avoid "
        "three blocking LLM calls it does not need")


def test_v1s_APPROVED_FORMULA_IS_STILL_CONSULTED_FOR_v1():
    """_is_sufficient_for_deterministic_pass carries "do not loosen without a
    new approval" (Chat Master, Task #76). Not loosened — just not consulted
    for an arm it was not written for. The v1 path must still reach it."""
    test = _deterministic_branch()
    assert "_is_sufficient_for_deterministic_pass" in test, (
        "v1 no longer consults its own formula — that IS loosening it")


def test_the_v2_arm_is_read_from_the_orchestrator_version():
    """Not inferred from a posture, a round count, or the presence of a v2
    field — any of which can be true on a v1 turn."""
    i = SRC.index("_v2_arm =")
    assert "orchestrator_version" in SRC[i:i + 160]


def test_the_v1_formula_itself_is_unmodified():
    """The one thing I must not do while fixing this: change the formula. It
    lives in react_loop and this change must not have touched it."""
    formula = pathlib.Path("app/pipeline/react_loop.py").read_text()
    j = formula.index("def _is_sufficient_for_deterministic_pass")
    body = formula[j:j + 2600]
    assert "v2_integration" not in body, (
        "the approved formula now reads a v2 verdict — that is the loosening "
        "its header forbids")
    assert "orchestrator_version" not in body
