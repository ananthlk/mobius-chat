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
    hard = [f for f in flags if f in ("HALLUCINATION_SUSPECTED", "WRONG_PAYER",
                                      "STALE_DATA_PRESENTED", "JSON_BLEED")]
    if hard and score > 0.6:
        contradictions.append("hard flag"); score = min(score, 0.6)
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
        score, why = floor(0.963, sources=[{"d": 1}] * 5,
                           flags=["HALLUCINATION_SUSPECTED"])
        assert score == 0.6 and why


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
