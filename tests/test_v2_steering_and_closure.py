"""The governor steers, and reads per-gap closure back.

THE DEFECT (2026-09-12): the governor selected a gap every round and wrote it
to turn_rounds.gap_targeted. Nothing on the prompt path read it. On Ananth's
three-payer question the model searched Molina only, and the answer said the
other two were "not available in the provided documents" -- never asked.

Spec: docs/governor-react-closure-contract.md
"""
import ast
import inspect
import pathlib

from app.pipeline.v2.instruct import HEADER, governor_block
from app.pipeline.v2.posture import (
    Attempt, Band, Closure, Gap, Trend, closure_band, closure_trend,
    is_closing, latest_closure,
)
from app.pipeline.v2.shadow import _closure_reports

SUN = "Sunshine Health care management philosophy"
UHC = "UnitedHealthcare care management philosophy"


def _gap(text, gid="S1", attempts=(), closures=()):
    return Gap(gap_id=gid, text=text, opened_round=1,
               attempted_by=tuple(attempts), closure_by=tuple(closures))


# ── bands: no false precision ───────────────────────────────────────────────

def test_bands_not_raw_numbers():
    assert closure_band(0) is Band.NONE
    assert closure_band(60) is Band.MOST
    assert closure_band(95) is Band.CLOSED


def test_unreported_closure_is_not_zero_closure():
    """"react said it found nothing" and "react was not asked" carry opposite
    advice about continuing. Collapsing them is the could-not-check /
    checked-false defect."""
    assert closure_band(None) is Band.UNKNOWN
    assert closure_band(None) is not Band.NONE


def test_noise_inside_a_band_is_not_a_trend():
    """62 -> 68 must not read as progress: closure is a self-report and a rule
    that separates those is tuned against its noise."""
    g = _gap(SUN, closures=(Closure(1, 62), Closure(2, 68)))
    assert closure_trend(g) is Trend.FLAT
    rising = _gap(SUN, closures=(Closure(1, 10), Closure(2, 70)))
    assert closure_trend(rising) is Trend.INCREASING


def test_one_observation_is_never_a_direction():
    """A single report describes a state. Spending rounds on an imagined trend
    is the expensive mistake here."""
    assert closure_trend(_gap(SUN, closures=(Closure(1, 80),))) is Trend.FLAT
    assert not is_closing(_gap(SUN, closures=(Closure(1, 80),)))


# ── the cross-check: closure is a CLAIM ─────────────────────────────────────

def test_claimed_progress_without_evidence_is_recorded_but_unsupported():
    got = _closure_reports({"gaps": [{"text": SUN, "closure": 70}]},
                           round_index=2, evidence_arrived=False)
    assert got[SUN].value == 70, "the claim is recorded, never deleted"
    assert got[SUN].supported is False


def test_an_unsupported_rise_does_not_advance_the_band():
    g = _gap(SUN, closures=(Closure(1, 0, supported=True),
                            Closure(2, 90, supported=False)))
    assert closure_trend(g) is Trend.FLAT, "an uncorroborated claim moved the band"


def test_claiming_zero_needs_no_corroboration():
    """"I found nothing" is self-consistent with no evidence arriving."""
    got = _closure_reports({"gaps": [{"text": SUN, "closure": 0}]},
                           round_index=2, evidence_arrived=False)
    assert got[SUN].supported is True


def test_a_bool_is_not_a_percentage():
    got = _closure_reports({"gaps": [{"text": SUN, "closure": True}]},
                           round_index=1, evidence_arrived=True)
    assert got[SUN].value is None


def test_absent_gaps_field_changes_nothing():
    """Ships dark: until the field exists, today's behaviour exactly."""
    assert _closure_reports({}, 1, True) == {}
    assert _closure_reports({"gaps": "not a list"}, 1, True) == {}


# ── the block ───────────────────────────────────────────────────────────────

def test_the_block_names_exactly_one_gap_to_close():
    sun, uhc = _gap(SUN, "S384280"), _gap(UHC, "S7c9bc3")
    block = governor_block(sun, remaining=(sun, uhc))
    assert block.count("close this gap:") == 1
    assert SUN in block
    # The others are named as REMAINING, never as things to answer now --
    # otherwise the block restates the question, which is what the model
    # already had when it covered one payer and stopped.
    assert UHC in block.split("still open after this one:")[1]


def test_the_block_never_writes_the_query():
    """The governor decides WHERE to spend, never WHAT to ask. A governor that
    writes queries is a second author of the answer."""
    g = _gap(SUN, "S384280",
             attempts=(Attempt(round_index=1, tool="rag", query="sunshine care",
                               returned_payload=True, targeted=True),))
    block = governor_block(g, remaining=(g,))

    # THE PROPERTY, not a banned substring. An earlier version of this test
    # banned "query:" -- which "Previous query: ..." trips while REPORTING
    # history, the opposite of writing one. A fingerprint gate fails on the
    # legitimate case and passes on the next illegitimate one.
    #
    # Every quoted string in the block must be something the governor was
    # GIVEN: the gap's own text, or a query react actually ran. Anything else
    # is the governor inventing a query.
    import re
    allowed = {g.text} | {a.query for a in g.attempted_by if a.query}
    for quoted in re.findall(r'"([^"]+)"', block):
        assert quoted in allowed, (
            f"the governor put {quoted!r} in the prompt; it was never given it"
        )


def test_no_gap_means_no_block():
    """A block that appears every round saying nothing trains the model to skip
    it, and then it is absent on the round that matters."""
    assert governor_block(None) is None


def test_a_stalled_gap_is_told_to_change_approach_with_its_history():
    g = _gap(SUN, "S384280",
             attempts=(Attempt(round_index=1, tool="rag", query="sunshine care",
                               returned_payload=True, targeted=True),
                       Attempt(round_index=2, tool="rag", query="sunshine mgmt",
                               returned_payload=True, targeted=True)),
             closures=(Closure(1, 40), Closure(2, 40)))
    block = governor_block(g, remaining=(g,))
    assert "not moving" in block
    assert "sunshine care" in block, "the attempt history must be carried"


def test_a_rising_gap_is_told_to_stay_on_it():
    g = _gap(SUN, "S384280",
             attempts=(Attempt(round_index=1, tool="rag", query="q",
                               returned_payload=True, targeted=True),),
             closures=(Closure(1, 10), Closure(2, 70)))
    assert "Stay on THIS gap" in governor_block(g, remaining=(g,))


def test_an_untried_gap_is_told_it_is_untried():
    block = governor_block(_gap(UHC, "S7c9bc3"), remaining=())
    assert "not yet attempted" in block and "has not been searched" in block


# ── the wiring: the defect was a missing consumer, so assert the consumer ───

def test_the_block_reaches_the_round_context():
    """gap_targeted existed for weeks with no reader. This asserts the READ,
    over the AST -- a substring search matches the comment explaining it."""
    src = pathlib.Path("app/pipeline/react_loop.py").read_text()
    tree = ast.parse(src)
    appended = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.AugAssign) and not isinstance(node, ast.Assign):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(t, ast.Name) and t.id == "reasoning_context"
                   for t in targets):
            continue
        if "_v2_steer_block" in ast.dump(node.value):
            appended = True
    assert appended, "the governor block is built and never appended to the prompt"


def test_the_steering_is_arm_scoped_and_revertible():
    """Steering the v1 arm would vary the prompt on both sides and the A/B
    could no longer attribute a divergence to the decision."""
    src = pathlib.Path("app/pipeline/react_loop.py").read_text()
    # Comments stripped: six gates in one day matched the prose describing
    # the code rather than the code.
    code = "\n".join(l.split("#")[0] for l in src.splitlines())
    i = code.index("MOBIUS_V2_STEER")
    window = code[i:i + 400]
    assert "orchestrator_version" in window, "steering is not arm-scoped"
    assert "rn > 1" in window, "steering must not fire on the decomposition round"


# ── round 2 discovers gaps; it does not close a question ────────────────────

def test_the_root_gap_is_never_rendered_as_something_to_close():
    """Ananth, 2026-09-12: splitting a question by its SURFACE -- one gap per
    payer named in the sentence -- is immature. The root gap IS the question;
    telling react to "close" it restates what it already has.

    Measured: a broad three-payer query had rag search all three through its
    own slot decomposition in ~30s, while a single-payer query spent 73s on
    one. The tool decomposes better than a surface split.
    """
    from app.pipeline.v2.posture import ROOT_GAP_ID, seed_root_gap
    root = seed_root_gap("care management philosophy for Molina, Sunshine and UHC")
    block = governor_block(root, remaining=(root,), round_index=2)
    assert "close this gap" not in block
    assert "review, do not re-ask" in block
    assert "still missing or thin" in block


def test_discover_directive_renders_the_review_block_for_any_gap():
    from app.pipeline.v2.posture import Directive
    g = _gap(SUN, "S384280")
    block = governor_block(g, remaining=(g,), round_index=2,
                           directive=Directive.DISCOVER)
    assert "review, do not re-ask" in block


def test_a_discovered_gap_is_closed_not_rediscovered():
    from app.pipeline.v2.posture import Directive
    g = _gap(SUN, "S384280",
             attempts=(Attempt(round_index=2, tool="rag", query="q",
                               returned_payload=True, targeted=True),),
             closures=(Closure(2, 30),))
    block = governor_block(g, remaining=(g,), round_index=3,
                           directive=Directive.CLOSE)
    assert "close this gap" in block
    assert "review, do not re-ask" not in block


def test_the_governor_does_not_name_the_missing_parts_itself():
    """A decomposition only the governor can see has no consumer -- the defect
    this whole contract exists to end. It ASKS for the review; react performs
    it."""
    from app.pipeline.v2.posture import seed_root_gap
    root = seed_root_gap("care management philosophy for Molina, Sunshine and UHC")
    block = governor_block(root, remaining=(root,), round_index=2)
    # The question is quoted once, as the question. The governor must not emit
    # a list of sub-questions of its own devising.
    assert block.count("Molina") == 1, "the governor split the question itself"


# ── the block asks for WHICH gap; the SHAPE of closure is the prompt seat's ──

def test_the_block_does_not_ask_for_closure():
    """LLM seat owns evidence_review's schema, and applied my own rule back at
    me: one place asking for it is right, two is a contradiction waiting for
    whoever debugs it next. WHICH gap is mine; the shape of the per-gap object
    is theirs."""
    from app.pipeline.v2.posture import Directive, seed_root_gap
    g = _gap(SUN, "S384280",
             attempts=(Attempt(round_index=2, tool="rag", query="q",
                               returned_payload=True, targeted=True),))
    root = seed_root_gap("a question")
    for block in (governor_block(g, remaining=(g,), directive=Directive.CLOSE),
                  governor_block(root, remaining=(root,), round_index=2)):
        assert "closure" not in block.lower(), (
            "the block is asking for a field the prompt seat owns"
        )


def test_one_attempt_is_not_a_stall():
    """closure_trend() answers FLAT with fewer than two reports -- correct for
    a TREND, wrong as ADVICE. With closure unreported (no producer yet), every
    gap would be told "not moving, change the approach" after a single try:
    could-not-check read as checked-false, arriving as a prompt.

    Found by rendering the block, not by reading the predicate.
    """
    from app.pipeline.v2.posture import Directive
    one = _gap(SUN, "S384280",
               attempts=(Attempt(round_index=2, tool="rag", query="q1",
                                 returned_payload=True, targeted=True),))
    block = governor_block(one, remaining=(one,), directive=Directive.CLOSE)
    assert "not moving" not in block
    assert "searched once" in block

    two = _gap(SUN, "S384280",
               attempts=(Attempt(round_index=2, tool="rag", query="q1",
                                 returned_payload=True, targeted=True),
                         Attempt(round_index=3, tool="rag", query="q2",
                                 returned_payload=True, targeted=True)))
    block2 = governor_block(two, remaining=(two,), directive=Directive.CLOSE)
    assert "not moving" in block2, "two pulls of the same lever IS a stall"
