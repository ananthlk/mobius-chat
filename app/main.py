"""FastAPI app: POST /chat (enqueue), GET /chat/response/:id (poll), GET /chat/stream/:id (SSE), GET /chat/plan/:id, health."""
import asyncio
import json
import logging
import os
import queue
import threading
import time
import uuid
from pathlib import Path

_chat_root = Path(__file__).resolve().parent.parent
# Load env first (module + global, fixes placeholder credentials) so GOOGLE_APPLICATION_CREDENTIALS is never /path/to/...
import sys
_config_dir = _chat_root.parent / "mobius-config"
if _config_dir.exists() and str(_config_dir) not in sys.path:
    sys.path.insert(0, str(_config_dir))
try:
    from env_helper import load_env
    load_env(_chat_root)
except ImportError:
    from dotenv import load_dotenv
    _env_file = _chat_root / ".env"
    _preserve = {k: os.environ.get(k) for k in ("QUEUE_TYPE", "REDIS_URL") if os.environ.get(k)}
    load_dotenv(_env_file, override=True)
    for k, v in _preserve.items():
        if v is not None:
            os.environ[k] = v
    # Clear placeholder credentials and resolve to credentials/*.json when env_helper not available
    _c = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or ""
    if "/path/to/" in _c or "your-service-account" in _c or "your-" in _c.lower():
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        for _d in (_chat_root / "credentials", _chat_root.parent / "mobius-config" / "credentials"):
            if _d.exists():
                for _p in _d.glob("*.json"):
                    if _p.is_file():
                        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(_p.resolve())
                        break
                if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
                    break

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import httpx

from app.chat_config import chat_config_for_api
from app.config import get_config
from app.queue import get_queue
from app.queue.redis_queue import RedisQueue
from app.storage import (
    get_most_helpful_documents,
    get_most_helpful_turns,
    get_plan,
    get_recent_turns,
    get_response,
)
from app.storage.threads import ensure_thread
from app.auth import get_user_id_from_request
from app.storage.feedback import get_feedback, get_source_feedback, insert_feedback, insert_source_feedback
from app.storage.progress import get_and_clear_events, get_progress
from app.worker import start_worker_background

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Mobius Chat", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Auth: proxy to Mobius-OS (plug-and-play) or use mobius-user (standalone)
_mobius_os_auth_url = (os.getenv("MOBIUS_OS_AUTH_URL") or "").rstrip("/")
_user_db_url = os.getenv("USER_DATABASE_URL")

if _mobius_os_auth_url:
    # Proxy auth to Mobius-OS - same users/tokens as extension
    _auth_base = f"{_mobius_os_auth_url}/api/v1/auth"

    @app.api_route("/api/v1/auth/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy_auth(request: Request, path: str):
        """Forward auth requests to Mobius-OS for plug-and-play shared users."""
        url = f"{_auth_base}/{path}" if path else _auth_base
        if request.url.query:
            url = f"{url}?{request.url.query}"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
        body = await request.body() if request.method in ("POST", "PUT", "PATCH") else None
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.request(request.method, url, headers=headers, content=body)
            ct = resp.headers.get("content-type", "")
            if "application/json" in ct:
                return JSONResponse(status_code=resp.status_code, content=resp.json())
            return JSONResponse(status_code=resp.status_code, content={"detail": resp.text or "Auth error"})
        except httpx.RequestError as e:
            logger.warning("Auth proxy to Mobius-OS failed: %s", e)
            return JSONResponse(status_code=502, content={"detail": "Auth service unavailable"})

    logger.info("Auth proxying to Mobius-OS at %s", _mobius_os_auth_url)
elif _user_db_url:
    try:
        import mobius_user.db.session as _user_db
        from mobius_user.routes.fastapi_auth import router as _auth_router
        _user_db.init_db(_user_db_url)
        app.include_router(_auth_router, prefix="/api/v1/auth")
        logger.info("Mounted mobius-user auth at /api/v1/auth")
    except ImportError as e:
        logger.warning("mobius-user not installed; auth disabled. pip install -e ../mobius-user : %s", e)
else:
    logger.info("MOBIUS_OS_AUTH_URL and USER_DATABASE_URL not set; auth disabled")

# Start worker in background only for in-memory queue (single process). For Redis, run worker separately.
_worker_started = False


@app.on_event("startup")
def maybe_start_worker():
    global _worker_started
    if _worker_started:
        return
    cfg = get_config()
    if cfg.queue_type == "memory":
        start_worker_background()
        _worker_started = True
        logger.info("Started in-process worker (memory queue)")
    else:
        logger.info("Queue type=%s: run worker separately with: python -m app.worker", cfg.queue_type)


class ChatRequest(BaseModel):
    message: str = ""
    session_id: str | None = None
    thread_id: str | None = None


class ChatResponse(BaseModel):
    correlation_id: str
    thread_id: str | None = None


FEEDBACK_COMMENT_MAX_LENGTH = 500


class FeedbackRequest(BaseModel):
    correlation_id: str
    rating: str  # "up" | "down"
    comment: str | None = None


@app.post("/chat/feedback")
def post_feedback(body: FeedbackRequest):
    """Persist thumbs up/down and optional comment for a turn. One feedback per correlation_id (upsert)."""
    if body.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating must be 'up' or 'down'")
    comment = (body.comment or "").strip()
    if len(comment) > FEEDBACK_COMMENT_MAX_LENGTH:
        comment = comment[:FEEDBACK_COMMENT_MAX_LENGTH]
    insert_feedback(body.correlation_id, body.rating, comment or None)
    return {"ok": True}


@app.get("/chat/feedback/{correlation_id}")
def get_feedback_route(correlation_id: str):
    """Return stored feedback for a turn, or 404 if none."""
    fb = get_feedback(correlation_id)
    if fb is None:
        raise HTTPException(status_code=404, detail="No feedback for this turn")
    return fb


class SourceFeedbackRequest(BaseModel):
    correlation_id: str
    source_index: int  # 1-based index in turn's sources array
    rating: str  # "up" | "down"


@app.post("/chat/feedback/source")
def post_source_feedback(body: SourceFeedbackRequest):
    """Persist thumbs up/down for one source in a turn. Upsert by (correlation_id, source_index)."""
    if body.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating must be 'up' or 'down'")
    if body.source_index < 1:
        raise HTTPException(status_code=400, detail="source_index must be >= 1")
    insert_source_feedback(body.correlation_id, body.source_index, body.rating)
    return {"ok": True}


@app.get("/chat/feedback/source/{correlation_id}")
def get_source_feedback_route(correlation_id: str):
    """Return per-source feedback for a turn: { ratings: [ { source_index, rating } ] }."""
    ratings = get_source_feedback(correlation_id)
    return {"ratings": ratings}


@app.get("/chat/history/recent")
def get_history_recent(limit: int = 10):
    """Return recent turns for left panel (correlation_id, question, created_at)."""
    return get_recent_turns(limit=max(1, min(limit, 100)))


@app.get("/chat/history/most-helpful-searches")
def get_history_most_helpful_searches(limit: int = 10):
    """Return turns with thumbs up for left panel."""
    return get_most_helpful_turns(limit=max(1, min(limit, 100)))


@app.get("/chat/history/most-helpful-documents")
def get_history_most_helpful_documents(limit: int = 10):
    """Return top documents from turns with thumbs up (document_name, document_id when present)."""
    return get_most_helpful_documents(limit=max(1, min(limit, 100)))


@app.get("/api/v1/documents/{document_id}/pages")
async def get_document_pages_proxy(document_id: str, page_number: int | None = None):
    """Proxy to RAG backend for full-page inline reader. Returns 501 if RAG_APP_API_BASE not set."""
    cfg = get_config()
    base = getattr(cfg, "rag_app_api_base", None) if cfg else None
    if not base:
        raise HTTPException(status_code=501, detail="Document pages proxy not configured (set RAG_APP_API_BASE)")
    base = base.rstrip("/")
    url = f"{base}/documents/{document_id}/pages"
    if page_number is not None:
        url = f"{url}?page_number={page_number}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail="Document not found")
        resp.raise_for_status()
        return resp.json()
    except httpx.RequestError as e:
        logger.warning("Document pages proxy failed: %s", e)
        raise HTTPException(status_code=502, detail="RAG backend unavailable")


@app.post("/chat", response_model=ChatResponse)
def post_chat(request: Request, body: ChatRequest):
    """Enqueue a chat request; returns correlation_id and thread_id for polling and subsequent messages."""
    correlation_id = str(uuid.uuid4())
    thread_id = (body.thread_id or "").strip() or None
    if not thread_id:
        thread_id = ensure_thread(None)
    payload: dict = {"message": body.message or "", "thread_id": thread_id}
    if body.session_id is not None:
        payload["session_id"] = body.session_id
    user_id = get_user_id_from_request(request)
    if user_id:
        payload["user_id"] = user_id
    get_queue().publish_request(correlation_id, payload)
    return ChatResponse(correlation_id=correlation_id, thread_id=thread_id)


@app.get("/chat/response/{correlation_id}")
def get_chat_response(correlation_id: str):
    """Poll for response. Returns completed payload when done; while in progress returns status 'processing' and live thinking_log."""
    q = get_queue()
    resp = q.get_response(correlation_id)
    if resp is None:
        resp = get_response(correlation_id)
    if resp is not None:
        return resp
    in_progress, thinking_log, message_so_far = get_progress(correlation_id)
    if in_progress:
        return {"status": "processing", "message": message_so_far or None, "plan": None, "thinking_log": thinking_log}
    return {"status": "pending", "message": None, "plan": None, "thinking_log": None}


def _redis_progress_subscriber(channel: str, out: queue.Queue) -> None:
    """Run in a thread. Subscribes to Redis channel and puts each message payload (JSON str) into out."""
    try:
        from app.config import get_config
        import redis
        cfg = get_config()
        r = redis.from_url(cfg.redis_url, decode_responses=True)
        pubsub = r.pubsub()
        pubsub.subscribe(channel)
        for message in pubsub.listen():
            if message.get("type") == "message":
                data = message.get("data")
                if data is not None:
                    out.put(data)
    except Exception as e:
        logger.exception("Redis progress subscriber error: %s", e)
        out.put(None)  # Signal error so stream can exit


@app.get("/chat/stream/{correlation_id}")
async def stream_chat_response(correlation_id: str):
    """SSE stream: yields thinking and message chunks in real time, then a 'completed' event with full response.
    With Redis queue, subscribes to progress channel. With memory queue, polls in-memory progress."""
    STREAM_POLL_INTERVAL = 0.05
    STREAM_REDIS_GET_TIMEOUT = 0.5
    STREAM_TIMEOUT_SEC = 300

    cfg = get_config()
    queue_is_redis = isinstance(get_queue(), RedisQueue)
    live_stream_env = getattr(cfg, "live_stream_via_redis", False)
    use_redis_stream = live_stream_env or queue_is_redis
    logger.info(
        "[stream] GET /chat/stream/%s live_stream_via_redis=%s queue_type=%s queue_is_redis=%s use_redis_stream=%s",
        correlation_id[:8],
        live_stream_env,
        getattr(cfg, "queue_type", "?"),
        queue_is_redis,
        use_redis_stream,
    )

    async def event_generator():
        start = time.monotonic()
        redis_queue = None
        redis_thread = None
        use_redis = use_redis_stream  # local copy so except can set False without UnboundLocalError
        if use_redis:
            try:
                channel = getattr(cfg, "redis_progress_channel_prefix", "mobius:chat:progress:") + correlation_id
                redis_queue = queue.Queue()
                redis_thread = threading.Thread(
                    target=_redis_progress_subscriber,
                    args=(channel, redis_queue),
                    daemon=True,
                )
                redis_thread.start()
                logger.info("[stream] Redis subscriber started for channel=%s", channel)
            except Exception as e:
                logger.warning("[stream] Redis progress subscribe failed, falling back to poll: %s", e)
                use_redis = False

        loop = asyncio.get_running_loop()
        redis_event_count = 0

        while True:
            if time.monotonic() - start > STREAM_TIMEOUT_SEC:
                yield f"data: {json.dumps({'event': 'error', 'data': {'message': 'Stream timeout'}})}\n\n"
                return

            if use_redis and redis_queue is not None:
                try:
                    def get_with_timeout():
                        return redis_queue.get(timeout=STREAM_REDIS_GET_TIMEOUT)
                    raw = await loop.run_in_executor(None, get_with_timeout)
                    if raw is None:
                        yield f"data: {json.dumps({'event': 'error', 'data': {'message': 'Stream error'}})}\n\n"
                        return
                    ev = json.loads(raw) if isinstance(raw, str) else raw
                    redis_event_count += 1
                    received_at = time.strftime("%H:%M:%S", time.localtime()) + f".{int(time.time() * 1000) % 1000:03d}"
                    data = ev.get("data") or {}
                    written_at = data.get("ts_readable", "")
                    if redis_event_count == 1:
                        logger.info("[stream] first progress event received for %s", correlation_id[:8])
                    logger.info(
                        "[stream] received #%s cid=%s event=%s written_at=%s received_at=%s",
                        redis_event_count, correlation_id[:8], ev.get("event"), written_at, received_at,
                    )
                    yield f"data: {json.dumps(ev)}\n\n"
                except queue.Empty:
                    pass
                except Exception as e:
                    logger.debug("Redis stream get: %s", e)
                await asyncio.sleep(0.02)
            else:
                for ev in get_and_clear_events(correlation_id):
                    yield f"data: {json.dumps(ev)}\n\n"

            q = get_queue()
            resp = q.get_response(correlation_id)
            if resp is None:
                resp = get_response(correlation_id)
            if resp is not None:
                logger.info("[stream] completed %s (redis_events=%s)", correlation_id[:8], redis_event_count if use_redis else "n/a")
                yield f"data: {json.dumps({'event': 'completed', 'data': resp})}\n\n"
                return
            await asyncio.sleep(STREAM_POLL_INTERVAL if not use_redis else 0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/chat/plan/{correlation_id}")
def get_chat_plan(correlation_id: str):
    """Get stored plan (and thinking log) for correlation_id."""
    plan_payload = get_plan(correlation_id)
    if plan_payload is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan_payload


@app.get("/chat/config")
def get_chat_config():
    """Chat-specific config and prompts (LLM, parser, prompts) for the hamburger menu."""
    return chat_config_for_api()


@app.get("/health")
def health():
    return {"status": "ok"}


# Serve chat UI at /
_frontend = Path(__file__).resolve().parent.parent / "frontend"
if _frontend.exists():
    app.mount("/static", StaticFiles(directory=_frontend / "static"), name="static")

    @app.get("/")
    def index():
        return FileResponse(_frontend / "index.html")
