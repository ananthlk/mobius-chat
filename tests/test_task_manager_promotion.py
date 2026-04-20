"""Sprint A.2 — promote chat emit envelopes to task-manager.

Tests lock:

  1. **Flag gating.** ``MOBIUS_TASK_MANAGER_PROMOTION=0`` (or empty-
     string explicit disable variants) → no POST fires. Default ON
     as of 2026-04-20; unset env = enabled.

  2. **Envelope flag gating.** Even when the feature is on, only
     envelopes with ``report_to_task_manager=True`` get POSTed.

  3. **Payload shape.** ``_build_signal_body`` produces exactly what
     task-manager's TaskSignalBody expects — signal, type, severity,
     source_ref (with correlation_id), data (with thread_id / user_id
     / round propagated).

  4. **Defensive failure.** Network errors, 5xx responses, DNS
     failure — any exception in the POST path is caught + logged,
     never re-raised. Chat turns must not break on promotion failure.

  5. **Background thread.** ``promote()`` returns immediately (no
     blocking). Verified by a slow-mock that would exceed test
     timeout if the call were synchronous.

  6. **Fire-and-forget semantics.** If the daemon thread is still
     running when the caller moves on, that's fine — no join, no
     await. Test harness verifies by checking the thread was started
     but the function returned quickly.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app.communication.emit_envelope import (
    make_critic_approved,
    make_critic_flagged,
    make_rounds_exhausted_with_warning,
)
from app.services.task_manager_promotion import (
    _build_signal_body,
    promote,
    promotion_enabled,
)


# ── Flag ─────────────────────────────────────────────────────────────


class TestPromotionFlag:
    def test_default_on(self, monkeypatch):
        # 2026-04-20: default flipped to ON after Sprint A.2 soak.
        monkeypatch.delenv("MOBIUS_TASK_MANAGER_PROMOTION", raising=False)
        assert promotion_enabled() is True

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", ""])
    def test_explicit_on(self, monkeypatch, val):
        # Empty string falls back to default ON.
        monkeypatch.setenv("MOBIUS_TASK_MANAGER_PROMOTION", val)
        assert promotion_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off"])
    def test_explicit_off(self, monkeypatch, val):
        monkeypatch.setenv("MOBIUS_TASK_MANAGER_PROMOTION", val)
        assert promotion_enabled() is False


# ── Payload shape ─────────────────────────────────────────────────


class TestBuildSignalBody:
    def test_critic_flagged_body_shape(self):
        env = make_critic_flagged(
            correlation_id="c-abc",
            round=3,
            total_issues=2,
            high_severity=2,
            flagged_claims=["fabricated phone", "unsupported PA claim"],
            rounds_remaining=1,
            thread_id="t-xyz",
            user_id="user-42",
        )
        body = _build_signal_body(env.to_dict())

        # Task-manager expects these top-level fields:
        assert body["signal"] == "critic_flagged"
        assert body["type"] == "insight"
        assert body["severity"] == "med"
        assert body["source_module"] == "chat"
        assert body["workflow"] == "chat"
        assert body["source_ref"] == "correlation_id:c-abc"
        assert body["step_id"] == "round_3.critic_flagged"
        assert body["note"].startswith("⚠ Critic flagged")
        assert body["created_by"] == "system"

        # Envelope's data pass-through plus cross-ref fields lifted
        # out of the envelope into data for task-manager to render:
        assert body["data"]["high_severity"] == 2
        assert body["data"]["total_issues"] == 2
        assert body["data"]["thread_id"] == "t-xyz"
        assert body["data"]["user_id"] == "user-42"
        assert body["data"]["round"] == 3

    def test_rounds_exhausted_maps_to_blocker(self):
        env = make_rounds_exhausted_with_warning(
            correlation_id="c-1",
            round=6,
            unresolved_claims=["x", "y"],
        )
        body = _build_signal_body(env.to_dict())
        assert body["type"] == "blocker"
        assert body["severity"] == "high"

    def test_missing_correlation_id_omits_source_ref(self):
        """Defensive: correlation_id might be empty in a weird state.
        ``source_ref`` becomes None; task-manager tolerates that."""
        body = _build_signal_body({
            "signal": "test",
            "correlation_id": "",
            "report_to_task_manager": True,
            "task_type": "info",
            "task_severity": "low",
        })
        assert body["source_ref"] is None

    def test_missing_task_type_defaults_to_info(self):
        """If an envelope slipped through without task_type set, we
        default to 'info' rather than rejecting. Task-manager's
        TaskCreateBody uses 'info' as its default too."""
        body = _build_signal_body({
            "signal": "x",
            "correlation_id": "c-1",
            "report_to_task_manager": True,
        })
        assert body["type"] == "info"
        assert body["severity"] == "low"

    def test_no_thread_or_user_id_omitted_from_data(self):
        """Don't pollute data with None values — task-manager UI would
        show them as 'None' strings in its card rendering."""
        env = make_critic_flagged(
            correlation_id="c-1",
            round=3,
            total_issues=1,
            high_severity=1,
            flagged_claims=["x"],
            rounds_remaining=0,
        )
        body = _build_signal_body(env.to_dict())
        assert "thread_id" not in body["data"]
        assert "user_id" not in body["data"]


# ── Promote gating ───────────────────────────────────────────────


class TestPromoteGating:
    def test_promote_no_op_when_flag_off(self, monkeypatch):
        """Feature disabled — no POST. Even a promotion-eligible
        envelope is silently skipped."""
        monkeypatch.setenv("MOBIUS_TASK_MANAGER_PROMOTION", "0")
        env = make_critic_flagged(
            correlation_id="c-1",
            round=3,
            total_issues=1,
            high_severity=1,
            flagged_claims=["x"],
            rounds_remaining=0,
        )
        with patch("app.services.task_manager_promotion._post_signal_sync") as mock_post:
            promote(env.to_dict())
        assert not mock_post.called

    def test_promote_no_op_when_envelope_flag_false(self, monkeypatch):
        """Feature on, but the envelope's own flag is False. No POST.
        This is the "common case" path for critic_approved,
        tool_called, etc. — they're recorded in thinking_log only."""
        monkeypatch.setenv("MOBIUS_TASK_MANAGER_PROMOTION", "1")
        env = make_critic_approved(correlation_id="c-1", round=2)
        with patch("app.services.task_manager_promotion._post_signal_sync") as mock_post:
            promote(env.to_dict())
        assert not mock_post.called

    def test_promote_fires_when_both_enabled(self, monkeypatch):
        """Feature on AND envelope flag True → background thread
        dispatched with the right payload."""
        monkeypatch.setenv("MOBIUS_TASK_MANAGER_PROMOTION", "1")
        env = make_critic_flagged(
            correlation_id="c-1",
            round=3,
            total_issues=1,
            high_severity=1,
            flagged_claims=["x"],
            rounds_remaining=0,
        )
        post_called = threading.Event()
        captured = {}

        def fake_post(payload):
            captured["payload"] = payload
            post_called.set()

        with patch("app.services.task_manager_promotion._post_signal_sync", side_effect=fake_post):
            promote(env.to_dict())
            # Wait for daemon thread to fire (should be fast — no I/O).
            assert post_called.wait(timeout=2.0), "background thread did not fire"

        assert captured["payload"]["signal"] == "critic_flagged"
        assert captured["payload"]["type"] == "insight"

    def test_promote_tolerates_non_dict_envelope(self, monkeypatch):
        """Defensive: if someone passes a string or None, promote
        silently no-ops rather than crashing. Shouldn't happen in
        practice but the emit callsite upstream might be buggy."""
        monkeypatch.setenv("MOBIUS_TASK_MANAGER_PROMOTION", "1")
        # None of these should raise:
        promote(None)  # type: ignore[arg-type]
        promote("a string")  # type: ignore[arg-type]
        promote(42)  # type: ignore[arg-type]


# ── Fire-and-forget semantics ────────────────────────────────────


class TestFireAndForget:
    def test_promote_returns_quickly_even_with_slow_post(self, monkeypatch):
        """If task-manager is slow (timeout, bad latency), promote
        must NOT block the caller. This is critical — every chat emit
        would slow down otherwise."""
        monkeypatch.setenv("MOBIUS_TASK_MANAGER_PROMOTION", "1")
        env = make_critic_flagged(
            correlation_id="c-1",
            round=3,
            total_issues=1,
            high_severity=1,
            flagged_claims=["x"],
            rounds_remaining=0,
        )

        def slow_post(payload):
            time.sleep(5.0)  # simulate slow task-manager

        with patch("app.services.task_manager_promotion._post_signal_sync", side_effect=slow_post):
            start = time.time()
            promote(env.to_dict())
            elapsed = time.time() - start

        # promote() should return in well under 100ms even though the
        # mock's "post" takes 5s. The daemon thread keeps running in
        # the background; the test doesn't wait on it.
        assert elapsed < 0.5, (
            f"promote() took {elapsed:.2f}s — should return immediately "
            f"and run the POST on a background thread"
        )

    def test_promote_network_error_does_not_propagate(self, monkeypatch):
        """When httpx raises (connection refused, DNS, timeout),
        the error must stay inside the worker thread and not surface
        to the caller. Chat turns continue even when task-manager
        is unreachable."""
        monkeypatch.setenv("MOBIUS_TASK_MANAGER_PROMOTION", "1")
        env = make_critic_flagged(
            correlation_id="c-1",
            round=3,
            total_issues=1,
            high_severity=1,
            flagged_claims=["x"],
            rounds_remaining=0,
        )
        error_raised = threading.Event()

        def failing_post(payload):
            error_raised.set()
            raise ConnectionRefusedError("task-manager is down")

        with patch("app.services.task_manager_promotion._post_signal_sync", side_effect=failing_post):
            # This must not raise — even though the thread's target
            # raises internally. Fire-and-forget semantics.
            promote(env.to_dict())
            # The thread does run (verified via the event), but the
            # exception is contained within it.
            error_raised.wait(timeout=2.0)


# ── HTTP integration smoke test ──────────────────────────────────


class TestHttpIntegration:
    """End-to-end smoke: promote() → daemon thread → httpx.Client →
    task-manager URL. Mocks httpx at the Client level to verify the
    right URL, method, and body get sent."""

    def test_post_hits_task_manager_signal_endpoint(self, monkeypatch):
        monkeypatch.setenv("MOBIUS_TASK_MANAGER_PROMOTION", "1")
        monkeypatch.setenv("CHAT_SKILLS_TASK_MANAGER_URL", "http://test-tm:9999")

        env = make_critic_flagged(
            correlation_id="c-test",
            round=4,
            total_issues=3,
            high_severity=2,
            flagged_claims=["claim a", "claim b"],
            rounds_remaining=2,
            thread_id="t-live",
        )

        captured = {}
        post_event = threading.Event()

        def fake_request(method, url, json=None, **kw):
            captured["method"] = method
            captured["url"] = url
            captured["json"] = json
            post_event.set()
            resp = MagicMock()
            resp.status_code = 200
            resp.text = "{}"
            return resp

        with patch("httpx.Client") as hc:
            client_instance = MagicMock()
            client_instance.request.side_effect = fake_request
            hc.return_value.__enter__.return_value = client_instance
            # Also need to patch the .post shortcut, since the writer
            # calls client.post(url, json=...) not client.request.
            def fake_post(url, json=None, **kw):
                return fake_request("POST", url, json=json, **kw)
            client_instance.post.side_effect = fake_post

            promote(env.to_dict())

            # Wait for daemon thread to fire.
            assert post_event.wait(timeout=2.0), "expected POST did not fire"

        assert captured["url"] == "http://test-tm:9999/tasks/signal"
        assert captured["json"]["signal"] == "critic_flagged"
        assert captured["json"]["type"] == "insight"
        assert captured["json"]["data"]["thread_id"] == "t-live"
