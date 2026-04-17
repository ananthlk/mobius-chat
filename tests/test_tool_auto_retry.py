"""Phase 0.13 — auto-retry on recoverable errors.

Closes the loop on ErrorEnvelope (Phase 0.6a) + the retry guard (Phase 0.7):
when a tool returns a recoverable error envelope (rate_limit, timeout,
provider_error, scrape_failed), the ReAct loop sleeps ``retry_after_seconds``
and re-runs the same call once before falling through to the retry guard.

Rules enforced:
- Exactly ONE retry per call (no spirals).
- Sleep bounded to ``_MAX_AUTO_RETRY_SLEEP_S`` (30s) regardless of what the
  provider hints.
- Non-recoverable codes return immediately (no retry).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from app.pipeline import react_loop


class _Emits:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, msg: str) -> None:
        self.lines.append(msg)


def _envelope_result(error_code: str, retry_after: int | None = None) -> dict[str, Any]:
    """Shape matching what tool_result_from_exception produces."""
    return {
        "tool": "search_corpus",
        "success": False,
        "result": "The model is temporarily busy — trying another option.",
        "error": {
            "schema_name": "error_envelope",
            "version": "v1",
            "error_code": error_code,
            "user_facing_message": "busy",
            "internal_detail": "",
            "retry_after_seconds": retry_after,
            "tool": "search_corpus",
            "round": 1,
        },
        "sources": [],
    }


def _success_result() -> dict[str, Any]:
    return {
        "tool": "search_corpus",
        "success": True,
        "result": "The answer.",
        "sources": [{"document_id": "d1", "page_number": 10}],
    }


# ── recoverable codes → retry once ──────────────────────────────────────────


class TestRecoverableRetry:
    def test_rate_limit_triggers_one_retry(self):
        emit = _Emits()
        call_count = {"n": 0}

        def fake_execute(tool, inputs, ctx, emitter):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _envelope_result("rate_limit", retry_after=1)
            return _success_result()

        with (
            patch.object(react_loop, "_execute_tool", side_effect=fake_execute),
            patch("time.sleep") as mock_sleep,
        ):
            result = react_loop._execute_tool_with_retry(
                "search_corpus", {"query": "x"}, ctx=None, round_num=1,
                emit_fn=emit, tool_emitter=None,
            )
        assert call_count["n"] == 2, "must retry exactly once"
        assert result["success"] is True, "retry succeeded → result is the success shape"
        assert result.get("auto_retried") is True, "retry marker set on output"
        mock_sleep.assert_called_once()
        # The retry status line should tell the user what's happening.
        assert any("retrying in" in line.lower() for line in emit.lines)

    def test_timeout_triggers_retry(self):
        call_count = {"n": 0}

        def fake_execute(*a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _envelope_result("timeout", retry_after=2)
            return _success_result()

        with (
            patch.object(react_loop, "_execute_tool", side_effect=fake_execute),
            patch("time.sleep"),
        ):
            result = react_loop._execute_tool_with_retry(
                "web_scrape", {"url": "x"}, ctx=None, round_num=1,
                emit_fn=_Emits(), tool_emitter=None,
            )
        assert call_count["n"] == 2
        assert result["success"] is True

    def test_provider_error_triggers_retry(self):
        call_count = {"n": 0}

        def fake_execute(*a, **kw):
            call_count["n"] += 1
            return _envelope_result("provider_error", retry_after=3)

        with (
            patch.object(react_loop, "_execute_tool", side_effect=fake_execute),
            patch("time.sleep"),
        ):
            result = react_loop._execute_tool_with_retry(
                "t", {}, ctx=None, round_num=1, emit_fn=_Emits(), tool_emitter=None,
            )
        # Retry was attempted, but still failed → surface the failed result.
        assert call_count["n"] == 2
        assert result.get("auto_retried") is True
        assert result["success"] is False


# ── non-recoverable codes → no retry ────────────────────────────────────────


class TestNonRecoverableNoRetry:
    def test_auth_error_does_not_retry(self):
        call_count = {"n": 0}

        def fake_execute(*a, **kw):
            call_count["n"] += 1
            return _envelope_result("auth_error")

        with (
            patch.object(react_loop, "_execute_tool", side_effect=fake_execute),
            patch("time.sleep") as mock_sleep,
        ):
            result = react_loop._execute_tool_with_retry(
                "t", {}, ctx=None, round_num=1, emit_fn=_Emits(), tool_emitter=None,
            )
        assert call_count["n"] == 1, "auth_error is non-recoverable — must NOT retry"
        mock_sleep.assert_not_called()
        assert "auto_retried" not in result

    def test_refusal_does_not_retry(self):
        call_count = {"n": 0}

        def fake_execute(*a, **kw):
            call_count["n"] += 1
            return _envelope_result("refusal")

        with (
            patch.object(react_loop, "_execute_tool", side_effect=fake_execute),
            patch("time.sleep") as mock_sleep,
        ):
            react_loop._execute_tool_with_retry(
                "t", {}, ctx=None, round_num=1, emit_fn=_Emits(), tool_emitter=None,
            )
        assert call_count["n"] == 1
        mock_sleep.assert_not_called()


# ── sleep bound + hygiene ───────────────────────────────────────────────────


class TestSleepBounds:
    def test_sleep_bounded_to_max(self):
        """A provider hinting retry_after=9999 must NOT stall the turn — cap at MAX."""
        def fake_execute(*a, **kw):
            return _envelope_result("rate_limit", retry_after=9999)

        with (
            patch.object(react_loop, "_execute_tool", side_effect=fake_execute),
            patch("time.sleep") as mock_sleep,
        ):
            react_loop._execute_tool_with_retry(
                "t", {}, ctx=None, round_num=1, emit_fn=_Emits(), tool_emitter=None,
            )
        (wait,), _ = mock_sleep.call_args
        assert wait == react_loop._MAX_AUTO_RETRY_SLEEP_S

    def test_sleep_defaults_when_retry_after_missing(self):
        """No retry_after → small default, not infinite."""
        def fake_execute(*a, **kw):
            return _envelope_result("provider_error", retry_after=None)

        with (
            patch.object(react_loop, "_execute_tool", side_effect=fake_execute),
            patch("time.sleep") as mock_sleep,
        ):
            react_loop._execute_tool_with_retry(
                "t", {}, ctx=None, round_num=1, emit_fn=_Emits(), tool_emitter=None,
            )
        (wait,), _ = mock_sleep.call_args
        assert 1 <= wait <= react_loop._MAX_AUTO_RETRY_SLEEP_S

    def test_sleep_minimum_one_second(self):
        def fake_execute(*a, **kw):
            return _envelope_result("rate_limit", retry_after=0)

        with (
            patch.object(react_loop, "_execute_tool", side_effect=fake_execute),
            patch("time.sleep") as mock_sleep,
        ):
            react_loop._execute_tool_with_retry(
                "t", {}, ctx=None, round_num=1, emit_fn=_Emits(), tool_emitter=None,
            )
        (wait,), _ = mock_sleep.call_args
        assert wait >= 1


# ── happy path: first try succeeds ──────────────────────────────────────────


class TestFirstTrySuccess:
    def test_success_no_retry_no_marker(self):
        call_count = {"n": 0}

        def fake_execute(*a, **kw):
            call_count["n"] += 1
            return _success_result()

        with (
            patch.object(react_loop, "_execute_tool", side_effect=fake_execute),
            patch("time.sleep") as mock_sleep,
        ):
            result = react_loop._execute_tool_with_retry(
                "t", {}, ctx=None, round_num=1, emit_fn=_Emits(), tool_emitter=None,
            )
        assert call_count["n"] == 1
        mock_sleep.assert_not_called()
        assert "auto_retried" not in result, (
            "clean first-try success must NOT be flagged as auto_retried"
        )
