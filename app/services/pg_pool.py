"""Async PostgreSQL pool for fire-and-forget analytics (llm_calls, llm_config_versions).

Phase 0.10 fix — pool thrash diagnosis
--------------------------------------
The previous implementation bound the pool to the asyncio loop that created
it, and reset the pool whenever a caller used a different loop
(``_pool_loop_id != lid`` → close + recreate). Combined with
:func:`app.prompts_llm_history.append_entry` falling back to
``asyncio.run`` whenever no loop was running (which creates a fresh loop
*per call*), this produced a pool create → write → destroy cycle every
couple of seconds under normal load. Pool churn starved the worker's async
capacity, producing the UI hang users saw during ReAct tool execution.

The fix:

1. The pool is created **once per process** and bound to the first loop
   that called us. Later calls from that loop reuse it.
2. Callers from a *different* loop (e.g. a throwaway ``asyncio.run`` loop
   in sync code) get ``None`` — they must no-op, not thrash.
3. Sync callers MUST therefore guard their scheduling (see
   ``prompts_llm_history.append_entry`` which now skips when no loop is
   running instead of spinning up a new one).

Pool size: env ``MOBIUS_PG_POOL_MAX_SIZE`` (default 2) — keep low when many
processes share one Postgres (local Docker default max_connections was 100;
several Uvicorn + workers each holding a pool adds up).
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _pool_max_size() -> int:
    raw = (os.environ.get("MOBIUS_PG_POOL_MAX_SIZE") or "2").strip()
    try:
        return max(1, min(32, int(raw)))
    except ValueError:
        return 2


# Module-level singletons. ``_pool_loop_id`` pins the pool to its creator
# loop; we do NOT destroy-and-recreate on loop change — that was the bug.
_pool: Any = None
_pool_loop_id: int | None = None


async def get_pool():
    """Return asyncpg pool for ``CHAT_RAG_DATABASE_URL``.

    Lazy-creates on first call from the main async loop (the worker's
    lifetime loop). Returns ``None`` in two cases so callers can no-op
    instead of thrashing:

    - ``CHAT_RAG_DATABASE_URL`` is unset (analytics disabled).
    - The current running loop is different from the loop that owns the
      pool — e.g. a sync caller used ``asyncio.run`` to invoke this
      coroutine. Creating a second pool would multiply the connection
      count; reusing a pool across loops is not asyncpg-safe.
    """
    global _pool, _pool_loop_id
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    lid = id(loop)

    if _pool is not None:
        if _pool_loop_id == lid:
            return _pool
        # A foreign loop is asking. Returning the pool bound to another
        # loop would be unsafe; returning None + logging is the contract
        # so callers can cleanly skip (we tolerated pool churn under the
        # old loop-id reset logic — no more).
        logger.debug(
            "pg_pool: current loop %s differs from owner loop %s; "
            "skipping (caller should no-op on None)",
            lid,
            _pool_loop_id,
        )
        return None

    try:
        # Route through db_client._get_fallback_url so we get a
        # psycopg2/asyncpg-ready DSN: the ``postgresql+psycopg2://``
        # SQLAlchemy-style prefix gets stripped (asyncpg rejects it
        # too), and CHAT_DB_PASSWORD from Secret Manager is injected
        # at the user segment. Previous direct read of
        # chat_config.rag.database_url blew up in Cloud Run with
        # ``invalid DSN: scheme is expected to be postgresql``.
        from app.db_client import _get_fallback_url

        url = _get_fallback_url("chat")
        if not url:
            logger.debug("CHAT_RAG_DATABASE_URL not set; analytics PG pool unavailable")
            return None
        import asyncpg

        mx = _pool_max_size()
        _pool = await asyncpg.create_pool(
            url, min_size=0, max_size=mx, command_timeout=10
        )
        _pool_loop_id = lid
        logger.info("asyncpg pool created max_size=%s loop_id=%s", mx, lid)
        return _pool
    except Exception as e:
        logger.warning("Failed to create asyncpg pool: %s", e)
        return None


def _reset_for_tests() -> None:
    """Testing hook only — drop the cached pool so a fresh one is created."""
    global _pool, _pool_loop_id
    _pool = None
    _pool_loop_id = None
