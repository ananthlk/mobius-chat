"""Front-door config + middleware (Phase 1h).

Centralizes the three public-surface knobs that used to live inline in
main.py:

- CORS allowlist
- Rate limit on POST /chat
- Auth requirement mode

All three are driven by the same ``CHAT_ENV`` gate Phase 0.17 introduced:

    CHAT_ENV=dev        → permissive defaults (preserves dev ergonomics)
    CHAT_ENV=staging    → fail-closed unless explicitly configured
    CHAT_ENV=prod       → fail-closed unless explicitly configured

The goal is that a hosted deployment can't silently ship with the dev
defaults (open CORS, no auth, no rate limit) — either the operator sets
the right env vars or the process refuses to start.

Design rules
------------
- **Explicit opt-in in dev.** Rate limit and auth default OFF in dev so a
  local `uvicorn app.main:app --reload` still works without env setup.
- **Explicit opt-out in prod.** CORS requires ``CHAT_CORS_ORIGINS`` to be
  set in staging/prod; no silent fallback to ``["*"]``. Auth likewise.
- **One reader per env var.** Any env-var lookup lives here, not in call
  sites. Keeps the surface auditable from a single file.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


# ── Environment gate ──────────────────────────────────────────────────────


def chat_env() -> str:
    """Normalized deployment env: 'dev' | 'staging' | 'prod'.

    Same contract as Phase 0.17's CHAT_ENV gate in app.storage.feedback.
    Unknown values fall back to 'dev' (permissive) so a typo doesn't
    accidentally fail-closed your entire deployment — but logs a warning
    so the typo is visible.
    """
    raw = (os.environ.get("CHAT_ENV") or "dev").strip().lower()
    if raw not in {"dev", "staging", "prod"}:
        logger.warning(
            "Unknown CHAT_ENV=%r — treating as 'dev'. Valid: dev, staging, prod.",
            raw,
        )
        return "dev"
    return raw


def is_hosted() -> bool:
    """True when CHAT_ENV is staging or prod (anywhere other than a dev laptop)."""
    return chat_env() in {"staging", "prod"}


# ── CORS ──────────────────────────────────────────────────────────────────


class CorsMisconfiguredError(RuntimeError):
    """Raised at app startup if CORS config is invalid for the current env.

    Hosted envs MUST set ``CHAT_CORS_ORIGINS`` to a comma-separated list.
    Refusing to start is better than silently shipping with ``allow_origins=["*"]``,
    which is the pattern Phase 1h is fixing.
    """


@dataclass(frozen=True)
class CorsConfig:
    """Resolved CORS settings for this process."""

    allow_origins: list[str]
    allow_methods: list[str]
    allow_headers: list[str]
    allow_credentials: bool


def resolve_cors_config() -> CorsConfig:
    """Read CORS config from env, validate against CHAT_ENV, return resolved config.

    Dev default: permissive (``["*"]``) so local frontend dev-servers work
    without env setup.

    Hosted (staging / prod): ``CHAT_CORS_ORIGINS`` must be set to a
    comma-separated list of exact origins. No wildcards — a typo'd
    ``*.example.com`` silently allowing everything is exactly the category
    of bug we're closing. If origins are missing, raises
    :class:`CorsMisconfiguredError` so the app won't start.
    """
    raw_origins = (os.environ.get("CHAT_CORS_ORIGINS") or "").strip()
    env = chat_env()

    if env == "dev":
        origins = _parse_origins(raw_origins) if raw_origins else ["*"]
        return CorsConfig(
            allow_origins=origins,
            allow_methods=["*"],
            allow_headers=["*"],
            # Cannot set allow_credentials=True with allow_origins=["*"];
            # starlette silently ignores the combination. Keep both aligned.
            allow_credentials=(origins != ["*"]),
        )

    # Hosted env: strict.
    if not raw_origins:
        raise CorsMisconfiguredError(
            f"CHAT_ENV={env} requires CHAT_CORS_ORIGINS to be set to a "
            f"comma-separated list of allowed origins. Example:\n"
            f"  CHAT_CORS_ORIGINS=https://app.example.com,https://admin.example.com"
        )
    origins = _parse_origins(raw_origins)
    _reject_wildcards(origins)
    return CorsConfig(
        allow_origins=origins,
        # Hosted: restrict to the methods chat actually uses.
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Correlation-Id"],
        allow_credentials=True,
    )


def _parse_origins(raw: str) -> list[str]:
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


def _reject_wildcards(origins: list[str]) -> None:
    bad = [o for o in origins if "*" in o]
    if bad:
        raise CorsMisconfiguredError(
            f"CORS wildcards rejected in hosted env: {bad}. "
            f"List exact origins (including scheme + port if non-default)."
        )


# ── Rate limiter ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RateLimitConfig:
    enabled: bool
    requests_per_minute: int
    # Path prefixes the limiter applies to. Empty tuple = all paths.
    path_prefixes: tuple[str, ...]


def resolve_rate_limit_config() -> RateLimitConfig:
    """Opt-in via ``CHAT_RATE_LIMIT_PER_MINUTE`` (integer).

    Dev default: OFF. Hosted default: ON at 30 req/min/IP against ``/chat``
    POST paths unless the operator explicitly sets the env var.
    """
    raw = (os.environ.get("CHAT_RATE_LIMIT_PER_MINUTE") or "").strip()
    if raw:
        try:
            rpm = max(1, int(raw))
        except ValueError:
            logger.warning(
                "Invalid CHAT_RATE_LIMIT_PER_MINUTE=%r — rate limit disabled.", raw,
            )
            return RateLimitConfig(enabled=False, requests_per_minute=0, path_prefixes=())
        return RateLimitConfig(
            enabled=True,
            requests_per_minute=rpm,
            path_prefixes=("/chat",),
        )

    if is_hosted():
        # Hosted default: 30 req/min per client IP against /chat. Operator
        # overrides with the env var.
        return RateLimitConfig(
            enabled=True,
            requests_per_minute=30,
            path_prefixes=("/chat",),
        )

    # Dev default: off.
    return RateLimitConfig(enabled=False, requests_per_minute=0, path_prefixes=())


class InMemoryRateLimitMiddleware(BaseHTTPMiddleware):
    """Simple sliding-window per-IP rate limiter.

    Good enough for a single-process deployment. For multi-replica
    deployments, swap to a Redis-backed implementation in a later phase —
    same config surface, different backend. This module pins the contract
    so the swap is isolated.

    Not applied when ``RateLimitConfig.enabled`` is False — the middleware
    short-circuits. Not applied to paths outside ``path_prefixes``.
    """

    def __init__(self, app, config: RateLimitConfig):
        super().__init__(app)
        self._config = config
        # Per-IP deque of request timestamps. Trimmed on each hit.
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next):
        if not self._config.enabled:
            return await call_next(request)

        path = request.url.path or ""
        if self._config.path_prefixes and not any(
            path.startswith(p) for p in self._config.path_prefixes
        ):
            return await call_next(request)

        client_ip = _client_ip(request)
        now = time.monotonic()
        window_start = now - 60.0

        bucket = self._buckets[client_ip]
        # Trim timestamps older than the sliding window.
        while bucket and bucket[0] < window_start:
            bucket.popleft()

        if len(bucket) >= self._config.requests_per_minute:
            # Retry-after is the time until the oldest-in-window expires.
            retry_after = max(1, int(bucket[0] + 60.0 - now))
            return JSONResponse(
                status_code=429,
                content={
                    "detail": (
                        f"Rate limit exceeded: "
                        f"{self._config.requests_per_minute} req/min/IP"
                    ),
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

        bucket.append(now)
        return await call_next(request)


def _client_ip(request: Request) -> str:
    """Best-effort client IP.

    Honors ``X-Forwarded-For`` when present (first hop) — assumes the
    deployment puts a trusted proxy in front. Falls back to request.client.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


# ── Auth mode ─────────────────────────────────────────────────────────────


def auth_mode() -> str:
    """Normalized auth mode: 'off' | 'optional' | 'required'.

    - ``off``       — no auth checks (dev default)
    - ``optional``  — decode JWT when present but don't require it
    - ``required``  — reject requests without a valid JWT (401)

    Hosted default: ``required``. Dev default: ``off``.
    Operator override: ``CHAT_AUTH_MODE`` env var.
    """
    raw = (os.environ.get("CHAT_AUTH_MODE") or "").strip().lower()
    if raw in {"off", "optional", "required"}:
        return raw
    return "required" if is_hosted() else "off"


async def require_user(request: Request) -> str | None:
    """FastAPI dependency that returns the authenticated user_id.

    Behavior depends on :func:`auth_mode`:

    - ``off``       — always returns None. Endpoints protected by this
                      dependency still execute.
    - ``optional``  — returns the user_id if a valid JWT is present,
                      else None.
    - ``required``  — returns the user_id if a valid JWT is present,
                      otherwise raises HTTPException(401).

    Use as:

        from fastapi import Depends
        from app.api.front_door import require_user

        @router.post("/chat/tasks")
        def create_task(user_id: str | None = Depends(require_user)):
            ...
    """
    mode = auth_mode()
    if mode == "off":
        return None

    from app.auth import get_user_id_from_request  # local import — optional dep
    user_id = get_user_id_from_request(request)

    if mode == "optional":
        return user_id

    # required
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_id
