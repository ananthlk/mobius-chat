"""The standard round report -- one shape every round.

Ananth, 2026-09-12: "the summary is the role it assumed, the overall thread
summary, turn summary, react round findings, open gaps, next round tools,
expanded answer, is_complete.. is there anything else" and "i am also working
to get an enriched output not required because i can format it".

That second line is the contract: this structure REPLACES the enricher, so a
field a formatter must fetch from somewhere else is a field it failed to carry.
"""
from app.pipeline.v2.report import RoundReport, render, to_dict

ASKED_FOR = ("roles", "thread_summary", "turn_summary", "findings",
             "open_gaps", "next_tools", "expanded_answer", "is_complete")
ADDED = ("evidence", "set_aside", "complete_why",
         "elapsed_s", "promise_s", "rounds_left", "unobservable")


def _full(**kw):
    base = dict(
        round_index=2, roles=("role_judge", "role_summarise"),
        thread_summary="FL payer policy", turn_summary="Molina ICM",
        findings=("UHC found",), open_gaps=("Sunshine philosophy",),
        next_tools=("web_scrape",), evidence=("m.pdf p102",),
        set_aside=("x.pdf (none useful)",), expanded_answer="THE FULL ANSWER",
        is_complete=False, complete_why="Sunshine unsearched",
        elapsed_s=20.0, promise_s=31.0, rounds_left=1)
    base.update(kw)
    return RoundReport(**base)


# ── the contract with the formatter ─────────────────────────────────────────

def test_every_asked_for_field_is_carried():
    d = to_dict(_full())
    missing = [f for f in ASKED_FOR if f not in d]
    assert not missing, missing


def test_keys_are_always_present_even_when_empty():
    """A formatter that must test for a key invents a default, and that default
    becomes a second author of the answer."""
    d = to_dict(RoundReport())
    for f in ASKED_FOR + ADDED:
        assert f in d, f


def test_not_stated_is_not_false():
    """is_complete=None means react has not said. Rendering that as "false"
    asserts a judgement nobody made."""
    assert "not stated" in render(RoundReport(round_index=1))
    assert "complete: false" in render(_full(is_complete=False))


# ── the expanded answer is the deliverable, not a progress note ─────────────

def test_expanded_answer_only_on_a_communicate_round():
    """Emitting it every round publishes a half-written answer as if final --
    three times on a three-round turn."""
    judging = render(_full(roles=("role_judge",), expanded_answer="THE FULL ANSWER"))
    assert "THE FULL ANSWER" not in judging
    talking = render(_full(roles=("role_judge", "role_communicate")))
    assert "THE FULL ANSWER" in talking


def test_the_turn_summary_still_shows_on_a_non_communicate_round():
    """The reader must see progress without the answer being published."""
    out = render(_full(roles=("role_judge",), turn_summary="Molina ICM so far"))
    assert "Molina ICM so far" in out


# ── empty is not absent ─────────────────────────────────────────────────────

def test_no_gaps_says_none_rather_than_omitting_the_line():
    assert "open:     none" in render(_full(open_gaps=()))


def test_no_tools_says_why_not():
    """An absent tools line reads as "no tools needed". It usually means the
    selector never answered -- which is what happens when toolreg is down."""
    assert "selector unavailable" in render(_full(next_tools=()))


def test_no_answer_yet_is_said_out_loud():
    assert "nothing yet" in render(_full(turn_summary=""))


def test_unobservable_has_its_own_line():
    """COULD NOT CHECK is not CHECKED FALSE -- eight instances of that collapse
    this session. It must not read as an empty result."""
    out = render(_full(unobservable=("citation coverage — no ack returned",)))
    assert "? unknown" in out and "citation coverage" in out


# ── the budget, because the objective is subject to it ──────────────────────

def test_budget_names_the_promise_and_flags_the_overrun():
    assert "20.0s of 31s" in render(_full())
    assert "OVER" in render(_full(elapsed_s=48.2))
    assert "OVER" not in render(_full(elapsed_s=12.0))


def test_budget_line_is_omitted_when_the_promise_is_unknown():
    """Printing a promise we do not have would invent the constraint."""
    assert "budget:" not in render(_full(promise_s=None))


# ── provenance travels with the answer ──────────────────────────────────────

def test_evidence_and_set_aside_are_carried_together():
    """The expanded answer carries citation markers; without provenance in the
    SAME structure they have no referent -- which is how [1,2,3,4,5] ended up
    pointing at evidence that was never in the prompt."""
    out = render(_full())
    assert "m.pdf p102" in out
    assert "x.pdf" in out and "aside" in out
