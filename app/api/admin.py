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

from fastapi import APIRouter, HTTPException
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
