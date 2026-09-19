"""A turn that has found NOTHING could never buy the round that would find it.

`converging()` requires that something CAME BACK. That is the right test for
"I am nearly done, let me finish" and the wrong one for "I have nothing, let
me look once more" — so the band opened exactly when a turn was already
succeeding and closed exactly when it was failing.

Live turn 4ab61f6a, "does Aetna Better Health of Florida cover doula
services":

    round 1  remaining 24.2s  spendable       -> explore, extend
    round 2  remaining 20.0s  NOT spendable   (needs 10.4 + 10.0 = 20.4s)
             converging=False (nothing returned) -> band refused
             -> NARROW, no tools, "not found in our available materials"

Four tenths of a second short, on a question the corpus answers. v1 took 47s
on it — 16s past its promise — retrieved twice and answered.
"""
import dataclasses

from app.pipeline.v2 import posture as P


def _state(grounded, *, gaps=1, band=8.0, drawn=0.0, remaining=20.0,
           attempts=(), round_index=2):
    gs = []
    for i in range(gaps):
        gs.append(P.Gap(gap_id=f"g{i}", text="does it cover doulas",
                        opened_round=1, importance="normal",
                        attempted_by=tuple(attempts)))
    return P.RoundState(
        round_index=round_index, open_gaps=tuple(gs),
        gaps_open_history=(1,) * round_index,
        budget=P.Budget(remaining_s=remaining, remaining_c=5.0,
                        cost_coverage=1.0, band_s=band, band_drawn_s=drawn),
        next_round_cost_s=0.0, acting_cost_s=0.0, validate_cost_s=5.0,
        grounded_facts=grounded)


class TestItFiresOnTheCaseItWasBuiltFor:
    def test_zero_grounded_facts_opens_the_band(self):
        ok, why = P.may_overrun(_state(0))
        assert ok, why
        assert "ZERO grounded facts" in why

    def test_the_doula_turn_would_now_get_its_round(self):
        """20.0s remaining, which spendable() refuses at 20.4s."""
        st = _state(0, remaining=20.0)
        assert P.spendable(st) is False, "premise: the promise does not fund it"
        assert P.converging(st) is False, "premise: nothing came back"
        assert P.may_overrun(st)[0] is True


class TestTheGuardsThatStopARunaway:
    def test_facts_NOT_MEASURED_is_not_a_licence_to_spend(self):
        """🔴 None is not zero. A contract we never recorded must not read as
        'found nothing' and buy a round on a telemetry gap."""
        ok, why = P.may_overrun(_state(None))
        assert not ok and "not measured" in why

    def test_a_turn_with_something_to_ship_does_not_use_this_path(self):
        ok, why = P.may_overrun(_state(2))
        assert not ok and "grounded fact(s) to ship" in why

    def test_a_stuck_gap_cannot_buy_the_same_nothing_again(self):
        """The clause that stops 'found nothing' funding rounds forever:
        found-nothing stays true, so without this it would re-fire."""
        tried = tuple(
            P.Attempt(round_index=i, tool=t, model="m", query="q",
                      returned_payload=False, targeted=True)
            for i, t in enumerate(("rag", "fetch_document", "web_scrape"), 1))
        ok, why = P.may_overrun(_state(0, attempts=tried, round_index=9))
        assert not ok and "stuck" in why

    def test_the_band_is_drawn_once_per_turn(self):
        ok, why = P.may_overrun(_state(0, drawn=6.0))
        assert not ok and "already been drawn" in why

    def test_it_still_refuses_when_even_the_band_cannot_fund_it(self):
        """The existing bound is not bypassed — it runs after this gate."""
        ok, why = P.may_overrun(_state(0, remaining=1.0, band=0.5))
        assert not ok and "band cannot fund it" in why

    def test_no_open_gaps_means_nothing_to_spend_on(self):
        ok, why = P.may_overrun(_state(0, gaps=0))
        assert not ok


class TestItDoesNotDisturbTheConvergingPath:
    def test_a_converging_turn_still_overruns_for_its_own_reason(self):
        got = P.Attempt(round_index=1, tool="rag", model="m", query="q",
                        returned_payload=True, targeted=True)
        st = _state(3, attempts=(got,))
        ok, why = P.may_overrun(st)
        assert ok and "converging" in why
