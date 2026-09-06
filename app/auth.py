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

import logging
import os
from dataclasses import dataclass
from typing import Optional

from app.secrets_loader import get_secret

logger = logging.getLogger(__name__)


def _jwt_secret() -> Optional[str]:
    return get_secret("JWT_SECRET")


def _auth_url() -> Optional[str]:
    v = (os.getenv("MOBIUS_OS_AUTH_URL") or "").strip()
    return v or None


@dataclass(frozen=True)
class TokenCheckResult:
    """Task #108 follow-up (2026-09-06, Chat Master ruling): distinguishes
    "no token was presented" from "a token was presented but failed
    validation" — under ``CHAT_AUTH_MODE=optional`` these two cases used
    to collapse into the same ``None``, so an expired/tampered/malformed
    token from a genuinely logged-in user silently degraded to anonymous
    with no signal anywhere. That's the leading suspect for #108's
    "new thread doesn't show in my sidebar" reports — a user with a
    stale token gets a real answer, but the turn is written with
    ``user_id=NULL`` and never appears in their OWN thread list, with
    nothing in the logs to say why.

    ``failure_reason`` is ``None`` in both the "no token" and "success"
    cases — it is ONLY set when a token was actually present and
    rejected, which is the specific, actionable signal callers care
    about. Values: ``"expired"``, ``"invalid_signature"``, ``"malformed"``,
    ``"wrong_token_type"``, ``"missing_sub_claim"``, or
    ``"decode_error:<ExceptionClassName>"`` for anything unanticipated.
    """
    user_id: Optional[str]
    failure_reason: Optional[str] = None


def get_user_id_from_token(token: str) -> TokenCheckResult:
    """Decode JWT and return a ``TokenCheckResult``.

    ``failure_reason`` stays ``None`` when there's genuinely no token to
    check (missing secret/auth_url config, or an empty token string) —
    that's not a validation failure, it's "auth isn't wired up" or
    "caller didn't send one," neither of which is the user's token going
    bad. It's set only when an actual token was decoded and rejected.
    """
    secret = _jwt_secret()
    if not secret or not _auth_url():
        return TokenCheckResult(None, None)
    if not token or not token.strip():
        return TokenCheckResult(None, None)
    import jwt

    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"verify_exp": True},
        )
    except jwt.ExpiredSignatureError:
        return TokenCheckResult(None, "expired")
    except jwt.InvalidSignatureError:
        return TokenCheckResult(None, "invalid_signature")
    except jwt.DecodeError:
        return TokenCheckResult(None, "malformed")
    except Exception as e:  # pragma: no cover — defensive catch-all
        return TokenCheckResult(None, f"decode_error:{type(e).__name__}")

    if payload.get("type") != "access":
        return TokenCheckResult(None, "wrong_token_type")
    sub = payload.get("sub")
    if not sub:
        return TokenCheckResult(None, "missing_sub_claim")
    return TokenCheckResult(str(sub), None)


def get_user_id_from_request(request) -> TokenCheckResult:
    """Extract Bearer token from request and validate it. See
    ``TokenCheckResult`` for the no-token-vs-failed-token distinction."""
    auth = request.headers.get("Authorization") if hasattr(request, "headers") else None
    if not auth or not auth.startswith("Bearer "):
        return TokenCheckResult(None, None)
    return get_user_id_from_token(auth[7:].strip())
