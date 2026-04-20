"""Optional auth: validate JWT when proxying to Mobius-OS.

``JWT_SECRET`` must match the secret mobius-os / mobius-user sign with.
In hosted envs the value comes from Secret Manager (secret name
``jwt-secret``, shared across services); in dev it comes from ``.env``
via the same ``secrets_loader`` abstraction.

Module-level reads removed 2026-04-20 — they ran at import time, which
meant ``monkeypatch.setenv('JWT_SECRET', ...)`` in tests had no effect.
Lazy reads also let Secret Manager values arrive slightly after import
without breaking boot.
"""
from __future__ import annotations

import os
from typing import Optional

from app.secrets_loader import get_secret


def _jwt_secret() -> Optional[str]:
    return get_secret("JWT_SECRET")


def _auth_url() -> Optional[str]:
    v = (os.getenv("MOBIUS_OS_AUTH_URL") or "").strip()
    return v or None


def get_user_id_from_token(token: str) -> Optional[str]:
    """Decode JWT and return user_id (sub) if valid. Returns None if no secret or invalid token."""
    secret = _jwt_secret()
    if not secret or not _auth_url():
        return None
    if not token or not token.strip():
        return None
    try:
        import jwt
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"verify_exp": True},
        )
        if payload.get("type") != "access":
            return None
        sub = payload.get("sub")
        return str(sub) if sub else None
    except Exception:
        return None


def get_user_id_from_request(request) -> Optional[str]:
    """Extract Bearer token from request and return user_id if valid."""
    auth = request.headers.get("Authorization") if hasattr(request, "headers") else None
    if not auth or not auth.startswith("Bearer "):
        return None
    return get_user_id_from_token(auth[7:].strip())
