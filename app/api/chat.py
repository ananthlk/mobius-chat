"""Chat lifecycle router — extracted from main.py (Phase 2b.2).

Four routes that form the core request-response lifecycle:

    POST /chat                            — enqueue a chat request
    GET  /chat/response/{correlation_id}  — poll for completed response
    GET  /chat/stream/{correlation_id}    — SSE stream of progress events
    GET  /chat/plan/{correlation_id}      — stored plan + thinking log

Plus:
    - ``ChatRequest`` / ``ChatResponse`` Pydantic models
    - ``_enrich_completed_response_from_db`` — overlay qc_audit +
      technical_feedback from Postgres onto a completed response so
      thumbs / edits survive poll/refresh

Why these four move together: they share request state (correlation_id)
and all plug into the same queue + progress pipeline. Splitting them
across modules would force the queue / progress imports into three
places. Together, they're the "the user sent something and wants to
see the answer" surface.

External URLs unchanged because ``main.py`` does
``app.include_router(chat.router)``.

**Not** included in this router:
  - ``/chat/roster-upload`` — stays in main.py, has the instant-RAG
    upload handler attached. A future commit can extract that as its
    own module when the roster/instant-RAG split solidifies.
  - ``/chat/thread/{id}/uploads`` — thread-scoped upload state; lives
    with the upload paths in main.py for now.
  - ``/chat/config/*``, ``/chat/skills/urls``, ``/chat/llm-router-report``
    — readonly meta endpoints; their own cluster, potential next
    extraction.
  - ``/chat/org-name-candidates`` — standalone proxy unrelated to the
    lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.front_door import require_user
from app.config import get_config
from app.queue import get_queue
from app.storage import fetch_turn_qc_audit, get_plan, get_response
from app.storage.feedback import get_adjudication_feedback, get_llm_performance_feedback
from app.storage.progress import (
    get_and_clear_events,
    get_progress,
    get_progress_events_from_db,
    get_progress_from_db,
)
from app.storage.threads import ensure_thread

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


# ── Request / response models ───────────────────────────────────────


class ChatRequest(BaseModel):
    # Tolerate extra keys so older frontend builds that still send
    # credentialing_options / reconciliation_upload_id / etc. after the
    # 2026-04-18 disconnect don't get 422'd. Server ignores them.
    model_config = {"extra": "ignore"}

    message: str = ""
    thread_id: str | None = None  # When provided, load state for jurisdiction/context
    use_react: bool | None = None
    """Per-request override for MOBIUS_USE_REACT; when None, worker uses env."""
    chat_mode: Literal["copilot", "agentic", "quick"] | None = None
    """copilot: registry-first, 3 rounds. agentic: web escalation, 6 rounds. quick: mini-container, 2 rounds, brief answers."""

    system_context: str | None = None
    """Pre-loaded context the worker should treat as ground truth.

    When present, the ReAct pipeline attempts a Round 0 context-grounded
    answer before invoking any tools. If the question can be answered
    entirely from this context, we publish that answer and skip the
    normal Round 1..N tool loop (~1 LLM call vs 3-5). If Round 0 reports
    the context is insufficient, we fall through to the normal loop with
    the system_context still available on every reasoning round.

    Primary consumer: the story presentation layer, which clicks produce
    a node with pre-computed verified values (share, beneficiaries,
    etc.) that should be rendered, not re-derived via tools.
    """


class ChatResponse(BaseModel):
    correlation_id: str
    thread_id: str  # Created or reused; client sends on follow-up requests


# ── Helpers ─────────────────────────────────────────────────────────


def _enrich_completed_response_from_db(resp: dict) -> dict:
    """Overlay qc_audit + technical_feedback from Postgres so edits and thumbs survive poll/refresh.

    A chat response that's been persisted accumulates feedback over
    time (adjudication thumbs, per-source ratings, LLM-performance
    thumbs). The worker-authored response in the queue doesn't know
    about any of that — the feedback endpoints write directly to
    Postgres. Overlay here so clients polling for the response see a
    consistent view of "latest answer + latest feedback" without
    needing to issue a second request.
    """
    if not isinstance(resp, dict) or resp.get("status") != "completed":
        return resp
    cid = (resp.get("correlation_id") or "").strip()
    if not cid:
        return resp
    try:
        db_qc = fetch_turn_qc_audit(cid)
        if isinstance(db_qc, dict) and db_qc:
            resp = {**resp, "qc_audit": db_qc}
        lp = get_llm_performance_feedback(cid)
        adj = get_adjudication_feedback(cid)
        if lp or adj:
            tf: dict = {}
            if lp:
                tf["llm_performance"] = lp
            if adj:
                tf["adjudication"] = adj
            resp["technical_feedback"] = tf
    except Exception as e:
        logger.debug("DB enrich for response %s: %s", cid[:8], e)
    return resp


# ── Routes ──────────────────────────────────────────────────────────


@router.post("/chat", response_model=ChatResponse)
def post_chat(
    body: ChatRequest,
    user_id: str | None = Depends(require_user),
):
    """Enqueue a chat request; returns correlation_id and thread_id for polling.

    Phase 2d: ``require_user`` respects ``CHAT_AUTH_MODE``. In hosted envs
    (``CHAT_ENV=staging`` or ``prod``) auth defaults to ``required`` — a
    request without a valid JWT gets 401. Dev is ``off`` by default so
    local testing is unchanged.

    Phase 2d completion (2026-04-19): the authenticated ``user_id`` is
    now forwarded through the queue payload → worker → pipeline → onto
    the ``chat_turns.user_id`` column for audit attribution. None when
    auth is disabled (``CHAT_AUTH_MODE=off``); only included in the
    payload when non-None so older worker binaries that haven't picked
    up the new signature still work.
    """
    correlation_id = str(uuid.uuid4())
    thread_id = ensure_thread((body.thread_id or "").strip() or None)
    payload: dict = {"message": body.message or "", "thread_id": thread_id}
    if body.use_react is not None:
        payload["use_react"] = body.use_react
    if body.chat_mode is not None:
        payload["chat_mode"] = body.chat_mode
    if user_id:
        payload["user_id"] = user_id
    if body.system_context:
        payload["system_context"] = body.system_context
    get_queue().publish_request(correlation_id, payload)
    return ChatResponse(correlation_id=correlation_id, thread_id=thread_id)


@router.get("/chat/response/{correlation_id}")
def get_chat_response(correlation_id: str):
    """Poll for response.

    Returns the completed payload when done (enriched with qc_audit +
    technical_feedback from DB); while in progress returns status
    ``processing`` and the live thinking_log. When the worker runs in
    a separate process (Redis queue), falls through to the DB-backed
    progress source because the in-memory queue is empty in the API
    process.
    """
    q = get_queue()
    resp = q.get_response(correlation_id)
    if resp is None:
        resp = get_response(correlation_id)
    if resp is not None:
        return _enrich_completed_response_from_db(resp)
    cfg = get_config()
    in_progress, thinking_log, message_so_far = get_progress(correlation_id)
    # When worker runs in separate process (Redis), in-memory progress
    # is empty in the API process; fetch from DB to bridge the gap.
    if not in_progress and cfg.queue_type == "redis":
        thinking_log, message_so_far = get_progress_from_db(correlation_id)
        in_progress = bool(thinking_log or message_so_far)
    if in_progress:
        return {
            "status": "processing",
            "message": message_so_far or None,
            "plan": None,
            "thinking_log": thinking_log,
        }
    return {"status": "pending", "message": None, "plan": None, "thinking_log": None}


@router.get("/chat/stream/{correlation_id}")
async def chat_stream(correlation_id: str):
    """SSE stream of progress events + the final completed response.

    Long default timeout (30 min) because large Medicaid reports can
    run 15+ min before finalizing — cutting the stream short mid-report
    would leak the partial progress the client had. Keepalive every
    15s keeps any intermediate proxies from idle-timing-out the
    connection.

    Polls the DB for progress events when the worker is separate
    (Redis queue) — in-memory events are empty in the API process,
    but the worker mirrors events to ``chat_progress_events`` for
    this exact bridging case.
    """
    cfg = get_config()
    q = get_queue()
    use_db = cfg.queue_type == "redis"
    last_progress_id = 0
    loop = asyncio.get_running_loop()
    last_keepalive = loop.time()
    timeout_s = int(os.environ.get("CHAT_STREAM_TIMEOUT_S", "1800"))

    async def event_generator():
        nonlocal last_progress_id, last_keepalive
        start = loop.time()
        # SSE hardening (2026-04-22): flush an immediate comment line so
        # Cloud Run / intermediate proxies see bytes within ~50ms of the
        # connection opening and don't buffer-and-flush-on-timeout. Some
        # Cloud Run load-balancer configurations hold the first body
        # bytes until ~200–500ms of data accumulate, which delays
        # ``es.onopen`` on the client and occasionally trips
        # ``es.onerror`` → fallback to 400ms polling — causing the
        # "hundreds of /chat/response polls per turn" pattern observed
        # in dev logs. A comment line (starts with ``:``) is ignored by
        # the SSE parser but forces a flush.
        yield ": stream-open\n\n"
        last_keepalive = loop.time()
        while True:
            now = loop.time()
            if now - start > timeout_s:
                yield f"data: {json.dumps({'event': 'error', 'data': {'message': 'Stream timeout'}})}\n\n"
                return
            # Progress events (DB or in-memory source depending on queue type)
            if use_db:
                for ev_id, ev in get_progress_events_from_db(correlation_id, after_id=last_progress_id):
                    last_progress_id = ev_id
                    yield f"data: {json.dumps(ev)}\n\n"
                    last_keepalive = now  # real data counts as keepalive
            else:
                for ev in get_and_clear_events(correlation_id):
                    yield f"data: {json.dumps(ev)}\n\n"
                    last_keepalive = now
            # Terminal: completed response
            resp = q.get_response(correlation_id)
            if resp is None:
                resp = get_response(correlation_id)
            if resp is not None:
                yield f"data: {json.dumps({'event': 'completed', 'data': resp})}\n\n"
                return
            # Keepalive every 10s (was 15s) — Cloud Run's HTTP/2 path
            # occasionally idle-timeouts SSE at ~30s without a cushion.
            # 10s gives two chances to hit the timer before it fires.
            if now - last_keepalive > 10:
                yield ": keepalive\n\n"
                last_keepalive = now
            await asyncio.sleep(0.2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # ``X-Accel-Buffering: no`` tells nginx-family proxies (and
            # some Cloud Run LB configurations respect it) NOT to buffer
            # the stream — send each yielded chunk to the client
            # immediately. Without this, small SSE events pile up in a
            # 4KB buffer until it flushes on timeout, which is the
            # exact pattern that makes thinking-panel updates look
            # "sticky" and causes client-side SSE fallback to polling.
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/chat/plan/{correlation_id}")
def get_chat_plan(correlation_id: str):
    """Get stored plan (and thinking log) for correlation_id.

    Intentionally 404s when missing rather than returning an empty
    object: the UI's plan inspector shows a different state for
    "not found" vs. "plan has zero subquestions," and a 200 with
    empty body would collapse those.
    """
    plan_payload = get_plan(correlation_id)
    if plan_payload is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan_payload
