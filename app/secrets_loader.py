"""Unified secret resolution: GCP Secret Manager in hosted envs, env var in dev.

Background (2026-04-20)
-----------------------
Pre-this-module, secret values (``GROQ_API_KEY``, ``ANTHROPIC_API_KEY``,
``JWT_SECRET``, Postgres passwords) were read straight from ``os.environ``
by individual modules. In dev that means ``.env``; in Cloud Run that
means secrets baked into the deployment's env-var manifest or (worse)
literal values in CI configs. Both paths are hard to rotate and easy to
leak.

This module gives every secret-reading callsite one API:

    from app.secrets_loader import get_secret

    key = get_secret("GROQ_API_KEY")
    # → hosted: value of Secret Manager secret ``groq-api-key``
    # → dev:    value of ``os.environ['GROQ_API_KEY']``

Resolution order
----------------
1. **Explicit env var override** — if the env var is set to a non-empty
   value, use it. This keeps tests, ``pytest monkeypatch.setenv``, and
   local dev working without touching Secret Manager. It also lets
   Cloud Run secret-env-var mounts (the idiomatic Google pattern)
   continue to Just Work — they set the env var for you.

2. **Secret Manager** — if the env var is unset AND we detect a hosted
   environment (``K_SERVICE`` / ``MOBIUS_PROD`` / ``CHAT_ENV_STRICT``),
   fetch from Secret Manager using the mapping below. Cached per
   process. A fetch failure raises so hosted boot fails loud.

3. **None / default** — in dev with no env var set, return the caller's
   default (or ``None``). Loud failures belong to the caller (e.g.
   ``GroqProvider`` already raises a helpful message).

Mapping
-------
Env var names are UPPER_SNAKE_CASE; Secret Manager names are
kebab-case. ``_ENV_TO_SECRET`` encodes the translation. Adding a new
secret is a one-line addition to that table plus a ``gcloud secrets
create`` on the project side.

Caching
-------
Per-process dict. Secret Manager doesn't change mid-request in
practice; rotate = redeploy. If that ever becomes false, expose a
``flush_cache()`` helper.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)


# ── Env var → Secret Manager name mapping ───────────────────────────
#
# Kept as an explicit dict (not a string transform) because some env
# vars don't have a 1:1 kebab-case Secret Manager twin — e.g. the
# shared ``jwt-secret`` serves both mobius-os and chat, so the chat-
# side env var ``JWT_SECRET`` maps to the already-provisioned shared
# secret name, not to ``chat-jwt-secret``.
_ENV_TO_SECRET: dict[str, str] = {
    "GROQ_API_KEY": "groq-api-key",
    "ANTHROPIC_API_KEY": "anthropic-api-key",
    "OPENAI_API_KEY": "openai-api-key",
    "JWT_SECRET": "jwt-secret",              # shared with mobius-os
    "CHAT_DB_PASSWORD": "db-password-mobius-chat",
    "MOBIUS_ORG_AGENT_INTERNAL_KEY": "org-agent-internal-key",
}


# ── Hosted-env detection (shared logic with front_door) ──────────────


def _looks_hosted() -> bool:
    """True when we're running in a managed environment where
    Secret Manager should be the source of truth.

    Matches the heuristic used by ``app.api.front_door._looks_hosted``
    so strict-env and secret-fetch behave consistently.
    """
    if (os.environ.get("K_SERVICE") or "").strip():
        return True  # Cloud Run injects K_SERVICE
    if (os.environ.get("MOBIUS_PROD") or "").strip().lower() in {"1", "true", "yes"}:
        return True
    if (os.environ.get("CHAT_ENV_STRICT") or "").strip().lower() in {"1", "true", "yes"}:
        return True
    return False


def _gcp_project_id() -> Optional[str]:
    """Project to read secrets from.

    Priority: explicit ``CHAT_GCP_PROJECT`` (lets staging point at a
    different project than prod without a redeploy) → ``GCP_PROJECT``
    → ``GOOGLE_CLOUD_PROJECT`` (what Cloud Run injects by default).
    """
    for var in ("CHAT_GCP_PROJECT", "GCP_PROJECT", "GOOGLE_CLOUD_PROJECT"):
        v = (os.environ.get(var) or "").strip()
        if v:
            return v
    return None


# ── Cache ────────────────────────────────────────────────────────────


_cache: dict[str, str] = {}
_cache_lock = threading.Lock()


def flush_cache() -> None:
    """Clear the in-process secret cache. Call after an explicit
    rotation if the process must continue running. Rare — the
    common rotation path is redeploy."""
    with _cache_lock:
        _cache.clear()


# ── Secret Manager fetch ─────────────────────────────────────────────


def _fetch_from_secret_manager(secret_name: str) -> str:
    """Fetch a secret version from Secret Manager. Raises on failure.

    Lazy-imports ``google.cloud.secretmanager`` so dev environments
    without the library installed don't pay the import cost — this
    function is only called in hosted envs.
    """
    project = _gcp_project_id()
    if not project:
        raise RuntimeError(
            f"Secret Manager lookup for {secret_name!r} requires a project. "
            "Set CHAT_GCP_PROJECT, GCP_PROJECT, or GOOGLE_CLOUD_PROJECT."
        )
    try:
        from google.cloud import secretmanager  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover — only hit in hosted without dep
        raise RuntimeError(
            "google-cloud-secret-manager is not installed. Add it to "
            "pyproject.toml to use hosted-env secret resolution."
        ) from e

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project}/secrets/{secret_name}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8")


# ── Public API ───────────────────────────────────────────────────────


def get_secret(env_var: str, default: Optional[str] = None) -> Optional[str]:
    """Resolve ``env_var`` to its secret value.

    - Env var set + non-empty → that value (dev path, test path, Cloud
      Run secret-env-var mount path).
    - Env var unset + hosted env → fetch from Secret Manager, cache,
      return.
    - Env var unset + dev → return ``default`` (or ``None``).

    Never raises for a missing dev secret — that's the caller's call
    (``GroqProvider`` raises a helpful "set GROQ_API_KEY in .env"
    message). Raises only if a hosted fetch fails, because that's a
    misconfigured deploy and we'd rather fail loudly at first use.
    """
    # 1. Explicit env var wins.
    raw = (os.environ.get(env_var) or "").strip()
    if raw:
        return raw

    # 2. Dev / unconfigured → just return the default.
    if not _looks_hosted():
        return default

    # 3. Hosted path: try cache, then Secret Manager.
    with _cache_lock:
        if env_var in _cache:
            return _cache[env_var]

    secret_name = _ENV_TO_SECRET.get(env_var)
    if not secret_name:
        # Not a known secret — hosted config bug. Surface it.
        logger.warning(
            "get_secret(%r) has no Secret Manager mapping; returning default",
            env_var,
        )
        return default

    try:
        value = _fetch_from_secret_manager(secret_name)
    except Exception as e:
        # Hosted miss is a real problem — log loudly and re-raise so the
        # caller's "key not set" path surfaces the root cause.
        logger.error(
            "secret_manager_fetch_failed env=%s secret=%s err=%s",
            env_var, secret_name, e,
        )
        raise

    with _cache_lock:
        _cache[env_var] = value
    return value


def require_secret(env_var: str) -> str:
    """Like ``get_secret`` but raises if the value is missing.

    Callers that want an unambiguous error at use time rather than
    downstream "why is the API 401ing?" use this. Internal convenience.
    """
    val = get_secret(env_var)
    if not val:
        raise RuntimeError(
            f"Required secret {env_var!r} is not set. "
            f"In dev: add to .env. In hosted env: ensure Secret Manager "
            f"has secret {_ENV_TO_SECRET.get(env_var, '(no mapping)')!r} "
            f"and the runtime service account has secretAccessor role."
        )
    return val
