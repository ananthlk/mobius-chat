"""Sprint A.1 commit 3 — fan-out helpers for the 7 remaining promoted signals.

Commit 1 introduced 5 critic helpers. This commit adds:

  - make_tool_exhausted             (insight / med)
  - make_tool_failed                (failure / med, only non-recoverable)
  - make_rate_limit_hit             (failure / high)
  - make_guidance_mode_activated    (insight / low)
  - make_confidence_filter_dropped_all  (insight / low)
  - make_turn_started               (chat-side only)
  - make_turn_completed             (info / low — throughput dashboards)
  - make_turn_failed                (failure / high — top-level failure rate)

Tests lock the promotion policy of each helper — if someone flips
report_to_task_manager or changes task_type/severity, the analytics
dashboards break in ways that aren't obvious from the code change
alone. This file is the safety net.
"""

from __future__ import annotations

import pytest

from app.communication.emit_envelope import (
    make_confidence_filter_dropped_all,
    make_guidance_mode_activated,
    make_rate_limit_hit,
    make_tool_exhausted,
    make_tool_failed,
    make_turn_completed,
    make_turn_failed,
    make_turn_started,
)


# ── Tool signals ────────────────────────────────────────────────────


class TestToolSignals:
    def test_tool_exhausted_promoted_as_insight_med(self):
        """Tool-exhaustion is the main analytics signal for RAG / tool
        quality tuning — which tools burn rounds without producing
        useful output. Must promote, otherwise the chat PM can't
        see the pattern."""
        env = make_tool_exhausted(
            correlation_id="c-1",
            round=3,
            tool="search_corpus",
            attempts=2,
        )
        assert env.report_to_task_manager is True
        assert env.task_type == "insight"
        assert env.task_severity == "med"
        assert env.data["tool"] == "search_corpus"
        assert env.data["attempts_before_exhaustion"] == 2

    def test_tool_failed_retryable_is_NOT_promoted(self):
        """Retryable failures (rate_limit, timeout, transient) get
        handled by the retry path. Promoting each one would flood
        task-manager with noise that's already represented via the
        aggregate tool_exhausted signal. Retryable = False-promotion
        is the policy."""
        env = make_tool_failed(
            correlation_id="c-1",
            round=2,
            tool="google_search",
            error_code="timeout",
            error_message="connection timed out",
            retryable=True,
        )
        assert env.report_to_task_manager is False
        assert env.task_type is None  # not promoted → no task type

    def test_tool_failed_non_recoverable_is_promoted_as_failure(self):
        """Non-recoverable errors (auth, validation, refusal, hard-500)
        signal an operator issue. Promote as failure/med for
        per-tool error-rate dashboards."""
        env = make_tool_failed(
            correlation_id="c-1",
            round=2,
            tool="healthcare_query",
            error_code="auth_error",
            error_message="API key invalid",
            retryable=False,
        )
        assert env.report_to_task_manager is True
        assert env.task_type == "failure"
        assert env.task_severity == "med"

    def test_rate_limit_hit_is_promoted_as_failure_high(self):
        """Rate-limiting is an operator-visible capacity issue (the
        2026-04-19 'Anthropic 400 credits' class). HIGH severity
        so it surfaces immediately in the ops feed — not aggregated."""
        env = make_rate_limit_hit(
            correlation_id="c-1",
            round=1,
            tool="planner",
            provider="anthropic",
            retry_after_seconds=30.0,
        )
        assert env.report_to_task_manager is True
        assert env.task_type == "failure"
        assert env.task_severity == "high"
        assert env.data["provider"] == "anthropic"
        assert env.data["retry_after_seconds"] == 30.0


# ── Guidance / confidence signals ────────────────────────────────


class TestGuidanceAndConfidence:
    def test_guidance_activation_is_promoted_as_insight_low(self):
        """Frequency of guidance-mode activation signals tuning need:
        if it fires on every turn, the planner's completion threshold
        is too strict; if never, the 80/20 split isn't actually
        helping. Analytics-only; LOW severity."""
        env = make_guidance_mode_activated(
            correlation_id="c-1",
            round=5,
            rounds_remaining=2,
            tools_used_so_far=["search_corpus", "google_search"],
        )
        assert env.report_to_task_manager is True
        assert env.task_type == "insight"
        assert env.task_severity == "low"
        assert env.data["rounds_remaining"] == 2
        assert "search_corpus" in env.data["tools_used_so_far"]

    def test_confidence_filter_dropped_all_is_promoted_as_insight_low(self):
        """The 'silent retrieval kill' class the 0.5→0.3 fix
        addressed. Track frequency over time: if it drops, the fix
        worked; if it spikes, the threshold needs more tuning. LOW
        severity."""
        env = make_confidence_filter_dropped_all(
            correlation_id="c-1",
            round=1,
            query="Sunshine Health H0036",
            chunks_retrieved=5,
            confidence_min=0.3,
        )
        assert env.report_to_task_manager is True
        assert env.task_type == "insight"
        assert env.task_severity == "low"
        assert env.data["chunks_retrieved"] == 5
        assert env.data["confidence_min"] == 0.3

    def test_confidence_filter_query_preview_truncated(self):
        """Long queries get truncated to keep envelope compact."""
        env = make_confidence_filter_dropped_all(
            correlation_id="c-1",
            round=1,
            query="x" * 500,
            chunks_retrieved=0,
            confidence_min=0.3,
        )
        assert len(env.data["query_preview"]) == 200


# ── Turn-level signals ───────────────────────────────────────────


class TestTurnSignals:
    def test_turn_started_is_NOT_promoted(self):
        """Every turn starts — promoting would double every turn's
        event count and dilute the feed. The complementary
        turn_completed / turn_failed events carry outcome data."""
        env = make_turn_started(
            correlation_id="c-1",
            mode="agentic",
        )
        assert env.report_to_task_manager is False

    def test_turn_completed_is_promoted_as_info_low(self):
        """Throughput, cost-per-turn, rounds distribution. Core
        analytics dashboard. INFO not FAILURE because successful
        turns shouldn't look alarming. LOW severity."""
        env = make_turn_completed(
            correlation_id="c-1",
            rounds_used=3,
            tools_used=["search_corpus"],
            final_signal="corpus_only",
            duration_ms=4500,
            total_llm_tokens=2500,
            total_cost_usd=0.02,
        )
        assert env.report_to_task_manager is True
        assert env.task_type == "info"
        assert env.task_severity == "low"
        assert env.data["rounds_used"] == 3
        assert env.data["duration_ms"] == 4500
        assert env.data["total_cost_usd"] == 0.02

    def test_turn_failed_is_promoted_as_failure_high(self):
        """Top-level failure. Operators must see this in the feed
        immediately — HIGH severity. The chat PM watches this
        counter for regression spikes."""
        env = make_turn_failed(
            correlation_id="c-1",
            error_class="TimeoutError",
            stage="react_loop",
            error_message="LLM timeout after 60s",
            last_tool="search_corpus",
        )
        assert env.report_to_task_manager is True
        assert env.task_type == "failure"
        assert env.task_severity == "high"
        assert env.data["stage"] == "react_loop"
        assert env.data["error_class"] == "TimeoutError"
        assert env.data["last_tool"] == "search_corpus"

    def test_turn_failed_error_message_truncated(self):
        """Long tracebacks don't blow up the envelope payload."""
        env = make_turn_failed(
            correlation_id="c-1",
            error_class="RuntimeError",
            stage="x",
            error_message="x" * 2000,
        )
        assert len(env.data["error_message"]) == 500
