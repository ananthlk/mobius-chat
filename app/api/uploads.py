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

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.front_door import auth_mode, require_user
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
    authed_user_id: str | None = Depends(require_user),
) -> dict[str, Any]:
    """List catalog rows.

    Exactly one of ``thread_id`` or ``user_id`` is required. Cross-thread
    "all uploads" listings are not supported without a user_id filter —
    that would be an accidental data leak surface when auth lands.

    Ownership (2026-04-20 hardening): when ``auth_mode() == 'required'``
    and the caller passes a ``user_id`` query param, that value MUST
    match the caller's authenticated identity. Without this check,
    ``GET /chat/uploads?user_id=alice`` with bob's JWT would return
    alice's uploads — a cross-account data leak. Returns 403 on
    mismatch.
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

    # Ownership check for the user-scoped list. Thread-scoped listings
    # are not gated here because thread access is governed separately
    # (a thread's uploads are visible to anyone who can name its id —
    # not ideal but pre-existing and out of scope for this hardening).
    if user_id and auth_mode() == "required":
        if not authed_user_id or authed_user_id != user_id:
            raise HTTPException(
                status_code=403,
                detail=(
                    "user_id query parameter does not match the "
                    "authenticated caller. You can only list your "
                    "own uploads."
                ),
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


@router.get("/chat/uploads/recent/for-restoration")
def list_recent_for_restoration(
    current_thread_id: str | None = Query(None, description="Exclude uploads that belong to this thread"),
    limit: int = Query(10, ge=1, le=50),
    user_id: str | None = Depends(require_user),
) -> dict[str, Any]:
    """Phase B.1d — power the "you have recent uploads" banner.

    Returns the user's most recent instant-rag uploads that are NOT
    already on the current thread. Used by the frontend on page load /
    new-thread to offer a one-click restore for a doc that was uploaded
    on a different thread earlier.

    Scoping by auth mode (Phase 1h):
      - auth=required: ``user_id`` is guaranteed non-null (dependency 401s
        without a valid JWT); we scope catalog reads to that user.
      - auth=off (dev): ``user_id`` is None. We fall back to returning the
        globally most-recent active uploads. That's safe in dev because no
        other users exist; in prod this branch never runs because
        auth_mode() is 'required' by default.
      - auth=optional: returns user-scoped when the caller is authed,
        global-scoped when they're not — matches the optional-auth intent
        of letting anonymous callers still see their own recent uploads
        (nothing to scope by, so show recent).

    The banner is strictly advisory; clicking "Attach to this chat" goes
    through link_upload_to_thread below which is the single write path.
    """
    # db-agent refactor: moved the inline "global recent" SQL into
    # ``list_recent_global`` on the catalog module so all catalog DB
    # access goes through ``app.db_client``.
    from app.storage.instant_rag_catalog import list_recent_global

    if user_id:
        rows = list_for_user(user_id, include_inactive=False, limit=limit * 2)
    else:
        # Dev / auth-off branch — query catalog without user scope.
        rows = list_recent_global(limit=limit * 2)

    # Filter out uploads already on the current thread so the banner only
    # offers things the user genuinely needs to restore.
    if current_thread_id:
        rows = [r for r in rows if r.get("thread_id") != current_thread_id]

    rows = rows[:limit]
    return {
        "auth_scope": "user" if user_id else "global",
        "current_thread_id": current_thread_id,
        "count": len(rows),
        "uploads": [_to_payload(r) for r in rows],
    }


@router.post("/chat/uploads/{document_id}/link-to-thread")
def link_upload_to_thread(
    document_id: str,
    body: dict[str, Any],
    user_id: str | None = Depends(require_user),
) -> dict[str, Any]:
    """Phase B.1d — attach an existing upload to another thread without
    re-uploading the bytes.

    The catalog row stays put (``thread_id`` field records the *origin*
    thread); we only write a JSONB reference into the target thread's
    ``active.uploaded_files[]`` so the ReAct loop's
    ``_resolve_upload_document_id`` fast-path finds it.

    Why not just change catalog.thread_id? Because one upload can be
    useful on many threads — the user might want to reference
    "Sunshine_Manual.pdf" from three different chats. The catalog
    records origin; thread-level JSONB records usage. This keeps the
    two dimensions separate.

    Security: auth_mode=required users can only link THEIR OWN uploads
    (catalog.user_id match). In auth=off the check is skipped (dev).
    """
    target_thread_id = (body.get("thread_id") or "").strip()
    if not target_thread_id:
        raise HTTPException(status_code=400, detail="thread_id required in body.")

    row = get_by_document_id(document_id)
    if not row:
        raise HTTPException(status_code=404, detail="Upload not found in catalog.")
    if row.get("status") != STATUS_ACTIVE:
        raise HTTPException(
            status_code=409,
            detail=f"Upload is not active (status={row.get('status')}); cannot link.",
        )

    # Ownership check when auth is on. In auth=off dev mode, skip.
    if auth_mode() == "required":
        if not user_id:
            # Shouldn't get here — require_user would have 401'd — but
            # defense in depth.
            raise HTTPException(status_code=401, detail="Authentication required.")
        if row.get("user_id") and row.get("user_id") != user_id:
            raise HTTPException(
                status_code=403,
                detail="This upload belongs to another user.",
            )

    # Write a JSONB entry into the target thread's active.uploaded_files[].
    # Re-use the same shape _handle_instant_rag_upload writes so the
    # ReAct loop + _resolve_upload_document_id don't need to know the
    # entry came from a link operation.
    from datetime import datetime, timezone

    from app.storage.threads import append_uploaded_file_record, ensure_thread

    record = {
        "upload_id": row.get("upload_id") or "",
        "org_id": "",
        "org_name": "instant-rag",
        "purpose": "instant_rag",
        "filename": row.get("filename") or "upload",
        "row_count": row.get("chunks_count") or 0,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "envelope_id": row.get("envelope_id"),
        "document_id": document_id,
        # Mark this specific record as a link so future read paths can
        # distinguish "original upload on this thread" from "linked-in".
        "linked_from_thread": row.get("thread_id"),
    }

    try:
        real_tid = ensure_thread(target_thread_id) or target_thread_id
        ok = append_uploaded_file_record(real_tid, record)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Link failed: {e}") from e

    return {
        "linked": bool(ok),
        "document_id": document_id,
        "target_thread_id": real_tid,
        "origin_thread_id": row.get("thread_id"),
        "filename": row.get("filename"),
    }
