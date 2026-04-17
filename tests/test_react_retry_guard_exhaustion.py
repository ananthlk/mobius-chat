"""Phase 0.19 — tool-exhaustion block in the ReAct retry guard.

The 2026-04-17 live test surfaced a gap in the Phase 0.7 guard: it only
blocks identical ``(tool, inputs_sig)`` repeats. When the reasoner
reformulates the query between rounds, the inputs_sig changes and the
guard lets the call through — even though the tool has already proven
unfruitful for this turn.

The concrete trace:

    R1  search_corpus  query="H0036 Sunshine medical necessity"  → 5 chunks, all abstain → 0 kept
    R2  search_corpus  query="Sunshine Health H0036 policy"       → 5 chunks, all abstain → 0 kept

Two reasoning-round LLM calls, two RAG LLM calls, zero new evidence.
Phase 0.19 adds a per-tool consecutive-failure counter — after 2 failures
of the same tool with no intervening success, the tool is blocked
regardless of inputs_sig and the planner must pivot.
"""

from __future__ import annotations

from app.pipeline.react_retry_guard import (
    ReactRetryGuard,
    _TOOL_EXHAUSTION_THRESHOLD,
)


def _fail_result(code: str = "empty_retrieval") -> dict:
    return {"success": False, "error": {"error_code": code}, "sources": []}


def _ok_result() -> dict:
    return {"success": True, "result": "answer body >80 chars ...", "sources": [{"id": "x"}]}


class TestToolExhaustionThreshold:
    def test_threshold_is_two(self):
        """Sanity: one failure is noise, two is a pattern."""
        assert _TOOL_EXHAUSTION_THRESHOLD == 2

    def test_one_failure_does_not_exhaust(self):
        g = ReactRetryGuard()
        g.record_result(
            tool="search_corpus",
            inputs={"query": "first try"},
            result=_fail_result(),
            round=1,
            results_count_before=0,
        )
        # Different query → different sig. Pre-0.19 this would not block,
        # and post-0.19 it still shouldn't — one failure is not exhaustion.
        assert g.should_block(
            tool="search_corpus",
            inputs={"query": "second try, different phrasing"},
            current_results_count=0,
        ) is None

    def test_two_failures_different_queries_exhausts_tool(self):
        """THE regression test. Two search_corpus failures with *different*
        queries — pre-0.19 the guard let a third call through. Post-0.19 the
        third call is blocked with ``error_code='tool_exhausted'``.
        """
        g = ReactRetryGuard()
        g.record_result(
            tool="search_corpus",
            inputs={"query": "H0036 Sunshine medical necessity"},
            result=_fail_result(),
            round=1,
            results_count_before=0,
        )
        g.record_result(
            tool="search_corpus",
            inputs={"query": "Sunshine Health H0036 policy"},
            result=_fail_result(),
            round=2,
            results_count_before=0,
        )
        blocked = g.should_block(
            tool="search_corpus",
            inputs={"query": "yet another re-phrasing of the same thing"},
            current_results_count=0,
        )
        assert blocked is not None, (
            "after 2 consecutive search_corpus failures, a third call must be "
            "blocked even with a new inputs_sig — this is the 0.19 fix"
        )
        assert blocked.error_code == "tool_exhausted"

    def test_success_resets_the_streak(self):
        """A successful call clears the per-tool failure streak, so an
        unrelated later failure doesn't inherit prior exhaustion state."""
        g = ReactRetryGuard()
        g.record_result(
            tool="search_corpus", inputs={"q": "a"}, result=_fail_result(),
            round=1, results_count_before=0,
        )
        g.record_result(
            tool="search_corpus", inputs={"q": "b"}, result=_ok_result(),
            round=2, results_count_before=0,
        )
        g.record_result(
            tool="search_corpus", inputs={"q": "c"}, result=_fail_result(),
            round=3, results_count_before=1,
        )
        # One failure after the success — not exhausted.
        assert g.should_block(
            tool="search_corpus", inputs={"q": "d"}, current_results_count=1,
        ) is None

    def test_exhaustion_is_per_tool(self):
        """search_corpus being exhausted must not block google_search."""
        g = ReactRetryGuard()
        for i in range(_TOOL_EXHAUSTION_THRESHOLD):
            g.record_result(
                tool="search_corpus",
                inputs={"query": f"attempt {i}"},
                result=_fail_result(),
                round=i + 1,
                results_count_before=0,
            )
        # search_corpus exhausted — google_search must still be allowed.
        assert g.should_block(
            tool="google_search",
            inputs={"query": "pivot to web"},
            current_results_count=0,
        ) is None

    def test_exhaustion_uses_latest_failed_attempt_round(self):
        """The synthetic FailedAttempt returned by the exhaustion path cites
        the most recent failure so the hint shows the relevant round."""
        g = ReactRetryGuard()
        g.record_result(
            tool="search_corpus", inputs={"q": "a"}, result=_fail_result(),
            round=1, results_count_before=0,
        )
        g.record_result(
            tool="search_corpus", inputs={"q": "b"}, result=_fail_result(),
            round=2, results_count_before=0,
        )
        blocked = g.should_block(
            tool="search_corpus", inputs={"q": "c"}, current_results_count=0,
        )
        assert blocked is not None
        assert blocked.round == 2, "should cite the most recent failure"


class TestFailureHintMentionsExhaustedTool:
    def test_hint_lists_exhausted_tool(self):
        g = ReactRetryGuard()
        for i in range(_TOOL_EXHAUSTION_THRESHOLD):
            g.record_result(
                tool="search_corpus",
                inputs={"query": f"q{i}"},
                result=_fail_result(),
                round=i + 1,
                results_count_before=0,
            )
        hint = g.failure_hint_for_prompt()
        assert "Exhausted tools" in hint, (
            "the planner prompt must call out exhausted tools explicitly so "
            "the reasoner knows re-phrasing won't help"
        )
        assert "search_corpus" in hint
        assert "pick a DIFFERENT tool" in hint

    def test_hint_empty_when_no_exhaustion(self):
        """Below-threshold failures get the normal per-attempt lines but no
        'Exhausted tools' line."""
        g = ReactRetryGuard()
        g.record_result(
            tool="search_corpus", inputs={"q": "a"}, result=_fail_result(),
            round=1, results_count_before=0,
        )
        hint = g.failure_hint_for_prompt()
        assert "Exhausted tools" not in hint
        assert "search_corpus" in hint  # the normal failure line is still there


class TestBackwardCompatibilityWithPhase07:
    """The exact-signature block from Phase 0.7 must still work unchanged."""

    def test_same_inputs_blocked_after_one_failure(self):
        g = ReactRetryGuard()
        g.record_result(
            tool="search_corpus", inputs={"q": "same"}, result=_fail_result(),
            round=1, results_count_before=0,
        )
        blocked = g.should_block(
            tool="search_corpus", inputs={"q": "same"}, current_results_count=0,
        )
        assert blocked is not None, (
            "Phase 0.7 exact-signature block must still fire on a single "
            "failure when inputs are identical"
        )
        # Phase 0.7 blocks return the original failure, not a synthetic one.
        assert blocked.error_code != "tool_exhausted"

    def test_new_evidence_unblocks_same_signature(self):
        """Phase 0.7 semantics: if tool_results grew since the failure, the
        same-signature call is allowed again. Phase 0.19 must not regress this.
        """
        g = ReactRetryGuard()
        g.record_result(
            tool="search_corpus", inputs={"q": "same"}, result=_fail_result(),
            round=1, results_count_before=0,
        )
        # results_count grew from 0 to 1 → new evidence → not blocked by 0.7.
        # And streak is only 1 → not blocked by 0.19 either.
        assert g.should_block(
            tool="search_corpus", inputs={"q": "same"}, current_results_count=1,
        ) is None
