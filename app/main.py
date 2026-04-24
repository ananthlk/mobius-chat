"""FastAPI app: startup + middleware + routes that haven't been extracted yet.

Phase 2b.1 / 2b.2 extracted the doc-reader proxy and core chat
lifecycle to ``app/api/doc_reader.py`` and ``app/api/chat.py``. Routes
still living here: /health, /chat/org-name-candidates,
/chat/roster-upload (with instant-RAG handler), /chat/thread/{id}/uploads,
/chat/config/*, /chat/skills/urls, /chat/llm-router-report, the static
mount, and /internal/skill-llm.
"""
import os
import logging
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
load_dotenv()  # load .env from project root (same pattern as Mobius RAG)

# Enable Groq / Anthropic / Together / OpenAI roster entries when API keys are present (same as worker).
try:
    from app.services.model_registry import auto_enable_from_env

    auto_enable_from_env()
except Exception:
    pass

# Always use ReAct (ignore .env); for legacy run API with MOBIUS_USE_REACT=0
os.environ["MOBIUS_USE_REACT"] = "1"

from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


class NoCacheStaticFiles(StaticFiles):
    """Static files with no-cache to ensure frontend changes are picked up after mstart."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        return response

from app.chat_config import chat_config_for_api
from app.config import get_config
from app.queue import get_queue
from app.storage.threads import append_uploaded_file_record, ensure_thread, get_state, save_state, save_state_full
from app.storage.llm_router_report import fetch_llm_router_report
from app.worker import start_worker_background

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _roster_freshness_days_threshold() -> int:
    try:
        return max(1, int(os.environ.get("CHAT_ROSTER_FRESH_DAYS", "14")))
    except ValueError:
        return 14


def _compute_roster_freshness(
    latest: dict[str, Any] | None,
) -> tuple[Literal["fresh", "stale", "none"], float | None]:
    """Age-based signal for UI: green vs grey roster indicator."""
    if not latest:
        return "none", None
    uid = (latest.get("upload_id") or "").strip()
    oid = (latest.get("org_id") or "").strip()
    if not uid or not oid:
        return "none", None
    uploaded_at = latest.get("uploaded_at")
    if not uploaded_at:
        return "stale", None
    try:
        from datetime import datetime, timezone

        raw = str(uploaded_at).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        age_days = (now - dt).total_seconds() / 86400.0
        thresh = float(_roster_freshness_days_threshold())
        return ("fresh" if age_days <= thresh else "stale"), age_days
    except Exception:
        return "stale", None


def _build_roster_upload_acknowledgment(
    *,
    filename: str,
    org_name_entered: str,
    billing_npi: str,
    matched_org_name: str,
    matched_practice_address: str | None,
    row_count_cleansed: int,
    row_count_resolved: int,
    process_status: str,
    resolution_summary: dict[str, Any] | None,
    pipeline_progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Plain-language recap for non-technical users (TurboTax-style checklist).
    Shown in the chat UI after a successful roster upload.
    """
    checks: list[dict[str, str]] = []

    checks.append(
        {
            "tone": "success",
            "title": "We received your file",
            "detail": (
                f"{filename} — we read {row_count_cleansed} roster row(s) after cleanup "
                f"(blank lines and basic formatting fixes)."
            ),
        }
    )
    if pipeline_progress and isinstance(pipeline_progress, dict):
        psum = (pipeline_progress.get("summary") or "").strip()
        if psum:
            checks.append(
                {
                    "tone": "success",
                    "title": "Pipeline — where your upload is",
                    "detail": psum,
                }
            )
    checks.append(
        {
            "tone": "success",
            "title": "Organization you entered",
            "detail": f"You told us this roster is for: {org_name_entered}.",
        }
    )
    entity = (matched_org_name or "").strip() or "Name from the national registry"
    addr = (matched_practice_address or "").strip()
    loc = f" Practice address on file: {addr}." if addr else ""
    checks.append(
        {
            "tone": "success",
            "title": "Billing organization we matched",
            "detail": (
                f"We’re using billing NPI {billing_npi} — {entity}.{loc} "
                f"This is the organization we use for outside-in claims data in reconciliation. "
                f"If that’s not the right entity, type: Use billing NPI and your 10-digit number."
            ),
        }
    )

    if row_count_resolved and resolution_summary:
        high = int(resolution_summary.get("confidence_high") or 0)
        med = int(resolution_summary.get("confidence_medium") or 0)
        low = int(resolution_summary.get("confidence_low") or 0)
        checks.append(
            {
                "tone": "success",
                "title": "Provider names and NPIs",
                "detail": (
                    f"We checked {row_count_resolved} provider row(s) against the national NPI registry. "
                    f"{high} row(s) matched with high confidence, {med} with medium, {low} with low. "
                    f"The reconciliation report will flag anything that needs a second look."
                ),
            }
        )
    elif row_count_resolved:
        checks.append(
            {
                "tone": "success",
                "title": "Provider rows prepared",
                "detail": f"We prepared {row_count_resolved} provider row(s) for reconciliation.",
            }
        )

    alerts: list[dict[str, str]] = []
    if resolution_summary:
        nm = int(resolution_summary.get("no_match") or 0) + int(resolution_summary.get("not_in_nppes") or 0)
        if nm > 0:
            alerts.append(
                {
                    "tone": "warning",
                    "message": (
                        f"{nm} row(s) did not match a national NPI record. They stay on your roster — "
                        f"the report may ask you to verify those providers manually."
                    ),
                }
            )
        low_n = int(resolution_summary.get("confidence_low") or 0)
        if low_n > 0:
            alerts.append(
                {
                    "tone": "notice",
                    "message": (
                        f"{low_n} row(s) have a low-confidence NPI match. "
                        f"The reconciliation output will highlight them — a quick review is a good idea."
                    ),
                }
            )
        ins = int(resolution_summary.get("insufficient") or 0)
        if ins > 0:
            alerts.append(
                {
                    "tone": "notice",
                    "message": (
                        f"{ins} row(s) didn’t have enough name or NPI information for an automatic registry lookup."
                    ),
                }
            )

    next_step = (
        "You’re set — we saved everything to this chat. If “Send reconciliation request after upload” was on, "
        "your request is already running. Otherwise, press Send with the message we put in the box."
    )

    return {
        "headline": "We’ve got your roster",
        "subhead": "Here’s what we understood and saved. You don’t need to upload again unless you change files.",
        "checks": checks,
        "alerts": alerts,
        "next_step": next_step,
        "process_status": process_status,
    }

# Structured logging (Sprint 1 #10, 2026-04-23) — must run before the
# FastAPI app is built so the first import-time log lines already go
# through the configured formatter. configure_logging is idempotent;
# a second call (e.g. from a worker module) is a no-op.
from app.logging_config import configure_logging, request_context_middleware
configure_logging()

# Distributed tracing (Sprint 1 #11, 2026-04-24) — init the TracerProvider
# before auto-instrumentation runs, so the FastAPI instrumentor picks it
# up on app construction. Env-gated: off by default in dev, on in hosted
# envs. See app/tracing_config.py for the full contract.
from app.tracing_config import configure_tracing, instrument_app
configure_tracing()

app = FastAPI(title="Mobius Chat", version="0.1.0")
# Auto-instrumentation must run AFTER the FastAPI app is constructed.
# Safe no-op when tracing is disabled.
instrument_app(app)

# Phase 1h: front-door hardening.
# - CORS is env-driven (CHAT_CORS_ORIGINS). Dev default: '*'; staging/prod
#   MUST set an explicit allowlist or the app refuses to start.
# - Rate limit is opt-in via CHAT_RATE_LIMIT_PER_MINUTE; hosted envs get a
#   30 req/min/IP default on /chat paths.
# - See app/api/front_door.py for the full contract. All env-var lookups
#   for front-door config live there so the surface is auditable from one file.
from app.api.front_door import (
    InMemoryRateLimitMiddleware,
    require_user,
    resolve_cors_config,
    resolve_rate_limit_config,
)

_cors_cfg = resolve_cors_config()
# Middleware registration order = REVERSE execution order (FastAPI puts
# last-added on the outside). We want:
#   request_context → (outer, runs first; stamps correlation_id)
#   rate_limit      → (middle; reads headers, peeks body)
#   CORS            → (inner; adds response headers on the way out)
# So register CORS first, then rate_limit, then request_context.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_cfg.allow_origins,
    allow_methods=_cors_cfg.allow_methods,
    allow_headers=_cors_cfg.allow_headers,
    allow_credentials=_cors_cfg.allow_credentials,
)
app.add_middleware(InMemoryRateLimitMiddleware, config=resolve_rate_limit_config())
app.middleware("http")(request_context_middleware)


# ── Request body size cap (2026-04-20) ─────────────────────────────
#
# /upload already has a 100 MB chunked cap enforced in the handler.
# This middleware protects /chat, /chat/...POST, and any other JSON
# body endpoint from oversized requests — 1 MB is plenty for a chat
# message plus UI metadata, and blocks a DoS vector where a malicious
# client sends a multi-GB JSON to stall the parser.
#
# Tunable via CHAT_MAX_REQUEST_BYTES (default 1 MB). Overridden for
# /upload and /chat/roster-upload since those legitimately ship
# megabytes of document content.

_DEFAULT_MAX_REQUEST_BYTES = 1 * 1024 * 1024  # 1 MB
_LARGE_BODY_PREFIXES = ("/upload", "/chat/roster-upload")


def _max_request_bytes() -> int:
    raw = (os.environ.get("CHAT_MAX_REQUEST_BYTES") or "").strip()
    if not raw:
        return _DEFAULT_MAX_REQUEST_BYTES
    try:
        n = int(raw)
        # Clamp to [64 KB, 128 MB] — prevents accidental footguns where
        # a 0 or negative disables the cap, and caps the max at
        # something the upload handler will still honor.
        return max(64 * 1024, min(128 * 1024 * 1024, n))
    except ValueError:
        return _DEFAULT_MAX_REQUEST_BYTES


@app.middleware("http")
async def _enforce_request_body_cap(request, call_next):
    """Reject requests whose Content-Length exceeds the configured cap.

    Checks the header only — doesn't drain the body — so oversized
    requests are rejected before the parser allocates memory. Clients
    that omit Content-Length (chunked uploads) are passed through;
    the /upload handler enforces its own cap via chunked reads.
    """
    # Skip cap for endpoints that legitimately receive large payloads.
    path = request.url.path or ""
    if any(path.startswith(p) for p in _LARGE_BODY_PREFIXES):
        return await call_next(request)

    cl = request.headers.get("content-length")
    if cl:
        try:
            n = int(cl)
        except ValueError:
            n = -1
        if n > _max_request_bytes():
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=413,
                content={
                    "error": "request_too_large",
                    "max_bytes": _max_request_bytes(),
                    "received_bytes": n,
                },
            )
    return await call_next(request)

# Start worker in background only for in-memory queue (single process). For Redis, run worker separately.
_worker_started = False


@app.on_event("startup")
def maybe_start_worker():
    global _worker_started
    if _worker_started:
        return

    # Phase 2c gate: fail fast in CHAT_ENV=staging|prod when critical env
    # vars are missing or placeholder. No-op in CHAT_ENV=dev (our laptop
    # default). Raises StartupAssertionError — FastAPI propagates and
    # boot fails with a clear message, which is much safer than
    # silently sending prod traffic to a dev-sandbox GCP project.
    from app.config import assert_hosted_config
    assert_hosted_config()

    cfg = get_config()
    if cfg.queue_type == "memory":
        start_worker_background()
        _worker_started = True
        logger.info("Started in-process worker (memory queue)")
    else:
        logger.info("Queue type=%s: run worker separately with: python -m app.worker", cfg.queue_type)

    # Warn when DB not configured: chat history, jurisdiction state, and retrieval persistence will not work.
    # (In hosted envs this fails earlier via assert_hosted_config; the
    # warning below is the dev-env soft reminder.)
    try:
        from app.chat_config import get_chat_config
        db_url = (get_chat_config().rag.database_url or "").strip()
        if not db_url:
            logger.warning(
                "CHAT_RAG_DATABASE_URL not set: chat turns, recent queries, jurisdiction state, "
                "and retrieval persistence will NOT be saved. Set it in mobius-chat/.env"
            )
    except Exception:
        pass

    # Phase 2c + MCPSkillAdapter: auto-register remote MCP tools as
    # skill registry entries so the planner sees them alongside the
    # five built-in skills. Best-effort — if the MCP server is down
    # we log and keep booting; chat still works with just builtins.
    #
    # 2026-04-19: default flipped OFF→ON after the critic + guidance
    # mode arc landed. The adapter has full test coverage (cold
    # integration tests + golden-case behavior + collision tests) and
    # graceful degradation at every failure mode (MCP down → []; bad
    # descriptors → skipped; name collisions with builtins → skipped).
    # Worst-case boot time impact: one list_tools round-trip to the
    # MCP server (skipped silently on failure).
    #
    # Rollback: set MOBIUS_MCP_AUTOREGISTER=0 (or "false"/"no"/"off")
    # in .env and restart. No code change needed.
    _mcp_autoreg_raw = (os.environ.get("MOBIUS_MCP_AUTOREGISTER") or "").strip().lower()
    if _mcp_autoreg_raw in ("0", "false", "no", "off"):
        _mcp_autoreg_enabled = False
    else:
        # Default ON when the env var is unset, empty, or any truthy
        # value. The permissive parsing mirrors the critic flag so
        # operators only need to remember "set to 0 to disable."
        _mcp_autoreg_enabled = True

    if _mcp_autoreg_enabled:
        try:
            from app.skills.mcp_adapter import register_mcp_skills
            names = register_mcp_skills()
            if names:
                logger.info("MCP auto-register: %d skill(s): %s", len(names), ", ".join(names))
            else:
                logger.info(
                    "MCP auto-register: no tools discovered "
                    "(MCP server down or returned empty tool list). "
                    "Chat continues with builtin skills only."
                )
        except Exception as e:
            logger.warning("MCP auto-register failed: %s — continuing with builtins", e, exc_info=True)


# Phase 2b.2: ChatRequest / ChatResponse + POST /chat + GET /chat/response
# + GET /chat/stream + GET /chat/plan moved to app.api.chat. Router
# included below with the other app.include_router calls.
from app.api.chat import ChatRequest, ChatResponse  # noqa: F401 — re-exported for back-compat imports


class OrgNameCandidatesRequest(BaseModel):
    """Proxy to provider-roster org search with practice address + taxonomy for billing-NPI pickers."""

    name: str = ""
    state: str = "FL"
    limit: int = 12
    search_mode: Literal["copilot", "agentic"] | None = None


@app.post("/chat/org-name-candidates")
def post_chat_org_name_candidates(body: OrgNameCandidatesRequest) -> dict[str, Any]:
    """Return NPPES/PML org matches with NPI, practice address, and primary taxonomy code."""
    base = (os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL") or "").rstrip("/").split("/report")[0]
    if not base:
        raise HTTPException(
            status_code=503,
            detail="Org search not configured. Set CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL.",
        )
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    import httpx

    url = f"{base}/search/org-names"
    try:
        with httpx.Client(timeout=45.0) as client:
            req_body: dict[str, Any] = {
                "name": name,
                "state": body.state,
                "limit": min(max(body.limit, 1), 25),
                "include_pml": True,
                "entity_type_filter": "2",
                "include_practice_address": True,
            }
            if body.search_mode is not None:
                req_body["search_mode"] = body.search_mode
            resp = client.post(url, json=req_body)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=e.response.text[:500] or str(e)) from e
    except Exception as e:
        logger.warning("org-name-candidates failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e)) from e


# Phase 2b.2: POST /chat moved to app.api.chat.


def _handle_instant_rag_upload(
    content: bytes, filename: str, org_name: str,
    thread_id: str | None, file_purpose: str,
) -> dict[str, Any]:
    """Route document uploads to the instant-rag skill for immediate RAG availability."""
    import json as json_mod
    import uuid as _uuid_mod
    import io
    import urllib.error
    import urllib.request
    import threading as _threading
    from datetime import datetime, timezone

    # Resolution order (fixes a 2026-04-23 prod failure where users hit
    # "Connection refused" on uploads):
    #   1. CHAT_SKILLS_INSTANT_RAG_URL  (the CHAT_SKILLS_* convention all
    #      other microservice URLs use; what deploy/dev.env + deploy.sh
    #      actually set)
    #   2. INSTANT_RAG_URL              (legacy name, kept for back-compat
    #      with any script/test that still exports it)
    #   3. http://localhost:8040        (local-dev fallback only; on Cloud
    #      Run nothing listens on that port so uploads would 503)
    instant_rag_url = (
        os.environ.get("CHAT_SKILLS_INSTANT_RAG_URL")
        or os.environ.get("INSTANT_RAG_URL")
        or "http://localhost:8040"
    ).rstrip("/")

    # Extract text from the file based on type
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    text = ""

    if ext == "pdf":
        try:
            import pymupdf
            doc = pymupdf.open(stream=content, filetype="pdf")
            text = "\n\n".join(page.get_text("text") for page in doc if page.get_text("text").strip())
            doc.close()
        except ImportError:
            try:
                import fitz
                doc = fitz.open(stream=content, filetype="pdf")
                text = "\n\n".join(page.get_text("text") for page in doc if page.get_text("text").strip())
                doc.close()
            except ImportError:
                raise HTTPException(status_code=500, detail="PDF extraction requires pymupdf: pip install pymupdf")
    elif ext in ("html", "htm"):
        raw = content.decode("utf-8", errors="replace")
        try:
            from bs4 import BeautifulSoup
            text = BeautifulSoup(raw, "html.parser").get_text(separator="\n\n", strip=True)
        except ImportError:
            import re
            text = re.sub(r"<[^>]+>", " ", raw)
            text = re.sub(r"\s+", " ", text).strip()
    elif ext == "docx":
        try:
            from docx import Document
            doc = Document(io.BytesIO(content))
            text = "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except ImportError:
            raise HTTPException(status_code=500, detail="DOCX extraction requires python-docx: pip install python-docx")
    else:
        # Treat as plain text (txt, csv, md, etc.)
        text = content.decode("utf-8", errors="replace").strip()

    if not text.strip():
        raise HTTPException(status_code=422, detail="No text content could be extracted from the file")

    # Call instant-rag skill /ingest/from-text
    payload = json_mod.dumps({
        "text": text,
        "content_type": "text/html" if ext in ("html", "htm") else "text/plain",
        "display_name": filename,
        "payer": org_name if org_name and org_name != "instant-rag" else "",
        "agent_scope_tags": ["chat"],
    }).encode()

    try:
        req = urllib.request.Request(
            f"{instant_rag_url}/ingest/from-text",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            rag_result = json_mod.loads(resp.read())
    except urllib.error.HTTPError as e:
        # Surface upstream skill errors with the actual status code so
        # the user gets actionable info — 413 (too large), 422 (bad
        # request body), 500 (skill crashed) all look very different
        # from the user's perspective. 2026-04-17: a large provider
        # manual triggered the skill's 50-page cap and the alert just
        # said "HTTP Error 413" with no next step.
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            body = ""
        logger.warning("Instant-RAG ingest failed: %s — %s", e, body)
        if e.code == 413:
            raise HTTPException(
                status_code=413,
                detail=(
                    "Document too large for instant upload. "
                    "The skill's page cap is controlled by INSTANT_RAG_MAX_PAGES "
                    "(default 300 pages of ~4KB each). For larger docs, either "
                    "raise the env var on the instant-rag skill or use the batch "
                    f"ingest pipeline. Skill said: {body or str(e)}"
                ),
            )
        if e.code == 422:
            raise HTTPException(
                status_code=422,
                detail=f"Instant-RAG rejected the upload: {body or str(e)}",
            )
        raise HTTPException(
            status_code=502,
            detail=f"Instant-RAG ingest failed ({e.code}): {body or str(e)[:200]}",
        )
    except Exception as e:
        logger.warning("Instant-RAG ingest failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Instant-RAG ingest failed: {str(e)[:200]}")

    # Save to thread state (same pattern as roster)
    tid = (thread_id or "").strip() or str(_uuid_mod.uuid4())
    upload_id = rag_result.get("envelope_id") or str(_uuid_mod.uuid4())
    record: dict[str, Any] = {
        "upload_id": upload_id,
        "org_id": "",
        "org_name": org_name,
        "purpose": file_purpose,
        "filename": filename,
        "row_count": rag_result.get("chunks_count", 0),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "envelope_id": rag_result.get("envelope_id"),
        "document_id": rag_result.get("document_id"),
    }

    # 2026-04-17 fix: persist SYNCHRONOUSLY before returning. Previously this
    # ran in a daemon thread, which raced the frontend's sendMessage() call
    # — by the time the ReAct loop read thread state for the next turn,
    # the uploaded_files[] entry might not have been written yet. The
    # upload path is already synchronous (blocks on urlopen for the skill
    # call), so adding a sub-second PG write to the tail is negligible
    # and removes a whole class of "invisible upload" bugs.
    # Resolve the real thread_id once up front so both writes use the
    # same value. ensure_thread either creates the row or returns the
    # existing thread_id; either way it's the canonical id we persist
    # against. Fall through to the raw tid if ensure_thread fails.
    real_tid = tid
    try:
        real_tid = ensure_thread(tid) or tid
    except Exception as _e:
        logger.warning("ensure_thread failed for tid=%s: %s", tid, _e)

    try:
        append_uploaded_file_record(real_tid, record)
    except Exception as _e:
        # Keep the upload itself successful even if thread-state persistence
        # fails — the chunks are already in Chroma + PG, so search works.
        # Log loud (not debug) so this shows up in ops dashboards.
        logger.warning("Thread state save (instant-rag) failed for thread=%s: %s", real_tid, _e)

    # Phase B.1c — dual-write to the durable catalog table. This is the
    # source of truth for cross-thread queries ("all uploads for this
    # user", cleanup cron's "expired rows"). The JSONB blob above stays
    # as the fast-path cache for the ReAct loop's _resolve_upload_document_id.
    # Failure here is non-fatal for the same reason as the JSONB save:
    # chunks are already durable in Chroma + PG; losing a catalog row
    # just means cross-thread queries miss this upload until we backfill.
    try:
        from app.storage.instant_rag_catalog import record_upload as _catalog_record
        _catalog_record(
            document_id=str(rag_result.get("document_id") or ""),
            envelope_id=str(rag_result.get("envelope_id") or upload_id),
            upload_id=upload_id,
            thread_id=real_tid,
            filename=filename,
            user_id=None,  # Phase 1h: user_id is None in auth=off (dev); wire when auth=required
            content_type=None,
            byte_size=None,  # chat side doesn't have the raw bytes by this point; skill saw them
            chunks_count=int(rag_result.get("chunks_count", 0) or 0),
        )
    except Exception as _e:
        logger.warning("[catalog] dual-write failed for thread=%s: %s", real_tid, _e)

    return {
        "upload_id": upload_id,
        "org_id": "",
        "org_name": org_name,
        "row_count": rag_result.get("chunks_count", 0),
        "thread_id": tid,
        "file_purpose": file_purpose,
        "filename": filename,
        "envelope_id": rag_result.get("envelope_id"),
        "document_id": rag_result.get("document_id"),
        "verification_tier": rag_result.get("verification_tier", "instant"),
        "status": rag_result.get("status", "live"),
        "chunks_count": rag_result.get("chunks_count", 0),
        "message": rag_result.get("message", ""),
    }


@app.post("/chat/roster-upload")
def post_chat_roster_upload(
    file: UploadFile = File(...),
    org_name: str = Form(...),
    thread_id: str | None = Form(None),
    run_id: str | None = Form(None),
    file_purpose: str | None = Form("roster_reconciliation"),
    _user_id: str | None = Depends(require_user),
) -> dict[str, Any]:
    """
    Upload a file for credentialing/reconciliation or instant RAG ingestion.
    Proxies to provider-roster-credentialing (roster) or instant-rag skill (documents).
    file_purpose: roster_reconciliation | instant_rag | other.
    Returns { upload_id, org_id, org_name, row_count, thread_id }.
    """
    # Size cap (2026-04-20 hardening). Enforce before reading the whole
    # body into memory so a malicious client can't exhaust disk / RSS.
    # Chunked reads until cap is exceeded OR the stream is exhausted.
    _MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB — covers any realistic
                                            # roster CSV or policy PDF
    buf = bytearray()
    while True:
        chunk = file.file.read(1024 * 1024)
        if not chunk:
            break
        if len(buf) + len(chunk) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Upload exceeds {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB "
                    "limit. Split the file or contact support if you need "
                    "a higher limit for this purpose."
                ),
            )
        buf.extend(chunk)
    content = bytes(buf)

    filename = file.filename or "upload"
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    purpose = (file_purpose or "roster_reconciliation").strip()

    # Block dangerous file types
    _BLOCKED_EXTS = {"exe", "bat", "sh", "dll", "so", "dylib", "com", "msi", "scr"}
    if ext in _BLOCKED_EXTS:
        raise HTTPException(status_code=400, detail=f"File type '.{ext}' is not allowed")

    org_name = (org_name or "").strip()

    # ── Instant RAG path ─────────────────────────────────────────────────
    if purpose == "instant_rag":
        return _handle_instant_rag_upload(
            content=content, filename=filename, org_name=org_name,
            thread_id=thread_id, file_purpose=purpose,
        )

    # ── Roster reconciliation path ────────────────────────────────────────
    if purpose == "roster_reconciliation":
        from app.api.roster_upload import handle_roster_upload
        return handle_roster_upload(
            content=content,
            filename=filename,
            ext=ext,
            org_name=org_name,
            thread_id=thread_id,
            run_id=run_id,
            file_purpose=purpose,
        )

    raise HTTPException(
        status_code=400,
        detail=(
            f"file_purpose={purpose!r} is not supported. "
            "Accepted values: 'roster_reconciliation', 'instant_rag'."
        ),
    )

@app.get("/chat/thread/{thread_id}/uploads")
def get_thread_uploads(thread_id: str) -> dict[str, Any]:
    """
    Document upload skill — list files attached to this chat thread (newest first).
    Used by the UI, MCP, and integrations; supports multiple uploads over time per thread.

    Post-2026-04-18 disconnect: ``uploaded_files`` is the canonical list.
    ``roster_reconciliation_files`` + ``latest_roster_reconciliation`` +
    ``roster_freshness`` fields are retained in the response shape for FE
    back-compat, but they'll always be empty / none now that the
    roster_reconciliation purpose is retired. The
    ``CHAT_ROSTER_FRESH_DAYS`` env (default 14) controls freshness
    comparison for any roster_reconciliation entries that might still
    exist in old thread state.
    """
    tid = (thread_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="thread_id is required")
    thresh = _roster_freshness_days_threshold()
    raw = get_state(tid)
    if not raw:
        return {
            "thread_id": tid,
            "uploaded_files": [],
            "roster_reconciliation_files": [],
            "latest_roster_reconciliation": None,
            "roster_freshness": "none",
            "roster_age_days": None,
            "roster_fresh_days_threshold": thresh,
        }
    active = raw.get("active") or {}
    uploaded = active.get("uploaded_files") or []
    roster_reconciliation_files = [
        {
            "upload_id": (u.get("upload_id") or "").strip(),
            "org_id": (u.get("org_id") or "").strip(),
            "org_name": (u.get("org_name") or "").strip(),
            "filename": (u.get("filename") or "").strip(),
            "purpose": (u.get("purpose") or "").strip(),
            "row_count": u.get("row_count"),
            "uploaded_at": u.get("uploaded_at"),
        }
        for u in uploaded
        if isinstance(u, dict) and (u.get("purpose") or "").strip() == "roster_reconciliation"
    ]
    latest_roster: dict[str, Any] | None = None
    if roster_reconciliation_files:
        head = roster_reconciliation_files[0]
        if (head.get("upload_id") or "").strip() and (head.get("org_id") or "").strip():
            latest_roster = dict(head)
    freshness, age_days = _compute_roster_freshness(latest_roster)
    return {
        "thread_id": tid,
        "uploaded_files": uploaded,
        "roster_reconciliation_files": roster_reconciliation_files,
        "latest_roster_reconciliation": latest_roster,
        "roster_freshness": freshness,
        "roster_age_days": round(age_days, 2) if age_days is not None else None,
        "roster_fresh_days_threshold": thresh,
    }


# Phase 1c: credentialing-runs + NPI lookup endpoints extracted to
# app.api.credentialing (router mounted near the top of this file).


# Phase 2b.2: _enrich_completed_response_from_db + GET /chat/response +
# GET /chat/stream + GET /chat/plan moved to app.api.chat.


@app.get("/chat/config")
def get_chat_config():
    """Chat-specific config and prompts (LLM, parser, prompts) for the hamburger menu."""
    return chat_config_for_api()


@app.get("/chat/skills/urls")
def get_skills_urls():
    """Return base URLs for each skill UI — used by the frontend Skills modal."""
    roster_base = (
        (os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL") or "").rstrip("/").split("/report")[0]
    )
    return {
        "roster_ui": f"{roster_base}/roster-ui/upload.html" if roster_base else None,
        "roster_base": roster_base or None,
    }


# Phase 1d: /chat/roster-reconcile/*, /chat/roster-truth/*, and
# /chat/roster-org/* endpoints extracted to app.api.roster. Router
# mounted near the top of this file.

@app.get("/chat/llm-router-report")
def get_llm_router_report(window_days: int = 30):
    """Per-stage model stats, adjudicated quality, composite ranking — for hamburger menu report UI."""
    wd = max(1, min(int(window_days or 30), 365))
    return fetch_llm_router_report(window_days=wd)


@app.get("/chat/config/history")
def get_chat_config_history(limit: int = 20):
    """Config version history for the hamburger menu (PG-backed llm_config_versions)."""
    from app.prompts_llm_history import list_entries
    return list_entries(limit=max(1, min(limit, 100)))


@app.get("/chat/config/history/{config_sha}")
def get_chat_config_by_sha(config_sha: str):
    """Full config snapshot for a given config_sha (for hamburger history view)."""
    from app.prompts_llm_history import get_by_sha
    config = get_by_sha(config_sha)
    if config is None:
        raise HTTPException(status_code=404, detail="Config version not found")
    return config


# Phase 1a: /chat/history/* extracted to app.api.history.
# Phase 1b: feedback + QC endpoints extracted to app.api.feedback.
# Phase 1c/1d/3a-3c: the credentialing and roster HTTP routers previously
# lived in app.api.credentialing + app.api.roster. Per the user's
# architectural direction ("credentialing is a skill, not a chat
# interface"), the entire chat-side credentialing + roster HTTP surface
# (41 endpoints) was removed in Phase 3c. Clients that previously called
# /chat/credentialing-runs/*, /chat/roster-reconcile/*, /chat/roster-truth/*,
# /chat/roster-org/*, or /chat/npi-lookup/* must now call the credentialing
# skill server directly (CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL).
# Chat's internal ReAct tools continue to use the services in
# app.services.credentialing_* and app.storage.credentialing_* for
# server-side orchestration — only the public HTTP surface was removed.
from app.api.admin import router as _admin_router
from app.api.chat import router as _chat_router
from app.api.credentialing import router as _credentialing_router
from app.api.doc_reader import router as _doc_reader_router
from app.api.feedback import router as _feedback_router
from app.api.history import router as _history_router
from app.api.tasks import router as _tasks_router
from app.api.uploads import router as _uploads_router
app.include_router(_chat_router)  # Phase 2b.2 — core chat lifecycle extracted from main.py
app.include_router(_credentialing_router)  # credentialing-runs + NPI lookup (restored for pipeline UI)
app.include_router(_history_router)
app.include_router(_feedback_router)
app.include_router(_tasks_router)
app.include_router(_uploads_router)  # Phase B.1c — cross-thread uploads catalog
app.include_router(_doc_reader_router)  # Phase 2b.1 — doc-reader proxy extracted from main.py
app.include_router(_admin_router)  # Dev-token minter + future ops-only endpoints

# Provider skill runs as its own server (provider-roster-credentialing, :8011).
# Chat calls it via CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL.


# Phase 1b: feedback / QC endpoints moved to app.api.feedback.
# Kept inline code here for 100+ lines; now just a router mount at the top.


# --- Internal: credentialing / other skills use chat's ModelRouter + llm_calls (no shared Python package name) ---
_SKILL_LLM_ALLOWED_STAGES = frozenset({
    "credentialing_draft",
    "credentialing_validate",
    "credentialing_critique",
    "credentialing_compose",
    "credentialing_report_qa",
    # Org intelligence stages
    "org_intel_synthesis",   # profile synthesis → structured JSON profile
    "org_intel_report",      # long-form report generation from synthesized profile
    # mobius-rag ingestion stages (2026-04-21): rag's extraction +
    # critique calls route through here so Thompson-bandit routing +
    # llm_calls analytics cover the full stack, not just chat-side.
    "rag_extraction",        # stream_extract_facts → structured fact JSON
    "rag_critique",          # critique_extraction → quality score + feedback
    "rag_lexicon_triage",    # candidate → {new_tag, alias, reject} verdict
    # mobius-qa/lexicon-maintenance stages (2026-04-23): curator UI's
    # LLM calls route through here. Same reason as rag — unified
    # bandit + telemetry. Three stages cover the four endpoint needs:
    #   * lexicon_triage : bulk candidate triage + health analysis
    #   * lexicon_suggest: single-phrase tag placement + candidate revise
    #   * lexicon_from_doc: "suggest tags from this document" flow
    "lexicon_triage",
    "lexicon_suggest",
    "lexicon_from_doc",
})


class SkillLLMRequest(BaseModel):
    """Body for POST /internal/skill-llm (credentialing report pipeline, etc.)."""

    system: str = ""
    user: str = ""
    stage: str = "credentialing_draft"
    max_tokens: int = 4096
    correlation_id: str | None = None
    thread_id: str | None = None
    mode: str | None = None


@app.post("/internal/skill-llm")
async def internal_skill_llm(
    body: SkillLLMRequest,
    x_mobius_skill_llm_key: str | None = Header(None, alias="X-Mobius-Skill-LLM-Key"),
):
    """
    Run one LLM completion through mobius-chat's dynamic model router and analytics.
    Secured with MOBIUS_SKILL_LLM_INTERNAL_KEY (header X-Mobius-Skill-LLM-Key).
    Used by provider-roster-credentialing when CREDENTIALING_LLM_ROUTER_URL points here.
    """
    expected = (os.environ.get("MOBIUS_SKILL_LLM_INTERNAL_KEY") or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="MOBIUS_SKILL_LLM_INTERNAL_KEY is not set on chat; skill LLM proxy disabled.",
        )
    if (x_mobius_skill_llm_key or "").strip() != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    stage = (body.stage or "").strip()
    if stage not in _SKILL_LLM_ALLOWED_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid stage; allowed: {sorted(_SKILL_LLM_ALLOWED_STAGES)}",
        )

    prompt = f"{body.system}\n\n{body.user}"
    from app.services import llm_manager

    try:
        text, usage = await llm_manager.generate(
            prompt,
            stage=stage,
            max_tokens=int(body.max_tokens),
            correlation_id=(body.correlation_id or "").strip() or None,
            thread_id=(body.thread_id or "").strip() or None,
            mode=(body.mode or "").strip() or None,
        )
    except asyncio.TimeoutError as e:
        raise HTTPException(
            status_code=504,
            detail={
                "message": "Skill LLM timed out (Vertex generate exceeded wait_for). "
                "Raise CREDENTIALING_LLM_TIMEOUT_SECONDS on mobius-chat if credentialing compose is slow.",
                "stage": stage,
            },
        ) from e
    return {"text": text, "usage": usage}


# ═══════════════════════════════════════════════════════════════════════════════
# Financial Strategy proxies REMOVED 2026-04-18.
# Nine /chat/financial-strategy/*, /chat/org-story, /chat/org-story-v2,
# /chat/market-map, /chat/industry-report-data routes plus their shared
# _fs_proxy helper were cut as part of the credentialing/roster/strategy
# disconnect. These will rebuild cleanly as a separate skill integration
# when that work lands; chat should not proxy into the credentialing
# skill server.
# ═══════════════════════════════════════════════════════════════════════════════


# Phase 2b: /chat/doc-reader/* moved to app.api.doc_reader.
# Router included below near the other app.include_router calls so
# external URLs are unchanged.


# ═══════════════════════════════════════════════════════════════════════════════
# Task Manager skill proxy — /chat/tasks/* → mobius-skills/task-manager
# ═══════════════════════════════════════════════════════════════════════════════

# Phase 1e: _task_manager_base consolidated into app.api._common.
from app.api._common import task_manager_base_url as _task_manager_base


# 2026-04-18 disconnect — /chat/runs aggregator + _STEP_LABELS /
# _STEP_TOTAL constants removed. The endpoint joined
# list_credentialing_runs() with task-manager per-run counts for the
# credentialing home page's run list. With the UI, page routes, and
# services all gone, the aggregator has no caller.

# Phase 1f.1: /chat/tasks/* moved to app.api.tasks. Router included below.


@app.get("/health")
def health():
    """Liveness probe — process is up, event loop responsive.

    Cheap enough for Cloud Run / k8s liveness at 1Hz. Does NOT touch
    the DB / queue / skills-MCP — use ``/ready`` for that. The
    distinction matters: a failing liveness check triggers container
    kill, so a transient DB blip shouldn't cycle pods.
    """
    return {"status": "ok"}


# ── Readiness probe ────────────────────────────────────────────────
#
# Introduced 2026-04-20 for Cloud Run deploys. Unlike ``/health``,
# ``/ready`` actively pings downstream dependencies. Cloud Run uses
# this to decide whether to route traffic to a new revision. Returns
# 503 when any dependency is down so traffic drains cleanly.
#
# Checks (all short-timeout, failure-tolerant):
#   * chat DB round-trip (SELECT 1)
#   * queue configured + reachable (already asserted at startup but
#     transient Redis outages need to show up here)
#   * skills-mcp ping (best-effort — failures degrade to warn, not 503,
#     since chat can fall back to built-in tool implementations)


@app.get("/ready")
def ready():
    """Readiness probe. 200 ok when chat can serve traffic; 503 otherwise.

    Fast (<500ms typical). Each check is wrapped so one failure
    doesn't mask the real cause in the response body.
    """
    checks: dict[str, dict[str, Any]] = {}
    all_ok = True

    # 1. Chat DB — one of the cheapest possible queries.
    #    Use db_query (SELECT 1 is a read; db_execute works but db_query
    #    is the right shape). Surface both the error code AND the
    #    underlying message so operators can diagnose without grepping
    #    Cloud Logging.
    try:
        from app.db_client import db_query, err_code, err_message
        result = db_query("SELECT 1 AS ok", "chat", params={})
        if err_code(result) is not None:
            checks["db"] = {
                "status": "fail",
                "error": str(err_code(result)),
                "message": err_message(result)[:300],
            }
            all_ok = False
        else:
            checks["db"] = {"status": "ok"}
    except Exception as e:
        checks["db"] = {"status": "fail", "error": str(e)[:300]}
        all_ok = False

    # 2. Queue — for Redis queues, verify the client can ping. In-memory
    #    queue always passes (it's in-process).
    try:
        q = get_queue()
        # Best-effort: queue impls expose ``ping()`` when they can;
        # when they don't, assume in-memory / always-up.
        ping = getattr(q, "ping", None)
        if callable(ping):
            ping()
        checks["queue"] = {"status": "ok", "type": type(q).__name__}
    except Exception as e:
        checks["queue"] = {"status": "fail", "error": str(e)[:200]}
        all_ok = False

    # 3. skills-mcp — degraded (warn) on failure, not 503. Chat can
    #    serve built-in tools without skills-mcp.
    try:
        base = (os.environ.get("CHAT_SKILLS_MCP_URL") or "").strip()
        if base:
            import urllib.request
            req = urllib.request.Request(base.rstrip("/") + "/health")
            with urllib.request.urlopen(req, timeout=2) as _resp:  # noqa: S310
                checks["skills_mcp"] = {"status": "ok"}
        else:
            checks["skills_mcp"] = {"status": "skipped", "reason": "not configured"}
    except Exception as e:
        checks["skills_mcp"] = {"status": "degraded", "error": str(e)[:200]}
        # Intentionally not flipping all_ok — skills-mcp outage is
        # not a ready-to-serve blocker.

    status_code = 200 if all_ok else 503
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=status_code,
        content={"status": "ready" if all_ok else "not_ready", "checks": checks},
    )


# ── Graceful shutdown ──────────────────────────────────────────────
#
# Cloud Run sends SIGTERM + gives the container ~10s to drain before
# SIGKILL. Without a shutdown hook, in-flight chat turns are dropped
# mid-processing — the client sees a hang, the DB sees partial writes.
#
# FastAPI's on_event("shutdown") runs during Uvicorn's lifespan
# shutdown (which in turn triggers on SIGTERM). We use the hook to:
#   * flag a shared event so the worker loop stops consuming new jobs
#   * the worker's current turn keeps running (deadline already caps
#     it at 90s, and we configure Cloud Run's timeout to match)


@app.on_event("shutdown")
def _graceful_shutdown() -> None:
    """SIGTERM drain hook. Signals the worker to stop taking new jobs.

    Chat turns already running continue to completion (bounded by the
    90s worker deadline). No new turns are dequeued.
    """
    try:
        from app.worker.run import request_shutdown
        request_shutdown()
        logger.info("Graceful shutdown signal sent to worker.")
    except Exception as e:
        logger.warning("Worker shutdown signal failed: %s", e)


# Serve chat UI at /
_frontend = Path(__file__).resolve().parent.parent / "frontend"
if _frontend.exists():
    app.mount("/static", NoCacheStaticFiles(directory=_frontend / "static"), name="static")

    @app.get("/")
    def index():
        r = FileResponse(_frontend / "index.html")
        r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return r

    @app.get("/pipeline")
    def pipeline():
        r = FileResponse(_frontend / "static" / "pipeline.html")
        r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return r

