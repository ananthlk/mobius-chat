"""BUDGET is the label for "work remains", not a terminal.

exit_mode() returns BUDGET as its FALL-THROUGH: material gaps exist and are
not all exhausted. The executor treated it as terminal, so the ordinary state
of an unfinished multi-part question meant STOP -- 23 of 47 stops in 24h, 25
of which suppressed a `rag` call the model had already chosen, and the
accelerator fired 0 times in 417 decisions because this branch returned first.

Live consequence (2026-09-12): Molina answered, Sunshine and UnitedHealthcare
never asked, answer said "not available in the provided documents".
"""
import ast
import inspect

from app.pipeline.v2.executor import MAX_V2_EXTENSIONS, decide
from app.pipeline.v2.posture import Decision, ExitMode, Posture


def _explore(**kw):
    sig = inspect.signature(Decision)
    base = dict(posture=Posture.EXPLORE, because="closing S384280",
                gap_targeted="S384280")
    base.update(kw)
    return Decision(**{k: v for k, v in base.items() if k in sig.parameters})


def test_budget_with_budget_left_continues():
    """THE REGRESSION. The posture wants a round; spendable() says yes;
    BUDGET is only the name for 'gaps remain'. Nothing to override."""
    act = decide(_explore(), ExitMode.BUDGET, affordable=True)
    assert act.continues, "BUDGET + affordable must not stop the turn"


def test_budget_without_budget_stops():
    act = decide(_explore(), ExitMode.BUDGET, affordable=False)
    assert not act.continues
    assert "budget exhausted" in act.because


def test_unsupplied_affordability_stops_and_says_so():
    """Unknown must not silently unlock spending -- that is how the 98-round
    runaway began -- and must not masquerade as a judgement either."""
    act = decide(_explore(), ExitMode.BUDGET)
    assert not act.continues
    assert "NOT SUPPLIED" in act.because


def test_capability_still_terminal_even_when_affordable():
    """A gap nothing can reach is not bought by another round."""
    act = decide(_explore(), ExitMode.CAPABILITY, affordable=True)
    assert not act.continues


def test_error_still_terminal_even_when_affordable():
    act = decide(_explore(), ExitMode.ERROR, affordable=True)
    assert not act.continues


def test_fuse_beats_affordability():
    """The ceiling is checked before anything else, including a caller that
    says the budget is fine. Overruling a finish is how the runaway began."""
    act = decide(_explore(), ExitMode.BUDGET, extensions_used=MAX_V2_EXTENSIONS,
                 affordable=True)
    assert not act.continues
    assert "CEILING" in act.because


def test_every_call_site_supplies_affordability():
    """AST, not grep: a call site that omits `affordable` silently reverts to
    stopping, and the only symptom is a turn that ends early -- the exact
    failure this change fixes. Parsed, because a substring search over source
    matches the comments describing it (six instances in one day)."""
    import app.pipeline.react_loop as rl
    import app.pipeline.v2.loop as v2l

    missing = []
    for mod in (rl, v2l):
        tree = ast.parse(inspect.getsource(mod))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (isinstance(f, ast.Attribute) and f.attr == "decide"):
                continue
            if not any(k.arg == "affordable" for k in node.keywords):
                missing.append(f"{mod.__name__}:{node.lineno}")
    assert not missing, f"decide() called without affordable at: {missing}"


def test_the_gate_can_see_a_call_site_at_all():
    """Guard against the assertion above passing over zero call sites -- a
    test that inspects nothing passes forever."""
    import app.pipeline.v2.loop as v2l
    tree = ast.parse(inspect.getsource(v2l))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "decide"]
    assert calls, "found no decide() call sites; the gate above proves nothing"
