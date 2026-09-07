"""Task #108-adjacent (Service Line Facts, 2026-09-07): a hard
account-level refusal (bad key, exhausted credit, revoked permission)
fails on every call, not just a fraction of a sliding window or a
second sample of the same model_id. Before this fix, neither health
tracker could ever degrade a model for this failure class:

  - model_registry._LiveHealth only counted `was_timeout` outcomes
    toward its threshold; a non-timeout failure never moved the needle
    no matter how many times it happened.
  - llm_health.LlmHealthState._evaluate() aggregated recent_failures
    per row but never compared it to a threshold — only recent_timeouts
    was checked.

These tests cover the fix: immediate degradation on a classified
permanent failure (in-memory tracker), and threshold-based degradation
on repeated non-timeout failures (Postgres-view tracker).
"""
from app.services.model_registry import _LiveHealth, classify_permanent_failure
from app.services.llm_health import LlmHealthState, _ModelHealthRow


class TestClassifyPermanentFailure:
    def test_anthropic_credit_balance_too_low(self):
        exc = 'Anthropic API error 400: {"error": {"message": "Your credit balance is too low to access the Anthropic API."}}'
        assert classify_permanent_failure(exc) == "credit balance is too low"

    def test_invalid_api_key(self):
        assert classify_permanent_failure("Invalid API Key provided") == "invalid api key"

    def test_bare_401(self):
        assert classify_permanent_failure("HTTP error 401 Unauthorized") == "http_401"

    def test_bare_403(self):
        assert classify_permanent_failure("request failed: 403 Forbidden") == "http_403"

    def test_rate_limit_429_is_not_permanent(self):
        # 429s are transient and already handled by tpd_tracker's
        # retry-after tracking — must not be classified as permanent.
        assert classify_permanent_failure("429 Too Many Requests. Please try again in 1h28m56s") is None

    def test_generic_timeout_is_not_permanent(self):
        assert classify_permanent_failure("TimeoutError: abandoned after 60s") is None

    def test_empty_string(self):
        assert classify_permanent_failure("") is None


class TestLiveHealthPermanentReason:
    def test_single_permanent_failure_degrades_immediately(self):
        lh = _LiveHealth()
        assert lh.is_degraded("claude-opus-4-6") is False
        # Only ONE call recorded — well below LIVE_HEALTH_MIN_SAMPLES —
        # but a permanent_reason must degrade on the very first sample.
        lh.record_outcome(
            "claude-opus-4-6", latency_ms=850, was_timeout=False,
            permanent_reason="credit balance is too low",
        )
        assert lh.is_degraded("claude-opus-4-6") is True
        reason = lh.degradation_reason("claude-opus-4-6")
        assert "permanent failure" in reason
        assert "credit balance is too low" in reason

    def test_single_non_permanent_failure_does_not_degrade(self):
        lh = _LiveHealth()
        lh.record_outcome("claude-sonnet-5", latency_ms=900, was_timeout=False, permanent_reason=None)
        assert lh.is_degraded("claude-sonnet-5") is False

    def test_different_models_each_degrade_independently_on_first_hit(self):
        # Reproduces the reported scenario: opus-4-6 fails once, then
        # sonnet-5 fails once (a different model each turn) — both must
        # degrade on their own first permanent failure, not require a
        # second sample of the SAME model_id.
        lh = _LiveHealth()
        lh.record_outcome("claude-opus-4-6", latency_ms=800, was_timeout=False, permanent_reason="credit balance is too low")
        lh.record_outcome("claude-sonnet-5", latency_ms=800, was_timeout=False, permanent_reason="credit balance is too low")
        assert lh.is_degraded("claude-opus-4-6") is True
        assert lh.is_degraded("claude-sonnet-5") is True


class TestLlmHealthEvaluateNonTimeoutFailures:
    def test_repeated_non_timeout_failures_degrade(self):
        row = _ModelHealthRow(
            model="claude-opus-4-6", stage="react_1",
            recent_total=5, recent_timeouts=0, recent_failures=3,
            recent_avg_latency_ms=500.0, recent_p95_latency_ms=600.0,
        )
        reason = LlmHealthState._evaluate(row, ema_latency_ms=500.0)
        assert reason is not None
        assert "failed (non-timeout)" in reason

    def test_below_threshold_stays_healthy(self):
        row = _ModelHealthRow(
            model="claude-opus-4-6", stage="react_1",
            recent_total=5, recent_timeouts=0, recent_failures=1,
            recent_avg_latency_ms=500.0, recent_p95_latency_ms=600.0,
        )
        assert LlmHealthState._evaluate(row, ema_latency_ms=500.0) is None

    def test_below_min_total_stays_healthy_even_with_all_failures(self):
        row = _ModelHealthRow(
            model="claude-opus-4-6", stage="react_1",
            recent_total=1, recent_timeouts=0, recent_failures=1,
            recent_avg_latency_ms=500.0, recent_p95_latency_ms=600.0,
        )
        assert LlmHealthState._evaluate(row, ema_latency_ms=500.0) is None

    def test_timeout_path_still_works(self):
        row = _ModelHealthRow(
            model="gemini-2.5-flash", stage="react_1",
            recent_total=5, recent_timeouts=2, recent_failures=2,
            recent_avg_latency_ms=500.0, recent_p95_latency_ms=600.0,
        )
        reason = LlmHealthState._evaluate(row, ema_latency_ms=500.0)
        assert "timed out" in reason
