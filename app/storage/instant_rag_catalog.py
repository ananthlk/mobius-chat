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

Migration history
-----------------
Phase B.1 stored everything in the JSONB blob. Worked for thread-
scoped use cases; broke for cross-thread. 2026-04-17 live test made
the gap visible: user hard-refreshed the browser, got a new thread,
couldn't find the doc they'd uploaded 40 minutes earlier (it was on
the previous thread). This module solves that by letting
``list_for_user`` return the doc regardless of which thread it was
uploaded to.

All writes go through :func:`record_upload`. All transitions go
through :func:`mark_status`. Reads are clustered into small functions
that each correspond to a concrete UI / background-job need:
``list_for_thread``, ``list_for_user``, ``get_by_document_id``,
``get_by_upload_id``, ``list_expiring_before``.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ── Status constants ──────────────────────────────────────────────────────


STATUS_ACTIVE    = "active"
STATUS_EXPIRED   = "expired"
STATUS_DISCARDED = "discarded"
STATUS_PROMOTED  = "promoted"

_VALID_STATUSES = frozenset({
    STATUS_ACTIVE, STATUS_EXPIRED, STATUS_DISCARDED, STATUS_PROMOTED,
})


# ── Internal helpers ──────────────────────────────────────────────────────


def _get_db_url() -> str:
    """Resolved DB URL for the catalog. Same env var as the rest of chat —
    catalog lives in the chat DB alongside chat_state/chat_turns."""
    return (os.environ.get("CHAT_RAG_DATABASE_URL") or "").strip()


def _conn():
    """Open a single-use psycopg2 connection to the chat DB.

    Callers are responsible for closing. We don't reuse a pooled
    connection here because catalog operations happen on the API +
    worker boundary (one write per upload, occasional reads for
    cross-thread lists) — pool overhead isn't justified.
    """
    import psycopg2
    url = _get_db_url()
    if not url:
        raise RuntimeError(
            "CHAT_RAG_DATABASE_URL not set — instant-rag catalog cannot "
            "function. Set it or run in a deployment where chat_state "
            "persistence is configured."
        )
    return psycopg2.connect(url, connect_timeout=5)


def _row_to_dict(row) -> dict[str, Any]:
    """Map a cursor-description-less row tuple to a dict using our
    canonical column order (see _SELECT_COLUMNS). Having column order
    in ONE place prevents drift between SELECT and map."""
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

    Called from ``_handle_instant_rag_upload`` right after the skill
    ingest response lands. Failure here is WARNING-logged but does NOT
    raise — the upload itself already succeeded (chunks are in PG +
    Chroma), the thread-state JSONB was written synchronously, and the
    catalog is a secondary durability layer. Losing a row means
    cross-thread queries miss the upload; the single-thread flow still
    works. We can backfill later.

    Returns True if a row was inserted, False if the row already existed
    (ON CONFLICT DO NOTHING) or the write failed.
    """
    if not document_id or not envelope_id or not upload_id or not thread_id:
        logger.warning(
            "[catalog] record_upload skipped — missing required field(s): "
            "document_id=%r envelope_id=%r upload_id=%r thread_id=%r",
            document_id, envelope_id, upload_id, thread_id,
        )
        return False

    # Compute expires_at default if the caller didn't provide one —
    # matches the instant-rag skill's INSTANT_RAG_TTL_DAYS (7) so the
    # cleanup cron works with or without a skill-provided value.
    if expires_at is None:
        ttl_days = int(os.environ.get("INSTANT_RAG_TTL_DAYS", "7") or "7")
        expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)

    s = suggested_tags or {}
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO instant_rag_uploads (
                    document_id, envelope_id, upload_id, thread_id, user_id,
                    filename, content_type, byte_size, chunks_count,
                    status,
                    suggested_payer, suggested_state, suggested_program, suggested_authority,
                    expires_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s,
                    %s, %s, %s, %s,
                    %s
                )
                ON CONFLICT (document_id) DO NOTHING
                RETURNING document_id
                """,
                (
                    document_id, envelope_id, upload_id, thread_id, user_id,
                    filename, content_type, byte_size, chunks_count,
                    STATUS_ACTIVE,
                    s.get("payer"), s.get("state"), s.get("program"), s.get("authority"),
                    expires_at,
                ),
            )
            inserted = cur.fetchone() is not None
            conn.commit()
            cur.close()
            if inserted:
                logger.info(
                    "[catalog] recorded upload document_id=%s filename=%s thread=%s",
                    document_id, filename, thread_id,
                )
            return inserted
        finally:
            conn.close()
    except Exception as e:
        logger.warning("[catalog] record_upload failed: %s", e)
        return False


def mark_status(document_id: str, status: str) -> bool:
    """Transition a row to one of the terminal statuses.

    'active' → 'expired'   (cleanup cron, TTL fired)
    'active' → 'discarded' (user deleted via UI)
    'active' → 'promoted'  (Phase B.7: batch pipeline ran, doc now lives
                           in the main corpus with proper tags)

    Returns True on success; False on DB error or unknown status.
    """
    if status not in _VALID_STATUSES:
        logger.warning("[catalog] mark_status rejected invalid status=%r", status)
        return False
    if not document_id:
        return False
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE instant_rag_uploads SET status=%s WHERE document_id=%s",
                (status, document_id),
            )
            changed = cur.rowcount > 0
            conn.commit()
            cur.close()
            if changed:
                logger.info(
                    "[catalog] document_id=%s status → %s",
                    document_id, status,
                )
            return changed
        finally:
            conn.close()
    except Exception as e:
        logger.warning("[catalog] mark_status failed: %s", e)
        return False


def touch_last_queried(document_id: str) -> None:
    """Update last_queried_at to now. Best-effort; swallows errors.

    Called from the lazy-RAG tool when a search actually matches chunks
    in this doc. Powers a future "most recently used" sort in the
    cross-thread picker UI.
    """
    if not document_id:
        return
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE instant_rag_uploads SET last_queried_at = now() "
                "WHERE document_id = %s AND status = 'active'",
                (document_id,),
            )
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        logger.debug("[catalog] touch_last_queried failed: %s", e)


def update_confirmed_tags(
    document_id: str,
    *,
    payer: str | None = None,
    state: str | None = None,
    program: str | None = None,
    authority: str | None = None,
) -> bool:
    """Phase B.3 hook — user reviewed the LLM suggestion and committed
    the final metadata. Any field left as None is not touched (the
    caller's unknown, not a deliberate clear — clears should pass "")."""
    if not document_id:
        return False
    sets: list[str] = []
    params: list = []
    for col, val in (
        ("confirmed_payer", payer),
        ("confirmed_state", state),
        ("confirmed_program", program),
        ("confirmed_authority", authority),
    ):
        if val is not None:
            sets.append(f"{col}=%s")
            params.append(val)
    if not sets:
        return False
    params.append(document_id)
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                f"UPDATE instant_rag_uploads SET {', '.join(sets)} WHERE document_id=%s",
                params,
            )
            changed = cur.rowcount > 0
            conn.commit()
            cur.close()
            return changed
        finally:
            conn.close()
    except Exception as e:
        logger.warning("[catalog] update_confirmed_tags failed: %s", e)
        return False


# ── Public API: reads ─────────────────────────────────────────────────────


def list_for_thread(thread_id: str, *, include_inactive: bool = False) -> list[dict[str, Any]]:
    """Return all catalog rows for this thread. Matches the per-thread
    JSONB reads but goes straight to PG so it's authoritative."""
    if not thread_id:
        return []
    where = "WHERE thread_id = %s"
    params: list = [thread_id]
    if not include_inactive:
        where += " AND status = 'active'"
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                f"{_SELECT_SQL} {where} ORDER BY created_at DESC",
                params,
            )
            rows = [_row_to_dict(r) for r in cur.fetchall()]
            cur.close()
            return rows
        finally:
            conn.close()
    except Exception as e:
        logger.warning("[catalog] list_for_thread failed: %s", e)
        return []


def list_for_user(user_id: str, *, include_inactive: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    """Cross-thread: all uploads this user owns. Powers the Phase B.1e
    "previous uploads" picker and the future "my uploads" view."""
    if not user_id:
        return []
    where = "WHERE user_id = %s"
    params: list = [user_id]
    if not include_inactive:
        where += " AND status = 'active'"
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                f"{_SELECT_SQL} {where} ORDER BY created_at DESC LIMIT %s",
                params + [limit],
            )
            rows = [_row_to_dict(r) for r in cur.fetchall()]
            cur.close()
            return rows
        finally:
            conn.close()
    except Exception as e:
        logger.warning("[catalog] list_for_user failed: %s", e)
        return []


def get_by_document_id(document_id: str) -> dict[str, Any] | None:
    if not document_id:
        return None
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                f"{_SELECT_SQL} WHERE document_id = %s",
                (document_id,),
            )
            row = cur.fetchone()
            cur.close()
            return _row_to_dict(row) if row else None
        finally:
            conn.close()
    except Exception as e:
        logger.warning("[catalog] get_by_document_id failed: %s", e)
        return None


def get_by_upload_id(upload_id: str) -> dict[str, Any] | None:
    if not upload_id:
        return None
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                f"{_SELECT_SQL} WHERE upload_id = %s",
                (upload_id,),
            )
            row = cur.fetchone()
            cur.close()
            return _row_to_dict(row) if row else None
        finally:
            conn.close()
    except Exception as e:
        logger.warning("[catalog] get_by_upload_id failed: %s", e)
        return None


def list_expiring_before(cutoff: datetime) -> list[dict[str, Any]]:
    """Cleanup cron input: rows whose expires_at has passed. Only active
    rows — expired/discarded/promoted ones are already in a terminal
    state, no action needed."""
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                f"{_SELECT_SQL} WHERE status = 'active' AND expires_at IS NOT NULL "
                f"AND expires_at < %s ORDER BY expires_at ASC",
                (cutoff,),
            )
            rows = [_row_to_dict(r) for r in cur.fetchall()]
            cur.close()
            return rows
        finally:
            conn.close()
    except Exception as e:
        logger.warning("[catalog] list_expiring_before failed: %s", e)
        return []
