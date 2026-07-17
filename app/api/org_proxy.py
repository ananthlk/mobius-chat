"""Forwards /api/v1/org/* to the Mobius Org Agent service.

The org agent owns employee onboarding and org data; chat proxies its
/api/v1/org/* surface so the frontend stays same-origin.

Route mapping (chat → org agent):
  GET  /api/v1/org/roles                       → GET  {ORG}/roles
  POST /api/v1/org/setup                       → POST {ORG}/org/setup
  GET  /api/v1/org/{slug}                      → GET  {ORG}/org/{slug}
  GET  /api/v1/org/{slug}/employees            → GET  {ORG}/org/{slug}/employees
  POST /api/v1/org/{slug}/employees/invite     → POST {ORG}/org/{slug}/employees/invite
  POST /api/v1/org/{slug}/employees/reinvite   → POST {ORG}/org/{slug}/employees/reinvite
  POST /api/v1/org/{slug}/datastore/provision  → POST {ORG}/org/{slug}/datastore/provision

AUTH:
  - User bearer token is forwarded on every request (org agent validates JWT).
  - Write routes (setup/invite/reinvite/provision) additionally receive
    X-Internal-Key from Secret Manager ``org-agent-internal-key``. The
    frontend never holds this key.
  - Read routes (roles, GET org, employees) require no internal key.

Env vars:
  MOBIUS_ORG_AGENT_URL — base URL of the org agent Cloud Run service.
  MOBIUS_ORG_AGENT_INTERNAL_KEY — override key for local dev (prod reads
      from Secret Manager via secrets_loader).
"""
from __future__ import annotations

import logging
import os

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response as FastAPIResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["org-proxy"])

_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=20.0, pool=5.0)

# Write routes need the org-agent internal key attached by chat backend.
_WRITE_PATHS = {
    "setup",
    "employees/invite",
    "employees/reinvite",
    "datastore/provision",
}


def _org_agent_base() -> str | None:
    return (os.environ.get("MOBIUS_ORG_AGENT_URL") or "").strip().rstrip("/") or None


def _internal_key() -> str | None:
    """Resolve org-agent internal key.

    Dev: MOBIUS_ORG_AGENT_INTERNAL_KEY env var.
    Hosted: Secret Manager ``org-agent-internal-key`` via secrets_loader.
    """
    direct = (os.environ.get("MOBIUS_ORG_AGENT_INTERNAL_KEY") or "").strip()
    if direct:
        return direct
    try:
        from app.secrets_loader import get_secret
        return get_secret("MOBIUS_ORG_AGENT_INTERNAL_KEY")
    except Exception as exc:
        logger.warning("org_proxy: failed to resolve internal key: %s", exc)
        return None


def _is_write_path(sub_path: str) -> bool:
    """True if sub_path (the part after /api/v1/org/) maps to a write route."""
    for write_suffix in _WRITE_PATHS:
        if sub_path == write_suffix or sub_path.endswith(f"/{write_suffix}"):
            return True
    return False


async def _forward(request: Request, upstream_path: str, is_write: bool) -> FastAPIResponse:
    base = _org_agent_base()
    if not base:
        return FastAPIResponse(
            content='{"error":"org agent not configured (MOBIUS_ORG_AGENT_URL unset)"}',
            status_code=503,
            media_type="application/json",
        )

    target = f"{base}/{upstream_path.lstrip('/')}"
    body = await request.body()

    headers: dict[str, str] = {}
    for h in ("authorization", "content-type", "user-agent"):
        v = request.headers.get(h)
        if v:
            headers[h] = v

    if is_write:
        key = _internal_key()
        if key:
            headers["X-Internal-Key"] = key
        else:
            logger.error("org_proxy: write route %s called but internal key is unavailable", upstream_path)
            return FastAPIResponse(
                content='{"error":"org agent internal key not configured"}',
                status_code=503,
                media_type="application/json",
            )

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            up = await client.request(
                request.method,
                target,
                content=body if body else None,
                headers=headers,
                params=dict(request.query_params),
            )
    except httpx.RequestError as exc:
        return FastAPIResponse(
            content=f'{{"error":"org agent unreachable: {type(exc).__name__}"}}',
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


# ── Routes ────────────────────────────────────────────────────────────

@router.get("/api/v1/org/roles")
async def org_roles(request: Request):
    """GET /roles on the org agent (top-level, not /org/roles)."""
    return await _forward(request, "roles", is_write=False)


@router.post("/api/v1/org/setup")
async def org_setup(request: Request):
    """POST /org/setup — create a new org."""
    return await _forward(request, "org/setup", is_write=True)


@router.get("/api/v1/org/{slug}")
async def org_get(slug: str, request: Request):
    """GET /org/{slug} — fetch org details."""
    return await _forward(request, f"org/{slug}", is_write=False)


@router.get("/api/v1/org/{slug}/employees")
async def org_employees(slug: str, request: Request):
    """GET /org/{slug}/employees — list employees."""
    return await _forward(request, f"org/{slug}/employees", is_write=False)


@router.post("/api/v1/org/{slug}/employees/invite")
async def org_invite(slug: str, request: Request):
    """POST /org/{slug}/employees/invite — invite an employee."""
    return await _forward(request, f"org/{slug}/employees/invite", is_write=True)


@router.post("/api/v1/org/{slug}/employees/reinvite")
async def org_reinvite(slug: str, request: Request):
    """POST /org/{slug}/employees/reinvite — resend invite."""
    return await _forward(request, f"org/{slug}/employees/reinvite", is_write=True)


@router.post("/api/v1/org/{slug}/datastore/provision")
async def org_datastore_provision(slug: str, request: Request):
    """POST /org/{slug}/datastore/provision — provision org doc store."""
    return await _forward(request, f"org/{slug}/datastore/provision", is_write=True)
