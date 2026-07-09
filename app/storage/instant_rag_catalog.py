"""Durable catalog for instant-RAG uploads (Phase B.1c).

Source of truth for "which documents exist in this deployment and who
owns them." Mirrors the per-thread JSONB blob
(``chat_state.state_json.active.uploaded_files[]``) during the
transition period: uploads are dual-written, reads split along the
fast-path / slow-path boundary.

  Fast path (unchanged): ReAct loop's ``_resolve_upload_document_id``
    reads ``active.uploaded_files[]`` from the in-memory thread state
    snapshot. No DB round-trip during tool dispatch.

  Slow path (new): cross-thread queries (list all uploads for a user,
    find expiring docs, surface "previous uploads" in the composer)
    go through this module's functions.

db-agent refactor (2026-04-19)
------------------------------
Swapped psycopg2 for ``app.db_client.db_query`` / ``db_execute``. Two
behavioral changes to note:

1. ``record_upload`` used ``INSERT … ON CONFLICT DO NOTHING RETURNING``
   and inspected ``fetchone()``. The agent's ``db_execute`` surfaces
   ``rows_affected`` which is 1 on fresh insert and 0 on conflict —
   same semantic, simpler code path. RETURNING values are discarded.

2. The hard ``RuntimeError`` that used to fire when CHAT_RAG_DATABASE_URL
   was unset is gone. ``connection_error`` from the agent is logged
   (warning) and returns the same "failed write / empty read" shape
   every other storage module uses under the agent. Callers that
   previously saw the exception just didn't — this module's writes are
   already documented as best-effort with WARNING-level failure.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db_client import db_execute, db_query

logger = logging.getLogger(__name__)

_DB = "chat"


# ── Status constants ──────────────────────────────────────────────────────


STATUS_ACTIVE    = "active"
STATUS_EXPIRED   = "expired"
STATUS_DISCARDED = "discarded"
STATUS_PROMOTED  = "promoted"

_VALID_STATUSES = frozenset({
    STATUS_ACTIVE, STATUS_EXPIRED, STATUS_DISCARDED, STATUS_PROMOTED,
})


# ── Internal helpers ──────────────────────────────────────────────────────


from app.db_client import _err_message  # noqa: E402, F401 — shared helper


def _row_to_dict(row) -> dict[str, Any]:
    """Map a rows-list row (list) to a dict using our canonical column order."""
    return dict(zip(_SELECT_COLUMNS, row))


_SELECT_COLUMNS: tuple[str, ...] = (
    "document_id",
    "envelope_id",
    "upload_id",
    "thread_id",
    "user_id",
    "filename",
    "content_type",
    "byte_size",
    "chunks_count",
    "status",
    "suggested_payer",
    "suggested_state",
    "suggested_program",
    "suggested_authority",
    "confirmed_payer",
    "confirmed_state",
    "confirmed_program",
    "confirmed_authority",
    "created_at",
    "expires_at",
    "last_queried_at",
)
_SELECT_SQL = "SELECT " + ", ".join(_SELECT_COLUMNS) + " FROM instant_rag_uploads"


# ── Public API: writes ────────────────────────────────────────────────────


def record_upload(
    *,
    document_id: str,
    envelope_id: str,
    upload_id: str,
    thread_id: str,
    filename: str,
    user_id: str | None = None,
    content_type: str | None = None,
    byte_size: int | None = None,
    chunks_count: int | None = None,
    expires_at: datetime | None = None,
    suggested_tags: dict | None = None,
) -> bool:
    """Insert a new upload catalog row. Idempotent on document_id.

    Returns True if a row was inserted, False if it already existed
    (ON CONFLICT DO NOTHING) or the write failed.
    """
    if not document_id or not envelope_id or not upload_id or not thread_id:
        logger.warning(
            "[catalog] record_upload skipped — missing required field(s): "
            "document_id=%r envelope_id=%r upload_id=%r thread_id=%r",
            document_id, envelope_id, upload_id, thread_id,
        )
        return False

    if expires_at is None:
        ttl_days = int(os.environ.get("INSTANT_RAG_TTL_DAYS", "7") or "7")
        expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)

    s = suggested_tags or {}
    result = db_execute(
        """
        INSERT INTO instant_rag_uploads (
            document_id, envelope_id, upload_id, thread_id, user_id,
            filename, content_type, byte_size, chunks_count,
            status,
            suggested_payer, suggested_state, suggested_program, suggested_authority,
            expires_at
        ) VALUES (
            :document_id, :envelope_id, :upload_id, :thread_id, :user_id,
            :filename, :content_type, :byte_size, :chunks_count,
            :status,
            :sp, :ss, :spg, :sa,
            :expires_at
        )
        ON CONFLICT (document_id) DO NOTHING
        """,
        _DB,
        params={
            "document_id": document_id,
            "envelope_id": envelope_id,
            "upload_id": upload_id,
            "thread_id": thread_id,
            "user_id": user_id,
            "filename": filename,
            "content_type": content_type,
            "byte_size": byte_size,
            "chunks_count": chunks_count,
            "status": STATUS_ACTIVE,
            "sp": s.get("payer"),
            "ss": s.get("state"),
            "spg": s.get("program"),
            "sa": s.get("authority"),
            "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else expires_at,
        },
    )
    if "error" in result:
        logger.warning("[catalog] record_upload failed: %s", _err_message(result))
        return False
    inserted = int(result.get("rows_affected") or 0) > 0
    if inserted:
        logger.info(
            "[catalog] recorded upload document_id=%s filename=%s thread=%s",
            document_id, filename, thread_id,
        )
    return inserted


def update_chunks_count(document_id: str, chunks_count: int) -> bool:
    """Update chunks_count on a catalog row once the watcher learns the real value."""
    if not document_id or chunks_count < 0:
        return False
    result = db_execute(
        "UPDATE instant_rag_uploads SET chunks_count=:cc WHERE document_id=:did",
        _DB,
        params={"cc": chunks_count, "did": document_id},
    )
    if "error" in result:
        logger.warning("[catalog] update_chunks_count failed: %s", _err_message(result))
        return False
    return int(result.get("rows_affected") or 0) > 0


def mark_status(document_id: str, status: str) -> bool:
    """Transition a row to one of the terminal statuses."""
    if status not in _VALID_STATUSES:
        logger.warning("[catalog] mark_status rejected invalid status=%r", status)
        return False
    if not document_id:
        return False
    result = db_execute(
        "UPDATE instant_rag_uploads SET status=:status WHERE document_id=:did",
        _DB,
        params={"status": status, "did": document_id},
    )
    if "error" in result:
        logger.warning("[catalog] mark_status failed: %s", _err_message(result))
        return False
    changed = int(result.get("rows_affected") or 0) > 0
    if changed:
        logger.info("[catalog] document_id=%s status → %s", document_id, status)
    return changed


def touch_last_queried(document_id: str) -> None:
    """Update last_queried_at to now. Best-effort; swallows errors."""
    if not document_id:
        return
    result = db_execute(
        "UPDATE instant_rag_uploads SET last_queried_at = now() "
        "WHERE document_id = :did AND status = 'active'",
        _DB,
        params={"did": document_id},
    )
    if "error" in result:
        logger.debug("[catalog] touch_last_queried failed: %s", _err_message(result))


def update_confirmed_tags(
    document_id: str,
    *,
    payer: str | None = None,
    state: str | None = None,
    program: str | None = None,
    authority: str | None = None,
) -> bool:
    """Phase B.3 hook — user reviewed the LLM suggestion and committed
    the final metadata. Any field left as None is not touched."""
    if not document_id:
        return False
    sets: list[str] = []
    params: dict[str, Any] = {}
    # Named placeholders matched by column; column names hardcoded (not user input).
    for col, val in (
        ("confirmed_payer", payer),
        ("confirmed_state", state),
        ("confirmed_program", program),
        ("confirmed_authority", authority),
    ):
        if val is not None:
            sets.append(f"{col}=:{col}")
            params[col] = val
    if not sets:
        return False
    params["did"] = document_id
    result = db_execute(
        f"UPDATE instant_rag_uploads SET {', '.join(sets)} WHERE document_id=:did",
        _DB,
        params=params,
    )
    if "error" in result:
        logger.warning("[catalog] update_confirmed_tags failed: %s", _err_message(result))
        return False
    return int(result.get("rows_affected") or 0) > 0


# ── Public API: reads ─────────────────────────────────────────────────────


def _run_select(sql: str, params: dict[str, Any], fn_name: str) -> list[dict[str, Any]]:
    result = db_query(sql, _DB, params=params)
    if "error" in result:
        logger.warning("[catalog] %s failed: %s", fn_name, _err_message(result))
        return []
    return [_row_to_dict(r) for r in (result.get("rows") or [])]


def list_for_thread(thread_id: str, *, include_inactive: bool = False) -> list[dict[str, Any]]:
    """Return all catalog rows for this thread."""
    if not thread_id:
        return []
    where = "WHERE thread_id = :tid"
    params: dict[str, Any] = {"tid": thread_id}
    if not include_inactive:
        where += " AND status = 'active'"
    return _run_select(
        f"{_SELECT_SQL} {where} ORDER BY created_at DESC",
        params,
        "list_for_thread",
    )


def list_for_user(user_id: str, *, include_inactive: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    """Cross-thread: all uploads this user owns."""
    if not user_id:
        return []
    where = "WHERE user_id = :uid"
    params: dict[str, Any] = {"uid": user_id, "lim": limit}
    if not include_inactive:
        where += " AND status = 'active'"
    return _run_select(
        f"{_SELECT_SQL} {where} ORDER BY created_at DESC LIMIT :lim",
        params,
        "list_for_user",
    )


def get_by_document_id(document_id: str) -> dict[str, Any] | None:
    if not document_id:
        return None
    rows = _run_select(
        f"{_SELECT_SQL} WHERE document_id = :did",
        {"did": document_id},
        "get_by_document_id",
    )
    return rows[0] if rows else None


def get_by_upload_id(upload_id: str) -> dict[str, Any] | None:
    if not upload_id:
        return None
    rows = _run_select(
        f"{_SELECT_SQL} WHERE upload_id = :uid",
        {"uid": upload_id},
        "get_by_upload_id",
    )
    return rows[0] if rows else None


def list_recent_global(limit: int = 100) -> list[dict[str, Any]]:
    """Return the most recent ACTIVE uploads across all threads / users.

    Used by the Phase B.1d restoration banner when auth is off (dev mode)
    — there's no user to scope by, so we show the globally-recent set.
    In prod this path never runs because auth_mode() defaults to 'required'
    and ``list_for_user`` covers that case.
    """
    return _run_select(
        f"{_SELECT_SQL} WHERE status = 'active' ORDER BY created_at DESC LIMIT :lim",
        {"lim": limit},
        "list_recent_global",
    )


def list_expiring_before(cutoff: datetime) -> list[dict[str, Any]]:
    """Cleanup cron input: rows whose expires_at has passed."""
    return _run_select(
        f"{_SELECT_SQL} WHERE status = 'active' AND expires_at IS NOT NULL "
        f"AND expires_at < :cutoff ORDER BY expires_at ASC",
        {"cutoff": cutoff.isoformat() if isinstance(cutoff, datetime) else cutoff},
        "list_expiring_before",
    )
