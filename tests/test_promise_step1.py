"""Promise step 1 — the boundary: opened at POST, closed exactly once at PUBLISH.

Work order: docs/work-order-promise-step1.md. These tests cover the SHAPE and
the CLOSE GUARANTEE. They deliberately do NOT stand in for the definition of
done, which is demonstrated evidence from real dev turns (written / persisted /
emitted). A green suite here can coexist with nothing reaching the database —
that is the exact failure this program keeps finding, so these assert what unit
tests can honestly assert and no more.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.pipeline.react.promise import (
    PROMISE_VERSION,
    Promise,
    close_promise,
    from_payload,
    open_promise,
    to_payload,
)


class TestOpenPromise:
    @pytest.mark.parametrize("mode,tier,lat,cost,qual", [
        ("quick", "fast", 13.0, 16.0, "none"),
        ("copilot", "normal", 31.0, 45.0, "some"),
        ("agentic", "thinking", 95.0, 81.0, "best"),
    ])
    def test_section7_tiers_are_carried_verbatim(self, mode, tier, lat, cost, qual):
        p = open_promise(mode, datetime.now(UTC))
        assert (p.tier, p.latency_s, p.cost_c, p.quality) == (tier, lat, cost, qual)
        assert p.version == PROMISE_VERSION
        assert p.unpromised_reason is None

    def test_case_and_whitespace_tolerated(self):
        assert open_promise("  AGENTIC ", datetime.now(UTC)).tier == "thinking"

    def test_task_mode_promises_nothing_and_says_why(self):
        """`task` is a legal chat_mode with no section-7 row. It must NOT be
        mapped to a neighbouring tier — that would invent a promise."""
        p = open_promise("task", datetime.now(UTC))
        assert p.tier is None
        assert (p.latency_s, p.cost_c, p.quality) == (None, None, None)
        assert "task" in (p.unpromised_reason or "")

    def test_absent_mode_promises_nothing_and_says_why(self):
        """POST cannot state a tier when chat_mode is absent: the worker
        resolves it from thread state (orchestrator.py:708-712)."""
        p = open_promise(None, datetime.now(UTC))
        assert p.tier is None
        assert "thread state" in (p.unpromised_reason or "")

    def test_unpromised_is_distinguishable_from_no_promise_at_all(self):
        """The distinction the work order turns on: a task-mode turn made a
        promise (of nothing); a pre-deploy turn made none. If these collapsed,
        the gap being measured would be invisible."""
        unpromised = open_promise("task", datetime.now(UTC))
        assert unpromised is not None
        assert from_payload(None) is None


class TestPayloadRoundTrip:
    def test_round_trip_is_lossless(self):
        p = open_promise("agentic", datetime.now(UTC))
        assert from_payload(to_payload(p)) == p

    @pytest.mark.parametrize("bad", [None, {}, {"posted_at": "not-a-date"}, "string", 42])
    def test_absent_or_unreadable_yields_none_never_a_guess(self, bad):
        assert from_payload(bad) is None

    def test_naive_datetime_is_assumed_utc_not_local(self):
        d = to_payload(open_promise("quick", datetime.now(UTC)))
        d["posted_at"] = "2026-09-10T12:00:00"       # no tzinfo
        assert from_payload(d).posted_at.tzinfo is not None


class TestClosePromise:
    def test_delivered_latency_spans_post_to_publish(self):
        posted = datetime.now(UTC)
        p = open_promise("copilot", posted)
        a = close_promise(p, correlation_id="c1", outcome="completed",
                          now=posted + timedelta(seconds=42.5), worker_latency_s=30.0)
        assert a.delivered_latency_s == pytest.approx(42.5)
        assert a.worker_latency_s == 30.0
        # The number the work order exists to expose:
        assert a.delivered_latency_s - a.worker_latency_s == pytest.approx(12.5)

    def test_a_turn_with_no_promise_still_attests(self):
        """Pre-deploy enqueue. A missing row would be indistinguishable from a
        turn that never ran."""
        a = close_promise(None, correlation_id="c2", outcome="completed",
                          now=datetime.now(UTC), worker_latency_s=1.0)
        assert a is not None
        assert (a.promise_version, a.tier, a.posted_at, a.delivered_latency_s) == \
               (None, None, None, None)
        assert "enqueued before" in (a.notes or "")

    def test_clock_skew_records_null_and_reason_never_a_clamped_zero(self):
        """A clamped zero is indistinguishable from a very fast turn."""
        posted = datetime.now(UTC)
        p = open_promise("quick", posted)
        a = close_promise(p, correlation_id="c3", outcome="completed",
                          now=posted - timedelta(seconds=3), worker_latency_s=1.0)
        assert a.delivered_latency_s is None
        assert "skew" in (a.notes or "")

    def test_step3_terms_are_explicit_nulls_not_zeros(self):
        a = close_promise(open_promise("quick", datetime.now(UTC)),
                          correlation_id="c4", outcome="completed",
                          now=datetime.now(UTC))
        assert a.delivered_cost_c is None, "0.0 would read as a cheap turn"
        assert a.delivered_quality is None

    def test_missing_outcome_becomes_unknown_not_success(self):
        a = close_promise(None, correlation_id="c5", outcome="",
                          now=datetime.now(UTC))
        assert a.outcome == "unknown"


class TestCloseGuarantee:
    """The point of section 2c: the promise closes on EVERY exit, not just the
    happy one. These drive run_pipeline and assert the attestation writer ran."""

    def _run(self, boom: bool):
        from app.pipeline import orchestrator as orch
        seen: list = []
        with patch.object(orch, "start_progress"), \
             patch("app.pipeline.react.promise.write", side_effect=lambda a: seen.append(a)), \
             patch.object(orch, "_publish_failed"), \
             patch.object(orch, "run_state_load", side_effect=RuntimeError("boom") if boom else None):
            try:
                orch.run_pipeline("cid-guard", "hello", None, t0_start=0.0)
            except Exception:
                pass
        return seen

    def test_attestation_is_written_even_when_the_turn_raises(self):
        seen = self._run(boom=True)
        assert len(seen) == 1, "a failed turn must still close its promise"

    def test_exactly_one_attestation_per_turn(self):
        seen = self._run(boom=True)
        assert len(seen) == 1, "a finally that fires twice writes two rows"
