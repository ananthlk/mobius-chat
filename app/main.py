"""FastAPI app: POST /chat (enqueue), GET /chat/response/:id (poll), GET /chat/plan/:id, health."""
import logging
import os
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
    load_dotenv(_chat_root / ".env", override=True)
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
