"""Preloaded evidence is never empty, so "nothing retrieved" can be rendered
as "the document does not say it".

`rag` is in preload's ALWAYS_PRELOAD: every v2 turn opens holding passages.
Deep Research's engine refuses to run its extractor on EMPTY evidence,
precisely so a failed search cannot be reported as a finding about the world.
A preload removes that guard's trigger without removing the failure.

mobius_contracts...WORLD_CLAIMS is empty: no tool outcome licenses telling a
person "not found". This loop has no `absent`-judgement layer, so it may not
make that claim, and the prompt must say so.
"""
import ast
import pathlib

from app.pipeline.v2 import posture_prompts as P

SRC = pathlib.Path(P.__file__).read_text()
SENTINEL = "Silence is a reason to look, never a reason to answer."


def flat(text: str) -> str:
    """Prompt text is hard-wrapped, so every phrase in it can fall across a
    line break. Matching raw text finds nothing and reports it as a missing
    rule -- a defect I have now shipped twice. Compare on normalised
    whitespace, always."""
    return " ".join((text or "").split())


def test_every_posture_that_reads_preloaded_evidence_carries_the_rule():
    """FRAME and EXPLORE both rule on pre-round evidence and both can end the
    turn. A rule on only one of them is a rule with a way around it."""
    for name in ("FRAME", "EXPLORE"):
        assert SENTINEL in flat(getattr(P, name)), (
            f"{name} reads pre-round evidence but never learns that its "
            f"silence is not absence"
        )


def test_the_rule_is_declared_once_not_copied_into_each_posture():
    """The drift this module exists to prevent.

    Parse the source and count STRING LITERALS containing the sentinel. Two
    means someone pasted it into a second posture, and the copies will part
    company. One means both postures render a single declaration.
    """
    tree = ast.parse(SRC)
    literals = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and SENTINEL in flat(n.value)
    ]
    assert len(literals) == 1, (
        f"the rule appears in {len(literals)} string literals; it must be "
        f"declared once and rendered in"
    )


def test_the_rule_names_the_claims_it_forbids():
    """A rule the model cannot apply is decoration. It has to carry the actual
    phrases, because those are what the model is about to write."""
    rule = P.SILENCE_IS_NOT_ABSENCE
    for phrase in ("is not specified", "is not mentioned", "the documents do not say"):
        assert phrase in flat(rule), f"forbidden claim {phrase!r} is not named"


def test_the_rule_says_what_to_do_instead():
    """A prohibition with no alternative gets routed around: the model still
    has to finish the turn."""
    assert "GAP" in flat(P.SILENCE_IS_NOT_ABSENCE)
    assert "plan a tool" in flat(P.SILENCE_IS_NOT_ABSENCE)


def test_the_contract_this_rule_enforces_still_forbids_world_claims():
    """If WORLD_CLAIMS ever becomes non-empty, this prompt rule is no longer
    the policy and must be revisited rather than silently contradicting it."""
    from mobius_contracts.taxonomies import tool_outcome
    assert tool_outcome.WORLD_CLAIMS == frozenset(), (
        "WORLD_CLAIMS gained a member — a tool outcome now licenses 'not "
        "found', and this prompt rule contradicts the contract"
    )
