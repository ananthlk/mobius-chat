"""Forwards /api/v1/auth/* + /api/v1/public-config to the mobius-user service.

Why this exists. mobius-user is the single owner of identity, auth, and user
preferences. Every consuming module (chat, rag, future surfaces) references
users by ``user_id`` (UUID) and never touches the user database directly. The
spec at ``Mobius-user/SPEC.md`` recommends the **proxy pattern** over the
**direct pattern** for hosts that have a backend, because it:

  * keeps the frontend ``apiBase = window.origin/api/v1`` contract that
    ``@mobius/auth`` ships out of the box,
  * means the browser never sees a cross-origin call (no CORS config on
    mobius-user),
  * lets chat stamp its own request IDs / rate limits / audit logs on
    auth traffic without touching mobius-user.

This router is a thin transparent forwarder. It intentionally does NOT:

  * Decode the access token here (already handled by ``app.auth`` for
    routes that ``Depends(require_user)``).
  * Override or enrich responses — the AuthEnvelope shape is the
    mobius-user contract; proxying it untouched keeps the SPEC valid.
  * Add caching — public-config is fetched once at boot by the FE; the
    auth/* surface is per-request.

Routes registered:

  * ``GET/POST/PUT/DELETE /api/v1/auth/{path:path}`` — generic forward.
  * ``GET /api/v1/public-config``                    — frontend bootstrap.

Behavior when ``MOBIUS_OS_AUTH_URL`` is unset / sentinel: routes return
503 with a clear "auth not configured" message rather than 404 / hang on
the invalid host. This protects against half-deployed states where chat
ships before the env-var update.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth-proxy"])


_SENTINEL_HOSTS = (
    "not-yet-deployed.invalid",
    "not-deployed.invalid",
    "localhost",
)


def _upstream_base() -> str | None:
    """Resolved mobius-user base URL, or None when not configured / sentinel.

    Reads ``MOBIUS_OS_AUTH_URL`` (legacy name kept for backwards-compat
    with existing deploys; spec also accepts ``MOBIUS_USER_URL`` /
    ``MOBIUS_AUTH_URL``). First non-empty wins.
    """
    for key in ("MOBIUS_USER_URL", "MOBIUS_AUTH_URL", "MOBIUS_OS_AUTH_URL"):
        v = (os.environ.get(key) or "").strip().rstrip("/")
        if not v:
            continue
        # Filter out sentinel placeholders that signal "auth disabled"
        # rather than "auth at this URL".
        if any(s in v for s in _SENTINEL_HOSTS):
            continue
        return v
    return None


# Single shared client. Connection-pooled, keepalive on. mobius-user runs on
# Cloud Run with HTTP/2; httpx negotiates that automatically.
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)
_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=_HTTP_TIMEOUT, http2=False)
    return _client


# Headers we forward verbatim. Most importantly Authorization (Bearer
# token) and Content-Type. Hop-by-hop headers (Host, Connection, etc.)
# are stripped — httpx + Cloud Run handle those.
_HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade",
}


def _filter_request_headers(req: Request) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in req.headers.items():
        if k.lower() in _HOP_BY_HOP:
            continue
        out[k] = v
    return out


def _filter_response_headers(headers: httpx.Headers) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in _HOP_BY_HOP:
            continue
        # Don't leak upstream-set CORS headers — chat is the origin browsers
        # see, so CORS is handled by chat's own middleware (or not needed
        # at all, since the proxy serves same-origin).
        if k.lower().startswith("access-control-"):
            continue
        out[k] = v
    return out


@router.api_route(
    "/api/v1/auth/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def proxy_auth(path: str, request: Request) -> Any:
    """Forward every /api/v1/auth/* call to mobius-user.

    Body, headers, query string forwarded verbatim. Response status,
    body, and headers (minus hop-by-hop / CORS) returned untouched.
    """
    base = _upstream_base()
    if not base:
        logger.warning("auth-proxy: MOBIUS_USER_URL/MOBIUS_OS_AUTH_URL unset; rejecting auth call /%s", path)
        raise HTTPException(
            status_code=503,
            detail="Auth service not configured. Set MOBIUS_USER_URL on this deployment.",
        )

    target = f"{base}/api/v1/auth/{path}"
    body = await request.body()
    headers = _filter_request_headers(request)
    qs = request.url.query

    try:
        upstream = _get_client().request(
            method=request.method,
            url=target,
            params=qs if qs else None,
            content=body if body else None,
            headers=headers,
        )
    except httpx.HTTPError as e:
        logger.warning("auth-proxy: upstream call failed for /api/v1/auth/%s: %s", path, e)
        raise HTTPException(
            status_code=502,
            detail=f"Auth upstream unreachable ({type(e).__name__}).",
        )

    from fastapi.responses import Response
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=_filter_response_headers(upstream.headers),
        media_type=upstream.headers.get("content-type"),
    )


@router.get("/api/v1/public-config")
def proxy_public_config() -> Any:
    """Forward to mobius-user's public-config (Google client ID, future flags).

    Frontend fetches this at boot to learn the Google client ID for the
    Sign-in-with-Google button. Spec §8 — single source of truth lives
    on mobius-user; chat could synthesize from its own GOOGLE_CLIENT_ID
    env, but proxying keeps drift impossible.

    When mobius-user is unconfigured we return ``{"google_client_id": null}``
    rather than 503 — the frontend treats null as "no Google sign-in"
    and falls back to email/password gracefully.
    """
    base = _upstream_base()
    if not base:
        return {"google_client_id": None, "note": "auth_url_unset"}

    try:
        upstream = _get_client().get(
            f"{base}/api/v1/public-config",
            timeout=httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=3.0),
        )
    except httpx.HTTPError as e:
        logger.warning("auth-proxy: public-config upstream failed: %s", e)
        return {"google_client_id": None, "note": f"upstream_error:{type(e).__name__}"}

    if upstream.status_code != 200:
        logger.warning("auth-proxy: public-config upstream HTTP %d", upstream.status_code)
        return {"google_client_id": None, "note": f"upstream_http_{upstream.status_code}"}

    try:
        return upstream.json()
    except Exception as e:
        logger.warning("auth-proxy: public-config upstream non-JSON: %s", e)
        return {"google_client_id": None, "note": "upstream_non_json"}
