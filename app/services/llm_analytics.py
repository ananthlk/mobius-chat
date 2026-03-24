"""PostgreSQL-backed analytics for every LLM call. Fire-and-forget writes via asyncpg."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from app.services.cost_model import compute_cost
from app.services.usage import LLMUsageDict

logger = logging.getLogger(__name__)


def _hash_prompt(prompt: str) -> str:
    """SHA-256 of prompt text (store hash only, not PII)."""
    return hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()


def build_record(
    *,
    model: str,
    provider: str,
    stage: str,
    success: bool,
    prompt: str,
    output_text: str = "",
    usage: LLMUsageDict | None = None,
    latency_ms: int | None = None,
    config_sha: str | None = None,
    correlation_id: str | None = None,
    thread_id: str | None = None,
    tier: str | None = None,
    complexity: str | None = None,
    is_ab_call: bool = False,
    ab_variant: str | None = None,
    is_rate_limit: bool = False,
    is_fallback: bool = False,
    fallback_from: str | None = None,
    completion_valid: bool = True,
    error_type: str | None = None,
    phi_detected: bool = False,
    phi_scrubbed: bool = False,
    phi_types: str | None = None,
) -> dict[str, Any]:
    """Build llm_calls row (prompt stored as hash only). Includes call_id and ts."""
    usage = usage or {}
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cost = compute_cost(usage) if usage else None
    prompt_hash = _hash_prompt(prompt)
    prompt_len = len(prompt) if prompt else 0
    output_len = len(output_text) if output_text else 0
    ts = datetime.now(timezone.utc)
    return {
        "call_id": uuid.uuid4(),
        "correlation_id": correlation_id,
        "thread_id": thread_id,
        "ts": ts,
        "config_sha": config_sha,
        "model": (model or "").strip() or "unknown",
        "provider": (provider or "").strip() or "unknown",
        "stage": (stage or "").strip() or "unknown",
        "tier": tier,
        "complexity": complexity,
        "is_ab_call": is_ab_call,
        "ab_variant": ab_variant,
        "success": success,
        "is_rate_limit": is_rate_limit,
        "is_fallback": is_fallback,
        "fallback_from": fallback_from,
        "completion_valid": completion_valid,
        "error_type": error_type,
        "latency_ms": latency_ms,
        "input_tokens": inp if inp else None,
        "output_tokens": out if out else None,
        "cost_usd": round(cost, 8) if cost is not None and cost > 0 else None,
        "quality_score": None,
        "quality_source": None,
        "phi_detected": phi_detected,
        "phi_scrubbed": phi_scrubbed,
        "phi_types": phi_types,
        "prompt_len_chars": prompt_len if prompt_len else None,
        "output_len_chars": output_len if output_len else None,
        "prompt_hash": prompt_hash,
        "synced_to_bq": False,
        "synced_at": None,
    }


async def _write_async(record: dict[str, Any]) -> None:
    """Insert one row into llm_calls. No-op if pool unavailable."""
    try:
        from app.services.pg_pool import get_pool
        pool = await get_pool()
        if not pool:
            return
        ts = record["ts"]
        # asyncpg expects datetime; do not stringify
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO llm_calls (
                    call_id, correlation_id, thread_id, ts, config_sha,
                    model, provider, stage, tier, complexity, is_ab_call, ab_variant,
                    success, is_rate_limit, is_fallback, fallback_from, completion_valid, error_type,
                    latency_ms, input_tokens, output_tokens, cost_usd,
                    quality_score, quality_source, phi_detected, phi_scrubbed, phi_types,
                    prompt_len_chars, output_len_chars, prompt_hash, synced_to_bq, synced_at
                ) VALUES (
                    $1, $2, $3, $4::timestamptz, $5, $6, $7, $8, $9, $10, $11, $12,
                    $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27,
                    $28, $29, $30, $31, $32
                )
                """,
                record["call_id"],
                record["correlation_id"],
                record["thread_id"],
                ts,
                record["config_sha"],
                record["model"],
                record["provider"],
                record["stage"],
                record["tier"],
                record["complexity"],
                record["is_ab_call"],
                record["ab_variant"],
                record["success"],
                record["is_rate_limit"],
                record["is_fallback"],
                record["fallback_from"],
                record["completion_valid"],
                record["error_type"],
                record["latency_ms"],
                record["input_tokens"],
                record["output_tokens"],
                record["cost_usd"],
                record["quality_score"],
                record["quality_source"],
                record["phi_detected"],
                record["phi_scrubbed"],
                record["phi_types"],
                record["prompt_len_chars"],
                record["output_len_chars"],
                record["prompt_hash"],
                record["synced_to_bq"],
                record["synced_at"],
            )
    except Exception as e:
        logger.warning("llm_analytics write failed: %s", e)


def write_record(record: dict[str, Any]) -> None:
    """Fire-and-forget write to llm_calls. Uses create_task when loop is running, else asyncio.run."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        loop.create_task(_write_async(record))
    else:
        try:
            asyncio.run(_write_async(record))
        except Exception as e:
            logger.warning("llm_analytics write_record (no loop): %s", e)


def _map_llm_call_stage_to_rubric_stage(stage: str) -> str:
    """Map llm_calls.stage (e.g. react_3) to STAGE_QUALITY_MAP key."""
    s = (stage or "").strip().lower()
    if s.startswith("react_"):
        return "planner"
    return s


async def update_quality_for_correlation_stages_async(
    correlation_id: str,
    sub_scores: dict[str, float | None],
    overall_score: float,
    quality_source: str = "adjudication_v2",
    stage_scores: dict[str, float] | None = None,
) -> None:
    """
    After full adjudication, write per-stage quality_score on llm_calls rows
    for this correlation_id (latest successful call per mapped stage).

    When stage_scores is provided (from adjudicator per-round evaluation), use
    those for react_1, react_2, etc. instead of the shared planner mapping.
    """
    try:
        from app.services.adjudication.utils import get_stage_quality_score
        from app.services.pg_pool import get_pool

        pool = await get_pool()
        if not pool or not correlation_id:
            return

        stage_scores = stage_scores or {}

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (stage) call_id::text AS id, stage
                FROM llm_calls
                WHERE correlation_id = $1 AND success = true
                ORDER BY stage, ts DESC
                """,
                correlation_id,
            )

        for row in rows:
            raw_stage = str(row["stage"] or "").strip()
            q: float | None = None
            if raw_stage.startswith("react_") and raw_stage in stage_scores:
                q = stage_scores[raw_stage]
            else:
                rubric_stage = _map_llm_call_stage_to_rubric_stage(raw_stage)
                q = get_stage_quality_score(rubric_stage, sub_scores, float(overall_score))
            if q is None:
                continue
            await update_quality_async(row["id"], float(q), quality_source)
    except Exception as e:
        logger.warning("update_quality_for_correlation_stages failed: %s", e)


async def update_quality_async(call_id: str | uuid.UUID, quality_score: float, quality_source: str) -> None:
    """Update llm_calls.quality_score/source and insert llm_quality_updates row."""
    try:
        from app.services.pg_pool import get_pool
        pool = await get_pool()
        if not pool:
            return
        cid = str(call_id) if isinstance(call_id, uuid.UUID) else call_id
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE llm_calls SET quality_score = $1, quality_source = $2 WHERE call_id = $3::uuid",
                round(quality_score, 3),
                quality_source,
                cid,
            )
            await conn.execute(
                """
                INSERT INTO llm_quality_updates (call_id, quality_score, quality_source)
                VALUES ($1::uuid, $2, $3)
                """,
                cid,
                round(quality_score, 3),
                quality_source,
            )
    except Exception as e:
        logger.warning("llm_analytics update_quality failed: %s", e)


async def fetch_quality_enrich_map_for_correlation_async(correlation_id: str) -> dict[str, dict[str, Any]]:
    """Return ``{ llm_call_id: { quality_score, quality_source } }`` for rows with QA scores (UI merge)."""
    try:
        from app.services.pg_pool import get_pool

        pool = await get_pool()
        if not pool or not correlation_id:
            return {}
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT call_id::text AS id, quality_score, quality_source
                FROM llm_calls
                WHERE correlation_id = $1 AND quality_score IS NOT NULL
                """,
                correlation_id,
            )
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            rid = str(r["id"] or "").strip()
            if not rid:
                continue
            qs = r["quality_score"]
            if qs is None:
                continue
            try:
                qf = float(qs)
            except (TypeError, ValueError):
                continue
            src = r["quality_source"]
            out[rid] = {
                "quality_score": round(qf, 3),
                "quality_source": (str(src).strip()[:200] if src else "") or "unknown",
            }
        return out
    except Exception as e:
        logger.debug("fetch_quality_enrich_map_for_correlation failed: %s", e)
        return {}
