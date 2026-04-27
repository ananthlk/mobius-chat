"""Chat history sidebar endpoints (Phase 1a).

Routes:
    GET /chat/history/recent                — legacy per-turn list (back-compat).
    GET /chat/history/threads               — thread-level rollup (Phase 2.3).
    GET /chat/history/threads/{id}/turns    — rehydrate a thread's turns (Phase 13.7).
    GET /chat/history/most-helpful-searches — turns with positive feedback.
    GET /chat/history/most-helpful-documents — documents most cited in liked turns.

Extracted from ``app/main.py`` as the first proof-of-pattern slice for the
Phase 1 main-split refactor. The router is ``include_router``-mounted in
``main.py`` so external URLs are unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.storage.turns import (
    get_most_helpful_documents,
    get_most_helpful_turns,
    get_recent_turns,
)

router = APIRouter(prefix="/chat/history", tags=["history"])


def _parse_limit(limit: int | None) -> int:
    """Parse and clamp limit query param. Default 10, max 100.

    Kept local to the router so other router modules can define their own
    conventions. When two routers want the same parsing, promote to
    ``app.api._common``.
    """
    if limit is None:
        return 10
    return max(1, min(limit, 100))


@router.get("/recent")
def get_chat_history_recent(limit: int | None = 10):
    """Recent chat turns for sidebar: ``[{correlation_id, question, created_at}]``.

    Legacy endpoint — returns every turn verbatim. Kept for back-compat with
    clients that haven't migrated to ``/chat/history/threads`` yet.
    """
    return get_recent_turns(_parse_limit(limit))


@router.get("/threads")
def get_chat_history_threads(limit: int | None = 10):
    """Phase 2.3: recent *threads* for the sidebar.

    Returns ``[{thread_id, title, updated_at, turn_count}]`` deduplicated at
    the thread level — replaces the legacy per-turn list that was dumping
    raw URLs and tool-invocation fragments as "helpful searches."

    Falls back to an empty list (not a 500) if migration 030 hasn't run yet.
    """
    from app.storage.threads import get_recent_threads

    return get_recent_threads(_parse_limit(limit))


@router.get("/threads/{thread_id}/turns")
def get_chat_history_thread_turns(thread_id: str, limit: int | None = 50):
    """Phase 13.7 — rehydrate a thread's turns for sidebar click.

    When the user clicks a recent thread in the sidebar, the frontend
    calls this endpoint to load the existing conversation rather than
    re-running the original query as a fresh turn (the previous
    behavior, which lost continuity and burned LLM cost on questions
    already answered).

    Returns ``[{correlation_id, question, final_message, sources,
    created_at}]`` — turns ordered chronologically (oldest first), so
    the frontend can render them in conversation order. ``final_message``
    is the AnswerCard JSON exactly as the frontend renders for live
    turns; ``sources`` is the array stored at write time. Both are
    pass-through — no re-rendering, no LLM calls.

    Falls back to an empty list on a missing thread or DB error
    (matches /threads behavior — never 500 for sidebar reads).
    """
    from app.storage.threads import get_thread_turns

    tid = (thread_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="thread_id required")
    return get_thread_turns(tid, _parse_limit(limit))


@router.get("/most-helpful-searches")
def get_chat_history_most_helpful_searches(limit: int | None = 10):
    """Turns with positive feedback for sidebar."""
    return get_most_helpful_turns(_parse_limit(limit))


@router.get("/most-helpful-documents")
def get_chat_history_most_helpful_documents(limit: int | None = 10):
    """Documents most cited in liked answers."""
    return get_most_helpful_documents(_parse_limit(limit))
