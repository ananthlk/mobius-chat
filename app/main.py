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
import threading
import time
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

# ── Eager cold-import block ─────────────────────────────────────────
#
# 2026-04-28 — Cold instances were paying 17–516s of lazy module
# imports inside the user's first request budget. Trace of a 5-min
# turn (cid 0479bc18) showed: payer_normalization YAML load 5s into
# the request, model_registry "Auto-enabled models" log 50s in,
# vertexai SDK init another 17s after that — none of it happening
# at startup, all on the user's clock.
#
# Cloud Run holds traffic until module imports complete, so doing
# them at module-load time (here) shifts the cost from
# user-visible to autoscaler-visible. With min-instances=4, scale-up
# cost is hidden by the warm pool. The earlier daemon-thread warmup
# was racing the user's request — when the warmup thread lost the
# race (e.g. CPU contention), turns paid the full tax.
#
# Each import below was observed firing inside a user turn on a
# cold instance:
#   - vertexai + GenerativeModel: heaviest, 12-15s typical, up to 8min
#     under contention because google-cloud-aiplatform pulls in
#     ~100 transitive deps.
#   - app.services.llm_provider: imports vertexai too + builds gRPC
#     client class hierarchy.
#   - app.skills.registry: triggers _load_builtins() which imports
#     all 9 builtin skill modules (cached_answer, corpus_search,
#     document_uploads, fetch_document, healthcare,
#     transform_previous, vibe, web, web_search).
#   - app.pipeline.orchestrator + react_loop: heaviest single
#     pipeline files; pulled in by worker on first request.
#   - app.payer_normalization: small YAML load, gated behind
#     _load_config() lazy init.
#
# Failures are logged at WARNING but never raise — if vertexai or a
# skill module has a real import error, /health will still respond
# and the actual failure surfaces on the first request as it would
# have anyway.
_cold_import_t0 = time.perf_counter()
try:
    import vertexai  # noqa: F401
    from vertexai.generative_models import GenerativeModel  # noqa: F401
    _t_vertex = time.perf_counter()
    from app.services import llm_provider  # noqa: F401
    _t_llm_provider = time.perf_counter()
    from app.skills import registry as _skill_registry  # noqa: F401  # triggers _load_builtins()
    _t_skills = time.perf_counter()
    from app.pipeline import orchestrator as _orchestrator  # noqa: F401
    from app.pipeline import react_loop as _react_loop  # noqa: F401
    from app.pipeline import tool_manifest as _tool_manifest  # noqa: F401
    _t_pipeline = time.perf_counter()
    from app.payer_normalization import _load_config as _payer_load
    _payer_load()  # warm the YAML alias map so first user turn doesn't
    _t_payer = time.perf_counter()
    logger.info(
        "cold-import: total=%.2fs (vertex=%.2fs llm_provider=%.2fs "
        "skills=%.2fs pipeline=%.2fs payer=%.2fs)",
        _t_payer - _cold_import_t0,
        _t_vertex - _cold_import_t0,
        _t_llm_provider - _t_vertex,
        _t_skills - _t_llm_provider,
        _t_pipeline - _t_skills,
        _t_payer - _t_pipeline,
    )
except Exception as _e:
    logger.warning(
        "cold-import: one or more eager imports failed (non-fatal — "
        "first user request will pay any remaining lazy import cost): %s",
        _e,
    )


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


def _prewarm_worker_caches() -> None:
    """Warm runtime caches the worker's first turn would otherwise
    pay for inside the user's request. Best-effort.

    Currently warms:
      1. DB connection pool — opens one connection via the same code
         path ``run_state_load`` uses. The handshake + initial SELECT
         is 100-300ms of dead time on a fresh process; better paid
         here than inside the user's first turn.
      2. ReAct prompt builders — render the system prompt + tool
         manifest once so any internal lazy caches (regex compile,
         YAML parse) settle.

    Explicitly does NOT warm Redis. The Redis client's first ping
    over the VPC connector takes 60-100s on a cold Cloud Run instance
    (observed in production logs 2026-04-29). Running that on a
    daemon thread DOES NOT help — Python's GIL means the slow ping
    competes with the user's first request handler and starves
    state_load of CPU. Net effect: state_load that should take 0.3s
    takes 13-19s on cold instances.

    Redis warms naturally on its actual first use (publish_progress
    inside the worker callback), which happens AFTER state_load
    completes — so the user's first DB read no longer pays the GIL
    contention tax.

    No LLM token is spent. If any step fails, log and return.
    """
    t0 = time.perf_counter()
    parts: list[str] = []
    try:
        from app.db_client import _get_fallback_url, _acquire_conn, _release_conn
        url = _get_fallback_url("chat")
        if url:
            t = time.perf_counter()
            conn, is_pooled = _acquire_conn(url)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            finally:
                _release_conn(url, conn, is_pooled, is_broken=False)
            parts.append(f"db_pool={int((time.perf_counter()-t)*1000)}ms")
    except Exception as e:
        parts.append(f"db_pool=FAIL({type(e).__name__})")
    try:
        from app.pipeline.react.prompts import _react_reasoning_system
        t = time.perf_counter()
        _react_reasoning_system(4, "fast")
        parts.append(f"react_prompt={int((time.perf_counter()-t)*1000)}ms")
    except Exception as e:
        parts.append(f"react_prompt=FAIL({type(e).__name__})")
    logger.info(
        "worker-prewarm: complete in %.2fs (%s) — first user turn skips this work",
        time.perf_counter() - t0, " ".join(parts),
    )


def _warmup_vertex_sdk() -> None:
    """Pre-pay the Vertex-SDK first-call tax so the user's first turn
    doesn't.

    Steps mirror what the FIRST generate_content call would do anyway —
    we just do it on a daemon thread at startup so the user never sees
    the 12-15s init tail:

      1. Import ``vertexai`` + ``GenerativeModel`` (~3-5s on first
         import; the google-cloud-aiplatform wheel is heavy).
      2. ``vertexai.init(project=..., location=...)`` — ADC chain
         resolution + project/location binding.
      3. Construct a ``GenerativeModel`` for the cheapest gemini we
         use (gemini-2.5-flash) — instantiates the gRPC channel and
         opens the TLS connection.
      4. Issue ONE tiny ``generate_content`` (max_output_tokens=8) so
         the first-byte path warms end-to-end.

    Best-effort. Failures are logged at WARNING but never raise — the
    actual user request will surface a real error if Vertex is genuinely
    broken; the warm-up is purely a latency optimization.

    No-op when ``VERTEX_PROJECT_ID`` / ``CHAT_VERTEX_PROJECT_ID`` is
    unset (local dev with ollama-only) or when
    ``CHAT_VERTEX_WARMUP_DISABLED=1``.
    """
    pid = (os.environ.get("VERTEX_PROJECT_ID") or os.environ.get("CHAT_VERTEX_PROJECT_ID") or "").strip()
    if not pid:
        logger.info("vertex-warmup: VERTEX_PROJECT_ID unset; skipping")
        return
    location = (os.environ.get("VERTEX_LOCATION") or "us-central1").strip()
    model_name = (os.environ.get("CHAT_VERTEX_WARMUP_MODEL") or "gemini-2.5-flash").strip()
    t0 = time.perf_counter()
    try:
        # Import + init — these are the slow parts on a cold container.
        import vertexai  # type: ignore
        from vertexai.generative_models import GenerativeModel  # type: ignore

        t_import = time.perf_counter()
        vertexai.init(project=pid, location=location)
        t_init = time.perf_counter()
        model = GenerativeModel(model_name)
        t_construct = time.perf_counter()

        # One small request — the FIRST generate_content is what pays
        # the gRPC + TLS + auth handshake cost. Cap output so the call
        # bills near-zero. We don't care about the response text.
        resp = model.generate_content(
            "ping",
            generation_config={"max_output_tokens": 8, "temperature": 0.0},
        )
        t_call = time.perf_counter()
        try:
            _ = (resp.text or "")[:8]
        except Exception:
            pass

        logger.info(
            "vertex-warmup: complete in %.2fs (import=%.2fs init=%.2fs "
            "construct=%.2fs first_call=%.2fs) — first user turn skips "
            "the SDK cold-start tax",
            t_call - t0,
            t_import - t0,
            t_init - t_import,
            t_construct - t_init,
            t_call - t_construct,
        )
    except Exception as e:
        logger.warning(
            "vertex-warmup: failed (non-fatal — first real turn pays the "
            "init tax): %s", e,
        )


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
    # 2026-04-27 — always spawn the in-process worker regardless of
    # queue type. Pre-Redis, this was gated to ``memory`` only because
    # the assumption was that ``redis`` would have a separate worker
    # process running ``python -m app.worker``. Cloud Run deploys the
    # API container only — so without an in-process worker, jobs land
    # in Redis but nothing pulls them.
    #
    # With ``CHAT_QUEUE_TYPE=redis`` and N Cloud Run instances, this
    # gives us N workers all BRPOPping from the same Redis list, which
    # is exactly the multi-instance parallelism we want. The earlier
    # comment about a separate worker process is preserved at the
    # bottom of this branch for future operators who want to scale
    # workers independently of the API.
    start_worker_background()
    _worker_started = True
    logger.info("Started in-process worker (queue_type=%s)", cfg.queue_type)
    if cfg.queue_type != "memory":
        logger.info(
            "(Optional: scale workers independently by running "
            "'python -m app.worker' as a separate Cloud Run service "
            "or sidecar — they'll all consume from the same Redis list.)"
        )

    # Worker pre-warm (2026-04-29).
    #
    # Eager imports loaded the modules; this warms the runtime CACHES
    # the worker's first real turn would otherwise pay for:
    #   - psycopg2 ThreadedConnectionPool (each first-conn = 100-300ms
    #     TCP handshake + auth + initial SELECT round-trip)
    #   - Redis client (first ping after import = full TCP+auth)
    #   - prompt-builder caches inside react/prompts (jurisdiction
    #     summary helpers, manifest text rendering)
    #
    # We don't run a real LLM call (would burn tokens with no value)
    # — just hit the read-only paths a follow-up turn touches before
    # the first generate_content. If any step fails we log and move
    # on; production traffic doesn't depend on this succeeding.
    if (os.environ.get("CHAT_WORKER_PREWARM_DISABLED") or "").strip().lower() not in ("1", "true", "yes"):
        threading.Thread(
            target=_prewarm_worker_caches,
            name="worker-prewarm",
            daemon=True,
        ).start()

    # Live-health refresher (2026-04-28).
    #
    # Polls model_health_recent (Postgres view) every 10s and caches
    # degraded-model state in memory. The bandit's circuit breaker
    # reads this cache to route around backends that are timing out
    # or running abnormally slow RIGHT NOW — the 24h-averaged
    # circuit breakers can't see a 5-minute spike.
    #
    # All instances poll the same view from the same Postgres, so
    # degradation signal is naturally consistent across instances.
    # No-op if the view is missing (migration 034 not applied) or if
    # LLM_HEALTH_DISABLED=1.
    try:
        from app.services.llm_health import LIVE_HEALTH as _LLM_HEALTH
        _LLM_HEALTH.start()
    except Exception as e:
        logger.warning("llm-health: failed to start refresher (non-fatal): %s", e)

    # Vertex SDK warm-up (2026-04-28).
    #
    # The first generate_content call after a fresh worker process starts
    # pays a 12-15s tax for SDK init: google-cloud-aiplatform library
    # import, ADC credential resolution, gRPC channel build, TLS handshake
    # to us-central1-aiplatform.googleapis.com, internal model registry
    # warm-up. Latency-anatomy probe (cid 56bfb67d) measured 15.3s of
    # silent gap before the first vertex log line on a cold worker.
    #
    # Pre-pay it on a daemon thread at startup. Doesn't block /health
    # (Cloud Run readiness uses /health which returns immediately). Once
    # this completes, the user's first turn doesn't pay the SDK init.
    # Subsequent worker processes (autoscale) each warm independently.
    #
    # CHAT_VERTEX_WARMUP_DISABLED=1 to skip (e.g. local dev or tests).
    if (os.environ.get("CHAT_VERTEX_WARMUP_DISABLED") or "").strip().lower() not in ("1", "true", "yes"):
        threading.Thread(
            target=_warmup_vertex_sdk,
            name="vertex-warmup",
            daemon=True,
        ).start()

    # Phase 13.7 — audit chat_turns.context_summary presence + nullability.
    # Logs a structured WARNING (channel=phase13_7_schema_audit) if the
    # column is missing or NOT NULL. /ready surfaces the cached status
    # so on-call can see schema state without grepping logs. Never
    # raises — Phase 13.7 degrades gracefully when the column is absent.
    try:
        from app.services.phase_13_7_metrics import audit_thread_summary_schema
        audit_thread_summary_schema()
    except Exception as e:
        logger.warning("Phase 13.7 schema audit failed to run: %s", e)

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


def _post_system_message_to_thread(thread_id: str, content: str) -> None:
    """Insert a role='system' row into chat_turn_messages.

    Used by the background-publish watcher to post "✓ X is ready" once
    rag finishes processing. Generates a fresh turn_id since the system
    message isn't tied to any user turn — the chat UI just renders it
    inline like any other message in the thread.

    Best-effort: failures are logged and swallowed; a missed system
    message is bad UX but not data loss (the catalog row already
    persisted, and the document is searchable as soon as rag publishes).

    TODO: this is the minimal version. A cleaner system-message helper
    (with a dedicated table or a proper `system_messages` role with FE
    rendering) is tracked separately.
    """
    import uuid as _uuid_mod
    tid = (thread_id or "").strip()
    if not tid:
        logger.warning("system-message: thread_id empty; skipping post")
        return
    try:
        from app.storage.threads import _insert_message, ensure_thread
        try:
            ensure_thread(tid)
        except Exception:
            pass
        _insert_message(tid, str(_uuid_mod.uuid4()), "system", content)
        logger.info("system-message posted to thread=%s: %s", tid[:8], content[:80])
    except Exception as e:
        logger.warning("system-message post failed for thread=%s: %s", tid[:8], e)


def _wait_for_publish_inline(
    rag_url: str, document_id: str, eta_seconds: int,
) -> dict[str, Any]:
    """Poll rag's status endpoint until published_at is set, or
    eta_seconds + 30s buffer elapses. Returns the final status dict
    (which the caller maps into the response shape).
    """
    import json as _json
    import time as _time
    import urllib.request

    max_attempts = max(20, (eta_seconds + 30) // 5)  # 5s intervals
    final_status: dict[str, Any] = {}
    for attempt in range(max_attempts):
        try:
            req = urllib.request.Request(
                f"{rag_url}/documents/{document_id}/status", method="GET",
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                final_status = _json.loads(r.read())
        except Exception as e:
            logger.warning(
                "rag status poll attempt %d/%d failed for doc=%s: %s",
                attempt + 1, max_attempts, document_id, e,
            )
            _time.sleep(5)
            continue
        status_value = (final_status.get("status") or "").lower()
        if status_value == "failed":
            return {"_failed": True, **final_status}
        if status_value == "completed" and final_status.get("published_at"):
            return final_status
        _time.sleep(5)
    return final_status  # caller treats as "still processing"


_BACKGROUND_WATCHERS: dict[str, threading.Thread] = {}


def _spawn_background_publish_watcher(
    rag_url: str,
    document_id: str,
    thread_id: str,
    filename: str,
    eta_seconds: int,
) -> None:
    """Detached thread that polls rag status and posts a system message
    to the originating chat thread once the doc is published.

    Implementation notes:
      * Uses ``threading.Thread`` (not FastAPI BackgroundTasks — those
        die when the request ends) so the watcher survives the original
        upload's HTTP response.
      * Module-level registry guards against duplicate watchers if the
        same upload retries.
      * Cap = eta_seconds + 5min (rag's own ETA isn't perfectly
        accurate; the buffer covers tail variance).
    """
    if document_id in _BACKGROUND_WATCHERS:
        logger.info("upload-watcher: doc=%s already being watched; skipping spawn", document_id[:8])
        return
    if not (thread_id or "").strip():
        logger.warning("upload-watcher: empty thread_id for doc=%s; cannot post system message", document_id[:8])
        return

    def _watch() -> None:
        import json as _json
        import time as _time
        import urllib.request
        max_seconds = eta_seconds + 300
        poll_interval = 15
        elapsed = 0
        try:
            while elapsed < max_seconds:
                _time.sleep(poll_interval)
                elapsed += poll_interval
                try:
                    req = urllib.request.Request(
                        f"{rag_url}/documents/{document_id}/status", method="GET",
                    )
                    with urllib.request.urlopen(req, timeout=15) as r:
                        s = _json.loads(r.read())
                except Exception as e:
                    logger.warning("upload-watcher: status fetch failed for doc=%s: %s", document_id[:8], e)
                    continue
                status_value = (s.get("status") or "").lower()
                if status_value == "failed":
                    _post_system_message_to_thread(
                        thread_id,
                        f"⚠ {filename} failed to process. Please try uploading again or open Mobius RAG to investigate.",
                    )
                    return
                if s.get("published_at"):
                    _chunks = int(s.get("chunks_count") or 0)
                    if _chunks > 0:
                        try:
                            from app.storage.threads import update_uploaded_file_chunk_count
                            update_uploaded_file_chunk_count(thread_id, document_id, _chunks)
                        except Exception as _ue:
                            logger.warning("upload-watcher: chunk count update failed: %s", _ue)
                    _post_system_message_to_thread(
                        thread_id,
                        f"✓ {filename} is ready. You can ask me about it now.",
                    )
                    return
            # Timed out
            _post_system_message_to_thread(
                thread_id,
                f"⚠ {filename} is still processing — taking longer than expected. "
                f"Check Mobius RAG for status, or try asking in a few minutes.",
            )
        finally:
            _BACKGROUND_WATCHERS.pop(document_id, None)

    t = threading.Thread(target=_watch, name=f"upload-watcher-{document_id[:8]}", daemon=True)
    _BACKGROUND_WATCHERS[document_id] = t
    t.start()
    logger.info(
        "upload-watcher: spawned for doc=%s thread=%s eta=%ds",
        document_id[:8], thread_id[:8], eta_seconds,
    )


def _handle_instant_rag_upload(
    content: bytes, filename: str, org_name: str,
    thread_id: str | None, file_purpose: str,
) -> dict[str, Any]:
    """Forward chat document uploads to mobius-rag's canonical /upload pipeline.

    2026-04-29 rewrite — non-blocking with ETA-based UX path.

    Three paths based on rag's ``estimated_processing_seconds`` response:
      * eta < 120s   → BLOCKING. Wait inline, return when published.
      * eta < 600s   → BACKGROUND. Spawn a watcher thread, return now.
                       System message posts to thread when ready.
      * eta >= 600s  → REDIRECT. Return immediately with a link to the
                       mobius-rag UI; doc is still being processed (the
                       background pipeline runs regardless of UX path),
                       so the user can choose to wait OR redirect.

    Backwards-compat: every field the old response returned is still
    present. New optional fields: ``ux_path``, ``page_count``,
    ``eta_minutes``, ``redirect_url``.
    """
    import json as json_mod
    import uuid as _uuid_mod
    import time as _time
    import urllib.error
    import urllib.request
    from datetime import datetime, timezone

    # Resolution order:
    #   1. MOBIUS_RAG_URL              (new canonical name — points
    #      at the mobius-rag API service)
    #   2. CHAT_SKILLS_INSTANT_RAG_URL (legacy fallback per the
    #      2026-04-27 dispatch — keeps a misconfigured deploy from
    #      hard 503-ing while the env is rotated. We log a warning
    #      when this path is taken so ops sees the misconfig.)
    #   3. http://localhost:8001       (local-dev fallback; rag dev
    #      server defaults to :8001)
    rag_url_env = os.environ.get("MOBIUS_RAG_URL")
    if not rag_url_env:
        rag_url_env = os.environ.get("CHAT_SKILLS_INSTANT_RAG_URL")
        if rag_url_env:
            logger.warning(
                "MOBIUS_RAG_URL unset — falling back to "
                "CHAT_SKILLS_INSTANT_RAG_URL=%s. This is a deploy "
                "misconfig: chat upload now targets mobius-rag's "
                "/upload, not the instant-rag skill.",
                rag_url_env,
            )
    rag_url = (rag_url_env or "http://localhost:8001").rstrip("/")

    # ── Build a multipart/form-data body around the raw bytes. We use
    #    urllib (zero new deps) rather than requests/httpx because the
    #    rest of this module already speaks urllib — staying consistent
    #    keeps the dep graph small and the proxy/retry behaviour uniform.
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    content_type_guess = {
        "pdf": "application/pdf",
        "html": "text/html",
        "htm": "text/html",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "txt": "text/plain",
        "csv": "text/csv",
        "md": "text/markdown",
    }.get(ext, "application/octet-stream")

    boundary = f"----mobiuschat{_uuid_mod.uuid4().hex}"
    body_parts: list[bytes] = []
    body_parts.append(f"--{boundary}\r\n".encode())
    body_parts.append(
        (
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\n'
        ).encode()
    )
    body_parts.append(f"Content-Type: {content_type_guess}\r\n\r\n".encode())
    body_parts.append(content)
    body_parts.append(f"\r\n--{boundary}--\r\n".encode())
    multipart_body = b"".join(body_parts)

    # Query params: ttl_days=7 (chat-uploaded docs are ephemeral),
    # payer=org_name (caller-friendly attribution), agent_scope=chat.
    from urllib.parse import quote_plus
    upload_qs = (
        f"?ttl_days=7"
        f"&payer={quote_plus(org_name or '')}"
        f"&agent_scope=chat"
    )

    try:
        req = urllib.request.Request(
            f"{rag_url}/upload{upload_qs}",
            data=multipart_body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(multipart_body)),
            },
            method="POST",
        )
        # 5 min upload window — large PDFs + GCS round-trip can be slow.
        with urllib.request.urlopen(req, timeout=300) as resp:
            rag_result = json_mod.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            body = ""
        logger.warning("mobius-rag /upload failed: %s — %s", e, body)
        if e.code == 409:
            # Duplicate-hash response from rag — surface the existing
            # document_id so the chat can use the prior copy without
            # forcing the user to find/dedupe themselves. Returns a
            # success-shaped dict (ux_path=duplicate) rather than
            # raising, per the 2026-04-29 upload spec.
            try:
                detail = json_mod.loads(body or "{}").get("detail") or {}
            except Exception:
                detail = {}
            existing_doc_id = (detail.get("document_id") or "").strip()
            if existing_doc_id:
                tid_dup = (thread_id or "").strip() or str(_uuid_mod.uuid4())
                return {
                    "upload_id": existing_doc_id,
                    "org_id": "",
                    "org_name": org_name,
                    "row_count": int(detail.get("chunks_count") or 0),
                    "thread_id": tid_dup,
                    "file_purpose": file_purpose,
                    "filename": filename,
                    "envelope_id": existing_doc_id,
                    "document_id": existing_doc_id,
                    "verification_tier": "rag",
                    "status": "ready",
                    "chunks_count": int(detail.get("chunks_count") or 0),
                    "message": "This file was already uploaded — using the existing copy.",
                    "published_at": detail.get("published_at"),
                    "ux_path": "duplicate",
                    "page_count": int(detail.get("page_count") or 1),
                    "eta_minutes": 0,
                    "eta_seconds": 0,
                    "original_filename": detail.get("original_filename") or filename,
                }
            # Couldn't parse the duplicate detail — fall through to the
            # legacy 409 raise so the caller surfaces a real error.
            raise HTTPException(
                status_code=409,
                detail=f"Duplicate file (already in rag corpus): {body or str(e)}",
            )
        if e.code == 413:
            raise HTTPException(
                status_code=413,
                detail=f"File too large for rag upload: {body or str(e)}",
            )
        raise HTTPException(
            status_code=502,
            detail=f"mobius-rag /upload failed ({e.code}): {body or str(e)[:200]}",
        )
    except Exception as e:
        logger.warning("mobius-rag /upload failed: %s", e)
        raise HTTPException(
            status_code=502,
            detail=f"mobius-rag /upload failed: {str(e)[:200]}",
        )

    document_id = rag_result.get("document_id")
    if not document_id:
        raise HTTPException(
            status_code=502,
            detail=f"mobius-rag /upload returned no document_id: {rag_result}",
        )

    # ── UX path selection ──────────────────────────────────────────
    # All new uploads use the background path. Blocking inline (eta<120s)
    # was removed because it caused the Cloud Run LB to drop client
    # connections after 60s idle (RAG inline fast-path + chat status poll
    # combined to exceed the LB idle timeout). The background watcher
    # already handles the full notification flow (system message + chunk
    # count flip) — the UX is equivalent and avoids connection drops.
    #
    # REDIRECT path retained for very large files (eta≥600s) where we
    # can't even guarantee the watcher will finish within a session.
    page_count = int(rag_result.get("page_count") or 1)
    eta_seconds = int(rag_result.get("estimated_processing_seconds") or 60)
    eta_minutes = max(1, eta_seconds // 60)

    final_status: dict[str, Any] = {}
    ux_path = "blocking"
    redirect_url: str | None = None

    if eta_seconds < 600:
        # BACKGROUND — return immediately; watcher posts system message when ready.
        ux_path = "background"
        _spawn_background_publish_watcher(
            rag_url=rag_url,
            document_id=str(document_id),
            thread_id=(thread_id or ""),
            filename=filename,
            eta_seconds=eta_seconds,
        )
    else:
        # REDIRECT — too large; surface rag UI link.
        ux_path = "redirect"
        rag_ui_url = (os.environ.get("MOBIUS_RAG_UI_URL") or rag_url).rstrip("/")
        redirect_url = f"{rag_ui_url}/?prefill=true&filename={quote_plus(filename)}"
        _spawn_background_publish_watcher(
            rag_url=rag_url,
            document_id=str(document_id),
            thread_id=(thread_id or ""),
            filename=filename,
            eta_seconds=eta_seconds,
        )

    rag_result_status = (final_status.get("status") or rag_result.get("status") or "uploaded")
    chunks_count = int(final_status.get("chunks_count") or rag_result.get("chunks_count") or 0)

    # Save to thread state (same pattern as roster). The new flow has
    # no envelope_id concept (instant-rag invented it; mobius-rag
    # speaks document_id). We synthesize one from document_id so
    # downstream consumers (catalog + thread state) keep the same
    # key shape and the frontend doesn't have to special-case.
    tid = (thread_id or "").strip() or str(_uuid_mod.uuid4())
    upload_id = str(document_id)
    envelope_id = str(document_id)
    record: dict[str, Any] = {
        "upload_id": upload_id,
        "org_id": "",
        "org_name": org_name,
        "purpose": file_purpose,
        "filename": filename,
        "row_count": chunks_count,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "envelope_id": envelope_id,
        "document_id": document_id,
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
            document_id=str(document_id),
            envelope_id=envelope_id,
            upload_id=upload_id,
            thread_id=real_tid,
            filename=filename,
            user_id=None,  # Phase 1h: user_id is None in auth=off (dev); wire when auth=required
            content_type=None,
            byte_size=len(content) if content else None,
            chunks_count=chunks_count,
        )
    except Exception as _e:
        logger.warning("[catalog] dual-write failed for thread=%s: %s", real_tid, _e)

    # Caller-shape compatibility: keep every key the previous
    # this function has historically returned. ``status`` flips to
    # "ready" once the doc is published end-to-end (chunks live in
    # rag_published_embeddings); otherwise we surface whatever the
    # rag pipeline reports so the frontend can show a meaningful
    # state. ``verification_tier`` stayed "instant" before; now we
    # return "rag" so downstream consumers can distinguish.
    is_ready = (
        (final_status.get("status") or "").lower() == "completed"
        and bool(final_status.get("published_at"))
    )

    # Per-ux-path status + message tuning. ``status`` collapses to
    # "ready" or "processing" for the FE; the legacy ``rag_result_status``
    # is preserved as ``rag_status`` for diagnostics.
    if ux_path == "blocking":
        status_out = "ready" if is_ready else "processing"
        message_out = (
            f"{filename} is ready."
            if is_ready
            else f"{filename} is taking longer than expected. It will become available shortly."
        )
    elif ux_path == "background":
        status_out = "processing"
        message_out = (
            f"Uploading {filename} ({page_count} pages, ~{eta_minutes} min). "
            f"I'll let you know when it's ready."
        )
    else:  # redirect
        status_out = "processing"
        message_out = (
            f"{filename} is a {page_count}-page document — processing it in chat will take "
            f"~{eta_minutes} min and lock the conversation. Recommend opening Mobius RAG to "
            f"upload there (it'll be available across all chats)."
        )

    response: dict[str, Any] = {
        # Legacy fields — every key the old return shape had, preserved
        # so existing callers (FE, react_loop, tests) keep working.
        "upload_id": upload_id,
        "org_id": "",
        "org_name": org_name,
        "row_count": chunks_count,
        "thread_id": tid,
        "file_purpose": file_purpose,
        "filename": filename,
        "envelope_id": envelope_id,
        "document_id": str(document_id),
        "verification_tier": "rag",
        "status": status_out,
        "chunks_count": chunks_count,
        "message": message_out,
        "published_at": final_status.get("published_at"),
        # New fields (2026-04-29) — UX-path metadata for the FE.
        "ux_path": ux_path,
        "page_count": page_count,
        "eta_minutes": eta_minutes,
        "eta_seconds": eta_seconds,
        "rag_status": rag_result_status,
    }
    if redirect_url:
        response["redirect_url"] = redirect_url
    return response


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
        # 2026-04-27 consolidation: route document uploads through
        # mobius-rag's canonical /upload pipeline (was instant-rag
        # skill, which bypassed lexicon expansion + hybrid retrieval
        # + rerank). The `instant_rag` purpose name is kept as a
        # back-compat alias so the frontend doesn't change.
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


@app.get("/api/v1/public-config")
def get_public_config():
    """Public, unauthenticated config consumed by the frontend on boot.

    Only safe-to-expose values: OAuth client IDs (designed to be public), feature flags.
    Never put secrets here.
    """
    return {
        "google_client_id": (os.getenv("GOOGLE_CLIENT_ID") or "").strip() or None,
    }


# Auth proxy: forward /api/v1/auth/* to MOBIUS_OS_AUTH_URL.
# The chat frontend's AuthService is configured with apiBase=window.origin/api/v1,
# so register/login/refresh/logout/me/google/check-email/preferences all hit chat
# first. Chat doesn't host auth itself — mobius-os does. This thin proxy is the
# bridge promised by the "proxy /api/v1/auth/* to Mobius-OS" comment in config.py.
from fastapi import Request
from fastapi.responses import Response as FastAPIResponse


@app.api_route(
    "/api/v1/auth/{auth_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy_auth(auth_path: str, request: Request):
    base = (os.getenv("MOBIUS_OS_AUTH_URL") or "").rstrip("/")
    if not base or "not-yet-deployed" in base:
        return FastAPIResponse(
            content='{"error":"auth not configured (MOBIUS_OS_AUTH_URL unset)"}',
            status_code=503,
            media_type="application/json",
        )
    target = f"{base}/api/v1/auth/{auth_path}"
    body = await request.body()

    forward_headers = {}
    for h in ("authorization", "content-type", "user-agent"):
        v = request.headers.get(h)
        if v:
            forward_headers[h] = v

    import httpx  # local import; chat already pulls httpx in for outbound calls
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            up = await client.request(
                request.method,
                target,
                content=body if body else None,
                headers=forward_headers,
                params=dict(request.query_params),
            )
    except httpx.RequestError as exc:
        return FastAPIResponse(
            content=f'{{"error":"auth upstream unreachable: {type(exc).__name__}"}}',
            status_code=502,
            media_type="application/json",
        )

    response_headers = {}
    ct = up.headers.get("content-type")
    if ct:
        response_headers["content-type"] = ct
    return FastAPIResponse(
        content=up.content,
        status_code=up.status_code,
        headers=response_headers,
    )


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
from app.api.auth_proxy import router as _auth_proxy_router
from app.api.chat import router as _chat_router
from app.api.credentialing import router as _credentialing_router
from app.api.doc_reader import router as _doc_reader_router
from app.api.email_thread import router as _email_thread_router
from app.api.feedback import router as _feedback_router
from app.api.history import router as _history_router
from app.api.tasks import router as _tasks_router
from app.api.uploads import router as _uploads_router
from app.api.user_tools import router as _user_tools_router
app.include_router(_chat_router)  # Phase 2b.2 — core chat lifecycle extracted from main.py
app.include_router(_credentialing_router)  # credentialing-runs + NPI lookup (restored for pipeline UI)
app.include_router(_history_router)
app.include_router(_feedback_router)
app.include_router(_tasks_router)
app.include_router(_uploads_router)  # Phase B.1c — cross-thread uploads catalog
app.include_router(_doc_reader_router)  # Phase 2b.1 — doc-reader proxy extracted from main.py
app.include_router(_email_thread_router)  # POST /chat/thread/{id}/email — proxy to mobius-skills/email
app.include_router(_admin_router)  # Dev-token minter + future ops-only endpoints
app.include_router(_auth_proxy_router)  # 2026-05-06 — /api/v1/auth/* + /api/v1/public-config → mobius-user
app.include_router(_user_tools_router)  # GET/PUT/DELETE /user/tools — per-user tool policy settings

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
    # mobius-rag corpus_search_agent strategy stages (2026-05-05): the
    # agent's per-strategy LLM synthesis passes route through here so
    # bandit routing + llm_calls analytics cover retrieval reasoning.
    "rag_strategy_a_synth",     # Strategy (a) BM25 cascade synthesis pass
    "rag_strategy_b_synth",     # Strategy (b) Wide-Themes-Narrow synthesis pass
    "rag_strategy_c_validate",  # Strategy (c) LLM→Validate citation generation
    "rag_strategy_d_external",  # Strategy (d) External First synthesis
    # mobius-qa/lexicon-maintenance stages (2026-04-23): curator UI's
    # LLM calls route through here. Same reason as rag — unified
    # bandit + telemetry. Stages cover the lexicon endpoints:
    #   * lexicon_triage : bulk candidate triage (FAST/Flash — fits 60s)
    #   * lexicon_suggest: single-phrase tag placement + candidate revise
    #   * lexicon_from_doc: "suggest tags from this document" flow
    #   * lexicon_analyze: whole-tree health analysis (Pro — big output)
    "lexicon_triage",
    "lexicon_suggest",
    "lexicon_from_doc",
    "lexicon_analyze",
    # mobius-skills/vibe (2026-04-25): one-line vibe responses (toast,
    # empathy, dry observation). Cheap+fast tier via CHEAP_STAGES.
    "vibe",
    # mobius-skills/email (2026-04-25): LLM-drafted subject/body for
    # craft-mode sends. Goes through the bandit so we learn which model
    # produces emails users actually release vs. discard.
    "email_draft",
    # mobius-skills/appeals-agent (2026-05-08): LLM root-cause analysis
    # for denial investigations — edge-case reasoning (dual eligibility,
    # COBRA, stale TPL, etc.) with likelihood scoring.
    "appeals_investigation",
    # mobius-skills/appeals-agent letter pipeline (2026-05-12):
    # 5-agent pipeline — compose → policy/factcheck/denial-sim (parallel) → final.
    # These stages need high output caps (2000–2500 tokens) for full letter text.
    "appeals_compose",      # Agent 1: formal letter structure
    "appeals_policy",       # Agent 2: regulatory citation injection
    "appeals_factcheck",    # Agent 3: factual accuracy check (short output)
    "appeals_denial_sim",   # Agent 4: payor denial simulation (short output)
    "appeals_final",        # Agent 5: final synthesiser — authoritative letter
    "appeals_packet",       # Metadata: docs checklist + next steps (short output)
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

    # 3. Phase 13.7 schema audit — read the cached startup result.
    #    If status != ok, surface as degraded (not a 503) — chat still
    #    works, the rolling thread summary just won't persist. Operators
    #    look at this when sidebar summaries are silent.
    try:
        from app.services.phase_13_7_metrics import schema_audit_status
        audit = schema_audit_status()
        if audit.get("status") == "ok":
            checks["phase_13_7_schema"] = {"status": "ok"}
        else:
            checks["phase_13_7_schema"] = {
                "status": "degraded",
                "audit_status": audit.get("status"),
                "detail": (audit.get("detail") or "")[:200],
            }
            # Intentionally NOT flipping all_ok — Phase 13.7 is a
            # feature, not a critical path. Sidebar summary degrades;
            # nothing else.
    except Exception as e:
        checks["phase_13_7_schema"] = {"status": "error", "error": str(e)[:200]}

    # 4. skills-mcp — degraded (warn) on failure, not 503. Chat can
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

