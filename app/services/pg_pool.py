"""Async PostgreSQL pool for fire-and-forget analytics (llm_calls, llm_config_versions).
Uses same CHAT_RAG_DATABASE_URL as sync persistence. Lazy init on first use.

Pool size: env ``MOBIUS_PG_POOL_MAX_SIZE`` (default 2) — keep low when many processes share one Postgres
(local Docker default max_connections was 100; several Uvicorn + workers each holding a pool adds up).
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

_pool: Any = None
_pool_loop_id: int | None = None


async def get_pool():
    """Return asyncpg pool for CHAT_RAG_DATABASE_URL. Creates on first call. Returns None if URL unset."""
    global _pool, _pool_loop_id
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    lid = id(loop)
    if _pool is not None and _pool_loop_id != lid:
        # Pool is bound to the loop that created it (e.g. eval: import-time loop vs asyncio.run loop.)
        try:
            await _pool.close()
        except Exception:
            pass
        _pool = None
        _pool_loop_id = None
    if _pool is not None:
        return _pool
    try:
        from app.chat_config import get_chat_config
        url = (get_chat_config().rag.database_url or "").strip()
        if not url:
            logger.debug("CHAT_RAG_DATABASE_URL not set; analytics PG pool unavailable")
            return None
        import asyncpg
        mx = _pool_max_size()
        _pool = await asyncpg.create_pool(url, min_size=0, max_size=mx, command_timeout=10)
        logger.info("asyncpg pool created max_size=%s", mx)
        _pool_loop_id = lid
        return _pool
    except Exception as e:
        logger.warning("Failed to create asyncpg pool: %s", e)
        return None
