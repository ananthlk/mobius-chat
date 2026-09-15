"""v2's next steps and follow-up questions must reach the screen.

Ananth, 2026-09-15: "we need a next steps and follow up questions which is
missing.. these are important.. USER first" and "lets start with producing
this every time".

TWO DEFECTS, and they compound:

  1. v2's integrator produced next_steps ONLY when the answer had open gaps —
     its prompt ended "nothing if the answer is complete". So a good turn
     returned an empty list, which is the turn where an onward route is most
     useful. Follow-up questions did not exist as a field at all.

  2. Whatever it produced reached nobody. integrate.py fills next_steps and
     next_questions_for_user from the COMPOSER'S answer card and never
     consulted v2's integration. Measured live (cid 36aa6171): the integrator
     reported `next_steps: ok` and the envelope carried no block.

A producer with no consumer, downstream of a producer that mostly produced
nothing.
"""

import ast
import inspect

from app.pipeline.v2 import integrator as I


def test_the_integration_carries_follow_up_questions():
    """The field must exist — it did not, which is why nothing downstream
    could have rendered one however well the model answered."""
    import dataclasses
    names = {f.name for f in dataclasses.fields(I.Integration)}
    assert "follow_up_questions" in names, (
        "Integration has no follow_up_questions field — the capability is "
        "absent, not broken")
    assert "next_steps" in names


def test_the_prompt_asks_for_both_and_does_not_excuse_an_empty_list():
    """THE DEFECT: the prompt ended "nothing if the answer is complete"."""
    sys_p, _ = I._next_steps_prompt("q", "a", ())
    assert '"next_steps"' in sys_p
    assert '"follow_up_questions"' in sys_p
    low = " ".join(sys_p.lower().split())
    assert "nothing if the answer is complete" not in low, (
        "the prompt still excuses an empty list on a complete answer — the "
        "turn where an onward route is MOST useful")
    assert "always" in low, "nothing tells the model both lists are required"


def test_every_parse_exit_returns_both_halves():
    """A bare () on the failure path unpacked to nothing at the call site, so
    a parse failure crashed the code that handles parse failures."""
    for raw in ("", "not json at all", "{}", '{"next_steps":["a"]}',
                '{"next_steps":["a"],"follow_up_questions":["q"]}'):
        out = I._parse_next_steps(raw, [])
        assert isinstance(out, tuple) and len(out) == 2, (
            f"{raw!r} returned {out!r} — every exit must carry both halves")
        assert all(isinstance(half, tuple) for half in out)


def test_integrate_reads_v2s_followups_when_the_card_supplied_none():
    """The consumer that did not exist. Asserts over the AST of run_integrate:
    v2_integration is read, and both lists are filled from it."""
    from app.stages import integrate as G

    tree = ast.parse(inspect.getsource(G))
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "run_integrate")
    src = ast.unparse(fn)
    assert "v2_integration" in src, (
        "run_integrate never reads v2_integration — v2's next steps and "
        "follow-ups reach no consumer")
    assert "follow_up_questions" in src, (
        "run_integrate never reads follow_up_questions from v2")


def test_the_card_still_wins_so_v1_is_untouched():
    """v1 has no v2_integration, and a card that supplied its own keeps them.
    The fill must be guarded on the list being EMPTY, not applied
    unconditionally — otherwise this changes the shared path."""
    from app.stages import integrate as G

    tree = ast.parse(inspect.getsource(G))
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "run_integrate")

    # Find the fill by what it DOES, not by the variable it happens to test —
    # my first version matched on "v2_integration" appearing in the `if` test,
    # but the code binds it to a local first, so the matcher found an
    # unrelated branch and the gate passed for the wrong reason.
    fills = [n for n in ast.walk(fn)
             if isinstance(n, ast.If)
             and "follow_up_questions" in ast.unparse(n)
             and "next_questions_for_user" in ast.unparse(n)]
    assert fills, "no branch fills the follow-up lists from v2"
    body = ast.unparse(fills[0])
    for name in ("next_steps", "next_questions_for_user"):
        assert f"if not {name}" in body, (
            f"{name} is filled from v2 without first checking the card left "
            "it empty — that would overwrite the composer on shared turns")
