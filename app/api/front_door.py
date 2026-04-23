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


_VALID_CHAT_ENVS = frozenset({"dev", "staging", "prod"})


class InvalidChatEnvError(RuntimeError):
    """Raised when CHAT_ENV is set to a value outside {dev, staging, prod}
    AND the process is running in a way that suggests it's a hosted
    deployment. See ``chat_env()`` for the strict-mode gating rules.
    """


def chat_env() -> str:
    """Normalized deployment env: 'dev' | 'staging' | 'prod'.

    Strict mode (2026-04-20 hardening) — the permissive fallback to
    ``dev`` was a production footgun: a typo in hosted config
    (``CHAT_ENV=prod-us`` → silently treated as ``dev`` →
    authentication off). Now:

    * Valid values (``dev`` / ``staging`` / ``prod``) → use as-is.
    * Empty / unset → default ``dev`` (matches legacy local-dev UX).
    * **Unknown value + hosted cues** (``K_SERVICE`` for Cloud Run,
      ``MOBIUS_PROD=1``, ``CHAT_ENV_STRICT=1``) → raise
      ``InvalidChatEnvError`` so the container refuses to start and
      ops sees a loud failure rather than a silent permissive deploy.
    * **Unknown value on a dev laptop** (no hosted cues) → log a
      warning and treat as ``dev``, preserving the "don't brick my
      workstation on a typo" ergonomics.

    The strict-mode check keys on cues rather than the unknown value
    itself because the whole point is that we can't trust the value —
    it might be the operator's intended prod config that typo'd.
    """
    raw = (os.environ.get("CHAT_ENV") or "").strip().lower()
    if not raw:
        return "dev"
    if raw in _VALID_CHAT_ENVS:
        return raw
    if _looks_hosted():
        raise InvalidChatEnvError(
            f"CHAT_ENV={raw!r} is not one of {sorted(_VALID_CHAT_ENVS)}. "
            "Refusing to start in what looks like a hosted deployment "
            "(detected via K_SERVICE / MOBIUS_PROD / CHAT_ENV_STRICT). "
            "Fix the env var or set CHAT_ENV_STRICT=0 to force-disable "
            "this gate (not recommended)."
        )
    logger.warning(
        "Unknown CHAT_ENV=%r on a non-hosted host — treating as 'dev'. "
        "Set CHAT_ENV to dev, staging, or prod. Set CHAT_ENV_STRICT=1 "
        "to turn this into a boot failure in any environment.",
        raw,
    )
    return "dev"


def _looks_hosted() -> bool:
    """Heuristic: is this process running in a hosted context?

    True when any of:
      * Cloud Run sets ``K_SERVICE`` automatically.
      * ``MOBIUS_PROD=1`` / ``MOBIUS_PROD=true`` — our own belt.
      * ``CHAT_ENV_STRICT=1`` — operator opt-in to strict mode.

    When none of these are set we assume dev. The strict-mode gate
    only fires on unknown CHAT_ENV values *in hosted*; unknown values
    on a dev laptop still log-and-default (preserves "don't brick my
    workstation on a typo" UX).
    """
    if (os.environ.get("K_SERVICE") or "").strip():
        return True
    if (os.environ.get("MOBIUS_PROD") or "").strip().lower() in {"1", "true", "yes"}:
        return True
    if (os.environ.get("CHAT_ENV_STRICT") or "").strip().lower() in {"1", "true", "yes"}:
        return True
    return False


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
    """Tiered rate-limit config.

    Three composable tiers:
      - L1 per-IP — always on when ``enabled=True``. Catches bots.
      - L2 per-thread — soft protection against a frontend retrying the
        same thread (e.g. double-submit from a flaky retry loop).
        ``thread_rpm == 0`` disables L2.
      - L3 per-user — stub until auth lands; activates transparently
        when ``require_user`` dependency populates ``request.state.user_id``.
        ``user_rpm == 0`` disables L3.

    IPs in ``exempt_ips`` bypass all three tiers — use for internal
    monitoring / ops / the bench harness host.

    Field order keeps backward-compat with callers that construct this
    with the original three positional arguments
    (``enabled``, ``requests_per_minute``, ``path_prefixes``). New
    fields carry defaults so ``RateLimitConfig(True, 30, ('/chat',))``
    still works.
    """
    enabled: bool
    requests_per_minute: int          # L1 — per-IP
    # Path prefixes the limiter applies to. Empty tuple = all paths.
    path_prefixes: tuple[str, ...]
    thread_rpm: int = 0               # L2 — per-thread_id (0 = off)
    user_rpm: int = 0                 # L3 — per-user_id   (0 = off)
    exempt_ips: frozenset[str] = frozenset()  # admin/ops IPs that skip all tiers


def _parse_exempt_ips() -> frozenset[str]:
    """Comma-separated IPs via ``RATE_LIMIT_EXEMPT_IPS``. Leave unset
    in prod unless there's a reason — every exemption is a gap."""
    raw = (os.environ.get("RATE_LIMIT_EXEMPT_IPS") or "").strip()
    if not raw:
        return frozenset()
    return frozenset({p.strip() for p in raw.split(",") if p.strip()})


def _parse_int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("Invalid %s=%r — using default %d", name, raw, default)
        return default


def resolve_rate_limit_config() -> RateLimitConfig:
    """L1 opt-in via ``CHAT_RATE_LIMIT_PER_MINUTE`` (integer).
    L2 opt-in via ``CHAT_RATE_LIMIT_THREAD_PER_MINUTE`` (0 = off).
    L3 opt-in via ``CHAT_RATE_LIMIT_USER_PER_MINUTE`` (0 = off; also
    requires auth middleware to populate ``request.state.user_id``).

    Dev default:     L1 OFF,  L2 OFF,  L3 OFF.
    Hosted default:  L1 30/min per IP on /chat, L2 20/min per thread,
                     L3 120/min per user (active only when auth lands).
    """
    l1_raw = (os.environ.get("CHAT_RATE_LIMIT_PER_MINUTE") or "").strip()
    hosted = is_hosted()
    exempt = _parse_exempt_ips()

    # L1 resolution (backward-compatible with existing CHAT_RATE_LIMIT_PER_MINUTE env).
    if l1_raw:
        try:
            l1_rpm = max(1, int(l1_raw))
            l1_enabled = True
        except ValueError:
            logger.warning(
                "Invalid CHAT_RATE_LIMIT_PER_MINUTE=%r — L1 rate limit disabled.", l1_raw,
            )
            l1_enabled = False
            l1_rpm = 0
    elif hosted:
        l1_enabled = True
        l1_rpm = 30
    else:
        l1_enabled = False
        l1_rpm = 0

    if not l1_enabled:
        return RateLimitConfig(
            enabled=False, requests_per_minute=0,
            thread_rpm=0, user_rpm=0,
            path_prefixes=(), exempt_ips=exempt,
        )

    # L2/L3 defaults are active only when L1 is active (turning on L1
    # implies the operator wants per-tier limits too).
    l2_default = 20 if hosted else 0
    l3_default = 120 if hosted else 0
    l2_rpm = _parse_int_env("CHAT_RATE_LIMIT_THREAD_PER_MINUTE", l2_default)
    l3_rpm = _parse_int_env("CHAT_RATE_LIMIT_USER_PER_MINUTE", l3_default)

    return RateLimitConfig(
        enabled=True,
        requests_per_minute=l1_rpm,
        thread_rpm=l2_rpm,
        user_rpm=l3_rpm,
        path_prefixes=("/chat",),
        exempt_ips=exempt,
    )


class InMemoryRateLimitMiddleware(BaseHTTPMiddleware):
    """Tiered sliding-window rate limiter (L1 IP + L2 thread + L3 user).

    All three tiers share the same sliding-window algorithm and storage
    shape — just different keys. A request is rejected when ANY enabled
    tier is at its cap; the 429 response names the tier that tripped
    first so clients and ops can debug.

    Good enough for a single-process deployment. For multi-replica
    deployments, swap to a Redis-backed implementation in a later phase —
    same config surface, different backend.

    L2 (thread_id) requires the request body to parse as JSON with a
    ``thread_id`` field. We peek at the body once (cached for downstream
    handlers) and skip L2 quietly if it's missing or malformed.

    L3 (user_id) reads ``request.state.user_id`` populated by the auth
    middleware. Currently stubbed — activates when auth lands without
    any changes here.

    IPs in ``exempt_ips`` skip ALL tiers. Use only for monitoring / ops
    hosts; every exemption is a hole in the safety net.

    Not applied when ``RateLimitConfig.enabled`` is False — short-circuits.
    Not applied to paths outside ``path_prefixes``.
    """

    def __init__(self, app, config: RateLimitConfig):
        super().__init__(app)
        self._config = config
        # Per-key deque of request timestamps. Trimmed on each hit.
        # Separate namespaces per tier so ``ip:1.2.3.4`` doesn't
        # collide with ``t:1.2.3.4`` if a user ever names their
        # thread after an IP.
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
        if client_ip in self._config.exempt_ips:
            return await call_next(request)

        now = time.monotonic()
        window_start = now - 60.0

        # L2/L3 key resolution. Peek at the body for L2 only when the
        # thread limit is active — avoids a body read on routes where
        # the tier does nothing.
        thread_id: str | None = None
        if self._config.thread_rpm > 0 and request.method == "POST":
            thread_id = await _peek_thread_id(request)
        user_id: str | None = None
        if self._config.user_rpm > 0:
            user_id = getattr(request.state, "user_id", None) or None

        # Check tiers in order IP → thread → user. First-to-trip wins.
        tiers: list[tuple[str, str, int]] = [
            ("ip", f"ip:{client_ip}", self._config.requests_per_minute),
        ]
        if thread_id and self._config.thread_rpm > 0:
            tiers.append(("thread", f"t:{thread_id}", self._config.thread_rpm))
        if user_id and self._config.user_rpm > 0:
            tiers.append(("user", f"u:{user_id}", self._config.user_rpm))

        for tier_label, key, limit in tiers:
            bucket = self._buckets[key]
            while bucket and bucket[0] < window_start:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(bucket[0] + 60.0 - now))
                logger.info(
                    "rate_limit: tier=%s key=%s limit=%d retry_after=%ds",
                    tier_label, key, limit, retry_after,
                )
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": (
                            f"Rate limit exceeded: {limit} req/min "
                            f"({tier_label}-tier)"
                        ),
                        "tier": tier_label,
                        "retry_after_seconds": retry_after,
                    },
                    headers={"Retry-After": str(retry_after)},
                )

        # Under limit for every enabled tier — increment each bucket.
        for _, key, _ in tiers:
            self._buckets[key].append(now)
        return await call_next(request)


async def _peek_thread_id(request: Request) -> str | None:
    """Read the request body once, cache it for downstream handlers,
    return ``thread_id`` if present. Returns None on non-JSON, missing
    field, or oversized body."""
    try:
        body = await request.body()
        if not body or len(body) > 100_000:  # cap at ~100KB
            return None
        import json as _json
        try:
            payload = _json.loads(body.decode("utf-8", errors="ignore"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        tid = payload.get("thread_id")
        if isinstance(tid, str) and tid.strip():
            return tid.strip()
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("rate_limit peek failed (non-fatal): %s", exc)
    return None


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
