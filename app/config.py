"""Config from environment. Single place for queue type, storage, LLM, etc.

Phase 2c (2026-04-18) added ``assert_hosted_config()`` — a startup
gate that refuses to boot with misconfigured env when ``CHAT_ENV`` is
``staging`` or ``prod``. Follows the same pattern as
:class:`app.api.front_door.CorsMisconfiguredError`: it's better to
fail loudly at boot than silently ship with (e.g.) a placeholder
``VERTEX_PROJECT_ID`` that sends prod traffic to the wrong GCP project.

Scope note: this module still only covers ~20 of the ~70 env vars the
codebase reads. The remaining scattered ``os.environ.get(...)`` calls
will migrate in later commits; this commit's goal is to catch the
deploy-time footguns (placeholder VERTEX project, missing DB URL in
hosted) and wire the MCP adapter at startup.
"""
import os
from dataclasses import dataclass
from typing import Literal

# Placeholder value widely sprinkled across the codebase as a fallback
# VERTEX_PROJECT_ID. In hosted envs we refuse to boot when the resolved
# project matches this — it means an operator forgot to set the real
# project and we'd silently send traffic to an internal sandbox.
_VERTEX_PLACEHOLDER_PROJECT = "mobiusos-new"


class StartupAssertionError(RuntimeError):
    """Raised at app startup when hosted env is misconfigured.

    Peer of :class:`app.api.front_door.CorsMisconfiguredError`. Both
    block the FastAPI app from booting in ``CHAT_ENV=staging`` or
    ``prod`` when a critical env var is missing / placeholder.
    """


@dataclass
class Config:
    """App config. Load from env or defaults."""
    queue_type: Literal["memory", "redis", "pubsub"] = "memory"
    redis_url: str = "redis://localhost:6379/0"
    redis_request_key: str = "mobius:chat:requests"
    redis_response_key_prefix: str = "mobius:chat:response:"
    redis_response_ttl_seconds: int = 86400  # 24h
    redis_progress_channel_prefix: str = "mobius:chat:progress:"
    live_stream_via_redis: bool = False  # When True, API subscribes to Redis for progress (set CHAT_LIVE_STREAM=1 when worker is separate)
    storage_backend: Literal["memory"] = "memory"
    api_base_url: str = "http://localhost:8000"
    # LLM (same pattern as Mobius RAG: vertex for prod, ollama for local)
    llm_provider: Literal["ollama", "vertex"] = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    ollama_num_predict: int = 8192
    vertex_project_id: str | None = None
    vertex_location: str = "us-central1"
    vertex_model: str = "gemini-2.5-flash"
    # Auth: when set, proxy /api/v1/auth/* to Mobius-OS (plug-and-play)
    mobius_os_auth_url: str | None = None
    # Document mini reader: when set, proxy GET /api/v1/documents/{id}/pages to RAG backend for full-page inline reader
    rag_app_api_base: str | None = None


def get_config() -> Config:
    return Config(
        queue_type=os.getenv("QUEUE_TYPE", "memory"),
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        redis_request_key=os.getenv("REDIS_REQUEST_KEY", "mobius:chat:requests"),
        redis_response_key_prefix=os.getenv("REDIS_RESPONSE_KEY_PREFIX", "mobius:chat:response:"),
        redis_response_ttl_seconds=int(os.getenv("REDIS_RESPONSE_TTL_SECONDS", "86400")),
        redis_progress_channel_prefix=os.getenv("REDIS_PROGRESS_CHANNEL_PREFIX", "mobius:chat:progress:"),
        live_stream_via_redis=os.getenv("CHAT_LIVE_STREAM", "").strip().lower() in ("1", "true", "yes"),
        storage_backend=os.getenv("STORAGE_BACKEND", "memory"),
        api_base_url=os.getenv("API_BASE_URL", "http://localhost:8000"),
        llm_provider=os.getenv("LLM_PROVIDER", "vertex" if os.getenv("VERTEX_PROJECT_ID") else "ollama") or "ollama",
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "llama3.1:8b"),
        ollama_num_predict=int(os.getenv("OLLAMA_NUM_PREDICT", "8192")),
        vertex_project_id=os.getenv("VERTEX_PROJECT_ID"),
        vertex_location=os.getenv("VERTEX_LOCATION", "us-central1"),
        vertex_model=os.getenv("VERTEX_MODEL", "gemini-2.5-flash"),
        mobius_os_auth_url=os.getenv("MOBIUS_OS_AUTH_URL") or None,
        rag_app_api_base=(os.getenv("RAG_APP_API_BASE") or "").strip() or None,
    )


# ── Hosted-env config gate (Phase 2c) ──────────────────────────────────


def chat_rag_database_url() -> str:
    """Resolve the chat/RAG database URL from the three-way fallback.

    Priority order (preserves existing behavior):
      1. ``CHAT_RAG_DATABASE_URL`` — the canonical chat key
      2. ``RAG_DATABASE_URL`` — legacy shared alias some scripts use
      3. ``CHAT_DATABASE_URL`` — oldest alias, still in some .env files

    Returns empty string when nothing is set (caller decides whether
    that's fatal).
    """
    return (
        (os.environ.get("CHAT_RAG_DATABASE_URL") or "").strip()
        or (os.environ.get("RAG_DATABASE_URL") or "").strip()
        or (os.environ.get("CHAT_DATABASE_URL") or "").strip()
    )


def resolved_vertex_project_id() -> str:
    """The VERTEX_PROJECT_ID we'd actually use, stripped.

    Falls back through ``VERTEX_PROJECT_ID`` → ``CHAT_VERTEX_PROJECT_ID``.
    Does NOT substitute the placeholder ``"mobiusos-new"`` here — that
    substitution still happens at the call sites in llm_provider /
    embedding_provider for now, because migrating all of them is
    outside Phase 2c's scope. ``assert_hosted_config()`` catches the
    placeholder at boot instead.
    """
    return (
        (os.environ.get("VERTEX_PROJECT_ID") or "").strip()
        or (os.environ.get("CHAT_VERTEX_PROJECT_ID") or "").strip()
    )


def assert_hosted_config() -> None:
    """Startup gate. Refuses to boot when ``CHAT_ENV`` is hosted and
    critical env vars are missing or placeholders.

    Safe in all envs: no-op when ``CHAT_ENV`` is unset / ``dev`` /
    unknown (the last matches front_door's permissive default — a typo
    shouldn't brick staging).

    Raises :class:`StartupAssertionError` with a human-readable list of
    problems so the operator can fix the env and retry without grepping
    logs. Messages name the exact env var and what a correct value
    looks like.

    Why a separate helper (not inline in main.py's startup hook):
    makes the logic directly unit-testable without spinning up the
    FastAPI app, and lets the worker — which has its own entry point
    — call the same gate in a future commit.
    """
    # Lazy-import to avoid a circular at module load time:
    # front_door imports config transitively for the auth middleware.
    from app.api.front_door import chat_env, is_hosted

    if not is_hosted():
        return  # dev / unknown → permissive

    env = chat_env()
    problems: list[str] = []

    # 1. Chat/RAG database URL — without this, chat turns + jurisdiction
    #    state + retrieval persistence all silently drop on the floor
    #    (worse: with intermittent writes succeeding while reads fail).
    if not chat_rag_database_url():
        problems.append(
            "CHAT_RAG_DATABASE_URL is unset. Set one of "
            "CHAT_RAG_DATABASE_URL / RAG_DATABASE_URL / CHAT_DATABASE_URL "
            "to the Postgres connection string for this environment."
        )

    # 2. VERTEX_PROJECT_ID — the hardcoded "mobiusos-new" fallback
    #    scattered across llm_provider.py / chat_config.py means an
    #    unset env silently uses the dev sandbox project. Catch that
    #    here so hosted boot fails loudly instead.
    pid = resolved_vertex_project_id()
    if not pid:
        problems.append(
            "VERTEX_PROJECT_ID is unset. Set VERTEX_PROJECT_ID (or "
            "CHAT_VERTEX_PROJECT_ID) to the Google Cloud project that "
            "hosts this environment's Vertex AI resources."
        )
    elif pid == _VERTEX_PLACEHOLDER_PROJECT:
        problems.append(
            f"VERTEX_PROJECT_ID={pid!r} is the dev-sandbox placeholder. "
            "Set it to the real Google Cloud project for this "
            "environment — the placeholder is only safe in CHAT_ENV=dev."
        )

    # 3. JWT_SECRET required when MOBIUS_OS_AUTH_URL is set.
    #    (auth.py already guards this at request time, but failing at
    #    boot surfaces the misconfig before any user hits /chat.)
    if (os.environ.get("MOBIUS_OS_AUTH_URL") or "").strip():
        if not (os.environ.get("JWT_SECRET") or "").strip():
            problems.append(
                "MOBIUS_OS_AUTH_URL is set but JWT_SECRET is unset. "
                "Either unset MOBIUS_OS_AUTH_URL (auth disabled) or "
                "set JWT_SECRET to the shared secret the Mobius-OS "
                "proxy uses to sign JWTs."
            )

    if problems:
        header = f"Refusing to start in CHAT_ENV={env!r}: {len(problems)} config problem(s):"
        bullets = "\n  - " + "\n  - ".join(problems)
        hint = (
            "\n\nFix the env and restart. If this is a local dev box, "
            "set CHAT_ENV=dev (or unset it) to disable this gate."
        )
        raise StartupAssertionError(header + bullets + hint)
