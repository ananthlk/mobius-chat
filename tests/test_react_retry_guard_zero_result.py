"""Phase 0.19b — zero-result "success" outcomes count as failures.

Background. The 2026-04-19 live validation on Molina FL behavioral
health and Sunshine H0036 both showed the same pathology: the
planner called ``search_corpus`` three times in a row with different
query strings, each returned "success" with zero retrieved chunks
(because all hits scored below the then-0.5 confidence threshold),
and tool-exhaustion never fired because the guard's ``_is_failure``
only looked at ``success=False`` and ``error != None``.

After today's confidence-threshold fix (760f06f) the chunks DO reach
the planner more often. But the guard's calibration bug still
stands: a tool that "succeeds" at running but produces no useful
output should count as a failure for retry-guard purposes.
Otherwise the planner is free to bash the same tool with minor
query variations for every round until the loop runs out.

This file tests the new classification:

  - ``_is_zero_result(result)`` — detects the specific shape:
    success=True + signal='no_sources' + empty/missing sources
  - ``_is_failure(result)`` now returns True for zero-results too
  - ``_error_code(result)`` returns 'no_results' (not 'tool_error')
    for zero-results, so operators reading llm_calls rows can
    distinguish "tool errored" from "tool ran but found nothing"
  - Integration: two consecutive zero-results on the same tool
    trips tool-exhaustion, forcing the planner to pivot
  - Partial-result / mixed-signal outcomes do NOT classify as
    zero-result (those are genuine partial successes the planner
    should keep using)

Scope note. The existing retry-guard tests (28 tests in
test_react_retry_guard.py + test_react_retry_guard_exhaustion.py)
lock the original behavior and all still pass. This file adds the
zero-result semantics on top.
"""

from __future__ import annotations

from app.pipeline.react_retry_guard import (
    ReactRetryGuard,
    inputs_signature,
)


# ── Shape detection ─────────────────────────────────────────────────


class TestIsZeroResult:
    """Unit tests on the classifier. Direct access via the private
    helper so we can exercise edge cases without constructing full
    ReactRetryGuard state."""

    def _classify(self, result: dict) -> bool:
        return ReactRetryGuard._is_zero_result(result)

    def test_canonical_zero_result(self):
        """The production pathology we're catching. search_corpus
        succeeded, signal='no_sources', no chunks made it past
        filtering."""
        result = {
            "tool": "search_corpus",
            "success": True,
            "result": "I didn't find anything specific; I'll answer from what I know.",
            "signal": "no_sources",
            "sources": [],
        }
        assert self._classify(result) is True

    def test_google_search_off_topic_hits_as_zero_result(self):
        """google_search that returned snippets but they're all
        off-topic (sec.gov when the user asked about Molina Medicaid)
        — ``_run_google_search`` emits signal='no_sources' when the
        result body starts with 'No search results' OR when the
        LLM-summarize path decides there's nothing useful."""
        result = {
            "tool": "google_search",
            "success": True,
            "result": "No relevant information found on the web for this query.",
            "signal": "no_sources",
            "sources": [],
        }
        assert self._classify(result) is True

    def test_explicit_failure_is_not_zero_result(self):
        """``success=False`` is a regular failure (tool errored), not
        a zero-result. The distinction matters: regular failures
        carry error envelopes with specific error_codes; zero-results
        are a "looked fine but nothing useful" outcome."""
        result = {
            "tool": "search_corpus",
            "success": False,
            "result": "Database connection timeout",
            "signal": "no_sources",
            "sources": [],
            "error": {"schema_name": "error_envelope", "error_code": "timeout"},
        }
        assert self._classify(result) is False

    def test_signal_corpus_only_with_chunks_is_not_zero_result(self):
        """Partial success — signal='corpus_only' means chunks DID
        flow through. Even if the answer text is terse, this is not
        a zero-result; the planner may synthesize from the chunks
        via guidance mode."""
        result = {
            "tool": "search_corpus",
            "success": True,
            "result": "The provider manual states...",
            "signal": "corpus_only",
            "sources": [{"document_name": "Manual", "page": 12, "text": "..."}],
        }
        assert self._classify(result) is False

    def test_no_sources_signal_with_sources_list_is_not_zero_result(self):
        """Edge case: signal='no_sources' but the sources field
        happens to carry something (e.g. a fallback 'Healthcare
        lookup' placeholder). Don't classify as zero-result — the
        planner might still learn from whatever's there."""
        result = {
            "tool": "healthcare_query",
            "success": True,
            "result": "NPI 1234567890: Dr Jones, Taxonomy 101Y00000X",
            "signal": "no_sources",
            "sources": [{"document_name": "Healthcare lookup", "text": "..."}],
        }
        assert self._classify(result) is False

    def test_missing_signal_field_is_not_zero_result(self):
        """Defensive: if the result is missing the signal field
        (maybe from a non-RAG tool that doesn't set it), don't
        classify as zero-result. Original failure-detection path
        still catches actual errors."""
        result = {
            "tool": "some_tool",
            "success": True,
            "result": "ok",
        }
        assert self._classify(result) is False

    def test_google_only_signal_never_classifies_as_zero(self):
        """google_only means at least the LLM summarized something —
        partial success. Not zero-result."""
        result = {
            "tool": "google_search",
            "success": True,
            "result": "Based on the search results, Sunshine Health offers...",
            "signal": "google_only",
            "sources": [{"document_name": "Web search", "source_type": "external"}],
        }
        assert self._classify(result) is False


# ── Failure + error_code propagation ──────────────────────────────


class TestIsFailureIncludesZeroResult:
    def test_zero_result_is_now_a_failure(self):
        """The core semantic change: zero-results count as failures
        for retry-guard purposes. This is what makes
        tool-exhaustion actually fire on zero-result streaks."""
        result = {
            "tool": "search_corpus",
            "success": True,
            "signal": "no_sources",
            "sources": [],
        }
        assert ReactRetryGuard._is_failure(result) is True

    def test_successful_call_with_sources_is_not_failure(self):
        """Regression guard: the original success path still classifies
        correctly. If this test fails, we've broken every successful
        tool call."""
        result = {
            "tool": "search_corpus",
            "success": True,
            "signal": "corpus_only",
            "sources": [{"document_name": "Manual", "page": 1}],
        }
        assert ReactRetryGuard._is_failure(result) is False

    def test_zero_result_error_code_is_no_results(self):
        """Operators reading llm_calls / retry-guard logs need to tell
        'tool errored' apart from 'tool ran but found nothing'.
        Distinct error_code values let them filter accordingly."""
        result = {
            "tool": "search_corpus",
            "success": True,
            "signal": "no_sources",
            "sources": [],
        }
        assert ReactRetryGuard._error_code(result) == "no_results"

    def test_tool_error_keeps_tool_error_code(self):
        """The pre-existing semantics for tool errors unchanged."""
        result = {
            "tool": "web_scrape",
            "success": False,
            "signal": "no_sources",
            "sources": [],
        }
        assert ReactRetryGuard._error_code(result) == "tool_error"

    def test_error_envelope_code_passes_through(self):
        """Structured error envelopes still win over our
        heuristic classification."""
        result = {
            "tool": "web_scrape",
            "success": False,
            "error": {"schema_name": "error_envelope", "error_code": "rate_limit"},
        }
        assert ReactRetryGuard._error_code(result) == "rate_limit"


# ── Integration: tool-exhaustion fires on zero-result streak ──────


class TestToolExhaustionOnZeroResults:
    """The production-facing behavior. With the classifier change,
    two consecutive zero-result calls to the same tool should trip
    tool-exhaustion — the planner is then forced to pivot to a
    different tool even if it tries a new query string."""

    def _zero_result(self, tool: str = "search_corpus") -> dict:
        return {
            "tool": tool,
            "success": True,
            "signal": "no_sources",
            "sources": [],
            "result": "I didn't find anything specific.",
        }

    def test_two_zero_results_trigger_tool_exhaustion(self):
        """The exact Molina live trace: three calls to search_corpus
        with different queries, all zero-result. Before this fix,
        none counted as failures and exhaustion never fired. After
        the fix, the second call already has the counter at 2 —
        the third call's should_block returns a FailedAttempt with
        error_code='tool_exhausted'."""
        guard = ReactRetryGuard()

        # Round 1: first zero-result for search_corpus
        guard.record_result(
            tool="search_corpus",
            inputs={"query": "attempt 1"},
            result=self._zero_result(),
            round=1,
            results_count_before=0,
        )
        # Round 2: second zero-result (different query string —
        # different inputs_sig — but still zero-result)
        guard.record_result(
            tool="search_corpus",
            inputs={"query": "attempt 2"},
            result=self._zero_result(),
            round=2,
            results_count_before=1,
        )

        # Before this commit: guard.consecutive_failures_per_tool
        # would still be 0 (zero-result not classified as failure).
        # After: it's 2.
        assert guard.consecutive_failures_per_tool["search_corpus"] == 2

        # Round 3: planner tries search_corpus yet again with a third
        # query. should_block must return a 'tool_exhausted' envelope
        # so the ReAct loop skips and the planner pivots.
        blocked = guard.should_block(
            tool="search_corpus",
            inputs={"query": "attempt 3"},
            current_results_count=2,
        )
        assert blocked is not None
        assert blocked.error_code == "tool_exhausted"
        assert blocked.tool == "search_corpus"

    def test_one_zero_result_does_not_trigger_exhaustion(self):
        """A single zero-result is noise, not a pattern — don't
        block on the first miss. The operator's framing: '_
        TOOL_EXHAUSTION_THRESHOLD = 2' (one is noise, two is
        pattern)."""
        guard = ReactRetryGuard()
        guard.record_result(
            tool="search_corpus",
            inputs={"query": "attempt 1"},
            result=self._zero_result(),
            round=1,
            results_count_before=0,
        )
        assert guard.consecutive_failures_per_tool["search_corpus"] == 1
        # Second call with different query: not yet blocked.
        blocked = guard.should_block(
            tool="search_corpus",
            inputs={"query": "attempt 2"},
            current_results_count=1,
        )
        assert blocked is None

    def test_success_between_zero_results_resets_counter(self):
        """A successful call of the same tool between zero-results
        clears the streak — the per-tool counter goes to 0. This
        prevents a long-running session from spuriously blocking
        tools that hit a rough patch but recovered."""
        guard = ReactRetryGuard()
        # First: zero-result
        guard.record_result(
            tool="search_corpus",
            inputs={"query": "attempt 1"},
            result=self._zero_result(),
            round=1,
            results_count_before=0,
        )
        # Second: success (non-zero)
        guard.record_result(
            tool="search_corpus",
            inputs={"query": "attempt 2"},
            result={
                "tool": "search_corpus",
                "success": True,
                "signal": "corpus_only",
                "sources": [{"document_name": "M", "page": 1}],
            },
            round=2,
            results_count_before=1,
        )
        # Counter reset: the success cleared the streak.
        assert guard.consecutive_failures_per_tool["search_corpus"] == 0

    def test_mixed_failures_and_zero_results_count_together(self):
        """A tool that errors once + zero-results once should still
        count toward exhaustion. Both are 'this tool isn't
        producing useful output' for the planner's purposes."""
        guard = ReactRetryGuard()
        # Round 1: hard failure
        guard.record_result(
            tool="search_corpus",
            inputs={"query": "attempt 1"},
            result={"tool": "search_corpus", "success": False},
            round=1,
            results_count_before=0,
        )
        # Round 2: zero-result
        guard.record_result(
            tool="search_corpus",
            inputs={"query": "attempt 2"},
            result=self._zero_result(),
            round=2,
            results_count_before=1,
        )
        # Combined = 2, at threshold.
        assert guard.consecutive_failures_per_tool["search_corpus"] == 2
        blocked = guard.should_block(
            tool="search_corpus",
            inputs={"query": "attempt 3"},
            current_results_count=2,
        )
        assert blocked is not None
        assert blocked.error_code == "tool_exhausted"


# ── Integration: all_rounds_failed and failure-hint prompt ────────


class TestLoopIntegration:
    def test_all_zero_results_trips_fail_fast(self):
        """When every round produced a zero-result, all_rounds_failed
        returns True so the ReAct loop can short-circuit to honest
        escalation instead of burning more rounds. Today the loop
        still runs to max_it because zero-results weren't failures."""
        guard = ReactRetryGuard()
        for i in range(3):
            guard.record_result(
                tool="search_corpus",
                inputs={"query": f"q{i}"},
                result={
                    "tool": "search_corpus",
                    "success": True,
                    "signal": "no_sources",
                    "sources": [],
                },
                round=i + 1,
                results_count_before=i,
            )
        assert guard.all_rounds_failed(rounds_completed=3) is True

    def test_failure_hint_lists_zero_results(self):
        """The hint injected into the next round's reasoning context
        should mention zero-results so the LLM sees which calls it
        made that returned nothing and doesn't repeat them. Ideally
        lists them with error_code='no_results' so the planner can
        tell 'tool errored' from 'tool found nothing'."""
        guard = ReactRetryGuard()
        guard.record_result(
            tool="search_corpus",
            inputs={"query": "q1"},
            result={
                "tool": "search_corpus",
                "success": True,
                "signal": "no_sources",
                "sources": [],
            },
            round=1,
            results_count_before=0,
        )
        hint = guard.failure_hint_for_prompt()
        assert "search_corpus" in hint
        assert "no_results" in hint
