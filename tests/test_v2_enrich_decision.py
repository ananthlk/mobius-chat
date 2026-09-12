"""Should critic + next-steps run? Decided and RECORDED, even while always-on.

Ananth, 2026-09-12: "the question is based on some criteria you still decide if
we need to run them or not.. for now assume always".

Hard-coding `return True` would have thrown away exactly the rows needed to
stop doing that. The criteria are evaluated every turn so the eventual
threshold is tuned against real data rather than invented later.
"""
import app.pipeline.v2.enrich as E
from app.pipeline.v2.enrich import should_enrich, should_reopen


class _F:
    def __init__(self, grounded):
        self.grounded = grounded


# ── always-on, but not blind ────────────────────────────────────────────────

def test_always_on_still_records_the_criteria():
    """The whole point: when the threshold question comes up, the rows exist."""
    d = should_enrich(answer="an answer", facts=[_F(True), _F(False)],
               open_gaps=("UHC",), is_complete=True)
    assert d.run_critic and d.run_next_steps
    assert d.criteria["ungrounded_facts"] == 1
    assert d.criteria["claims_complete_with_gaps_open"] is True
    assert "not gating" in d.why


def test_no_answer_is_a_prerequisite_not_a_policy():
    """ALWAYS cannot mean "critique an empty string" -- that returns confident
    prose about nothing."""
    d = should_enrich(answer="   ", facts=(), open_gaps=())
    assert not d.run_critic and not d.run_next_steps
    assert "nothing to critique" in d.why


def test_the_gating_shape_exists_and_is_reachable(monkeypatch):
    """The version we will switch to is written and tested, not a TODO."""
    monkeypatch.setattr(E, "ALWAYS_ENRICH", False)
    grounded_complete = should_enrich(answer="a", facts=[_F(True)], open_gaps=(),
                               is_complete=True)
    assert not grounded_complete.run_critic
    unsupported = should_enrich(answer="a", facts=[_F(False)], open_gaps=())
    assert unsupported.run_critic


# ── affordability is ours, never the model's ────────────────────────────────

def test_reopen_affordability_is_computed_from_budget_not_asked():
    d = should_enrich(answer="a", elapsed_s=20.0, promise_s=31.0, rounds_left=2,
               round_cost_s=5.0)
    assert d.reopen_affordable
    broke = should_enrich(answer="a", elapsed_s=29.0, promise_s=31.0, rounds_left=2,
                   round_cost_s=5.0)
    assert not broke.reopen_affordable


def test_no_rounds_left_is_not_affordable_however_fast_the_turn_was():
    assert not should_enrich(answer="a", elapsed_s=1.0, promise_s=31.0,
                      rounds_left=0, round_cost_s=1.0).reopen_affordable


def test_unknown_budget_is_not_treated_as_affordable():
    """Missing numbers must not read as permission to spend."""
    assert not should_enrich(answer="a", rounds_left=2).reopen_affordable


# ── reopening stays strict ──────────────────────────────────────────────────

def test_reopen_only_on_objectively_unfinished_work():
    d = should_enrich(answer="a", elapsed_s=1.0, promise_s=31.0, rounds_left=2,
               round_cost_s=1.0)
    ok, why = should_reopen({"parts": [{"part": "UHC", "status": "not_attempted"}]}, d)
    assert ok and "never attempted" in why


def test_could_be_better_never_reopens():
    """Unbounded: a loop that reopens on dissatisfaction spends every second of
    every budget, every time."""
    d = should_enrich(answer="a", elapsed_s=1.0, promise_s=31.0, rounds_left=2,
               round_cost_s=1.0)
    for status in ("partial", "weak", "supported", "could_be_better"):
        ok, _ = should_reopen({"parts": [{"part": "x", "status": status}]}, d)
        assert not ok, status


def test_unaffordable_reopen_hands_the_choice_to_the_user():
    """Not a refusal. The user can only make that call if the answer says what
    is missing -- which is why role_communicate must name the gap."""
    d = should_enrich(answer="a", elapsed_s=30.0, promise_s=31.0, rounds_left=1,
               round_cost_s=9.0)
    ok, why = should_reopen({"parts": [{"part": "UHC", "status": "not_attempted"}]}, d)
    assert not ok and "user" in why


def test_no_verdict_does_not_reopen():
    d = should_enrich(answer="a", elapsed_s=1.0, promise_s=31.0, rounds_left=2,
               round_cost_s=1.0)
    assert should_reopen(None, d)[0] is False
    assert should_reopen({}, d)[0] is False


def test_malformed_verdict_parts_do_not_crash():
    d = should_enrich(answer="a", elapsed_s=1.0, promise_s=31.0, rounds_left=2,
               round_cost_s=1.0)
    for parts in ("nope", [None], [3], [{"no_status": 1}]):
        assert should_reopen({"parts": parts}, d)[0] is False
