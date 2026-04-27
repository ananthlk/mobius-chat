"""Phase 13.7 — structured metric emitters + schema audit.

Three things live here:

1. ``audit_thread_summary_schema()`` — runs once at startup. Queries
   information_schema to confirm ``chat_turns.context_summary`` exists
   and is nullable. Caches the result on a module global so the /ready
   endpoint can read it cheaply. If the column is missing or NOT NULL,
   logs a structured WARNING and the service still boots — Phase 13.7
   degrades gracefully but operators want to know *why* the sidebar
   summary feature is silent.

2. Metric emitters — three structured ``logger.info`` lines that
   Cloud Logging's metric-extraction can scrape into time-series:

     * ``record_thread_summary_emitted(present: bool)`` — fired by
       run_integrate per turn. ``phase13_7_thread_summary_emit`` log
       channel, with field ``emitted=true|false``. Track the rate of
       integrator compliance with the prompt's required field.

     * ``record_persist_fallback_tier(tier: int)`` — fired by
       _atomic_save_turn_with_messages when any fallback tier runs.
       tier=1 means the user_id fallback fired; tier=2 means the
       legacy-no-context fallback fired. Either is a schema-drift
       incident, not a routine event.

     * ``record_rehydrate_request(thread_id_prefix: str, turn_count: int)``
       — fired by the /chat/history/threads/{id}/turns endpoint.
       Tracks click-to-rehydrate rate; useful for sidebar-feature
       adoption metrics.

3. Cached startup status — readable via ``schema_audit_status()``.

Why structured-log metrics not Prometheus / OpenTelemetry: this codebase
already uses Cloud Logging exclusively; adding a metrics dependency for
3 counters is overkill. Cloud Logging's "log-based metrics" feature
turns these structured INFO lines into dashboards in 2 clicks. When
the team adopts OTLP for the broader pipeline, swap these helpers for
``otel_meter.create_counter()`` calls — call sites don't change.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)


# ── Schema audit ─────────────────────────────────────────────────────


_SCHEMA_AUDIT_STATUS: dict[str, Any] = {
    "status": "unknown",  # "ok" | "missing_column" | "wrong_type" | "error" | "unknown"
    "detail": "audit not yet run",
    "checked_at": None,
}


def schema_audit_status() -> dict[str, Any]:
    """Return the cached startup-audit status. Read-only."""
    return dict(_SCHEMA_AUDIT_STATUS)


def audit_thread_summary_schema() -> None:
    """Verify chat_turns.context_summary exists and is nullable.

    Called once at startup. Sets _SCHEMA_AUDIT_STATUS for /ready to
    surface. Never raises — a startup that fails here would block
    the whole service boot, which is wrong: Phase 13.7 degrades
    gracefully when the column is absent (the persistence fallback
    chain handles it), so this is a *warning-level* concern, not a
    *fatal* one.

    Logs a structured WARNING with channel=``phase13_7_schema_audit``
    so Cloud Logging metric-extraction can build an alert: the moment
    a deploy lands on an environment without the migration, ops sees
    a count > 0 in the next minute.
    """
    import time

    try:
        from app.db_client import db_query, err_code, err_message

        result = db_query(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'chat_turns'
              AND column_name = 'context_summary'
            """,
            "chat",
            params={},
        )
        if err_code(result) is not None:
            _SCHEMA_AUDIT_STATUS.update({
                "status": "error",
                "detail": f"db query failed: {err_message(result)[:200]}",
                "checked_at": time.time(),
            })
            logger.warning(
                "phase13_7_schema_audit channel=phase13_7_schema_audit "
                "status=error detail=%r",
                err_message(result)[:200],
            )
            return

        rows = result.get("rows") or []
        if not rows:
            _SCHEMA_AUDIT_STATUS.update({
                "status": "missing_column",
                "detail": "chat_turns.context_summary not found",
                "checked_at": time.time(),
            })
            logger.warning(
                "phase13_7_schema_audit channel=phase13_7_schema_audit "
                "status=missing_column "
                "detail='chat_turns.context_summary not found — run migration 017'"
            )
            return

        # Column exists. Confirm nullable; the fallback chain assumes it.
        cols = result.get("columns") or []
        row = dict(zip(cols, rows[0]))
        is_nullable = (row.get("is_nullable") or "").upper() == "YES"
        data_type = (row.get("data_type") or "").lower()
        if not is_nullable:
            _SCHEMA_AUDIT_STATUS.update({
                "status": "wrong_type",
                "detail": "chat_turns.context_summary is NOT NULL — fallback chain assumes nullable",
                "checked_at": time.time(),
            })
            logger.warning(
                "phase13_7_schema_audit channel=phase13_7_schema_audit "
                "status=wrong_type detail='context_summary NOT NULL'"
            )
            return

        _SCHEMA_AUDIT_STATUS.update({
            "status": "ok",
            "detail": f"context_summary {data_type} nullable",
            "checked_at": time.time(),
        })
        logger.info(
            "phase13_7_schema_audit channel=phase13_7_schema_audit "
            "status=ok type=%s",
            data_type,
        )
    except Exception as e:
        # Never let audit fail the boot. Log + leave status=unknown so
        # /ready surfaces it.
        _SCHEMA_AUDIT_STATUS.update({
            "status": "error",
            "detail": f"audit raised: {type(e).__name__}: {str(e)[:160]}",
            "checked_at": time.time(),
        })
        logger.warning(
            "phase13_7_schema_audit channel=phase13_7_schema_audit "
            "status=error detail=%r",
            f"{type(e).__name__}: {str(e)[:160]}",
        )


# ── Metric emitters ──────────────────────────────────────────────────
#
# All three emit structured logger.info lines on a dedicated channel so
# Cloud Logging metric-extraction can pivot on them. Channel format:
#   phase13_7_<event_name> channel=phase13_7_<event_name> field=value ...
# That repetition is intentional — the channel keyword makes the line
# greppable; the leading prefix is what humans read in raw logs.


def record_thread_summary_emitted(*, emitted: bool, mode: str | None = None) -> None:
    """Fire from run_integrate after thread_summary extraction.

    ``emitted=True`` means the integrator's AnswerCard contained a
    non-empty thread_summary field. ``False`` means the field was
    absent or empty (model didn't comply with the prompt's REQUIRED
    rule). Aggregate the False rate as ``phase_13_7_thread_summary_
    miss_rate`` in your dashboard.
    """
    logger.info(
        "phase13_7_thread_summary_emit channel=phase13_7_thread_summary_emit "
        "emitted=%s mode=%s",
        "true" if emitted else "false",
        mode or "?",
    )


def record_persist_fallback_tier(tier: Literal[0, 1, 2]) -> None:
    """Fire from _atomic_save_turn_with_messages when ANY fallback runs.

    tier=0  → primary insert succeeded (no fallback). Default — most
              calls. We log this anyway so the dashboard ratio works.
    tier=1  → user_id column missing; first fallback fired.
    tier=2  → context_summary ALSO missing; second fallback fired.

    tier=1 or tier=2 is a schema-drift incident — alert on count > 0.
    """
    logger.info(
        "phase13_7_persist_fallback channel=phase13_7_persist_fallback tier=%d",
        int(tier),
    )


def record_rehydrate_request(*, thread_id: str, turn_count: int) -> None:
    """Fire from /chat/history/threads/{id}/turns each call.

    Tracks the sidebar-rehydration adoption rate. Truncate the
    thread_id to first 8 chars before logging — full UUIDs aren't
    PHI but they're unnecessarily verbose in the Cloud Logging UI.
    """
    logger.info(
        "phase13_7_rehydrate_request channel=phase13_7_rehydrate_request "
        "thread=%s turns=%d",
        (thread_id or "")[:8],
        int(turn_count),
    )
