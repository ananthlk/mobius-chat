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

from fastapi import Body, FastAPI, File, Form, Header, HTTPException, UploadFile
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
    # Phase 1a: get_most_helpful_documents / get_most_helpful_turns /
    # get_recent_turns moved to app.api.history.
    # Phase 1b: insert_adjudication_feedback / insert_feedback /
    # insert_llm_performance_feedback / insert_source_feedback moved to
    # app.api.feedback. fetch_turn_qc_audit stays — still used by the
    # response-fetch endpoint (see /chat/response/{cid}).
    get_plan,
    get_response,
)
from app.storage.feedback import get_adjudication_feedback, get_llm_performance_feedback
from app.storage.threads import append_uploaded_file_record, ensure_thread, get_state, save_state, save_state_full
from app.storage.progress import (
    get_and_clear_events,
    get_progress,
    get_progress_events_from_db,
    get_progress_from_db,
)
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
    chat_mode: Literal["copilot", "agentic", "quick"] | None = None
    """copilot: registry-first, 3 rounds. agentic: web escalation, 6 rounds. quick: mini-container, 2 rounds, brief answers."""


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


def _handle_instant_rag_upload(
    content: bytes, filename: str, org_name: str,
    thread_id: str | None, file_purpose: str,
) -> dict[str, Any]:
    """Route document uploads to the instant-rag skill for immediate RAG availability."""
    import json as json_mod
    import uuid as _uuid_mod
    import io
    import urllib.request
    import threading as _threading
    from datetime import datetime, timezone

    instant_rag_url = (os.environ.get("INSTANT_RAG_URL") or "http://localhost:8040").rstrip("/")

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
        "reconciliation_upload_id": None,
        "envelope_id": rag_result.get("envelope_id"),
        "document_id": rag_result.get("document_id"),
    }

    def _persist(tid: str, rec: dict) -> None:
        try:
            real_tid = ensure_thread(tid)
            append_uploaded_file_record(real_tid, rec)
        except Exception as _e:
            logger.debug("Thread state save (instant-rag): %s", _e)

    _threading.Thread(target=_persist, args=(tid, record), daemon=True).start()

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
) -> dict[str, Any]:
    """
    Upload a file for credentialing/reconciliation or instant RAG ingestion.
    Proxies to provider-roster-credentialing (roster) or instant-rag skill (documents).
    file_purpose: roster_reconciliation | instant_rag | other.
    Returns { upload_id, org_id, org_name, row_count, thread_id }.
    """
    content = file.file.read()
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

    # ── Roster path (original) ───────────────────────────────────────────
    if ext not in ("csv", "xlsx", "xls"):
        raise HTTPException(status_code=400, detail="Roster files must be CSV or Excel (.csv, .xlsx, .xls)")

    base = (os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL") or "").rstrip("/").split("/report")[0]
    if not base:
        raise HTTPException(
            status_code=503,
            detail="Roster upload not configured. Set CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL.",
        )
    if not org_name:
        raise HTTPException(status_code=400, detail="org_name is required for roster uploads")

    # 1. Resolve org_id — fast path: read from pipeline run state if available.
    #    Fallback: quick NPPES search with 5s timeout (non-fatal if it fails).
    import urllib.request
    import urllib.parse
    import json as json_mod
    import threading as _threading
    org_id = ""
    matched_org_name = org_name
    matched_practice_address: str | None = None

    # Fast path: get org NPI from the credentialing run (already looked up in Step 1)
    _run_id_val = (run_id or "").strip()
    if _run_id_val:
        try:
            from app.services.credentialing_run_service import _store_get
            _rec = _store_get(_run_id_val)
            if _rec:
                _state_dict = (_rec.get("orchestrator_state_dict") or {})
                _npi = (
                    _state_dict.get("org_npi")
                    or _state_dict.get("billing_npi")
                    or (_state_dict.get("selected_npis") or [None])[0]
                    or ""
                )
                if _npi:
                    org_id = str(_npi).strip().zfill(10)
                matched_org_name = _state_dict.get("org_name") or org_name
        except Exception as _e:
            logger.debug("Could not get org_id from run state: %s", _e)

    # org_id unknown — will be resolved asynchronously after upload_id is known (see step 2b below)
    _needs_bg_org_search = not org_id

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

    # 2b. Backfill org_id asynchronously (search takes ~60s, non-critical metadata)
    if _needs_bg_org_search:
        def _bg_org_search(base_url: str, name: str, uid: str) -> None:
            try:
                _r = urllib.request.Request(
                    f"{base_url}/search/org-names",
                    data=json_mod.dumps({"name": name}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(_r, timeout=60) as _resp:
                    _top = (json_mod.loads(_resp.read().decode()).get("results") or [{}])[0]
                    _oid = (_top.get("org_id") or _top.get("npi") or "").strip().zfill(10)
                if _oid and uid:
                    import httpx as _hx
                    with _hx.Client(timeout=10) as _c:
                        _c.patch(f"{base_url}/roster-uploads/{uid}", json={"org_id": _oid})
            except Exception as _e:
                logger.debug("Background org search: %s", _e)
        _threading.Thread(target=_bg_org_search, args=(base, org_name, upload_id), daemon=True).start()

    # 3. Register with the new NPI reconciliation pipeline (TurboTax-style progress UI).
    #    Runs right after upload so it always has the file, regardless of step 4 outcome.
    #    Non-fatal: if this fails the primary credentialing pipeline still proceeds.
    reconciliation_upload_id: str | None = None
    reconciliation_ui_url: str | None = None
    try:
        with httpx.Client(timeout=30.0) as rc_client:
            # 3a: store the file in the reconciliation pipeline (no auto-reconcile to avoid blocking)
            new_upload_resp = rc_client.post(
                f"{base}/roster/upload",
                files={"file": (filename, io.BytesIO(content), ct)},
                data={
                    "org_name": org_name,
                    "file_purpose": "roster_reconciliation",
                    "auto_reconcile": "false",
                    "uploaded_by": "chat",
                },
            )
            if new_upload_resp.status_code == 200:
                rc_data = new_upload_resp.json()
                reconciliation_upload_id = rc_data.get("upload_id") or None
                if reconciliation_upload_id:
                    reconciliation_ui_url = (
                        f"{base}/roster-ui/progress.html?upload_id={reconciliation_upload_id}"
                    )
                    # 3b: kick off reconciliation — pass Step-2 org locations if available
                    try:
                        # Look up Step-2 practice locations from the active pipeline run
                        _org_locations: list[dict] = []
                        _run_id = (run_id or "").strip()
                        if _run_id:
                            try:
                                from app.services.credentialing_run_service import _store_get
                                _rec = _store_get(_run_id)
                                if _rec:
                                    _state_dict = (_rec.get("orchestrator_state_dict") or {})
                                    _org_locations = _state_dict.get("locations") or []
                            except Exception as _loc_err:
                                logger.debug("Could not load run locations: %s", _loc_err)
                        rc_client.post(
                            f"{base}/roster/reconcile/{reconciliation_upload_id}",
                            json={"org_locations": _org_locations} if _org_locations else None,
                            timeout=5.0,
                        )
                    except Exception:
                        pass
            else:
                logger.warning("New reconciliation upload returned %s", new_upload_resp.status_code)
    except Exception as exc:
        logger.warning("New reconciliation upload skipped: %s", exc)

    # 3c. Persist reconciliation_upload_id to the pipeline run state (step3_roster_upload_id)
    #     so the pipeline page auto-loads the last roster without requiring a re-upload.
    if reconciliation_upload_id and run_id:
        def _patch_run_upload_id(rid: str, uid: str) -> None:
            try:
                from app.storage.credentialing_runs_pg import patch_step3_upload_id
                patch_step3_upload_id(rid, uid)
            except Exception as _e:
                logger.debug("patch_step3_upload_id failed: %s", _e)
        _threading.Thread(
            target=_patch_run_upload_id,
            args=(run_id, reconciliation_upload_id),
            daemon=True,
        ).start()

    # 4. Process via legacy credentialing pipeline (parse, clean, resolve NPIs via GCS/BQ).
    #    Runs in a background thread — DOES NOT block the response.
    #    The new reconciliation pipeline (step 3) is the primary real-time path.
    def _run_legacy_process(url: str, data: bytes) -> None:
        try:
            _req = urllib.request.Request(url, data=data,
                                          headers={"Content-Type": "application/json"},
                                          method="POST")
            with urllib.request.urlopen(_req, timeout=120) as _resp:
                _resp.read()
        except Exception as _e:
            logger.debug("Legacy roster process (background): %s", _e)

    _proc_payload = json_mod.dumps({"resolve_npi": True, "state": "FL"}).encode()
    _threading.Thread(
        target=_run_legacy_process,
        args=(f"{base}/roster-uploads/{upload_id}/process", _proc_payload),
        daemon=True,
    ).start()

    # These values are no longer available synchronously (legacy runs in background).
    proc_data: dict = {}
    _process_error: str | None = None
    rc_clean = 0
    rc_res = 0
    row_count = 0
    resolution_summary: dict | None = None
    pipeline_progress: dict | None = None

    # 5. Save to thread state — runs in a background thread so the response is instant.
    #    The upload IDs are returned immediately; thread state persists asynchronously.
    from datetime import datetime, timezone

    purpose = (file_purpose or "roster_reconciliation").strip() or "roster_reconciliation"
    if purpose not in ("roster_reconciliation", "instant_rag", "other"):
        purpose = "roster_reconciliation"

    # Generate thread id without blocking (no DB call yet)
    _tid_input = (thread_id or "").strip() or None
    import uuid as _uuid_mod
    tid = _tid_input or str(_uuid_mod.uuid4())

    record: dict[str, Any] = {
        "upload_id": upload_id,
        "org_id": org_id,
        "org_name": org_name,
        "purpose": purpose,
        "filename": filename,
        "row_count": 0,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "reconciliation_upload_id": reconciliation_upload_id,
    }

    def _persist_thread_state(tid: str, record: dict) -> None:
        try:
            real_tid = ensure_thread(tid)
            append_uploaded_file_record(real_tid, record)
        except Exception as _e:
            logger.debug("Background thread-state save skipped: %s", _e)

    _threading.Thread(
        target=_persist_thread_state,
        args=(tid, record),
        daemon=True,
    ).start()

    # Log roster upload event to audit log (fire-and-forget, non-fatal)
    def _log_upload_audit(skill_base: str, org: str, fname: str, uid: str, rc_uid: str | None) -> None:
        try:
            import urllib.request as _ur
            evt = [{
                "org_name":    org,
                "event_type":  "uploaded",
                "upload_id":   uid,
                "actor":       "user",
                "actor_label": "Roster file upload",
                "event_data": {
                    "filename":                fname,
                    "upload_id":               uid,
                    "reconciliation_upload_id": rc_uid,
                },
            }]
            _req = _ur.Request(
                f"{skill_base}/roster/log-events",
                data=json_mod.dumps(evt).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _ur.urlopen(_req, timeout=8):
                pass
        except Exception as _e:
            logger.debug("Upload audit log (non-fatal): %s", _e)
    if base:
        _threading.Thread(
            target=_log_upload_audit,
            args=(base, org_name, filename, upload_id, reconciliation_upload_id),
            daemon=True,
        ).start()

    out: dict[str, Any] = {
        "upload_id": upload_id,
        "org_id": org_id,
        "org_name": org_name,
        "row_count": 0,
        "thread_id": tid,
        "file_purpose": purpose,
        "default_billing_npi": org_id,
        "filename": filename,
        "matched_organization_name": matched_org_name,
        "matched_practice_address": matched_practice_address,
        "reconciliation_upload_id": reconciliation_upload_id,
        "reconciliation_ui_url": reconciliation_ui_url,
    }
    return out


@app.get("/chat/thread/{thread_id}/uploads")
def get_thread_uploads(thread_id: str) -> dict[str, Any]:
    """
    Document upload skill — list files attached to this chat thread (newest first).
    Used by the UI, MCP, and integrations; supports multiple uploads over time per thread.

    Also returns ``latest_roster_reconciliation`` and ``roster_freshness`` (``fresh`` | ``stale`` | ``none``)
    so the client can show whether an existing roster is recent enough to reuse without re-uploading.
    Threshold comes from env ``CHAT_ROSTER_FRESH_DAYS`` (default 14).
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
            "reconciliation_upload_id": None,
            "reconciliation_org_id": None,
            "reconciliation_org_name": None,
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
    if latest_roster is None:
        rup = (active.get("reconciliation_upload_id") or "").strip()
        rid = (active.get("reconciliation_org_id") or "").strip()
        ron = (active.get("reconciliation_org_name") or "").strip()
        if rup and rid:
            uploaded_at_u: Any = None
            filename_u = ""
            row_count_u: Any = None
            org_name_u = ron
            for u in uploaded:
                if not isinstance(u, dict):
                    continue
                if (u.get("upload_id") or "").strip() != rup:
                    continue
                uploaded_at_u = u.get("uploaded_at")
                filename_u = (u.get("filename") or "").strip()
                row_count_u = u.get("row_count")
                on = (u.get("org_name") or "").strip()
                if on:
                    org_name_u = on
                break
            latest_roster = {
                "upload_id": rup,
                "org_id": rid,
                "org_name": org_name_u,
                "filename": filename_u,
                "row_count": row_count_u,
                "uploaded_at": uploaded_at_u,
            }
    freshness, age_days = _compute_roster_freshness(latest_roster)
    return {
        "thread_id": tid,
        "uploaded_files": uploaded,
        "roster_reconciliation_files": roster_reconciliation_files,
        "reconciliation_upload_id": (active.get("reconciliation_upload_id") or "").strip() or None,
        "reconciliation_org_id": (active.get("reconciliation_org_id") or "").strip() or None,
        "reconciliation_org_name": (active.get("reconciliation_org_name") or "").strip() or None,
        "latest_roster_reconciliation": latest_roster,
        "roster_freshness": freshness,
        "roster_age_days": round(age_days, 2) if age_days is not None else None,
        "roster_fresh_days_threshold": thresh,
    }


# Phase 1c: credentialing-runs + NPI lookup endpoints extracted to
# app.api.credentialing (router mounted near the top of this file).


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
# Phase 1c: credentialing-runs + NPI lookup extracted to app.api.credentialing.
# Phase 1d: roster-reconcile + roster-truth + roster-org extracted to app.api.roster.
# Router mounts below preserve external URLs.
from app.api.credentialing import router as _credentialing_router
from app.api.feedback import router as _feedback_router
from app.api.history import router as _history_router
from app.api.roster import router as _roster_router
app.include_router(_history_router)
app.include_router(_feedback_router)
app.include_router(_credentialing_router)
app.include_router(_roster_router)


# Phase 1b: feedback / QC endpoints moved to app.api.feedback.
# Kept inline code here for 100+ lines; now just a router mount at the top.


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
# Financial Strategy skill proxy — /chat/financial-strategy/* → provider-roster-credentialing
# ═══════════════════════════════════════════════════════════════════════════════

def _fs_proxy(method: str, path: str, *, json_body=None, timeout: float = 30.0):
    """Proxy helper for financial-strategy routes on the credentialing skill server."""
    import httpx
    base = _skill_base()
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.request(method, f"{base}{path}", json=json_body)
            r.raise_for_status()
            return r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Skill server error: {e}") from e


@app.get("/chat/financial-strategy/orgs")
def fs_list_orgs():
    """Proxy: list available orgs with canonical data."""
    return _fs_proxy("GET", "/financial-strategy/orgs")


@app.get("/chat/financial-strategy/industry")
def fs_industry():
    """Proxy: static industry landscape chapter."""
    return _fs_proxy("GET", "/financial-strategy/industry")


@app.post("/chat/financial-strategy/generate-baseline")
def fs_generate_baseline(body: dict = Body(...)):
    """Proxy: generate industry + org baseline chapter."""
    return _fs_proxy("POST", "/financial-strategy/generate-baseline", json_body=body)


@app.post("/chat/financial-strategy/ask")
def fs_ask(body: dict = Body(...)):
    """Proxy: Q&A over org's financial position (includes LLM reframe)."""
    return _fs_proxy("POST", "/financial-strategy/ask", json_body=body, timeout=60.0)


@app.post("/chat/financial-strategy/generate-plan")
def fs_generate_plan(body: dict = Body(...)):
    """Proxy: convert findings into investigation tasks."""
    return _fs_proxy("POST", "/financial-strategy/generate-plan", json_body=body)


@app.post("/chat/org-story")
def fs_org_story(body: dict = Body(...)):
    """Proxy: 5-factor Laspeyres decomposition + conversion + leakage dashboard."""
    return _fs_proxy("POST", "/org-story", json_body=body, timeout=120.0)


@app.post("/chat/org-story-v2")
def fs_org_story_v2(body: dict = Body(...)):
    """Proxy: org story v2 — pre-computed from v2 tables (<2s vs 30s)."""
    return _fs_proxy("POST", "/org-story-v2", json_body=body, timeout=30.0)


@app.get("/chat/market-map")
def fs_market_map():
    """Proxy: FL BH market map data — all org locations with revenue."""
    return _fs_proxy("GET", "/market-map", timeout=60.0)


@app.get("/chat/industry-report-data")
def fs_industry_report_data():
    """Proxy: Industry report data — archetype distributions, code metrics, trends, CMHC."""
    return _fs_proxy("GET", "/industry-report-data", timeout=120.0)


# ═══════════════════════════════════════════════════════════════════════════════
# Doc Reader skill proxy — /chat/doc-reader/* → mobius-skills/doc-reader
# ═══════════════════════════════════════════════════════════════════════════════

def _doc_reader_proxy(method: str, path: str, *, json_body=None, timeout: float = 30.0):
    """Proxy helper for doc-reader skill routes."""
    import httpx
    base = (os.environ.get("CHAT_SKILLS_DOC_READER_URL") or "http://localhost:8018").rstrip("/")
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.request(method, f"{base}{path}", json=json_body)
            r.raise_for_status()
            return r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Doc-reader skill error: {e}") from e


@app.post("/chat/doc-reader/read")
def dr_read(body: dict = Body(...)):
    """Proxy: read/reassemble a published document."""
    return _doc_reader_proxy("POST", "/read", json_body=body)


@app.post("/chat/doc-reader/extract")
def dr_extract(body: dict = Body(...)):
    """Proxy: query-targeted extraction from a document."""
    return _doc_reader_proxy("POST", "/extract", json_body=body, timeout=60.0)


@app.post("/chat/doc-reader/summarize")
def dr_summarize(body: dict = Body(...)):
    """Proxy: generate LLM summary of a document."""
    return _doc_reader_proxy("POST", "/summarize", json_body=body, timeout=60.0)


@app.get("/chat/doc-reader/health")
def dr_health():
    """Proxy: doc-reader health check."""
    return _doc_reader_proxy("GET", "/health")


# ═══════════════════════════════════════════════════════════════════════════════
# Task Manager skill proxy — /chat/tasks/* → mobius-skills/task-manager
# ═══════════════════════════════════════════════════════════════════════════════

# Phase 1e: _task_manager_base consolidated into app.api._common.
from app.api._common import task_manager_base_url as _task_manager_base


def _task_proxy(method: str, path: str, *, params=None, json_body=None, timeout: float = 15.0):
    """Generic proxy helper for task-manager skill calls. Raises HTTPException on failure."""
    import httpx
    base = _task_manager_base()
    if not base:
        raise HTTPException(status_code=503, detail="Task manager skill not configured (CHAT_SKILLS_TASK_MANAGER_URL)")
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.request(method, f"{base}{path}", params=params, json=json_body)
            if r.status_code == 404:
                raise HTTPException(status_code=404, detail="Task not found")
            if r.status_code == 422:
                raise HTTPException(status_code=422, detail=r.json())
            r.raise_for_status()
            return r
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Task manager error: {e}")


_STEP_LABELS: dict[str, str] = {
    "ensure_benchmarks":        "Ensuring revenue metrics",
    "identify_org":             "Identifying organization",
    "find_locations":           "Mapping practice locations",
    "find_associated_providers":"Finding associated providers",
    "nppes_alignment":          "Aligning NPPES data",
    "medicaid_enrollment":      "Checking Medicaid enrollment",
    "compliance_check":         "Running compliance check",
    "taxonomy_optimization":    "Optimizing taxonomy codes",
}
_STEP_TOTAL = len(_STEP_LABELS)


@app.get("/chat/runs")
def chat_runs_list(
    status: str | None = None,   # active | complete | all (default all)
    limit: int = 20,
) -> dict[str, Any]:
    """
    Aggregate credentialing runs with task counts for the credentialing home page.
    Merges /chat/credentialing-runs with task-manager counts in two bulk calls.
    """
    from collections import defaultdict
    from app.storage.credentialing_runs_pg import list_credentialing_runs

    runs = list_credentialing_runs(limit=limit)

    # Bulk-fetch all open tasks + resolved info tasks across all runs in two calls
    try:
        open_tasks = _task_proxy("GET", "/tasks", params={
            "status": "open", "workflow": "credentialing", "limit": 500,
        }).json().get("tasks", [])
    except Exception:
        open_tasks = []

    try:
        resolved_info = _task_proxy("GET", "/tasks", params={
            "status": "resolved", "workflow": "credentialing", "limit": 500,
        }).json().get("tasks", [])
    except Exception:
        resolved_info = []

    # Group by run_id
    open_by_run: dict[str, list] = defaultdict(list)
    for t in open_tasks:
        if t.get("run_id"):
            open_by_run[t["run_id"]].append(t)

    resolved_info_by_run: dict[str, int] = defaultdict(int)
    for t in resolved_info:
        if t.get("run_id") and t.get("type") == "info":
            resolved_info_by_run[t["run_id"]] += 1

    def _phase_to_status(phase: str) -> str:
        if phase in ("running", "awaiting_validation"):
            return "running"
        if phase == "complete":
            return "complete"
        if phase == "error":
            return "error"
        return "paused"

    result: list[dict[str, Any]] = []
    for run in runs:
        phase     = run.get("phase", "")
        run_id    = run["run_id"]
        run_status = _phase_to_status(phase)

        if status == "active" and run_status not in ("running", "paused"):
            continue
        if status == "complete" and run_status != "complete":
            continue

        run_tasks      = open_by_run.get(run_id, [])
        open_decisions = sum(1 for t in run_tasks if t.get("type") == "decision")
        open_blockers  = sum(1 for t in run_tasks if t.get("type") == "blocker")
        resolved_steps = resolved_info_by_run.get(run_id, 0)
        pending_step   = run.get("pending_step_id") or ""
        pending_label  = _STEP_LABELS.get(pending_step, pending_step.replace("_", " ").title() if pending_step else "")

        result.append({
            "run_id":            run_id,
            "org_name":          run.get("org_name", ""),
            "run_status":        run_status,
            "phase":             phase,
            "started_at":        run.get("created_at") or run.get("updated_at"),
            "provider_count":    None,
            "step_current":      resolved_steps,
            "step_total":        _STEP_TOTAL,
            "pending_step_label": pending_label,
            "open_decisions":    open_decisions,
            "open_blockers":     open_blockers,
            "resolved_steps":    resolved_steps,
        })

    return {"runs": result}


@app.get("/chat/tasks")
def chat_tasks_list(
    org_name: str | None = None,
    module: str | None = None,
    status: str | None = None,
    assignee: str | None = None,
    npi: str | None = None,
    run_id: str | None = None,
    severity: str | None = None,
    limit: int = 200,
    offset: int = 0,
):
    """Proxy: list tasks from task-manager skill. Injects run_status when run_id provided."""
    params = {k: v for k, v in {
        "org_name": org_name, "module": module, "status": status,
        "assignee": assignee, "npi": npi, "run_id": run_id,
        "severity": severity, "limit": limit, "offset": offset,
    }.items() if v is not None}
    result = _task_proxy("GET", "/tasks", params=params).json()

    # When querying cross-run (no run_id) with status=open, sort blockers first
    # then decisions, then others — all ordered by created_at ascending.
    if not run_id and status == "open":
        _TYPE_PRIORITY = {"blocker": 0, "decision": 1}
        result["tasks"] = sorted(
            result.get("tasks", []),
            key=lambda t: (
                _TYPE_PRIORITY.get(t.get("type", ""), 2),
                t.get("created_at", ""),
            ),
        )

    # Inject run_status so the frontend knows when to stop polling
    if run_id:
        try:
            from app.services.credentialing_run_service import get_credentialing_run
            rec = get_credentialing_run(run_id)
            rec_data = rec or {}
            phase = rec_data.get("phase", "")
            pending_step = rec_data.get("pending_step_id") or ""
            if phase == "running":
                run_status = "running"
            elif phase == "awaiting_validation":
                run_status = "awaiting_validation"
                result["pending_step_id"] = pending_step
            elif phase == "complete":
                run_status = "complete"
            elif phase == "error":
                run_status = "error"
            else:
                run_status = "paused"
        except Exception:
            run_status = "unknown"
        result["run_status"] = run_status

    return result


@app.post("/chat/tasks")
def chat_tasks_create(body: dict = Body(...)):
    """Proxy: create a manual task."""
    return _task_proxy("POST", "/tasks", json_body=body).json()


@app.get("/chat/tasks/export")
def chat_tasks_export(org_name: str | None = None, module: str | None = None, status: str | None = None):
    """Proxy: export tasks as CSV."""
    from fastapi.responses import PlainTextResponse
    params = {k: v for k, v in {"org_name": org_name, "module": module, "status": status}.items() if v is not None}
    r = _task_proxy("GET", "/tasks/export", params=params)
    return PlainTextResponse(
        content=r.text,
        media_type="text/csv",
        headers={"Content-Disposition": r.headers.get("Content-Disposition", 'attachment; filename="tasks.csv"')},
    )


@app.post("/chat/tasks/bulk-import")
def chat_tasks_bulk_import(body: dict = Body(...)):
    """Proxy: bulk upsert tasks (used by orchestrator and skills)."""
    return _task_proxy("POST", "/tasks/bulk-import", json_body=body).json()


@app.get("/chat/tasks/{task_id}")
def chat_tasks_get(task_id: str):
    """Proxy: fetch a single task."""
    return _task_proxy("GET", f"/tasks/{task_id}").json()


@app.patch("/chat/tasks/{task_id}")
def chat_tasks_patch(task_id: str, body: dict = Body(...)):
    """Proxy: update task fields (status, assignee, deadline, notes, etc.)."""
    return _task_proxy("PATCH", f"/tasks/{task_id}", json_body=body).json()


@app.post("/chat/tasks/{task_id}/resolve")
def chat_tasks_resolve(task_id: str, body: dict = Body(default={})):
    """Proxy: mark a task resolved."""
    return _task_proxy("POST", f"/tasks/{task_id}/resolve", json_body=body).json()


@app.post("/chat/tasks/{task_id}/dismiss")
def chat_tasks_dismiss(task_id: str, body: dict = Body(default={})):
    """Proxy: dismiss a task."""
    return _task_proxy("POST", f"/tasks/{task_id}/dismiss", json_body=body).json()


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

    @app.get("/pipeline")
    def pipeline():
        r = FileResponse(_frontend / "pipeline.html")
        r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return r

    @app.get("/financial-strategy")
    def financial_strategy():
        r = FileResponse(_frontend / "financial-strategy.html")
        r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return r

    @app.get("/org-story")
    def org_story_page():
        r = FileResponse(_frontend / "org-story.html")
        r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return r

    @app.get("/market-map")
    def market_map_page():
        r = FileResponse(_frontend / "market-map.html")
        r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return r

    @app.get("/industry-report")
    def industry_report_page():
        r = FileResponse(_frontend / "static" / "fl-bh-industry-report.html")
        r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return r

    @app.get("/roster")
    def roster():
        # roster.html lives in static/ (not the top-level frontend/ dir)
        p = _frontend / "static" / "roster.html"
        r = FileResponse(p)
        r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return r
