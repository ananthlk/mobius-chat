"""FastAPI app: POST /chat (enqueue), GET /chat/response/:id (poll), GET /chat/stream/:id (SSE), health."""
import asyncio
import json
import logging
import uuid
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # load .env from project root (same pattern as Mobius RAG)

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.chat_config import chat_config_for_api
from app.config import get_config
from app.queue import get_queue
from app.storage import (
    get_most_helpful_documents,
    get_most_helpful_turns,
    get_plan,
    get_recent_turns,
    get_response,
)
from app.storage.progress import get_and_clear_events, get_progress, get_progress_events_from_db
from app.worker import start_worker_background

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Mobius Chat", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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
    thread_id: str | None = None  # When provided, load state for jurisdiction/context


class ChatResponse(BaseModel):
    correlation_id: str


@app.post("/chat", response_model=ChatResponse)
def post_chat(body: ChatRequest):
    """Enqueue a chat request; returns correlation_id for polling."""
    correlation_id = str(uuid.uuid4())
    payload: dict = {"message": body.message or ""}
    if body.thread_id and body.thread_id.strip():
        payload["thread_id"] = body.thread_id.strip()
    get_queue().publish_request(correlation_id, payload)
    return ChatResponse(correlation_id=correlation_id)


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


@app.get("/chat/stream/{correlation_id}")
async def chat_stream(correlation_id: str):
    """SSE stream: progress events (thinking, message) then completed. Polls DB when worker is separate (Redis)."""
    cfg = get_config()
    q = get_queue()
    use_db = cfg.queue_type == "redis"
    last_progress_id = 0
    loop = asyncio.get_running_loop()
    last_keepalive = loop.time()
    timeout_s = 300

    async def event_generator():
        nonlocal last_progress_id, last_keepalive
        start = loop.time()
        while True:
            now = loop.time()
            if now - start > timeout_s:
                yield f"data: {json.dumps({'event': 'error', 'data': {'message': 'Stream timeout'}})}\n\n"
                return
            # Progress events
            if use_db:
                for ev_id, ev in get_progress_events_from_db(correlation_id, after_id=last_progress_id):
                    last_progress_id = ev_id
                    yield f"data: {json.dumps(ev)}\n\n"
            else:
                for ev in get_and_clear_events(correlation_id):
                    yield f"data: {json.dumps(ev)}\n\n"
            # Check for completed response
            resp = q.get_response(correlation_id)
            if resp is None:
                resp = get_response(correlation_id)
            if resp is not None:
                yield f"data: {json.dumps({'event': 'completed', 'data': resp})}\n\n"
                return
            # Keepalive every 15s
            if now - last_keepalive > 15:
                yield ": keepalive\n\n"
                last_keepalive = now
            await asyncio.sleep(0.2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
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


def _parse_limit(limit: int | None) -> int:
    """Parse and clamp limit query param. Default 10, max 100."""
    if limit is None:
        return 10
    return max(1, min(limit, 100))


@app.get("/chat/history/recent")
def get_chat_history_recent(limit: int | None = 10):
    """Recent chat turns for sidebar: { correlation_id, question, created_at }."""
    return get_recent_turns(_parse_limit(limit))


@app.get("/chat/history/most-helpful-searches")
def get_chat_history_most_helpful_searches(limit: int | None = 10):
    """Turns with positive feedback for sidebar."""
    return get_most_helpful_turns(_parse_limit(limit))


@app.get("/chat/history/most-helpful-documents")
def get_chat_history_most_helpful_documents(limit: int | None = 10):
    """Documents most cited in liked answers."""
    return get_most_helpful_documents(_parse_limit(limit))


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
