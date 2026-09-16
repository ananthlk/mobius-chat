"""Three defects from one pinned turn (cid ee783367).

Ananth, reading the trace: "a) we should have preloaded both the appeals tools
2) frame should have said that is perfect.. 3) why the react is not running
both the tools in parallel".
"""

import ast
import inspect

from app.pipeline.v2 import loop as L
from app.pipeline.v2.posture_prompts import EXPLORE


# ── (a) the arguments the manifest already filled ───────────────────────────

def test_preload_passes_the_filled_inputs_through():
    """THE DEFECT. I passed plan() the tool KEYS and dropped ToolOffer.inputs.
    With no inputs supplied, preload.execute falls back to
    {"query": question} (preload.py:825), so both appeals tools were called
    with the whole sentence and rejected before calling:

        ⊘ appeals_get_playbook  missing required ['payor']; unknown ['query']
        ⊘ appeals_lookup_rules  missing required ['carc'];  unknown ['query']

    estimate had ALREADY resolved them — payor='sunshine health', carc='22',
    inputs_status=fillable — and react called them with exactly those values
    two and three rounds later, successfully. The arguments existed at round
    zero and my code discarded them.
    """
    tree = ast.parse(inspect.getsource(L))
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "run_react_v2")

    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and "plan" in ast.unparse(n.func)
             and "_v2pre" in ast.unparse(n.func)]
    assert calls, "preload.plan is never called"

    # the MAIN plan call (the one built from the offer) must carry inputs
    with_inputs = [c for c in calls
                   if any(k.arg == "inputs" for k in c.keywords)]
    assert with_inputs, (
        "preload.plan is called without inputs= — the filled arguments are "
        "dropped and execute falls back to {'query': question}")

    execs = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and "execute" in ast.unparse(n.func)]
    assert any(k.arg == "inputs" for c in execs for k in c.keywords), (
        "preload.execute is called without inputs=")


def test_the_inputs_map_is_built_from_the_offer():
    """Built from ToolOffer.inputs, not re-derived. Two extractors of the same
    entities would be two authors of the argument list."""
    src = inspect.getsource(L.run_react_v2)
    assert "_offer_inputs" in src
    assert "t.inputs" in src


# ── (b) permission to finish ────────────────────────────────────────────────

def test_the_exploring_round_may_stop_when_the_evidence_already_answers():
    """Ananth: "frame should have said that is perfect".

    FRAME itself stays unreachable ON PURPOSE — its old trigger was
    `round_index <= 1`, which produced 50 of 88 shadow divergences, every one
    at round 1, and posture.py says it plainly: "a posture whose trigger is a
    round number is not a posture, it is a label for a position". Resurrecting
    that trigger would reintroduce the measured defect.

    What Ananth is asking for is PERMISSION TO FINISH — his own earlier ask,
    "allow it permission to find it has everything and move to communicate" —
    and that belongs in the round that actually runs.
    """
    # WHITESPACE-NORMALISED. My first version matched the raw string and
    # failed on "SAY\nSO AND STOP" — a phrase broken by a line wrap. I fixed
    # that exact defect this morning (be874c8, "the prompt gate matched across
    # a line break") and reproduced it within hours. Normalise, always.
    low = " ".join(EXPLORE.lower().split())
    assert "already" in low and "settles" in low
    assert "say so and stop" in low, (
        "the exploring round has no permission to finish — it must spend a "
        "round even when preload already answered the question")
    assert "do not manufacture a gap" in low


def test_frame_stays_unreachable_and_that_is_deliberate():
    """A guard on the guard: if someone resurrects FRAME on a positional
    trigger, the measured reason it was removed should stop them."""
    from app.pipeline.v2 import posture as P

    tree = ast.parse(inspect.getsource(P))
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "select")
    returned = {n.attr for n in ast.walk(fn)
                if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name) and n.value.id == "Posture"}
    assert "FRAME" not in returned, (
        "select() returns FRAME again — check it is not on a round-number "
        "trigger, which is what produced 50 of 88 divergences")


# ── (c) parallel tools ──────────────────────────────────────────────────────

def test_explore_asks_for_every_tool_at_once():
    """THE DEFECT. The loop has always read a `tools` LIST and executed them
    together at the top of a round — my own comment said it was "the loop being
    ready for it rather than the model being asked for it". So react named one
    tool per round: appeals_lookup_rules on round 1, appeals_get_playbook on
    round 2, two rounds for two independent lookups the preload could have run
    together."""
    low = " ".join(EXPLORE.lower().split())
    assert "same round" in low and "parallel" in low, (
        "EXPLORE does not tell the model that tools named together run in one "
        "round, so it will keep naming them one at a time")
    assert '"tools"' in EXPLORE, "the plural key is never named"


def test_the_loop_really_executes_a_list():
    """The other half — the prompt asking for plural is useless if the loop
    only runs the first."""
    src = inspect.getsource(L.run_react_v2)
    assert 'decision_json.get("tools")' in src
    assert "for _t, _in in pending" in src


# ── round 1 must see what preload fetched ───────────────────────────────────

def test_the_round_list_is_seeded_from_preload():
    """THE DEFECT, measured on pinned turn c11ea4af. preload ran rag (14
    sources), appeals_get_playbook and appeals_lookup_rules SUCCESSFULLY, and
    round 1 reported "tools in hand: none · evidence: 637 chars".

    build_reasoning_context reads ONLY the list passed to it
    (react/prompts.py:913), and mine was initialised `= []`. Three consequences
    from one line: the permission-to-finish had nothing to read; the governor
    chose `nothing_worth_buying` at 12s of a 31s promise while holding fourteen
    unread sources; and round 3 re-requested a tool preload had already run.

    v1 seeds its list at react_loop.py:6155 and I did not carry it across.
    """
    # OVER THE AST, NOT THE TEXT. My first version searched the source for
    # "tool_results: list[dict] =" and matched MY OWN COMMENT quoting v1's
    # line — the read-prose-not-program defect, again. The AST sees only the
    # program.
    tree = ast.parse(inspect.getsource(L))
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "run_react_v2")
    inits = [n for n in ast.walk(fn)
             if isinstance(n, ast.AnnAssign)
             and isinstance(n.target, ast.Name)
             and n.target.id == "tool_results"]
    assert inits, "tool_results is never initialised"
    value = ast.unparse(inits[0].value)
    assert value != "[]", (
        "the round list starts empty — round 1 cannot see preloaded evidence")
    assert "seed_tool_results" in value, (
        f"the round list is initialised to {value!r}, not seeded from "
        "ctx.seed_tool_results — the channel v1 uses and "
        "build_reasoning_context renders")


def test_preload_results_are_published_on_the_shared_seed_channel():
    """Seeded from the SAME ctx.seed_tool_results v1 builds, so the two loops
    cannot disagree about what round 1 was handed."""
    src = inspect.getsource(L.run_react_v2)
    assert "ctx.seed_tool_results" in src
    assert '"round_virtual": 0' in src, (
        "a preloaded result must not credit round 1 with fetching it")


def test_a_seeded_result_is_TEXT_not_a_dict():
    """`result` is a string by convention — nine readers call .strip() on it,
    and a raw dict there killed a live turn this morning."""
    assert L._payload_as_text({"deadline_appeal_days": 90}) == '{"deadline_appeal_days": 90}'
    assert L._payload_as_text("already text") == "already text"
    assert L._payload_as_text(None) == ""
