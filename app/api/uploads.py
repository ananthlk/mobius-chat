"""Cross-thread uploads router (Phase B.1c).

Thin FastAPI router that exposes the durable instant-rag upload catalog
for the frontend's "my uploads" picker (Phase B.1e, future) and for
future observability dashboards.

Distinct from ``GET /chat/thread/{id}/uploads`` (in main.py) which lists
uploads for one specific thread via the JSONB fast path. This router
goes through the catalog table to answer questions the JSONB can't:

  GET /chat/uploads?thread_id=X   → rows for that thread from catalog
  GET /chat/uploads?user_id=Y     → all rows for that user
  GET /chat/uploads/{document_id} → single row

The old JSONB-backed thread-scoped endpoint stays where it is for now.
When / if we fully migrate, that endpoint becomes a thin proxy to
list_for_thread here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.storage.instant_rag_catalog import (
    STATUS_ACTIVE,
    get_by_document_id,
    list_for_thread,
    list_for_user,
)

router = APIRouter()


def _to_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a catalog row for JSON: stringify datetimes."""
    out = dict(row)
    for k in ("created_at", "expires_at", "last_queried_at"):
        v = out.get(k)
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    return out


@router.get("/chat/uploads")
def list_uploads(
    thread_id: str | None = Query(None, description="Filter to a specific thread's uploads"),
    user_id: str | None = Query(None, description="Filter to a specific user's uploads (cross-thread)"),
    include_inactive: bool = Query(False, description="Include expired/discarded/promoted rows"),
    limit: int = Query(100, ge=1, le=500, description="Max rows to return for user-scoped list"),
) -> dict[str, Any]:
    """List catalog rows.

    Exactly one of ``thread_id`` or ``user_id`` is required. Cross-thread
    "all uploads" listings are not supported without a user_id filter —
    that would be an accidental data leak surface when auth lands.
    """
    if not thread_id and not user_id:
        raise HTTPException(
            status_code=400,
            detail="Provide either thread_id or user_id to scope the list.",
        )
    if thread_id and user_id:
        raise HTTPException(
            status_code=400,
            detail="Pass thread_id OR user_id, not both — use separate requests.",
        )

    if thread_id:
        rows = list_for_thread(thread_id, include_inactive=include_inactive)
        scope = f"thread={thread_id}"
    else:
        rows = list_for_user(user_id, include_inactive=include_inactive, limit=limit)
        scope = f"user={user_id}"

    return {
        "scope": scope,
        "count": len(rows),
        "uploads": [_to_payload(r) for r in rows],
    }


@router.get("/chat/uploads/{document_id}")
def get_upload(document_id: str) -> dict[str, Any]:
    row = get_by_document_id(document_id)
    if not row:
        raise HTTPException(status_code=404, detail="Upload not found in catalog.")
    return _to_payload(row)
