"""react may call it done at any point — and communicate is more than summarise.

Ananth: "react should be able to call it done at any point.. communicate is
more than summarize so why not stop then".

🔴 MEASURED, cid ed5d06fd (rev 01089-k62):

    +0.8s   round 1  roles=confirm,plan,COMMUNICATE
    +5.7s   contract round=1 facts=4  complete
    +10.2s  [v2.exec] round=1 v1=complete -> v2=EXTEND posture=explore
    +17.3s  round 2  facts=4          — the same four facts

The executor overruled a finish on a round that had already written the answer,
and bought a round that added nothing.
"""
import pytest

from app.pipeline.v2 import executor as X


class _Dec:
    def __init__(self, gap="G1"):
        self.gap_targeted = gap
        self.because = "a gap is open"
        self.overran = False
        self.directive = None


def _explore_decision():
    """A decision whose posture extends — the shape that reaches the overrule."""
    d = _Dec()
    for posture in X.PROMPT_MISMATCH:          # EXPLORE is the extending one
        d.posture = posture
    return d


def test_an_answered_round_is_not_overruled():
    d = _explore_decision()
    act = X.decide(d, None, extensions_used=0,
                   model_proposes_complete=True, communicated=True)
    assert not act.continues, (
        "the executor extended a round that had already communicated — the "
        "answer exists and the open gap is for the ANSWER to name")


def test_a_summarising_round_IS_still_overruled():
    """The overrule is right when the completing round only summarised: react
    can call the evidence sufficient without ever having answered the person,
    and a targeted gap is real evidence against that."""
    d = _explore_decision()
    act = X.decide(d, None, extensions_used=0,
                   model_proposes_complete=True, communicated=False)
    assert act.continues
    assert "OVERRULING" in act.because


def test_the_default_preserves_todays_behaviour():
    """Callers that do not pass it must be unaffected — the parameter defaults
    to False, which is the pre-change path."""
    d = _explore_decision()
    assert X.decide(d, None, extensions_used=0,
                    model_proposes_complete=True).continues


def test_it_can_only_ever_reduce_extensions():
    """The fuse exists because an executor that can say extend forever once
    said it 98 times. A guard that only ever SUPPRESSES an extend cannot reach
    that failure through this branch."""
    d = _explore_decision()
    with_guard = X.decide(d, None, extensions_used=0,
                          model_proposes_complete=True, communicated=True)
    without = X.decide(d, None, extensions_used=0,
                       model_proposes_complete=True, communicated=False)
    assert without.continues and not with_guard.continues
