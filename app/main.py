"""FastAPI app: POST /chat (enqueue), GET /chat/response/:id (poll), GET /chat/plan/:id, health."""
import logging
import uuid
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # load .env from project root (same pattern as Mobius RAG)

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.chat_config import chat_config_for_api
from app.config import get_config
from app.queue import get_queue
from app.storage import get_plan, get_response
from app.storage.progress import get_progress
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


class ChatResponse(BaseModel):
    correlation_id: str


@app.post("/chat", response_model=ChatResponse)
def post_chat(body: ChatRequest):
    """Enqueue a chat request; returns correlation_id for polling."""
    correlation_id = str(uuid.uuid4())
    get_queue().publish_request(correlation_id, {"message": body.message or ""})
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
