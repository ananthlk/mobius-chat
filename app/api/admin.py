"""Admin + dev-only endpoints.

Router for operational utilities that don't belong on the public chat
surface. Every endpoint here is gated behind an explicit env flag
(``MOBIUS_DEV_TOKEN_ENABLED``, etc.) so a production deploy without
those flags set has zero admin surface exposed.

Currently hosts:
  * ``POST /chat/admin/mint-dev-token`` — mint a short-lived JWT
    signed with the shared ``JWT_SECRET``. Lets bench harnesses and
    local dev exercise the authed path without a running mobius-os
    service. When mobius-os actually deploys, flip
    ``MOBIUS_DEV_TOKEN_ENABLED=0`` and point clients at mobius-os's
    real login flow — no chat code change needed.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.secrets_loader import get_secret

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])


# ── Feature gate ────────────────────────────────────────────────────────


def _dev_token_enabled() -> bool:
    """Master kill switch. Default OFF — explicitly opt in with
    ``MOBIUS_DEV_TOKEN_ENABLED=1`` in the env. Production deploys must
    keep this unset / 0."""
    raw = (os.environ.get("MOBIUS_DEV_TOKEN_ENABLED") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _dev_token_ttl_seconds() -> int:
    """TTL for minted dev tokens. Capped at 1 day so forgotten tokens
    can't outlive a dev session by much. Default: 1 hour."""
    try:
        ttl = int((os.environ.get("MOBIUS_DEV_TOKEN_TTL_SECONDS") or "3600").strip())
        return max(60, min(86400, ttl))   # clamp [1 minute, 1 day]
    except (TypeError, ValueError):
        return 3600


# ── Request / response shapes ──────────────────────────────────────────


class MintDevTokenRequest(BaseModel):
    """Minimal inputs. All optional — defaults give you a unique user
    per call (UUID4) in the default tenant."""
    user_id: str | None = None
    tenant_id: str | None = None
    ttl_seconds: int | None = None
    """Override the module-default TTL. Clamped identically."""


class MintDevTokenResponse(BaseModel):
    access_token: str
    user_id: str
    tenant_id: str
    expires_at: str   # ISO-8601 UTC
    ttl_seconds: int
    warning: str


# ── Route ──────────────────────────────────────────────────────────────


# Default tenant matches mobius-os's DEFAULT_TENANT_ID so tokens minted
# here are interchangeable with mobius-os-issued tokens when we point
# at the real auth service later.
_DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"


@router.post("/chat/admin/mint-dev-token", response_model=MintDevTokenResponse)
def mint_dev_token(body: MintDevTokenRequest) -> MintDevTokenResponse:
    """Mint a short-lived HS256 JWT signed with the chat ``JWT_SECRET``.

    Payload format matches mobius-os ``create_access_token`` exactly so
    tokens are validated by the same ``app.auth.get_user_id_from_token``
    code path that'd validate a real mobius-os token.

    Gated by ``MOBIUS_DEV_TOKEN_ENABLED``. Returns 404 (not 403) when
    disabled so the endpoint looks non-existent to attackers.
    """
    if not _dev_token_enabled():
        raise HTTPException(status_code=404, detail="Not found")

    secret = get_secret("JWT_SECRET")
    if not secret:
        # Fail loud here — the dev-token endpoint without a secret is
        # actively misleading (any token it mints wouldn't validate).
        logger.error("mint_dev_token: JWT_SECRET missing")
        raise HTTPException(
            status_code=500,
            detail="JWT_SECRET not configured — check Secret Manager / env.",
        )

    user_id = (body.user_id or "").strip() or str(uuid.uuid4())
    tenant_id = (body.tenant_id or "").strip() or _DEFAULT_TENANT
    ttl = body.ttl_seconds if body.ttl_seconds is not None else _dev_token_ttl_seconds()
    ttl = max(60, min(86400, int(ttl)))

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)

    import jwt  # lazy — PyJWT already a direct dep
    payload = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "exp": expires_at,
        "type": "access",
    }
    token = jwt.encode(payload, secret, algorithm="HS256")

    logger.info(
        "mint_dev_token: minted user_id=%s tenant=%s ttl=%ds",
        user_id[:8], tenant_id[:8], ttl,
    )

    return MintDevTokenResponse(
        access_token=token,
        user_id=user_id,
        tenant_id=tenant_id,
        expires_at=expires_at.isoformat().replace("+00:00", "Z"),
        ttl_seconds=ttl,
        warning=(
            "Dev-only token. Do NOT use in production. "
            "Disable by unsetting MOBIUS_DEV_TOKEN_ENABLED."
        ),
    )


# ── Model profile (Sprint 2 #0, 2026-04-24) ───────────────────────────
#
# Runtime switch for model_registry's per-stage pinning. See
# app/services/model_profile.py for the full contract.
#
# Gated by ``MOBIUS_ADMIN_ENABLED`` (default follows
# MOBIUS_DEV_TOKEN_ENABLED — same audience: dev + demo operators).
# Disabled endpoints return 404 so they don't fingerprint as existing
# in production environments.


def _admin_enabled() -> bool:
    """Admin surface uses a dedicated env flag that *defaults* to the
    dev-token flag. This lets ops leave dev-token minting off while
    keeping the model-profile toggle available for demo operators,
    without shipping a new release."""
    raw = (os.environ.get("MOBIUS_ADMIN_ENABLED") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    # Default: mirror the dev-token gate.
    return _dev_token_enabled()


class ModelProfileState(BaseModel):
    active_profile: str
    override_set: bool
    available_profiles: list[str]


class SetModelProfileRequest(BaseModel):
    # ``None`` clears the override and reverts to env/default.
    profile: str | None = None


@router.get("/chat/admin/model-profile", response_model=ModelProfileState)
def get_model_profile() -> ModelProfileState:
    """Report the currently-active model profile + the set of
    available profile names from ``config/model_profiles.yaml``.
    No-auth, no body — operators use this to check state before
    flipping it.
    """
    if not _admin_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    from app.services.model_profile import _load, get_active_profile_name
    profiles = _load()
    return ModelProfileState(
        active_profile=get_active_profile_name(),
        override_set=bool(
            __import__("app.services.model_profile", fromlist=["_ACTIVE_PROFILE_OVERRIDE"])
            ._ACTIVE_PROFILE_OVERRIDE
        ),
        available_profiles=sorted(profiles.keys()),
    )


@router.post("/chat/admin/model-profile", response_model=ModelProfileState)
def set_model_profile(body: SetModelProfileRequest) -> ModelProfileState:
    """Switch the active model profile at runtime. Pass ``null`` to
    clear the override and revert to ``MOBIUS_MODEL_PROFILE`` (or
    ``default`` when that's unset).

    Single-instance dev (``minScale=1``) sees the change on the very
    next request. Multi-instance deployments will need a
    Postgres-backed config (tracked for Sprint 2 after the worker
    split lands) — until then, each instance has its own override.
    """
    if not _admin_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    from app.services.model_profile import set_active_profile
    try:
        state = set_active_profile(body.profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    logger.info("model_profile admin switch: active=%s", state["active_profile"])
    return ModelProfileState(**state)


@router.get("/chat/admin/model-health")
def get_model_health() -> dict:
    """Snapshot of the live-health detector — degraded models, recent
    call windows, time-to-next-probe.

    Per Cloud Run instance: each instance learns independently. Hitting
    this endpoint multiple times can return different snapshots
    depending on which instance the LB picks. That's intentional — a
    healthy aggregate view requires the cross-instance Redis broadcast
    we haven't built yet (tracked separately).

    Use this during testing/incident triage to see which models the
    bandit is currently routing around and why.
    """
    if not _admin_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    out: dict = {}
    # Postgres-backed (canonical, cross-instance via model_health_recent view)
    try:
        from app.services.llm_health import LIVE_HEALTH as _PG_LIVE_HEALTH
        out["postgres"] = _PG_LIVE_HEALTH.snapshot()
    except Exception as exc:
        out["postgres"] = {"error": f"{type(exc).__name__}: {exc}"}
    # Per-instance in-memory (fallback layer)
    try:
        from app.services.model_registry import _LIVE_HEALTH
        out["local"] = _LIVE_HEALTH.snapshot()
    except Exception as exc:
        out["local"] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


# ── Per-query dump dashboard (2026-05-05) ──────────────────────────────
#
# Flat dump of recent chat_turns joined with llm_calls aggregates,
# retrieval_runs aggregates, and chat_feedback. Pre-users phase: a
# simple "show me what's happening" view, no charts. JSON by default;
# ``?format=html`` renders a server-side table for browser viewing.
#
# Same admin gate as the model-profile/health endpoints.


def _parse_since(raw: str | None) -> datetime | None:
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        # accept ISO-8601 with or without trailing Z
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"since must be ISO-8601 (got {raw!r})",
        )


@router.post("/chat/admin/backfill-phi-classify")
def backfill_phi_classify(
    limit: int = Query(20, ge=1, le=200, description="Max rows to process per call"),
    dry_run: bool = Query(False, description="List candidates without firing classify"),
    reclassify_errors: bool = Query(False, description="Re-classify rows with layers_run=[\"error\"] instead of unclassified"),
    reclassify_conservative: bool = Query(False, description="Re-classify rows with phi_flag=true AND layers_run=[] (burst-storm conservative-failure repair)"),
    delay_ms: int = Query(800, ge=0, le=5000, description="Delay between sequential calls (ms)"),
):
    """Fire §3.3 PHI classify for instant_rag_uploads rows, SEQUENTIALLY with retry+backoff.

    Default mode: rows where classified_at IS NULL.
    reclassify_errors=true: rows where layers_run contains 'error' (transient-503 damage repair).
    reclassify_conservative=true: rows with phi_flag=true AND layers_run=[] (burst-storm conservative-failure repair).

    Processes one doc at a time with delay_ms between calls — never floods the classifier.
    Retries 5xx responses up to 3× with exponential backoff before giving up on a row.
    Only stores the conservative 'private' fallback after all retries exhaust (not on first 5xx).
    Runs in a single background thread so the HTTP response returns immediately.
    """
    import os as _os
    import threading as _threading
    import json as _json
    import time as _time
    import urllib.request as _urlreq
    import urllib.error as _urlerr
    from app.db_client import db_query as _dbq, db_execute as _dbe

    rag_url = (_os.environ.get("MOBIUS_RAG_URL") or "").rstrip("/")
    if not rag_url:
        raise HTTPException(status_code=503, detail="MOBIUS_RAG_URL not set")
    phi_url = (_os.environ.get("PHI_CLASSIFIER_URL") or "").rstrip("/")
    if not phi_url:
        raise HTTPException(status_code=503, detail="PHI_CLASSIFIER_URL not set — classify is a no-op")

    if reclassify_conservative:
        # Target burst-storm conservative failures: phi=true stamped without any layers running
        sql = (
            "SELECT document_id FROM instant_rag_uploads "
            "WHERE phi_flag = true AND layers_run = '[]'::jsonb "
            "AND document_id IS NOT NULL LIMIT :lim"
        )
    elif reclassify_errors:
        sql = (
            "SELECT document_id FROM instant_rag_uploads "
            "WHERE classified_at IS NOT NULL AND layers_run::text LIKE '%error%' "
            "AND document_id IS NOT NULL LIMIT :lim"
        )
    else:
        sql = (
            "SELECT document_id FROM instant_rag_uploads "
            "WHERE classified_at IS NULL AND document_id IS NOT NULL LIMIT :lim"
        )
    result = _dbq(sql, "chat", params={"lim": limit})
    cols = result.get("columns") or []
    doc_ids = [
        row_dict["document_id"]
        for row_dict in (dict(zip(cols, r)) for r in (result.get("rows") or []))
        if row_dict.get("document_id")
    ]
    if dry_run:
        return {"dry_run": True, "candidates": len(doc_ids), "document_ids": doc_ids}

    def _classify_sequential() -> None:
        for did in doc_ids:
            # Fetch text from RAG.
            try:
                with _urlreq.urlopen(f"{rag_url}/documents/{did}/pages", timeout=30) as _r:
                    pages = _json.loads(_r.read()).get("pages") or []
                # Truncate to 8000 chars — classifier only needs enough to detect PHI,
                # and very large docs (100+ pages) cause LLM-layer timeouts.
                text = "\n".join((p.get("text") or "") for p in pages).strip()[:8000]
            except Exception:
                text = ""

            # Call classifier with up to 3 retries + exponential backoff on 5xx/network.
            verdict: dict = {}
            for attempt in range(3):
                try:
                    _req = _urlreq.Request(
                        f"{phi_url}/classify",
                        data=_json.dumps({"text": text, "document_id": did}).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with _urlreq.urlopen(_req, timeout=45) as _r:
                        verdict = _json.loads(_r.read())
                    break  # success
                except _urlerr.HTTPError as _he:
                    if _he.code in (500, 502, 503, 504) and attempt < 2:
                        _time.sleep(2 ** attempt * 3)  # 3s, 6s
                        continue
                    logger.warning("backfill-phi: classify %s failed after %d tries: %s", did[:8], attempt + 1, _he)
                    verdict = {}
                    break
                except Exception as _e:
                    if attempt < 2:
                        _time.sleep(2 ** attempt * 3)
                        continue
                    logger.warning("backfill-phi: classify %s failed: %s", did[:8], _e)
                    verdict = {}
                    break

            # Only write the conservative fallback if retries exhausted AND verdict empty.
            # If verdict is populated, write the real result.
            if not verdict:
                logger.warning("backfill-phi: skipping store for %s — all retries failed, leaving row for next pass", did[:8])
                _time.sleep(delay_ms / 1000.0)
                continue

            ceiling = verdict.get("recommended_ceiling") or "private"
            if ceiling not in ("private", "org", "public"):
                ceiling = "private"
            try:
                _dbe(
                    """UPDATE instant_rag_uploads SET
                        suggested_visibility=:c, phi_flag=:pf, phi_evidence=:pe::jsonb,
                        identifiers_found=:ids::jsonb, classifier_confidence=:conf,
                        classifier_version=:ver, layers_run=:lr::jsonb, classified_at=now()
                       WHERE document_id=:did""",
                    "chat",
                    params={
                        "c": ceiling, "pf": bool(verdict.get("phi_flag", True)),
                        "pe": _json.dumps(verdict.get("phi_evidence") or []),
                        "ids": _json.dumps(verdict.get("identifiers_found") or []),
                        "conf": verdict.get("confidence"),
                        "ver": verdict.get("classifier_version") or "",
                        "lr": _json.dumps(verdict.get("layers_run") or []),
                        "did": did,
                    },
                )
                logger.info("backfill-phi: stored for %s phi=%s ceiling=%s", did[:8], verdict.get("phi_flag"), ceiling)
            except Exception as _e:
                logger.warning("backfill-phi: store failed for %s: %s", did[:8], _e)

            _time.sleep(delay_ms / 1000.0)

    _threading.Thread(target=_classify_sequential, name="backfill-phi-sequential", daemon=True).start()
    mode = "reclassify_conservative" if reclassify_conservative else ("reclassify_errors" if reclassify_errors else "unclassified")
    return {"queued": len(doc_ids), "mode": mode, "delay_ms": delay_ms, "document_ids": doc_ids}


@router.get("/chat/admin/queries")
def get_queries_dump(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    since: str | None = Query(None, description="ISO-8601 timestamp"),
    user_id: str | None = Query(None),
    has_feedback: bool | None = Query(None),
    has_error: bool | None = Query(None),
    format: str = Query("json", pattern="^(json|html)$"),
):
    """Per-turn dump for ad-hoc inspection.

    Default JSON shape::

        {"rows": [...], "count": N, "warning": str | None}

    Browser-friendly HTML rendering: append ``?format=html``.
    """
    if not _admin_enabled():
        raise HTTPException(status_code=404, detail="Not found")

    from app.storage.queries_dump import fetch_query_dump

    result = fetch_query_dump(
        limit=limit,
        offset=offset,
        since=_parse_since(since),
        user_id=user_id,
        has_feedback=has_feedback,
        has_error=has_error,
    )

    if format == "html":
        return HTMLResponse(_render_queries_html(result, limit, offset))
    return result


def _render_queries_html(result: dict, limit: int, offset: int) -> str:
    """Minimal server-rendered table. No JS, no styling framework — just
    a readable monospace dump good enough for the pre-users phase."""
    import html as _html

    rows = result.get("rows") or []
    warning = result.get("warning")

    cols = [
        ("created_at",          "time"),
        ("user_id",             "user"),
        ("thread_id",           "thread"),
        ("question_preview",    "question"),
        ("total_latency_ms",    "ms"),
        ("llm_call_count",      "llm calls"),
        ("input_tokens",        "in tok"),
        ("output_tokens",       "out tok"),
        ("cost_usd",            "$"),
        ("models_used",         "models"),
        ("llm_error_count",     "errs"),
        ("last_error_type",     "err type"),
        ("retrieval_runs_count", "rag runs"),
        ("chunks_assembled",    "chunks"),
        ("cache_mode",          "cache"),
        ("cache_top_similarity", "cache sim"),
        ("feedback_rating",     "fb"),
        ("feedback_comment",    "fb comment"),
    ]

    def _fmt(v):
        if v is None:
            return ""
        if isinstance(v, float):
            return f"{v:.4f}".rstrip("0").rstrip(".")
        return _html.escape(str(v))

    head = "".join(f"<th>{_html.escape(label)}</th>" for _, label in cols)
    body_rows = []
    for r in rows:
        cells = "".join(f"<td>{_fmt(r.get(key))}</td>" for key, _ in cols)
        body_rows.append(f"<tr>{cells}</tr>")
    body = "\n".join(body_rows)

    warn_html = ""
    if warning:
        warn_html = (
            f'<div style="background:#fee;border:1px solid #c00;padding:8px;'
            f'margin:8px 0;color:#900">DB warning: {_html.escape(warning)}</div>'
        )

    next_offset = offset + limit
    prev_offset = max(0, offset - limit)
    nav = (
        f'<div style="margin:8px 0">'
        f'showing rows {offset+1}–{offset+len(rows)} (limit={limit}) &nbsp;'
        f'<a href="?limit={limit}&offset={prev_offset}&format=html">« prev</a> &nbsp;'
        f'<a href="?limit={limit}&offset={next_offset}&format=html">next »</a>'
        f'</div>'
    )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>chat queries dump</title>
<style>
  body {{ font-family: ui-monospace, Menlo, monospace; font-size: 12px; margin: 16px; }}
  h1 {{ font-size: 14px; margin: 0 0 8px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ddd; padding: 4px 6px; text-align: left;
            vertical-align: top; max-width: 360px; overflow: hidden;
            text-overflow: ellipsis; white-space: nowrap; }}
  th {{ background: #f4f4f4; position: sticky; top: 0; }}
  tr:hover td {{ background: #fafafa; }}
  td:hover {{ white-space: normal; word-break: break-word; max-width: 600px; }}
</style></head>
<body>
<h1>/chat/admin/queries — {len(rows)} rows</h1>
{warn_html}
{nav}
<table><thead><tr>{head}</tr></thead><tbody>
{body}
</tbody></table>
{nav}
</body></html>"""
