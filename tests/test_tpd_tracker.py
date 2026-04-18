"""Phase 2.5b — daily-quota-aware bandit.

tpd_tracker guards the LLM router against end-of-day 429s. Phase 2.5
already filtered candidates by per-minute TPM; 2.5b adds the per-day
TPD budget + reactive 429 "try again in X" honoring. Observed live
2026-04-17: 99946 / 100_000 tokens used on llama-3.3-70b-versatile,
every turn's integrator blocked until the 24-hour window rolled.

Coverage:
  - record_usage → get_used_today rolling sum
  - window prunes entries > 24h old
  - is_exhausted honors spec_tpd vs used+request projection with safety margin
  - None spec_tpd = unlimited (never exhausted by usage)
  - mark_rate_limited_until short-circuits is_exhausted
  - hold expires cleanly (next is_exhausted check clears stale deadline)
  - parse_retry_after_seconds handles the observed Groq formats
  - thread-safety under contention
  - snapshot for /metrics
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset_tracker():
    """Every test gets a fresh tracker — module-level state would
    otherwise leak counts across tests."""
    from app.services import tpd_tracker
    tpd_tracker.reset()
    yield
    tpd_tracker.reset()


# ── Recording and rolling-sum reads ──────────────────────────────────────


class TestRecordAndReadUsage:
    def test_single_record_appears_in_sum(self):
        from app.services import tpd_tracker
        tpd_tracker.record_usage("m-1", 500)
        assert tpd_tracker.get_used_today("m-1") == 500

    def test_multiple_records_accumulate(self):
        from app.services import tpd_tracker
        tpd_tracker.record_usage("m-1", 100)
        tpd_tracker.record_usage("m-1", 200)
        tpd_tracker.record_usage("m-1", 300)
        assert tpd_tracker.get_used_today("m-1") == 600

    def test_records_are_per_model(self):
        from app.services import tpd_tracker
        tpd_tracker.record_usage("m-1", 100)
        tpd_tracker.record_usage("m-2", 500)
        assert tpd_tracker.get_used_today("m-1") == 100
        assert tpd_tracker.get_used_today("m-2") == 500

    def test_zero_or_negative_tokens_ignored(self):
        """Guards against partial usage dicts where the field is missing
        and returns 0/-1. Don't pollute the window with meaningless rows."""
        from app.services import tpd_tracker
        tpd_tracker.record_usage("m-1", 0)
        tpd_tracker.record_usage("m-1", -50)
        assert tpd_tracker.get_used_today("m-1") == 0

    def test_empty_model_id_ignored(self):
        from app.services import tpd_tracker
        tpd_tracker.record_usage("", 500)
        # Nothing crashed, and nothing landed in the tracker for "".
        assert tpd_tracker.get_used_today("") == 0

    def test_unknown_model_returns_zero(self):
        from app.services import tpd_tracker
        assert tpd_tracker.get_used_today("never-seen") == 0


class TestRollingWindow:
    def test_entries_older_than_24h_drop_off(self):
        """The tracker uses a 24h rolling window. Entries older than that
        must not count toward today's total — otherwise budgets never reset."""
        from app.services import tpd_tracker

        # Freeze time via monotonic patch. Insert 1000 tokens "yesterday"
        # (25h ago), then advance and insert 500 "now". Only 500 should count.
        with patch("app.services.tpd_tracker.time") as mock_time:
            # First record at t=0
            mock_time.monotonic.return_value = 0.0
            tpd_tracker.record_usage("m-1", 1000)
            # Jump forward 25 hours
            mock_time.monotonic.return_value = 25 * 3600
            tpd_tracker.record_usage("m-1", 500)
            # Read — the old 1000 must have been pruned
            assert tpd_tracker.get_used_today("m-1") == 500

    def test_entry_exactly_at_24h_boundary_is_pruned(self):
        from app.services import tpd_tracker

        with patch("app.services.tpd_tracker.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            tpd_tracker.record_usage("m-1", 1000)
            # The window is [now - 24h, now]. Reading at exactly 24h later
            # should prune the old entry (strictly less-than cutoff).
            mock_time.monotonic.return_value = 24 * 3600 + 1
            assert tpd_tracker.get_used_today("m-1") == 0


# ── is_exhausted: the router's question ──────────────────────────────────


class TestIsExhausted:
    def test_unlimited_model_never_exhausted(self):
        """spec_tpd=None means the tracker has no opinion (unknown or
        unlimited). The router treats that model as always eligible."""
        from app.services import tpd_tracker
        tpd_tracker.record_usage("m-1", 1_000_000)
        assert tpd_tracker.is_exhausted("m-1", None, 5_000) is False

    def test_well_under_limit_not_exhausted(self):
        from app.services import tpd_tracker
        tpd_tracker.record_usage("m-1", 10_000)
        assert tpd_tracker.is_exhausted("m-1", 100_000, 5_000) is False

    def test_near_limit_with_safety_margin_blocks(self):
        """5% safety margin. At 99000 used + 5000 requested on a 100000
        limit, projected = 104000 ≥ 100000 → blocked even though strict
        arithmetic would permit it. This is the exact scenario from
        2026-04-17: 99946/100000 used, every subsequent call failed."""
        from app.services import tpd_tracker
        tpd_tracker.record_usage("m-1", 99_000)
        assert tpd_tracker.is_exhausted("m-1", 100_000, 5_000) is True

    def test_sum_plus_request_exceeds_limit(self):
        from app.services import tpd_tracker
        tpd_tracker.record_usage("m-1", 95_000)
        # 95k + 10k = 105k, × 1.05 safety = 110.25k > 100k limit
        assert tpd_tracker.is_exhausted("m-1", 100_000, 10_000) is True

    def test_empty_model_id_not_exhausted(self):
        from app.services import tpd_tracker
        assert tpd_tracker.is_exhausted("", 100_000, 5_000) is False


# ── 429 retry-after hold ─────────────────────────────────────────────────


class TestRateLimitedHold:
    def test_marked_model_is_exhausted_until_hold_expires(self):
        from app.services import tpd_tracker

        with patch("app.services.tpd_tracker.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0
            tpd_tracker.mark_rate_limited_until("m-1", 1000.0 + 600)  # 10-min hold

            # Inside the hold → exhausted regardless of usage.
            mock_time.monotonic.return_value = 1100.0
            assert tpd_tracker.is_exhausted("m-1", spec_tpd=None, request_tokens=0) is True

            # After the hold → not exhausted.
            mock_time.monotonic.return_value = 1700.0
            assert tpd_tracker.is_exhausted("m-1", spec_tpd=None, request_tokens=0) is False

    def test_hold_trumps_usage_quota(self):
        """A 429 hold blocks the model even if TPD math says it has room.
        Provider-sent signals win over our local accounting."""
        from app.services import tpd_tracker

        with patch("app.services.tpd_tracker.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0
            # Usage: essentially nothing.
            tpd_tracker.record_usage("m-1", 10)
            # But provider says try again later.
            tpd_tracker.mark_rate_limited_until("m-1", 1000.0 + 300)
            mock_time.monotonic.return_value = 1100.0
            assert tpd_tracker.is_exhausted("m-1", spec_tpd=1_000_000, request_tokens=1) is True

    def test_expired_hold_is_cleared_on_check(self):
        """Stale deadlines must not accumulate. On the first check after
        expiry, the tracker should clear the flag so subsequent requests
        take the fast path."""
        from app.services import tpd_tracker

        with patch("app.services.tpd_tracker.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0
            tpd_tracker.mark_rate_limited_until("m-1", 1000.0 + 60)
            # Jump past the hold.
            mock_time.monotonic.return_value = 2000.0
            # First check clears it.
            assert tpd_tracker.is_exhausted("m-1", None, 0) is False
            # Snapshot confirms the flag is gone (None).
            snap = tpd_tracker.snapshot()
            assert snap["m-1"]["rate_limited_until"] is None


# ── parse_retry_after_seconds ─────────────────────────────────────────────


class TestParseRetryAfterSeconds:
    def test_groq_hours_minutes_seconds(self):
        from app.services.tpd_tracker import parse_retry_after_seconds
        # Exact message observed 2026-04-17
        msg = "Please try again in 1h28m56.928s"
        result = parse_retry_after_seconds(msg)
        assert result is not None
        # 1*3600 + 28*60 + 56.928 = 5336.928
        assert abs(result - 5336.928) < 0.01

    def test_groq_minutes_seconds(self):
        from app.services.tpd_tracker import parse_retry_after_seconds
        msg = "Please try again in 9m29.376s"
        result = parse_retry_after_seconds(msg)
        assert result is not None
        assert abs(result - (9 * 60 + 29.376)) < 0.01

    def test_seconds_only(self):
        from app.services.tpd_tracker import parse_retry_after_seconds
        assert parse_retry_after_seconds("try again in 45s") == pytest.approx(45)

    def test_retry_after_header_form(self):
        from app.services.tpd_tracker import parse_retry_after_seconds
        # Some providers send just a numeric Retry-After instead of prose
        assert parse_retry_after_seconds("Retry-After: 120") == pytest.approx(120)
        assert parse_retry_after_seconds("retry-after 60") == pytest.approx(60)

    def test_no_hint_returns_none(self):
        from app.services.tpd_tracker import parse_retry_after_seconds
        assert parse_retry_after_seconds("some unrelated error") is None
        assert parse_retry_after_seconds("") is None
        assert parse_retry_after_seconds(None) is None  # type: ignore[arg-type]

    def test_zero_or_missing_duration_returns_none(self):
        """If the parse matches the regex but every group is zero, the
        hint is effectively absent — don't mark a zero-duration hold."""
        from app.services.tpd_tracker import parse_retry_after_seconds
        # Match-but-empty: "try again in " with no numbers should not fire
        # (regex requires at least one group populated). Defensive edge.
        assert parse_retry_after_seconds("try again in ") is None

    def test_case_insensitive_match(self):
        from app.services.tpd_tracker import parse_retry_after_seconds
        assert parse_retry_after_seconds("TRY AGAIN IN 30S") is not None


# ── Thread-safety smoke test ─────────────────────────────────────────────


class TestThreadSafety:
    def test_concurrent_record_does_not_lose_updates(self):
        """Multiple workers recording concurrently must produce the exact
        sum of all their contributions. Data-race would cause an under-count."""
        from app.services import tpd_tracker

        N_THREADS = 20
        N_PER_THREAD = 50
        TOKENS = 100

        def worker():
            for _ in range(N_PER_THREAD):
                tpd_tracker.record_usage("m-concurrent", TOKENS)

        threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert tpd_tracker.get_used_today("m-concurrent") == N_THREADS * N_PER_THREAD * TOKENS


# ── snapshot ──────────────────────────────────────────────────────────────


class TestSnapshot:
    def test_empty_snapshot(self):
        from app.services import tpd_tracker
        assert tpd_tracker.snapshot() == {}

    def test_snapshot_shape(self):
        from app.services import tpd_tracker
        tpd_tracker.record_usage("m-1", 1500)
        snap = tpd_tracker.snapshot()
        assert "m-1" in snap
        assert snap["m-1"]["used_today"] == 1500
        assert snap["m-1"]["window_size"] >= 1
        assert snap["m-1"]["rate_limited_until"] is None


# ── Integration: ModelSpec + filter ─────────────────────────────────────


class TestModelSpecAndFilterIntegration:
    """End-to-end: a Groq spec with spec_tpd_limit, the tracker records
    near-exhaustion usage, then the filter drops the candidate. This is
    the actual 2026-04-17 scenario wrapped as a single assertion."""

    def test_exhausted_groq_trimmed_from_candidate_pool(self):
        from app.services import tpd_tracker
        from app.services.model_registry import _filter_by_token_budget, ModelSpec

        # Groq-ish spec with the observed daily cap.
        groq_spec = ModelSpec(
            model_id="llama-3.3-70b-versatile",
            provider="groq",
            display_name="Groq 70B",
            spec_context_k=131,
            spec_tpm_limit=12_000,
            spec_tpd_limit=100_000,
        )
        gemini_spec = ModelSpec(
            model_id="gemini-2.5-flash",
            provider="vertex",
            display_name="Gemini Flash",
            spec_context_k=1000,
            spec_tpm_limit=None,
            spec_tpd_limit=None,  # unlimited
        )

        # Simulate end-of-day usage on Groq.
        tpd_tracker.record_usage("llama-3.3-70b-versatile", 99_000)

        surviving, meta = _filter_by_token_budget(
            candidates=[groq_spec, gemini_spec],
            estimated_prompt_tokens=4_000,
            expected_output_tokens=3_000,
        )
        surviving_ids = [c.model_id for c in surviving]
        assert "llama-3.3-70b-versatile" not in surviving_ids, (
            "Exhausted Groq model must be trimmed before it reaches the bandit — "
            "this is the whole point of Phase 2.5b."
        )
        assert "gemini-2.5-flash" in surviving_ids
        assert meta["candidates_trimmed_by_tpd"] == 1
        assert "llama-3.3-70b-versatile" in meta.get("tpd_trimmed_models", [])

    def test_fresh_groq_passes_when_not_exhausted(self):
        """Regression: we shouldn't be over-trimming. Fresh morning, low
        usage, the Groq model must still be a candidate."""
        from app.services.model_registry import _filter_by_token_budget, ModelSpec

        groq_spec = ModelSpec(
            model_id="llama-3.3-70b-versatile",
            provider="groq",
            display_name="Groq 70B",
            spec_context_k=131,
            spec_tpm_limit=12_000,
            spec_tpd_limit=100_000,
        )
        surviving, meta = _filter_by_token_budget(
            candidates=[groq_spec],
            estimated_prompt_tokens=1_000,
            expected_output_tokens=500,
        )
        assert [c.model_id for c in surviving] == ["llama-3.3-70b-versatile"]
        assert meta["candidates_trimmed_by_tpd"] == 0
