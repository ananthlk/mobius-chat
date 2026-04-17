"""Append-only history for prompts+LLM config. PostgreSQL-backed (llm_config_versions).

Phase 0.10 — process-level dedup to stop pool thrash
----------------------------------------------------
``append_entry`` used to fall back to ``asyncio.run`` when called from sync
code with no running loop. Each ``asyncio.run`` created a fresh event loop,
which invalidated :mod:`app.services.pg_pool`'s cached pool and forced a
pool create → write → destroy cycle every few seconds. The churn was
visible in worker logs as a stream of ``asyncpg pool created max_size=2``
/ ``pool is closed`` / ``connection was closed in the middle of operation``
messages.

The fix:

1. When called from sync code with no running loop, **skip** the append
   rather than spinning up a disposable loop. This is telemetry — dropping
   a config-history row is strictly better than thrashing the pool.
2. Client-side dedup on ``config_sha`` per process: once we've successfully
   appended a sha in this process we short-circuit further calls. The
   server-side ``ON CONFLICT (config_sha) DO NOTHING`` already makes this
   idempotent; the client-side cache eliminates the connection round-trip.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.prompts_llm_config import compute_config_sha

logger = logging.getLogger(__name__)

# Process-level cache of SHAs we've already tried to append. The config is
# effectively immutable per-process (only changes when the worker restarts
# with new prompt/model config), so one successful write per sha is enough.
_appended_shas: set[str] = set()


def _model_provider_prompt_count(config: dict[str, Any]) -> tuple[str | None, str | None, int]:
    """Extract model, provider from config.llm and prompt_count from config.prompts."""
    llm = config.get("llm") or {}
    if isinstance(llm, dict):
        model = llm.get("model") or llm.get("vertex_model") or llm.get("ollama_model")
        provider = llm.get("provider")
    else:
        model, provider = None, None
    prompts = config.get("prompts") or {}
    prompt_count = len(prompts) if isinstance(prompts, dict) else 0
    return (model, provider, prompt_count)


async def _append_async(config: dict[str, Any], created_by: str, notes: str | None) -> None:
    """Insert one row into llm_config_versions. ON CONFLICT (config_sha) DO NOTHING."""
    sha = compute_config_sha(config)
    if sha in _appended_shas:
        # Already successfully appended this sha in this process — skip the
        # round-trip entirely. Prevents pool thrash when many callers load
        # the same config in quick succession.
        return
    try:
        from app.services.pg_pool import get_pool
        pool = await get_pool()
        if not pool:
            logger.debug("pg_pool unavailable; skip config history append")
            return
        model, provider, prompt_count = _model_provider_prompt_count(config)
        config_json = json.dumps(config)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO llm_config_versions
                (config_sha, config_json, created_by, notes, model, provider, prompt_count)
                VALUES ($1, $2::jsonb, $3, $4, $5, $6, $7)
                ON CONFLICT (config_sha) DO NOTHING
                """,
                sha,
                config_json,
                created_by or "api",
                notes,
                model,
                provider,
                prompt_count,
            )
        _appended_shas.add(sha)
        logger.info("Appended config history entry config_sha=%s", sha)
    except Exception as e:
        logger.warning("Failed to append config history: %s", e)


def append_entry(
    config: dict[str, Any],
    created_by: str = "api",
    notes: str | None = None,
) -> None:
    """Append one config snapshot to history (PG).

    Fire-and-forget when a running event loop exists. **Skips silently**
    when there is no running loop — previous behavior was to call
    ``asyncio.run``, which created a disposable loop for each call and
    invalidated the asyncpg pool cache (see module docstring).

    The process-level dedup cache means each unique ``config_sha`` is
    written at most once per worker lifetime, so dropping a call when no
    loop exists is safe: a later call with the same sha will be a no-op
    anyway.
    """
    sha = compute_config_sha(config)
    if sha in _appended_shas:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        loop.create_task(_append_async(config, created_by or "api", notes))
    else:
        # Phase 0.10: no-op instead of asyncio.run. Telemetry is best-effort;
        # the next call from an async context will persist it.
        logger.debug(
            "prompts_llm_history.append_entry called with no running loop; "
            "skipping to avoid pool thrash (sha=%s)",
            sha,
        )


async def _list_entries_async(limit: int) -> list[dict[str, Any]]:
    """Return list of history entries, newest first."""
    try:
        from app.services.pg_pool import get_pool
        pool = await get_pool()
        if not pool:
            return []
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT config_sha, created_at, created_by, model, provider, prompt_count
                FROM llm_config_versions
                ORDER BY created_at DESC
                LIMIT $1
                """,
                max(1, min(500, limit)),
            )
        return [
            {
                "config_sha": r["config_sha"],
                "created_at": r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else str(r["created_at"]),
                "created_by": r["created_by"],
                "model": r["model"],
                "provider": r["provider"],
                "prompt_count": r["prompt_count"],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("Failed to list config history: %s", e)
        return []


def list_entries(limit: int = 50) -> list[dict[str, Any]]:
    """Return list of history entries, newest first: [{ config_sha, created_at, ... }, ...]."""
    try:
        return asyncio.run(_list_entries_async(limit))
    except Exception as e:
        logger.warning("list_entries failed: %s", e)
        return []


async def _get_by_sha_async(config_sha: str) -> dict[str, Any] | None:
    """Return full config dict for the given config_sha, or None."""
    sha = (config_sha or "").strip()
    if not sha:
        return None
    try:
        from app.services.pg_pool import get_pool
        pool = await get_pool()
        if not pool:
            return None
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT config_json FROM llm_config_versions WHERE config_sha = $1",
                sha,
            )
        if not row or not row["config_json"]:
            return None
        raw = row["config_json"]
        return raw if isinstance(raw, dict) else json.loads(raw)
    except Exception as e:
        logger.warning("get_by_sha failed: %s", e)
        return None


def get_by_sha(config_sha: str) -> dict[str, Any] | None:
    """Return full config dict for the given config_sha, or None if not found."""
    try:
        return asyncio.run(_get_by_sha_async(config_sha))
    except Exception as e:
        logger.warning("get_by_sha failed: %s", e)
        return None
