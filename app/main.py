"""FastAPI app: POST /chat (enqueue), GET /chat/response/:id (poll), GET /chat/stream/:id (SSE), health."""
import asyncio
import json
import os
import logging
import uuid
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

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
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
from app.storage import (
    fetch_turn_qc_audit,
    get_most_helpful_documents,
    get_most_helpful_turns,
    get_plan,
    get_recent_turns,
    get_response,
    insert_adjudication_feedback,
    insert_feedback,
    insert_llm_performance_feedback,
    insert_source_feedback,
)
from app.storage.feedback import get_adjudication_feedback, get_llm_performance_feedback
from app.storage.threads import append_uploaded_file_record, ensure_thread, get_state, save_state, save_state_full
from app.storage.progress import (
    get_and_clear_events,
    get_progress,
    get_progress_events_from_db,
    get_progress_from_db,
    publish_quality_audit_event,
)
from app.storage.llm_router_report import fetch_llm_router_report
from app.storage.turns import update_turn_qc_audit
from app.worker import start_worker_background

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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
    # Warn when DB not configured: chat history, jurisdiction state, and retrieval persistence will not work
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


class CredentialingOptions(BaseModel):
    """Structured choices from the credentialing envelope (POST /chat)."""

    org_name: str | None = None
    mode: Literal["autopilot", "copilot"] | None = None
    force_refresh: bool | None = None
    report_kind: Literal["auto", "credentialing", "reconciliation"] | None = None
    """auto: server picks reconciliation if thread has a roster upload, else outside-in credentialing."""
    prefer_outside_in: bool | None = None
    """True: run Medicaid NPI / credentialing pipeline even when a roster exists on the thread."""
    prefer_fresh_report: bool | None = None
    """True: skip same-day cached credentialing report and run full orchestrator (outside-in path)."""


class ChatRequest(BaseModel):
    message: str = ""
    thread_id: str | None = None  # When provided, load state for jurisdiction/context
    credentialing_options: CredentialingOptions | None = None
    """When set (e.g. after envelope confirm), worker merges into run_credentialing_report."""
    use_react: bool | None = None
    """Per-request override for MOBIUS_USE_REACT; when None, worker uses env."""
    chat_mode: Literal["copilot", "agentic"] | None = None
    """Composer UI: agentic enables skill-side web escalation; copilot is registry-first. Persisted per thread."""


class ChatResponse(BaseModel):
    correlation_id: str
    thread_id: str  # Created or reused; client sends on follow-up requests


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


@app.post("/chat", response_model=ChatResponse)
def post_chat(body: ChatRequest):
    """Enqueue a chat request; returns correlation_id and thread_id for polling."""
    correlation_id = str(uuid.uuid4())
    thread_id = ensure_thread((body.thread_id or "").strip() or None)
    payload: dict = {"message": body.message or "", "thread_id": thread_id}
    if body.credentialing_options is not None:
        payload["credentialing_options"] = body.credentialing_options.model_dump(exclude_none=True)
    if body.use_react is not None:
        payload["use_react"] = body.use_react
    if body.chat_mode is not None:
        payload["chat_mode"] = body.chat_mode
    get_queue().publish_request(correlation_id, payload)
    return ChatResponse(correlation_id=correlation_id, thread_id=thread_id)


@app.post("/chat/roster-upload")
def post_chat_roster_upload(
    file: UploadFile = File(...),
    org_name: str = Form(...),
    thread_id: str | None = Form(None),
    file_purpose: str | None = Form("roster_reconciliation"),
) -> dict[str, Any]:
    """
    Upload a roster file (CSV or Excel) for credentialing/reconciliation reports.
    Proxies to provider-roster-credentialing, processes, saves upload_id and org_id to thread state.
    file_purpose: roster_reconciliation | other (stored for future RAG / workflows).
    Returns { upload_id, org_id, org_name, row_count, thread_id }.
    """
    base = (os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL") or "").rstrip("/").split("/report")[0]
    if not base:
        raise HTTPException(
            status_code=503,
            detail="Roster upload not configured. Set CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL.",
        )
    org_name = (org_name or "").strip()
    if not org_name:
        raise HTTPException(status_code=400, detail="org_name is required")

    content = file.file.read()
    filename = file.filename or "roster.csv"
    ext = filename.lower().split(".")[-1]
    if ext not in ("csv", "xlsx", "xls"):
        raise HTTPException(status_code=400, detail="File must be CSV or Excel (.csv, .xlsx, .xls)")

    # 1. Resolve org_id via search
    import urllib.request
    import urllib.parse
    import json as json_mod
    search_url = f"{base}/search/org-names"
    req = urllib.request.Request(
        search_url,
        data=json_mod.dumps({"name": org_name, "include_practice_address": True}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json_mod.loads(resp.read().decode())
    except Exception as e:
        logger.warning("Org search failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Could not resolve org: {e}") from e
    results = data.get("results") or []
    if not results:
        raise HTTPException(status_code=404, detail=f"No org match for {org_name!r}")
    top_result = results[0] if results else {}
    org_id = (
        top_result.get("org_id") or top_result.get("npi") or top_result.get("billing_npi") or ""
    ).strip().zfill(10)
    matched_org_name = (top_result.get("name") or "").strip()
    _pra = (top_result.get("practice_address") or "").strip()
    matched_practice_address = _pra or None

    # 2. Upload roster to provider-roster-credentialing
    ct = "text/csv" if ext == "csv" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    upload_url = f"{base}/roster-uploads"
    import io
    import httpx
    files = {"file": (filename, io.BytesIO(content), ct)}
    data = {"org_name": org_name, "org_id": org_id}
    try:
        with httpx.Client(timeout=60.0) as client:
            upload_resp = client.post(upload_url, files=files, data=data)
    except Exception as e:
        logger.warning("Roster upload request failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Roster upload failed: {e}") from e
    if upload_resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Roster upload rejected: {upload_resp.text[:500]}")
    upload_data = upload_resp.json()
    upload_id = upload_data.get("upload_id") or ""
    if not upload_id:
        raise HTTPException(status_code=502, detail="No upload_id from roster upload")

    # 3. Process (parse, clean, resolve NPIs)
    process_url = f"{base}/roster-uploads/{upload_id}/process"
    req = urllib.request.Request(
        process_url,
        data=json_mod.dumps({"resolve_npi": True, "state": "FL"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            proc_data = json_mod.loads(resp.read().decode())
    except Exception as e:
        logger.warning("Roster process failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Roster processing failed: {e}") from e
    rc_clean = int(proc_data.get("row_count_cleansed") or 0)
    rc_res = int(proc_data.get("row_count_resolved") or 0)
    row_count = rc_res or rc_clean or 0
    _rsum = proc_data.get("resolution_summary")
    resolution_summary = _rsum if isinstance(_rsum, dict) else None

    # 4. Save to thread state (upload list + reconciliation pointers for roster)
    tid = ensure_thread((thread_id or "").strip() or None)
    purpose = (file_purpose or "roster_reconciliation").strip() or "roster_reconciliation"
    if purpose not in ("roster_reconciliation", "other"):
        purpose = "roster_reconciliation"

    acknowledgment: dict[str, Any] | None = None
    if purpose == "roster_reconciliation":
        acknowledgment = _build_roster_upload_acknowledgment(
            filename=filename,
            org_name_entered=org_name,
            billing_npi=org_id,
            matched_org_name=matched_org_name,
            matched_practice_address=matched_practice_address,
            row_count_cleansed=rc_clean,
            row_count_resolved=rc_res,
            process_status=str(proc_data.get("status") or ""),
            resolution_summary=resolution_summary,
        )
    from datetime import datetime, timezone

    record: dict[str, Any] = {
        "upload_id": upload_id,
        "org_id": org_id,
        "org_name": org_name,
        "purpose": purpose,
        "filename": filename,
        "row_count": int(row_count) if row_count is not None else 0,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    persisted = append_uploaded_file_record(tid, record)
    if not persisted:
        raise HTTPException(
            status_code=503,
            detail=(
                "Roster was accepted by the processing service but could not be linked to this chat "
                "(chat state database not configured). Set CHAT_RAG_DATABASE_URL, then upload again."
            ),
        )

    out: dict[str, Any] = {
        "upload_id": upload_id,
        "org_id": org_id,
        "org_name": org_name,
        "row_count": row_count,
        "thread_id": tid,
        "file_purpose": purpose,
        "default_billing_npi": org_id,
        "filename": filename,
        "matched_organization_name": matched_org_name,
        "matched_practice_address": matched_practice_address,
        "row_count_cleansed": rc_clean,
        "row_count_resolved": rc_res,
        "process_status": proc_data.get("status"),
        "resolution_summary": resolution_summary,
        "acknowledgment": acknowledgment,
    }
    return out


@app.get("/chat/thread/{thread_id}/uploads")
def get_thread_uploads(thread_id: str) -> dict[str, Any]:
    """
    Document upload skill — list files attached to this chat thread (newest first).
    Used by the UI, MCP, and integrations; supports multiple uploads over time per thread.
    """
    tid = (thread_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="thread_id is required")
    raw = get_state(tid)
    if not raw:
        return {
            "thread_id": tid,
            "uploaded_files": [],
            "roster_reconciliation_files": [],
            "reconciliation_upload_id": None,
            "reconciliation_org_id": None,
            "reconciliation_org_name": None,
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
        }
        for u in uploaded
        if isinstance(u, dict) and (u.get("purpose") or "").strip() == "roster_reconciliation"
    ]
    return {
        "thread_id": tid,
        "uploaded_files": uploaded,
        "roster_reconciliation_files": roster_reconciliation_files,
        "reconciliation_upload_id": (active.get("reconciliation_upload_id") or "").strip() or None,
        "reconciliation_org_id": (active.get("reconciliation_org_id") or "").strip() or None,
        "reconciliation_org_name": (active.get("reconciliation_org_name") or "").strip() or None,
    }


class CredentialingRunCreateBody(BaseModel):
    """Start credentialing report run: autopilot (full pipeline) or copilot (step-by-step validation)."""

    org_name: str = ""
    mode: Literal["autopilot", "copilot"] = "copilot"
    thread_id: str | None = None


class CredentialingValidateBody(BaseModel):
    """Commit user-validated output for the pending step; server runs the next step."""

    step_id: str = ""
    validated_output: dict[str, Any] = {}


@app.post("/chat/credentialing-runs")
def post_credentialing_runs(body: CredentialingRunCreateBody) -> dict[str, Any]:
    """
    Create a credentialing pipeline run.
    - autopilot: same as chat tool (full orchestrator), returns when complete.
    - copilot: runs the first step only; use POST .../validate with validated_output, then repeat until phase=complete.
    """
    from app.services.credentialing_run_service import create_credentialing_run

    org = (body.org_name or "").strip()
    if not org:
        raise HTTPException(status_code=400, detail="org_name is required")
    tid = ensure_thread((body.thread_id or "").strip() or None)
    try:
        result = create_credentialing_run(org, body.mode, thread_id=tid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    save_state(tid, {"active": {"credentialing_run_id": result.get("run_id"), "credentialing_run_mode": body.mode}})
    result["thread_id"] = tid
    return result


@app.get("/chat/credentialing-runs/{run_id}")
def get_credentialing_run(run_id: str, full: int = 0) -> dict[str, Any]:
    from app.services.credentialing_run_service import get_credentialing_run

    rec = get_credentialing_run(run_id, include_state=bool(full))
    if not rec:
        raise HTTPException(status_code=404, detail="run not found")
    return rec


@app.post("/chat/credentialing-runs/{run_id}/validate")
def post_credentialing_run_validate(run_id: str, body: CredentialingValidateBody) -> dict[str, Any]:
    from app.services.credentialing_run_service import validate_and_advance_credentialing_run

    sid = (body.step_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="step_id is required")
    try:
        return validate_and_advance_credentialing_run(run_id, sid, body.validated_output or {})
    except KeyError:
        raise HTTPException(status_code=404, detail="run not found") from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


def _enrich_completed_response_from_db(resp: dict) -> dict:
    """Overlay qc_audit + technical_feedback from Postgres so edits and thumbs survive poll/refresh."""
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


@app.get("/chat/response/{correlation_id}")
def get_chat_response(correlation_id: str):
    """Poll for response. Returns completed payload when done; while in progress returns status 'processing' and live thinking_log."""
    q = get_queue()
    resp = q.get_response(correlation_id)
    if resp is None:
        resp = get_response(correlation_id)
    if resp is not None:
        return _enrich_completed_response_from_db(resp)
    cfg = get_config()
    in_progress, thinking_log, message_so_far = get_progress(correlation_id)
    # When worker runs in separate process (Redis), in-memory progress is empty; fetch from DB.
    if not in_progress and cfg.queue_type == "redis":
        thinking_log, message_so_far = get_progress_from_db(correlation_id)
        in_progress = bool(thinking_log or message_so_far)
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
    timeout_s = int(os.environ.get("CHAT_STREAM_TIMEOUT_S", "1800"))  # 30 min default (large Medicaid reports e.g. Aspire 772 providers can take 15+ min)

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


class FeedbackBody(BaseModel):
    rating: str  # "up" | "down"
    comment: str | None = None


class QcAuditBody(BaseModel):
    passed: bool
    reason: str | None = None
    source: str = "eval_adjudicator"
    score: float | None = None  # automated 0–1; defaults from passed if omitted
    sub_scores: dict[str, float] | None = None
    adjudicator_full_response: str | None = None
    adjudicator_model: str | None = None
    adjudicator_llm_call_id: str | None = None


class QcUserScoreBody(BaseModel):
    """Human override for adjudicator score; merged into chat_turns.qc_audit JSON."""

    user_score: float
    user_score_comment: str | None = None


class AdjudicationFeedbackBody(BaseModel):
    rating: str
    comment: str | None = None


class SourceFeedbackBody(BaseModel):
    source_index: int  # 1-based
    rating: str  # "up" | "down"


@app.post("/chat/qc-audit/{correlation_id}")
def post_chat_qc_audit(
    correlation_id: str,
    body: QcAuditBody,
    x_mobius_qc_audit_secret: str | None = Header(None, alias="X-Mobius-QC-Audit-Secret"),
):
    """Merge QC / eval adjudication into the live response, turn row, and progress stream (thinking)."""
    secret = (os.environ.get("MOBIUS_QC_AUDIT_SECRET") or "").strip()
    if secret and (x_mobius_qc_audit_secret or "").strip() != secret:
        raise HTTPException(status_code=403, detail="Invalid or missing QC audit secret")
    from datetime import datetime, timezone

    audited_at = datetime.now(timezone.utc).isoformat()
    src = (body.source or "eval_adjudicator").strip()[:200] or "eval_adjudicator"
    reason_str = (body.reason or "").strip()[:2000]
    auto_score = body.score
    if auto_score is not None:
        auto_score = max(0.0, min(1.0, float(auto_score)))
    else:
        auto_score = 1.0 if body.passed else 0.0
    qc_dict: dict[str, Any] = {
        "passed": body.passed,
        "reason": reason_str,
        "source": src,
        "audited_at": audited_at,
        "automated_score": round(auto_score, 4),
    }
    if body.sub_scores:
        cleaned: dict[str, float] = {}
        for k, v in body.sub_scores.items():
            ks = str(k).strip()[:120]
            if not ks:
                continue
            try:
                fv = float(v)
                cleaned[ks] = round(max(0.0, min(1.0, fv)), 4)
            except (TypeError, ValueError):
                pass
        if cleaned:
            qc_dict["sub_scores"] = cleaned
    if body.adjudicator_full_response and str(body.adjudicator_full_response).strip():
        qc_dict["adjudicator_full_response"] = str(body.adjudicator_full_response).strip()[:8000]
    if body.adjudicator_model and str(body.adjudicator_model).strip():
        qc_dict["adjudicator_model"] = str(body.adjudicator_model).strip()[:200]
    if body.adjudicator_llm_call_id and str(body.adjudicator_llm_call_id).strip():
        qc_dict["adjudicator_llm_call_id"] = str(body.adjudicator_llm_call_id).strip()[:120]
    sym = "✓" if body.passed else "⚠"
    label = "passed" if body.passed else "flagged"
    reason_bit = f" — {reason_str[:180]}" if reason_str else ""
    line = f"{sym} Quality audit {label}{reason_bit}"
    update_turn_qc_audit(correlation_id, qc_dict)
    full_qc = fetch_turn_qc_audit(correlation_id) or qc_dict
    publish_quality_audit_event(
        correlation_id,
        {"passed": body.passed, "source": src},
        line,
    )
    get_queue().patch_response_merge(
        correlation_id,
        {"qc_audit": full_qc, "thinking_log": [line]},
    )
    return {"status": "ok", "qc_audit": full_qc}


@app.post("/chat/qc-user-score/{correlation_id}")
def post_qc_user_score(correlation_id: str, body: QcUserScoreBody):
    """Persist edited quality score (0–1) + optional note into qc_audit; patches live response for poll/SSE."""
    if body.user_score < 0.0 or body.user_score > 1.0:
        raise HTTPException(status_code=400, detail="user_score must be between 0 and 1")
    from datetime import datetime, timezone

    merge = {
        "user_score": round(float(body.user_score), 4),
        "user_score_comment": (body.user_score_comment or "").strip()[:2000] or None,
        "user_score_updated_at": datetime.now(timezone.utc).isoformat(),
    }
    update_turn_qc_audit(correlation_id, merge)
    full = fetch_turn_qc_audit(correlation_id)
    if isinstance(full, dict) and full:
        get_queue().patch_response_merge(correlation_id, {"qc_audit": full})
    return {"status": "ok", "qc_audit": full or merge}


@app.post("/chat/adjudication-feedback/{correlation_id}")
def post_adjudication_feedback_route(correlation_id: str, body: AdjudicationFeedbackBody):
    """Thumbs + comment on the adjudicator / QA scorecard (technical users)."""
    if body.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating must be 'up' or 'down'")
    insert_adjudication_feedback(correlation_id, body.rating, body.comment or None)
    return {"status": "ok"}


@app.post("/chat/feedback/{correlation_id}")
def post_chat_feedback(correlation_id: str, body: FeedbackBody):
    """Persist turn-level feedback (thumbs up/down + optional comment)."""
    if body.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating must be 'up' or 'down'")
    insert_feedback(correlation_id, body.rating, body.comment or None)
    return {"status": "ok"}


class LlmPerformanceFeedbackBody(BaseModel):
    rating: str
    comment: str | None = None


@app.post("/chat/llm-performance-feedback/{correlation_id}")
def post_llm_performance_feedback(correlation_id: str, body: LlmPerformanceFeedbackBody):
    """Model routing / efficiency feedback (separate from answer-quality thumbs)."""
    if body.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating must be 'up' or 'down'")
    insert_llm_performance_feedback(correlation_id, body.rating, body.comment or None)
    return {"status": "ok"}


@app.post("/chat/source-feedback/{correlation_id}")
def post_chat_source_feedback(correlation_id: str, body: SourceFeedbackBody):
    """Persist per-source feedback (thumbs up/down)."""
    if body.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating must be 'up' or 'down'")
    if body.source_index < 1:
        raise HTTPException(status_code=400, detail="source_index must be >= 1")
    insert_source_feedback(correlation_id, body.source_index, body.rating)
    return {"status": "ok"}


# --- Internal: credentialing / other skills use chat's ModelRouter + llm_calls (no shared Python package name) ---
_SKILL_LLM_ALLOWED_STAGES = frozenset({
    "credentialing_draft",
    "credentialing_validate",
    "credentialing_critique",
    "credentialing_compose",
    "credentialing_report_qa",
})


class SkillLLMRequest(BaseModel):
    """Body for POST /internal/skill-llm (credentialing report pipeline, etc.)."""

    system: str = ""
    user: str = ""
    stage: str = "credentialing_draft"
    max_tokens: int = 4096
    correlation_id: str | None = None
    thread_id: str | None = None


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


@app.get("/health")
def health():
    return {"status": "ok"}


# Serve chat UI at /
_frontend = Path(__file__).resolve().parent.parent / "frontend"
if _frontend.exists():
    app.mount("/static", NoCacheStaticFiles(directory=_frontend / "static"), name="static")

    @app.get("/")
    def index():
        r = FileResponse(_frontend / "index.html")
        r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return r
