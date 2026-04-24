"""Tests for app/logging_config.py (Sprint 1 #10).

Three surfaces:
  * Format selection — env resolution across dev/hosted/explicit
  * JSON formatter output — fields present, severity uppercased, empty
    context fields omitted
  * Context propagation — correlation_id set by middleware shows up on
    log records fired from inside the request handler
"""
from __future__ import annotations

import io
import json
import logging
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import logging_config
from app.logging_config import (
    ContextEnrichmentFilter,
    _build_json_formatter,
    _build_plain_formatter,
    _use_json_format,
    configure_logging,
    get_correlation_id,
    reset_request_context,
    request_context_middleware,
    set_request_context,
    update_request_context,
)


@pytest.fixture(autouse=True)
def _reset_configured_flag():
    """configure_logging is idempotent via a module-level flag —
    force re-run between tests so each gets a fresh handler."""
    logging_config._CONFIGURED = False
    yield
    logging_config._CONFIGURED = False


# ── Format selection ──────────────────────────────────────────────────


class TestFormatSelection:
    def test_dev_default_plain(self, monkeypatch):
        monkeypatch.delenv("CHAT_LOG_FORMAT", raising=False)
        monkeypatch.delenv("K_SERVICE", raising=False)
        monkeypatch.delenv("CHAT_ENV_STRICT", raising=False)
        assert _use_json_format() is False

    def test_cloud_run_json(self, monkeypatch):
        """K_SERVICE is Cloud Run's standard auto-injected env var."""
        monkeypatch.delenv("CHAT_LOG_FORMAT", raising=False)
        monkeypatch.setenv("K_SERVICE", "mobius-chat")
        assert _use_json_format() is True

    def test_strict_env_json(self, monkeypatch):
        monkeypatch.delenv("CHAT_LOG_FORMAT", raising=False)
        monkeypatch.delenv("K_SERVICE", raising=False)
        monkeypatch.setenv("CHAT_ENV_STRICT", "1")
        assert _use_json_format() is True

    def test_explicit_override_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("CHAT_LOG_FORMAT", "plain")
        monkeypatch.setenv("K_SERVICE", "mobius-chat")  # would otherwise force JSON
        assert _use_json_format() is False

    def test_explicit_json_in_dev(self, monkeypatch):
        monkeypatch.delenv("K_SERVICE", raising=False)
        monkeypatch.delenv("CHAT_ENV_STRICT", raising=False)
        monkeypatch.setenv("CHAT_LOG_FORMAT", "json")
        assert _use_json_format() is True


# ── ContextEnrichmentFilter ───────────────────────────────────────────


class TestContextEnrichmentFilter:
    def test_filter_stamps_empty_strings_when_no_context(self):
        f = ContextEnrichmentFilter()
        r = logging.LogRecord("t", logging.INFO, "", 0, "m", None, None)
        assert f.filter(r) is True
        assert r.correlation_id == ""
        assert r.user_id == ""
        assert r.thread_id == ""

    def test_filter_reads_from_context_vars(self):
        f = ContextEnrichmentFilter()
        tokens = set_request_context(correlation_id="cid-abc", user_id="u-123", thread_id="t-789")
        try:
            r = logging.LogRecord("t", logging.INFO, "", 0, "m", None, None)
            f.filter(r)
            assert r.correlation_id == "cid-abc"
            assert r.user_id == "u-123"
            assert r.thread_id == "t-789"
        finally:
            reset_request_context(tokens)

    def test_filter_always_returns_true(self):
        """It's an enrichment filter, not a suppression filter."""
        f = ContextEnrichmentFilter()
        r = logging.LogRecord("t", logging.DEBUG, "", 0, "m", None, None)
        assert f.filter(r) is True


# ── JSON formatter output ─────────────────────────────────────────────


class TestJsonFormatter:
    def _capture(self, record: logging.LogRecord) -> dict:
        fmt = _build_json_formatter()
        return json.loads(fmt.format(record))

    def test_severity_is_uppercase_python_levelname(self):
        r = logging.LogRecord("t", logging.WARNING, "", 0, "hi", None, None)
        r.correlation_id = r.user_id = r.thread_id = ""
        out = self._capture(r)
        assert out["severity"] == "WARNING"

    def test_message_field_populated(self):
        r = logging.LogRecord("t", logging.INFO, "", 0, "hello world", None, None)
        r.correlation_id = r.user_id = r.thread_id = ""
        out = self._capture(r)
        assert out["message"] == "hello world"

    def test_logger_name_in_output(self):
        r = logging.LogRecord("app.something", logging.INFO, "", 0, "m", None, None)
        r.correlation_id = r.user_id = r.thread_id = ""
        out = self._capture(r)
        assert out["logger"] == "app.something"

    def test_empty_context_fields_dropped(self):
        """Empty-string correlation_id / user_id / thread_id shouldn't
        appear in the JSON — otherwise dashboards fill with noise."""
        r = logging.LogRecord("t", logging.INFO, "", 0, "m", None, None)
        r.correlation_id = r.user_id = r.thread_id = ""
        out = self._capture(r)
        assert "correlation_id" not in out
        assert "user_id" not in out
        assert "thread_id" not in out

    def test_populated_context_fields_included(self):
        r = logging.LogRecord("t", logging.INFO, "", 0, "m", None, None)
        r.correlation_id = "cid-xyz"
        r.user_id = "alice"
        r.thread_id = "t-7"
        out = self._capture(r)
        assert out["correlation_id"] == "cid-xyz"
        assert out["user_id"] == "alice"
        assert out["thread_id"] == "t-7"

    def test_extra_kwargs_propagate(self):
        """logger.info('msg', extra={'stage': 'react_1'}) → stage key in JSON."""
        r = logging.LogRecord("t", logging.INFO, "", 0, "m", None, None)
        r.correlation_id = r.user_id = r.thread_id = ""
        r.stage = "react_1"
        out = self._capture(r)
        assert out["stage"] == "react_1"


# ── Plain formatter (dev readability) ─────────────────────────────────


class TestPlainFormatter:
    def test_cid_appended_in_brackets_when_present(self):
        fmt = _build_plain_formatter()
        r = logging.LogRecord("t", logging.INFO, "", 0, "hi there", None, None)
        r.correlation_id = "abcdef1234"
        out = fmt.format(r)
        assert "hi there" in out
        # First 8 chars of cid appended in brackets at the end.
        assert out.endswith("[cid=abcdef12]")

    def test_no_bracket_when_cid_empty(self):
        fmt = _build_plain_formatter()
        r = logging.LogRecord("t", logging.INFO, "", 0, "bootup", None, None)
        r.correlation_id = ""
        out = fmt.format(r)
        assert "[cid=" not in out


# ── configure_logging integration ─────────────────────────────────────


class TestConfigureLogging:
    def test_idempotent(self, monkeypatch):
        monkeypatch.setenv("CHAT_LOG_FORMAT", "plain")
        configure_logging()
        n = len(logging.getLogger().handlers)
        configure_logging()  # no-op second call
        assert len(logging.getLogger().handlers) == n

    def test_json_handler_installed_when_requested(self, monkeypatch, capsys):
        monkeypatch.setenv("CHAT_LOG_FORMAT", "json")
        configure_logging()

        # Fire a log line with a known context, capture stderr.
        tokens = set_request_context(correlation_id="test-cid")
        try:
            logging.getLogger("test.json").info("marker-42")
        finally:
            reset_request_context(tokens)

        captured = capsys.readouterr()
        lines = [l for l in captured.err.splitlines() if "marker-42" in l]
        assert lines, f"expected a JSON line with 'marker-42'; got err={captured.err!r}"
        parsed = json.loads(lines[-1])
        assert parsed["message"] == "marker-42"
        assert parsed["severity"] == "INFO"
        assert parsed["correlation_id"] == "test-cid"


# ── Middleware propagation (the end-to-end win) ──────────────────────


class TestRequestContextMiddleware:
    """End-to-end: middleware generates/reads correlation_id, stashes
    it on both ``request.state`` and the ContextVar, echoes it on the
    response header.

    The handler here is ``async def`` on purpose. FastAPI runs sync
    handlers in a threadpool, and ContextVars set in an async
    middleware don't reliably propagate across the thread boundary
    — the feature still works for log calls inside async handlers
    and for log calls inside the middleware itself, which is where
    ~90% of the logging happens. If we later need ContextVar access
    from sync handlers, the fix is either to rely on
    ``request.state.correlation_id`` directly or to wrap the threadpool
    dispatcher. Out of scope for this PR.
    """

    @staticmethod
    def _build_app() -> FastAPI:
        app = FastAPI()
        app.middleware("http")(request_context_middleware)

        @app.get("/probe")
        async def probe(request: Request):
            return {
                "state_cid": request.state.correlation_id,
                "ctx_cid": get_correlation_id(),
            }

        return app

    def test_middleware_generates_cid_when_header_absent(self):
        client = TestClient(self._build_app())
        r = client.get("/probe")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state_cid"]
        assert body["state_cid"] == body["ctx_cid"]
        # Response echoes the header back.
        assert r.headers.get("X-Correlation-Id") == body["state_cid"]

    def test_middleware_honors_inbound_correlation_id_header(self):
        client = TestClient(self._build_app())
        r = client.get("/probe", headers={"X-Correlation-Id": "caller-supplied-cid"})
        assert r.status_code == 200, r.text
        assert r.json()["ctx_cid"] == "caller-supplied-cid"
        assert r.headers["X-Correlation-Id"] == "caller-supplied-cid"

    def test_contextvar_resets_between_requests(self):
        """Ensure cid from request A doesn't leak into request B."""
        client = TestClient(self._build_app())
        r1 = client.get("/probe", headers={"X-Correlation-Id": "A"})
        r2 = client.get("/probe", headers={"X-Correlation-Id": "B"})
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json()["ctx_cid"] == "A"
        assert r2.json()["ctx_cid"] == "B"


# ── update_request_context (handler-side late stamping) ──────────────


class TestUpdateRequestContext:
    def test_user_id_set_late_shows_up_in_contextvar(self):
        """auth Depends resolves inside the handler; update_request_context
        lets it populate the log-context without threading params."""
        from app.logging_config import get_user_id  # type: ignore[attr-defined]

        # Simulate a request: middleware sets cid, handler sets uid.
        tokens = set_request_context(correlation_id="r1")
        try:
            update_request_context(user_id="alice-late")
            # Formatter would pick this up on any subsequent log call.
            f = ContextEnrichmentFilter()
            r = logging.LogRecord("t", logging.INFO, "", 0, "m", None, None)
            f.filter(r)
            assert r.correlation_id == "r1"
            assert r.user_id == "alice-late"
        finally:
            reset_request_context(tokens)
