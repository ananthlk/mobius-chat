"""v2's own round loop.

Ananth, 2026-09-11: "so you don't have your own loop — why not start that, so
that we can really have a real A/B where both can complete."
"""

import ast
import pathlib

import pytest

from app.pipeline.v2 import loop as L
from app.pipeline.v2 import posture as P


def _src():
    return pathlib.Path("app/pipeline/v2/loop.py").read_text()


def _code():
    """Source with comments stripped. Six gates of mine today matched my own
    prose; the general fix is to stop handing the assertion prose."""
    import re
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in _src().splitlines())


# ── the reason this loop exists ─────────────────────────────────────────────

def test_the_model_proposing_complete_is_an_INPUT_not_the_decision():
    """THE point of owning the loop.

    In v1, `is_complete` ends the turn — which is why the three-payer question
    stopped after round 2 with 62.6s of a 95s promise left, two gaps open, and
    v2 saying EXPLORE into a void. Here the proposal is recorded and the
    governor gets the next round to disagree.
    """
    code = _code()
    i = code.index('decision_json.get("is_complete")')
    block = code[i:i + 900]
    assert "continue" in block, "is_complete still ends the turn immediately"
    # it may only break when the model itself reports nothing left open
    assert "_no_gaps_left" in block


def test_the_loop_can_EXTEND_not_only_stop():
    """The asymmetry this replaces: the framing hook could stop a turn and
    never extend one — a brake with no accelerator. A loop that only ever
    shortens is not a governor, it is a timeout."""
    code = _code()
    # the round counter advances on a CONTINUE decision, not only on a stop
    assert "if not action.continues:" in code
    i = code.index("if not action.continues:")
    after = code[i:i + 400]
    assert "break" in after
    # ...and there is a path that goes round again
    assert "extensions_used += 1" in code


# ── the fuses, because a decision core defect already ran 98 rounds ─────────

def test_the_loop_has_a_hard_round_fuse():
    """On 2026-09-11 a decision-core defect produced 98 rounds on one turn and
    the thing that stopped it was a person watching. The executor's
    MAX_V2_EXTENSIONS bounds extensions; this bounds the LOOP."""
    assert L.MAX_ROUNDS_HARD <= 12
    assert "min(react_max_iterations_for_mode(mode), MAX_ROUNDS_HARD)" in _code()


def test_unusable_rounds_end_the_turn():
    """A loop that cannot tell "the model is stuck" from "keep trying" is how
    a runaway starts."""
    assert L.MAX_UNUSABLE_ROUNDS == 2
    code = _code()
    assert "unusable >= MAX_UNUSABLE_ROUNDS" in code


# ── one terminal ────────────────────────────────────────────────────────────

def test_every_exit_goes_through_ONE_publish():
    """Ananth: "there should just be one way to communicate out even on early
    exit, I have had too much trouble with that." Asserted over the AST: one
    _finalize_response call, and it is NOT inside the loop."""
    tree = ast.parse(_src())
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "run_react_v2")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_finalize_response"]
    assert len(calls) == 1, f"{len(calls)} publish sites — early exits will diverge"
    loops = [n for n in ast.walk(fn) if isinstance(n, (ast.While, ast.For))]
    for lp in loops:
        assert not any(c in ast.walk(lp) for c in calls), \
            "a publish inside the loop is a second terminal"


# ── holding everything else constant ────────────────────────────────────────

def test_the_loop_REUSES_reacts_prompt_model_and_tools():
    """The experiment is "the decision loop varies and nothing else does". A
    second prompt builder, model call or tool dispatcher would make every
    divergence unattributable."""
    code = _code()
    for shared in ("_react_reasoning_system", "build_reasoning_context",
                   "_call_llm_json", "_execute_tool_with_retry",
                   "_finalize_response"):
        assert shared in code, f"v2 does not reuse {shared}"
    # ...and does not roll its own
    for forbidden in ("import httpx", "requests.post", "openai", "genai"):
        assert forbidden not in code, f"v2's loop calls a model directly ({forbidden})"


def test_state_comes_from_the_SAME_builder_the_observer_uses():
    """Two state builders would drift, and the first place they drifted would
    be the place the comparison mattered."""
    assert "sh.state_from_ctx(" in _code()


def test_v1_fields_are_NULL_not_guessed():
    """v1 did not run this turn. A fabricated v1_directive would make the
    comparison v2-against-a-prediction-of-v1 rather than against v1."""
    code = _code()
    assert '"v1_directive": None' in code
    assert '"v1_reason": None' in code


def test_the_governor_never_writes_the_answer():
    """It decides WHEN to stop, never WHAT to say. If it stops a turn the
    answer is the model's own running answer from the evidence it had."""
    code = _code()
    assert "_best_running_answer" in code
    fn = next(f for f in ast.walk(ast.parse(_src()))
              if isinstance(f, ast.FunctionDef) and f.name == "_best_running_answer")
    src = ast.unparse(fn)
    assert "running_answer" in src
    # no literal apology/answer strings authored here
    assert "I couldn't" not in src and "Sorry" not in src


def test_the_signature_matches_run_react():
    """The orchestrator routes to one or the other without knowing which it
    called; a different signature would make the routing site know."""
    import inspect
    from app.pipeline.react_loop import run_react
    assert (list(inspect.signature(L.run_react_v2).parameters)
            == list(inspect.signature(run_react).parameters))


# ── routing: one turn, one loop ─────────────────────────────────────────────

def _orch():
    return pathlib.Path("app/pipeline/orchestrator.py").read_text()


def test_a_turn_runs_ONE_loop_never_both():
    """Two loops on one turn would share ctx, tool_results and the publish
    path — the two-writer defect at maximum scale. The A/B harness forks by
    running TWO TURNS, which is a different thing entirely."""
    src = _orch()
    i = src.index("ROUTE TO A LOOP")
    block = src[i:i + 1800]
    import re
    code = "\n".join(re.sub(r"#.*$", "", ln) for ln in block.splitlines())
    # exactly one invocation, through a selected function
    assert code.count("_loop_fn(ctx, emitter=on_thinking)") == 1
    assert "run_react(ctx, emitter=on_thinking)" not in code, \
        "v1's loop is still called unconditionally alongside the selection"


def test_the_own_loop_flag_is_SEPARATE_from_the_arm_split():
    """A v2 turn with the flag off still runs v1's loop with v2's substituted
    decision — the behaviour verified all day. Coupling them would mean
    turning on the arm split silently changed the loop too."""
    src = _orch()
    assert 'os.environ.get("MOBIUS_V2_OWN_LOOP"' in src
    assert 'getattr(ctx, "orchestrator_version", "v1") == "v2"' in src
    i = src.index("_v2_own_loop = (")
    assert "MOBIUS_V2_PCT" not in src[i:i + 400], \
        "the loop choice reads the arm-split percentage directly"


def test_the_own_loop_defaults_OFF_even_in_dev():
    """New and unproven on live traffic. Turning it on is a deliberate act,
    and the deploy allowlist must carry it or the lever cannot be pulled at
    all — SET_ENV_VARS is an allowlist, and that has silently disabled a v2
    flag once already today."""
    env = pathlib.Path("deploy/dev.env").read_text()
    assert "\nMOBIUS_V2_OWN_LOOP=\n" in env or "\nMOBIUS_V2_OWN_LOOP=" in env
    assert "MOBIUS_V2_OWN_LOOP=1" not in env, "it is on by default in dev"
    sh = pathlib.Path("scripts/deploy.sh").read_text()
    assert "MOBIUS_V2_OWN_LOOP=" in sh, "absent from the allowlist — cannot be enabled"
