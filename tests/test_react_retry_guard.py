"""Smart-retry guard unit tests (Phase 0.7).

Direct regression for the pathology observed in the user's test run, where
the ReAct loop burned rounds re-picking tools that had already failed with
no new evidence in between.
"""

from __future__ import annotations

from app.pipeline.react_retry_guard import (
    FailedAttempt,
    ReactRetryGuard,
    inputs_signature,
)


# ── inputs_signature ───────────────────────────────────────────────────────


class TestInputsSignature:
    def test_empty_inputs_have_stable_sig(self):
        assert inputs_signature(None) == inputs_signature({}) == "empty"

    def test_same_inputs_same_sig(self):
        a = {"query": "Sunshine Health medical necessity H0036"}
        b = {"query": "Sunshine Health medical necessity H0036"}
        assert inputs_signature(a) == inputs_signature(b)

    def test_whitespace_and_case_normalized(self):
        a = {"query": "Sunshine Health"}
        b = {"query": "sunshine health "}
        assert inputs_signature(a) == inputs_signature(b)

    def test_key_order_doesnt_matter(self):
        a = {"query": "x", "org": "Y"}
        b = {"org": "y", "query": "X"}
        assert inputs_signature(a) == inputs_signature(b)

    def test_different_inputs_different_sig(self):
        assert inputs_signature({"query": "A"}) != inputs_signature({"query": "B"})

    def test_none_values_dropped(self):
        """query=None should normalize away (tool didn't meaningfully pick it)."""
        assert inputs_signature({"query": "x", "filter": None}) == inputs_signature({"query": "x"})


# ── Failure detection ──────────────────────────────────────────────────────


class TestFailureDetection:
    def test_success_false_counts_as_failure(self):
        g = ReactRetryGuard()
        g.record_result(
            tool="search_corpus",
            inputs={"query": "x"},
            result={"success": False, "result": "nothing"},
            round=1,
            results_count_before=0,
        )
        assert len(g.failed_attempts) == 1
        assert g.successful_attempts == 0

    def test_error_envelope_counts_as_failure(self):
        g = ReactRetryGuard()
        g.record_result(
            tool="search_corpus",
            inputs={"query": "x"},
            result={
                "success": True,  # caller said success, but error present
                "result": "ok",
                "error": {"error_code": "rate_limit", "user_facing_message": "busy"},
            },
            round=1,
            results_count_before=0,
        )
        assert len(g.failed_attempts) == 1
        assert g.failed_attempts[0].error_code == "rate_limit"

    def test_success_true_no_error_is_success(self):
        g = ReactRetryGuard()
        g.record_result(
            tool="search_corpus",
            inputs={"query": "x"},
            result={"success": True, "result": "answer"},
            round=1,
            results_count_before=0,
        )
        assert len(g.failed_attempts) == 0
        assert g.successful_attempts == 1


# ── should_block — the core rule ───────────────────────────────────────────


class TestShouldBlock:
    def test_repeat_same_tool_same_inputs_no_new_evidence_blocks(self):
        """The exact pathology: round 1 search_corpus fails, round 2 tries
        the SAME search_corpus with SAME query, no other tools ran in between.
        """
        g = ReactRetryGuard()
        g.record_result(
            tool="search_corpus",
            inputs={"query": "H0036 criteria"},
            result={"success": False, "error": {"error_code": "rate_limit"}},
            round=1,
            results_count_before=0,
        )
        # Round 2 wants to retry same thing — tool_results has 0 new entries.
        block = g.should_block(
            tool="search_corpus",
            inputs={"query": "H0036 criteria"},
            current_results_count=0,
        )
        assert block is not None
        assert block.tool == "search_corpus"
        assert block.error_code == "rate_limit"

    def test_different_inputs_do_not_block(self):
        """Planner refined the query — let it retry."""
        g = ReactRetryGuard()
        g.record_result(
            tool="search_corpus",
            inputs={"query": "H0036 criteria"},
            result={"success": False, "error": {"error_code": "rate_limit"}},
            round=1,
            results_count_before=0,
        )
        assert g.should_block(
            tool="search_corpus",
            inputs={"query": "Sunshine Health H0036 medical necessity"},
            current_results_count=0,
        ) is None

    def test_new_evidence_unblocks_same_tool(self):
        """Round 1 search_corpus fails, round 2 web_scrape succeeds, round 3
        wants to retry search_corpus — allow it (new evidence in context).
        """
        g = ReactRetryGuard()
        g.record_result(
            tool="search_corpus",
            inputs={"query": "H0036"},
            result={"success": False, "error": {"error_code": "rate_limit"}},
            round=1,
            results_count_before=0,
        )
        # web_scrape succeeded in round 2 → tool_results now has 1 entry after the failure.
        assert g.should_block(
            tool="search_corpus",
            inputs={"query": "H0036"},
            current_results_count=1,
        ) is None

    def test_different_tool_never_blocks(self):
        g = ReactRetryGuard()
        g.record_result(
            tool="search_corpus",
            inputs={"query": "x"},
            result={"success": False},
            round=1,
            results_count_before=0,
        )
        assert g.should_block(
            tool="web_scrape",
            inputs={"query": "x"},
            current_results_count=0,
        ) is None


# ── all_rounds_failed — fail-fast signal ───────────────────────────────────


class TestAllRoundsFailed:
    def test_all_rounds_failed_triggers_when_nothing_succeeded(self):
        g = ReactRetryGuard()
        for rn in (1, 2, 3):
            g.record_result(
                tool=f"tool_{rn}",
                inputs={"q": f"r{rn}"},
                result={"success": False, "error": {"error_code": "rate_limit"}},
                round=rn,
                results_count_before=rn - 1,
            )
        assert g.all_rounds_failed(rounds_completed=3) is True

    def test_all_rounds_failed_false_when_any_success(self):
        g = ReactRetryGuard()
        g.record_result(
            tool="search_corpus", inputs={"q": "x"},
            result={"success": False}, round=1, results_count_before=0,
        )
        g.record_result(
            tool="web_scrape", inputs={"q": "y"},
            result={"success": True, "result": "found it"},
            round=2, results_count_before=1,
        )
        assert g.all_rounds_failed(rounds_completed=2) is False

    def test_zero_rounds_returns_false(self):
        g = ReactRetryGuard()
        assert g.all_rounds_failed(rounds_completed=0) is False


# ── failure_hint_for_prompt ────────────────────────────────────────────────


class TestFailureHint:
    def test_empty_when_no_failures(self):
        assert ReactRetryGuard().failure_hint_for_prompt() == ""

    def test_lists_failed_attempts_for_llm(self):
        g = ReactRetryGuard()
        g.record_result(
            tool="search_corpus",
            inputs={"q": "a"},
            result={"success": False, "error": {"error_code": "rate_limit"}},
            round=1, results_count_before=0,
        )
        g.record_result(
            tool="web_scrape",
            inputs={"url": "https://x"},
            result={"success": False, "error": {"error_code": "scrape_failed"}},
            round=2, results_count_before=0,
        )
        hint = g.failure_hint_for_prompt()
        assert "search_corpus" in hint
        assert "rate_limit" in hint
        assert "web_scrape" in hint
        assert "scrape_failed" in hint
        assert "do not repeat" in hint.lower()
