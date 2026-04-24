"""Structured logging — JSON output in hosted envs, human-readable in dev.

Sprint 1 #10 (2026-04-23). Cloud Logging parses JSON ``jsonPayload``
natively — severity, trace, and arbitrary fields become first-class
query dimensions without regex gymnastics. Dev keeps the
human-readable format because nobody wants to ``jq`` their terminal
output during a local debug session.

Contract
--------
* ``configure_logging()`` is idempotent. Call it at FastAPI app
  startup; multiple calls are no-ops.
* Env gate: ``CHAT_LOG_FORMAT`` wins when set. When unset, JSON in
  hosted envs (K_SERVICE or CHAT_ENV_STRICT=1), plain text in dev.
* Every log record picks up ``correlation_id`` + ``user_id`` +
  ``thread_id`` from a ``ContextVar`` when the request middleware
  stashed them. None-valued fields are omitted from the JSON so the
  dashboards don't fill with empty keys.
* Existing ``logger.info("msg")`` / ``logger.exception(...)`` calls
  keep working unchanged. New code can pass ``extra={"stage": ...}``
  kwargs and those fields show up in the JSON.

Why not structlog
-----------------
structlog is more featureful but requires rewriting log call sites
to use its bound-logger API. python-json-logger is a drop-in
formatter — we get structured output without touching the ~500
existing ``logger.xxx`` call sites in the codebase.
"""
from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from typing import Any

# ── Request-scoped correlation context ─────────────────────────────────
#
# Stashed by ``request_context_middleware`` (see below) on each
# request so every log line inside the handler — even ones buried
# 10 stack frames deep in the pipeline — automatically picks up the
# correlation_id / user_id without threading a parameter through.

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_user_id: ContextVar[str | None] = ContextVar("user_id", default=None)
_thread_id: ContextVar[str | None] = ContextVar("thread_id", default=None)


def set_request_context(
    *,
    correlation_id: str | None = None,
    user_id: str | None = None,
    thread_id: str | None = None,
) -> list:
    """Stash request-scoped identifiers. Returns the set of ContextVar
    tokens that the caller can pass to :func:`reset_request_context` to
    tear down — required so we don't leak values across requests when
    the event loop reuses threads."""
    tokens = []
    if correlation_id is not None:
        tokens.append(_correlation_id.set(correlation_id))
    if user_id is not None:
        tokens.append(_user_id.set(user_id))
    if thread_id is not None:
        tokens.append(_thread_id.set(thread_id))
    return tokens


def reset_request_context(tokens: list) -> None:
    for token in reversed(tokens):
        try:
            # ContextVar tokens are specific to the var they came from;
            # each one knows how to reset itself.
            token.var.reset(token)
        except Exception:
            pass


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def get_user_id() -> str | None:
    return _user_id.get()


# ── Logging filter that enriches every record ─────────────────────────


class ContextEnrichmentFilter(logging.Filter):
    """Stamps correlation_id / user_id / thread_id onto every LogRecord
    that passes through. Non-filtering — always returns True; we just
    use the Filter hook because it runs for every record regardless of
    logger.

    None-valued fields are written as the empty string so the json
    formatter can omit them consistently. (The formatter can't reliably
    tell a missing field from a None field through the standard
    ``%()s`` placeholder.)
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = _correlation_id.get() or ""
        record.user_id = _user_id.get() or ""
        record.thread_id = _thread_id.get() or ""
        return True


# ── JSON formatter ────────────────────────────────────────────────────


def _build_json_formatter() -> logging.Formatter:
    """python-json-logger's JsonFormatter rendered with a fixed field
    set that matches Cloud Logging's conventions for ``severity`` +
    ``message`` while preserving our correlation context as
    ``jsonPayload.<field>``."""
    from pythonjsonlogger import jsonlogger

    class _CloudLoggingFormatter(jsonlogger.JsonFormatter):
        """Cloud Logging looks at ``severity`` (uppercase), not
        ``level``. Map levelname → severity and drop empty context
        fields so the JSON stays compact."""

        def add_fields(self, log_record, record, message_dict):
            super().add_fields(log_record, record, message_dict)
            # Cloud Logging's severity levels match Python's; uppercase
            # is the canonical form.
            log_record["severity"] = record.levelname
            # Stable shape: timestamp + logger name always present. Use
            # direct assignment (not setdefault) so they always land —
            # jsonlogger's format-string parsing doesn't always add them.
            log_record["timestamp"] = self.formatTime(record, self.datefmt)
            log_record["logger"] = record.name
            # Drop empty-string context fields so the JSON isn't
            # polluted with {"correlation_id": "", "user_id": ""} on
            # every startup log line.
            for k in ("correlation_id", "user_id", "thread_id"):
                if log_record.get(k) in ("", None):
                    log_record.pop(k, None)

    # Include line info in every record — trivial to ignore when you
    # don't care, critical when you do.
    fmt = "%(timestamp)s %(severity)s %(logger)s %(correlation_id)s %(user_id)s %(thread_id)s %(message)s"
    return _CloudLoggingFormatter(
        fmt,
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def _build_plain_formatter() -> logging.Formatter:
    """Human-readable formatter for dev. Keeps the existing log style
    (``%(asctime)s %(levelname)s %(name)s: %(message)s``) and appends
    the correlation_id in brackets when present — looks natural in a
    terminal without being noisy on boot lines."""

    class _DevFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            base = super().format(record)
            cid = getattr(record, "correlation_id", "") or ""
            if cid:
                return f"{base}  [cid={cid[:8]}]"
            return base

    return _DevFormatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


# ── Public entrypoint ─────────────────────────────────────────────────


def _use_json_format() -> bool:
    """Resolve which format to use.

    Priority:
      1. CHAT_LOG_FORMAT=json or =plain  — explicit override
      2. Cloud Run's K_SERVICE env var present → json
      3. CHAT_ENV_STRICT=1 → json
      4. Otherwise → plain
    """
    explicit = (os.environ.get("CHAT_LOG_FORMAT") or "").strip().lower()
    if explicit in ("json", "plain"):
        return explicit == "json"
    if os.environ.get("K_SERVICE"):
        return True
    if (os.environ.get("CHAT_ENV_STRICT") or "").strip() == "1":
        return True
    return False


_CONFIGURED = False


def configure_logging() -> None:
    """Install the enrichment filter + pick the right formatter.

    Idempotent. Safe to call multiple times. Replaces any handlers
    already on the root logger so we don't double-emit when uvicorn
    boots its own handler.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    # Drop uvicorn's default handler so we don't get two copies of
    # each record (one plain, one JSON).
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler()
    if _use_json_format():
        handler.setFormatter(_build_json_formatter())
    else:
        handler.setFormatter(_build_plain_formatter())
    handler.addFilter(ContextEnrichmentFilter())
    root.addHandler(handler)

    # Respect LOG_LEVEL env for ops override. Default INFO.
    level_name = (os.environ.get("LOG_LEVEL") or "INFO").upper()
    root.setLevel(getattr(logging, level_name, logging.INFO))

    # uvicorn + httpx default to quite chatty — quiet them a level
    # unless LOG_LEVEL was explicitly set to DEBUG.
    if level_name != "DEBUG":
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    _CONFIGURED = True
    logging.getLogger(__name__).info(
        "logging configured",
        extra={"format": "json" if _use_json_format() else "plain",
               "level": level_name},
    )


# ── FastAPI middleware to set context per request ─────────────────────
#
# Declared here (not in app/api/_common.py) so a non-FastAPI caller
# that imports app.logging_config gets the context vars + formatter
# without pulling starlette. The middleware itself is registered from
# main.py.


async def request_context_middleware(request, call_next):
    """Stash ``correlation_id`` onto a ContextVar for the lifetime of
    the request so every log line inside the handler picks it up
    without threading a parameter through ten stack frames.

    MVP scope: correlation_id only. ``user_id`` and ``thread_id`` come
    from the handler (via ``Depends(require_user)`` and POST body
    parsing respectively) — they're not on ``request.state`` at
    middleware time under Starlette's registration ordering. Handlers
    that want those fields in logs call :func:`update_request_context`
    once they resolve.
    """
    import uuid

    cid = request.headers.get("x-correlation-id") or str(uuid.uuid4())
    tokens = set_request_context(correlation_id=cid)
    request.state.correlation_id = cid  # handler-accessible mirror

    try:
        response = await call_next(request)
        # Echo the correlation_id back so the client can log it too —
        # makes cross-system debugging trivial.
        response.headers.setdefault("X-Correlation-Id", cid)
        return response
    finally:
        reset_request_context(tokens)


def update_request_context(
    *,
    user_id: str | None = None,
    thread_id: str | None = None,
) -> None:
    """Handler-side hook to populate ``user_id`` / ``thread_id`` on
    the log-context ContextVars after auth / body parsing have
    resolved them. No-op when called outside a request scope (the
    ContextVars just stay at their defaults)."""
    # We intentionally DON'T return tokens here — the middleware's
    # finally block resets everything via its own tokens list, and
    # these late sets get captured by that reset. Callers don't need
    # to own teardown.
    if user_id:
        _user_id.set(user_id)
    if thread_id:
        _thread_id.set(thread_id)
