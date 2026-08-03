"""Admin CRUD + monitoring API for the v2 composable prompt-block system.

Backend for the prompt-management surface (blocks, compositions, versions,
live usage/quality) — the persistence layer (migration 053/054,
prompt_manager.py resolvers) has been live since 2026-07-26; this is the
first API surface for actually managing it instead of hand-authored SQL
against the dev DB.

Gated behind the same ``_admin_enabled()`` flag as the rest of app/api/admin.py
(404 when disabled — no fingerprinting in prod). Read endpoints are safe by
construction (SELECT-only). Write endpoints (new version / activate /
deactivate) never DELETE or UPDATE existing prompt_blocks rows — versions are
append-only and "rollback" means deactivating a bad version so the resolver's
max(active version) picks the previous one, never rewriting history.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.admin import _admin_enabled

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-prompts"])


def _gate() -> None:
    if not _admin_enabled():
        raise HTTPException(status_code=404, detail="Not found")


async def _pool():
    from app.services.pg_pool import get_pool
    pool = await get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="DB pool unavailable")
    return pool


# ── Compositions ──────────────────────────────────────────────────────────


@router.get("/admin/api/prompts/compositions")
async def list_compositions() -> list[dict[str, Any]]:
    _gate()
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, c.module_key, c.prompt_address, c.variant_id, c.version,
                   c.status, c.active, c.composition_hash, c.created_at, c.created_by,
                   (SELECT COUNT(*) FROM prompt_composition_members m WHERE m.composition_id = c.id) AS block_count
            FROM prompt_compositions c
            ORDER BY c.active DESC, c.module_key, c.variant_id, c.version DESC
            """
        )
    return [dict(r) for r in rows]


@router.get("/admin/api/prompts/compositions/{composition_id}")
async def get_composition(composition_id: int) -> dict[str, Any]:
    _gate()
    pool = await _pool()
    async with pool.acquire() as conn:
        comp = await conn.fetchrow(
            "SELECT * FROM prompt_compositions WHERE id = $1", composition_id,
        )
        if comp is None:
            raise HTTPException(status_code=404, detail="Composition not found")
        members = await conn.fetch(
            """
            SELECT m.position, m.block_key, m.pinned_version,
                   COALESCE(m.pinned_version, la.version) AS effective_version,
                   b.role, b.block_kind, b.is_authority, b.template_body, b.owner,
                   b.active AS block_row_active
            FROM prompt_composition_members m
            LEFT JOIN prompt_blocks b
              ON b.block_key = m.block_key
             AND b.version = COALESCE(m.pinned_version, (
                   SELECT MAX(version) FROM prompt_blocks b2
                   WHERE b2.block_key = m.block_key AND b2.active
                 ))
            LEFT JOIN LATERAL (
                SELECT MAX(version) AS version FROM prompt_blocks b3
                WHERE b3.block_key = m.block_key AND b3.active
            ) la ON true
            WHERE m.composition_id = $1
            ORDER BY m.position
            """,
            composition_id,
        )
    return {
        "composition": dict(comp),
        "members": [dict(m) for m in members],
    }


# ── Blocks ────────────────────────────────────────────────────────────────


@router.get("/admin/api/prompts/blocks")
async def list_blocks() -> list[dict[str, Any]]:
    """One row per block_key: the current max-active version (what the
    resolver actually serves today), plus how many versions exist total."""
    _gate()
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT block_key,
                   MAX(version) FILTER (WHERE active) AS active_version,
                   COUNT(*) AS version_count,
                   MAX(role) AS role,
                   MAX(owner) AS owner,
                   BOOL_OR(is_authority) AS has_authority_version
            FROM prompt_blocks
            GROUP BY block_key
            ORDER BY block_key
            """
        )
    return [dict(r) for r in rows]


@router.get("/admin/api/prompts/blocks/{block_key}/versions")
async def get_block_versions(block_key: str) -> list[dict[str, Any]]:
    _gate()
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, block_key, version, block_kind, role, template_body,
                   condition, is_authority, directives, owner,
                   validated_at, validated_by, active, created_at, created_by
            FROM prompt_blocks
            WHERE block_key = $1
            ORDER BY version DESC
            """,
            block_key,
        )
    if not rows:
        raise HTTPException(status_code=404, detail="No versions for this block_key")
    return [dict(r) for r in rows]


class NewBlockVersionRequest(BaseModel):
    template_body: str
    block_kind: str = "static"
    role: str = "system"
    condition: str | None = None
    is_authority: bool = False
    directives: list[str] = []
    owner: str
    created_by: str
    activate: bool = True


@router.post("/admin/api/prompts/blocks/{block_key}/versions")
async def create_block_version(block_key: str, body: NewBlockVersionRequest) -> dict[str, Any]:
    """Append-only: always inserts version = max(existing)+1 (or 1 for a
    brand-new block_key). Never rewrites an existing row."""
    _gate()
    if body.block_kind not in ("static", "conditional", "derived", "per_turn"):
        raise HTTPException(status_code=400, detail="invalid block_kind")
    if body.role not in ("system", "user"):
        raise HTTPException(status_code=400, detail="invalid role")
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            next_ver = await conn.fetchval(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM prompt_blocks WHERE block_key = $1",
                block_key,
            )
            validated_at = datetime.now(timezone.utc) if (body.activate or body.is_authority) else None
            validated_by = body.created_by if validated_at else None
            row = await conn.fetchrow(
                """
                INSERT INTO prompt_blocks
                  (block_key, version, block_kind, role, template_body, condition,
                   is_authority, directives, owner, validated_at, validated_by, active, created_by)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                RETURNING id, block_key, version, active
                """,
                block_key, next_ver, body.block_kind, body.role, body.template_body,
                body.condition, body.is_authority, body.directives, body.owner,
                validated_at, validated_by, body.activate, body.created_by,
            )
    logger.info("[admin-prompts] new block version block_key=%s version=%s activate=%s by=%s",
                block_key, next_ver, body.activate, body.created_by)
    return dict(row)


class SetVersionActiveRequest(BaseModel):
    active: bool
    actor: str


@router.post("/admin/api/prompts/blocks/{block_key}/versions/{version}/active")
async def set_block_version_active(block_key: str, version: int, body: SetVersionActiveRequest) -> dict[str, Any]:
    """Flip a version's active flag. This IS the rollback mechanism —
    deactivating the current version makes the resolver fall back to the
    next-highest active version automatically (no data rewritten, no
    redeploy). Refuses to deactivate the last remaining active version for
    a block_key that's actually in use by an active composition, so a
    click can't blank out a live prompt."""
    _gate()
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_authority, active FROM prompt_blocks WHERE block_key=$1 AND version=$2",
            block_key, version,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Version not found")
        if not body.active and row["is_authority"]:
            other_active = await conn.fetchval(
                "SELECT COUNT(*) FROM prompt_blocks WHERE block_key=$1 AND active AND version <> $2",
                block_key, version,
            )
            if other_active == 0:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot deactivate the last active version of an authority block.",
                )
        await conn.execute(
            "UPDATE prompt_blocks SET active = $1 WHERE block_key = $2 AND version = $3",
            body.active, block_key, version,
        )
    logger.info("[admin-prompts] block_key=%s version=%s active=%s by=%s",
                block_key, version, body.active, body.actor)
    return {"block_key": block_key, "version": version, "active": body.active}


# ── Monitoring ────────────────────────────────────────────────────────────


@router.get("/admin/api/prompts/monitoring/{composition_id}")
async def get_composition_monitoring(composition_id: int, days: int = 7) -> dict[str, Any]:
    """Usage + quality for one composition over the trailing ``days``,
    broken out by composition_hash — a version change shows up as a new
    hash bucket, so a regression after an edit is visible as "this hash's
    quality dropped", not smeared into one aggregate number."""
    _gate()
    days = max(1, min(90, days))
    pool = await _pool()
    async with pool.acquire() as conn:
        comp = await conn.fetchrow(
            "SELECT id, module_key, prompt_address FROM prompt_compositions WHERE id = $1",
            composition_id,
        )
        if comp is None:
            raise HTTPException(status_code=404, detail="Composition not found")
        by_hash = await conn.fetch(
            """
            SELECT composition_hash,
                   COUNT(*) AS call_count,
                   AVG(latency_ms) AS avg_latency_ms,
                   AVG(quality_score) AS avg_quality,
                   SUM(cost_usd) AS total_cost_usd,
                   SUM(CASE WHEN NOT success THEN 1 ELSE 0 END) AS error_count,
                   MIN(ts) AS first_seen,
                   MAX(ts) AS last_seen
            FROM llm_calls
            WHERE composition_id = $1 AND ts > NOW() - ($2 || ' days')::interval
            GROUP BY composition_hash
            ORDER BY MAX(ts) DESC
            """,
            composition_id, str(days),
        )
    return {
        "composition_id": comp["id"],
        "module_key": comp["module_key"],
        "prompt_address": comp["prompt_address"],
        "window_days": days,
        "by_hash": [dict(r) for r in by_hash],
    }


@router.get("/admin/api/prompts/monitoring")
async def list_monitoring_overview(days: int = 7) -> list[dict[str, Any]]:
    """Fleet-wide: one row per active composition, trailing ``days`` volume
    + quality — the landing view before drilling into one composition."""
    _gate()
    days = max(1, min(90, days))
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id AS composition_id, c.module_key, c.prompt_address,
                   COUNT(l.call_id) AS call_count,
                   AVG(l.latency_ms) AS avg_latency_ms,
                   AVG(l.quality_score) AS avg_quality,
                   SUM(l.cost_usd) AS total_cost_usd,
                   MAX(l.ts) AS last_called_at
            FROM prompt_compositions c
            LEFT JOIN llm_calls l
              ON l.composition_id = c.id AND l.ts > NOW() - ($1 || ' days')::interval
            WHERE c.active
            GROUP BY c.id, c.module_key, c.prompt_address
            ORDER BY call_count DESC NULLS LAST
            """,
            str(days),
        )
    return [dict(r) for r in rows]
