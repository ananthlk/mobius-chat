"""Optional auth: validate JWT when proxying to Mobius-OS (JWT_SECRET must match Mobius-OS)."""
import os
from typing import Optional

_jwt_secret = os.getenv("JWT_SECRET")
_mobius_os_auth_url = os.getenv("MOBIUS_OS_AUTH_URL")


def get_user_id_from_token(token: str) -> Optional[str]:
    """Decode JWT and return user_id (sub) if valid. Returns None if no secret or invalid token."""
    if not _jwt_secret or not _mobius_os_auth_url:
        return None
    if not token or not token.strip():
        return None
    try:
        import jwt
        payload = jwt.decode(
            token,
            _jwt_secret,
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
