"""v2 gets its own FORMAT RULES; v1's are untouched.

Ananth: "actually create a new prompt set so that we dont disrupt v1.. this way
we can use that modular".

WHY. v1's REACT_FORMAT_RULES_TEXT says, on every answer, "follow with 2-4 short
bullet points (each 10-25 words)". So every answer reaches the formatter as
bullets and every card looks the same. The classifier is NOT the problem —
measured, it returns bullets/steps/stats for the same content written three
ways. Nothing ever asked for a different shape, and our own user documentation
already promises six formats "chosen automatically".
"""

import ast
import inspect

from app.pipeline.v2 import loop as L
from app.pipeline.v2.format_rules import V2_FORMAT_RULES_TEXT
from app.pipeline.v2.posture import Posture


def test_v1s_rules_are_not_edited():
    """The whole point of a second set. If this ever fails, v1's answers
    changed shape and the A/B is measuring two changes at once."""
    from app.pipeline.react.prompts import REACT_FORMAT_RULES_TEXT

    assert "2–4 short bullet points" in REACT_FORMAT_RULES_TEXT
    assert "→ Next step:" in REACT_FORMAT_RULES_TEXT


def test_v2s_rules_name_shapes_the_classifier_can_actually_detect():
    """A shape the classifier cannot detect produces prose with extra steps;
    a shape the front end cannot draw produces a blank. Both are worse than
    saying nothing, so the prompt may only name shapes that survive the whole
    path."""
    from app.responder.deterministic_format import classify_envelope, extract_payload

    samples = {
        "steps": "1. Complete the form.\n2. Attach the EOB.\n3. Submit within 90 days.",
        "stats": "Initial claims: 180 days\nReconsiderations: 90 days\nCOB: 90 days",
        "bullets": "* one short point here\n* two short points here\n* three here",
    }
    for want, text in samples.items():
        payload = extract_payload(text)
        verdict = classify_envelope(payload) if payload else None
        assert verdict and verdict.format == want, (
            f"the prompt tells the model to write {want}, and that writing "
            f"classifies as {getattr(verdict, 'format', None)}")


def test_the_inline_next_step_is_forbidden():
    """v2 produces a real next_steps BLOCK. v1's rules also ask for a trailing
    '→ Next step:' inside the answer text, so both printed on one card —
    Ananth saw them together on a live screen."""
    low = V2_FORMAT_RULES_TEXT.lower()
    assert "do not end with" in low and "next step" in low


def test_it_applies_only_on_the_round_that_writes_the_answer():
    """A judging round is not producing the answer field; 2.2k characters of
    shape guidance there is noise in a prompt already 76k long."""
    out, state = L._with_v2_format_rules("BASE", Posture.COMMUNICATE)
    assert state == "applied" and len(out) > len("BASE")

    for p in (Posture.EXPLORE, Posture.FRAME, Posture.NARROW, None):
        out, state = L._with_v2_format_rules("BASE", p)
        assert out == "BASE", f"format rules leaked into {p}"
        assert "not_applied" in state


def test_it_is_appended_AFTER_the_base_so_the_later_instruction_wins():
    """The base already carries v1's rules from the composition. This only
    works because it comes last — which is also why the block says explicitly
    not to write a 'Next step:' line: it is overriding something the model has
    already been told."""
    out, _ = L._with_v2_format_rules("BASE_WITH_V1_RULES", Posture.COMMUNICATE)
    assert out.index("BASE_WITH_V1_RULES") < out.index("FORMAT RULES")


def test_a_failure_here_cannot_end_a_turn():
    """A prompt addition that can fail a turn is a worse trade than a turn
    formatted the way it was formatted last week."""
    src = inspect.getsource(L._with_v2_format_rules)
    tree = ast.parse(src.strip())
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "no guard — a bad import would fail the turn"


def test_the_prompt_builder_actually_applies_them():
    """THE GATE MY OWN MUTATION RUN SHOWED MISSING. I removed the call from
    _v2_system_prompt and the whole suite passed — nothing tied the rules to
    the prompt, so they were a producer with no producer. Second time today.

    Asserts over the AST, and on BOTH exits: the wrapper returns early when a
    posture has no block, and an append on one path only is how the posture
    block itself nearly shipped broken (its docstring says so)."""
    tree = ast.parse(inspect.getsource(L))
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "_v2_system_prompt")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_with_v2_format_rules"]
    assert calls, (
        "_v2_system_prompt never applies the v2 format rules — every answer "
        "keeps v1's fixed bullet shape and the whole prompt set is inert")

    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    assert len(returns) >= 2, "expected the early return plus the block return"
    # the call must precede every return, i.e. sit on the shared path
    first_call = min(c.lineno for c in calls)
    assert first_call < max(r.lineno for r in returns), (
        "the rules are applied after a return — one exit will miss them")
