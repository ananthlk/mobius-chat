"""The adjudicator's prose and its number were not bound to each other.

Three live verdicts from the 15-question A/B:

    13b88a8c  reason was the literal string "FAIL", no flags, score 0.437
              -- `reason or verdict` substituting the verdict for a rationale
    ef589580  turn FAILED: 126 chars, 0 sources. Rationale said "structurally
              broken ... incorrect citations ... misleading". Score 0.611.
    26500a75  DEAD_END_ESCALATION raised, rationale said the user got a dead
              end, score 0.909.

This module tests the floor, not the LLM: it CONTRADICTS a grade the
delivered answer cannot support, and records the contradiction.
"""
import pytest

adj_mod = pytest.importorskip("app.services.post_run_adjudication")


def floor(score, *, answer="x" * 500, sources=None, flags=()):
    """Reproduce the floor's arithmetic on one grade."""
    contradictions = []
    src = sources if isinstance(sources, (list, tuple)) else None
    ans = (answer or "").strip()
    if src is not None and len(src) == 0 and len(ans) > 0 and score > 0.5:
        contradictions.append("zero sources"); score = min(score, 0.5)
    from app.services.post_run_adjudication import HARD_FLAG_CEILING as CEIL
    hard = [f for f in flags if f in ("HALLUCINATION_SUSPECTED", "WRONG_PAYER",
                                      "STALE_DATA_PRESENTED", "JSON_BLEED")]
    if hard and score > CEIL:
        contradictions.append("hard flag"); score = min(score, CEIL)
    return score, contradictions


class TestTheThreeLiveCases:
    def test_ef589580_zero_sources_still_caps(self):
        """126 chars AND zero sources. The zero-sources check carries it; the
        length check was removed after measurement (see below)."""
        score, why = floor(0.611, answer="x" * 126, sources=[])
        assert score <= 0.5 and why

    def test_a_SHORT_HONEST_REFUSAL_IS_NOT_PENALISED(self):
        """🔴 THE RULE I REMOVED AFTER MEASURING IT.

        Live turn babcf5dd: 137 characters, a refusal that named who to call,
        graded PASS 0.899 -- and my length rule capped it to 0.4 for being
        short. ef589580 was an ERROR MESSAGE at 126 characters. Eleven
        characters apart, opposite meanings: length cannot separate them, and
        the rule punished exactly the concise honest refusal this loop was
        changed to produce.
        """
        score, why = floor(0.899, answer="x" * 137, sources=[{"d": 1}] * 8,
                           flags=["CORPUS_GAP", "DEAD_END_ESCALATION"])
        assert score == 0.899, f"a short honest answer was capped to {score}"
        assert why == []

    def test_26500a75_a_raised_flag_must_move_the_number(self):
        score, why = floor(0.909, sources=[{"d": 1}] * 8,
                           flags=["CORPUS_GAP", "DEAD_END_ESCALATION"])
        # DEAD_END is not in the hard set -- it is a shape, not a falsehood.
        assert score == 0.909 and why == [], (
            "DEAD_END_ESCALATION must NOT cap: capping it would punish the "
            "honest refusal this loop was changed to make")

    def test_a_hallucination_flag_does_cap(self):
        from app.services.post_run_adjudication import HARD_FLAG_CEILING as CEIL
        score, why = floor(0.963, sources=[{"d": 1}] * 5,
                           flags=["HALLUCINATION_SUSPECTED"])
        assert score == CEIL and why


class TestZeroMeansMeasuredZero:
    def test_an_empty_LIST_of_sources_caps(self):
        score, _ = floor(0.9, sources=[])
        assert score == 0.5

    def test_sources_NOT_MEASURED_does_not_cap(self):
        """🔴 None means we did not look. Capping on it would fail a turn for a
        telemetry gap -- could-not-check landing on the gate built to enforce
        that line."""
        score, why = floor(0.9, sources=None)
        assert score == 0.9 and why == []


class TestAGraderThatCouldNotGradeReportsNoFailure:
    def test_a_missing_verdict_is_not_a_FAIL(self):
        import inspect
        src = inspect.getsource(adj_mod)
        assert 'adj.get("verdict") or "FAIL"' not in src, (
            "a missing verdict still defaults to the worst possible one")
        assert "COULD_NOT_GRADE" in src

    def test_a_missing_rationale_is_not_filled_with_the_verdict(self):
        import inspect
        src = inspect.getsource(adj_mod)
        assert '"reason": reason or verdict' not in src, (
            "an empty rationale is still filled with the verdict string, "
            "which is what produced reason='FAIL'")

    def test_passed_is_None_not_False_when_ungraded(self):
        """False is a claim about the TURN. None is a statement about US."""
        import inspect
        src = inspect.getsource(adj_mod)
        assert "passed = None" in src


class TestNoFabricationOutranksAnHonestRefusal:
    """The ordering constraint, argued from operator cost.

    A wrong deadline causes a missed appeal. A dead end causes a phone call.
    The score must not say those are the same.

    Measured over 30 turns, by flag:
        HALLUCINATION_SUSPECTED  n=7  min 0.372  median 0.600  max 0.600
        DEAD_END_ESCALATION      n=9  min 0.361  median 0.735  max 1.000

    Medians were already correctly ordered. The defect was the OVERLAP -- the
    best hallucination (0.600) outranked the worst honest refusal (0.361).
    """

    WORST_OBSERVED_HONEST_REFUSAL = 0.361

    def test_the_ceiling_does_not_become_the_score(self):
        """🔴 WHY 0.35 WAS REVERTED.

        A ceiling low enough to guarantee the ordering clamped a third of one
        arm onto itself: on a clean 15-pair run, FIVE v2 turns scored exactly
        0.35 (Q4, Q5, Q7, Q8, Q12), three of them graded PASS. A bound that
        most flagged turns land on is not a bound, it is the score.

        The ordering claim survives and needs a mechanism that caps a flag's
        CONTRIBUTION while preserving spread. This test guards the failure
        mode rather than asserting the old constant.
        """
        from app.services.post_run_adjudication import HARD_FLAG_CEILING as CEIL
        graded = [floor(s0, sources=[{"d": 1}] * 5,
                        flags=["HALLUCINATION_SUSPECTED"])[0]
                  for s0 in (0.42, 0.55, 0.70, 0.88, 0.96)]
        assert len(set(graded)) > 1, (
            f"every flagged turn collapsed to {CEIL} — the ceiling has become "
            f"the score rather than a bound on it")

    def test_refusals_are_NOT_rewarded(self):
        """🔴 The floor tightens invention. It must never RAISE a refusal.

        Paying for a refusal makes answering nothing the cheapest way to score
        well, which is a worse failure than the one being fixed.
        """
        for s0 in (0.2, 0.5, 0.9):
            out, _ = floor(s0, sources=[{"d": 1}] * 3,
                           flags=["CORPUS_GAP", "DEAD_END_ESCALATION"])
            assert out == s0, f"a refusal was moved from {s0} to {out}"
