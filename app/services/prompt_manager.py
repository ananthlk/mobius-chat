"""PromptManager — DB-backed, versioned, tag-matched prompt templates.

Part of the LLMManager (Chat pipeline refactor). Replaces the hardcoded /
YAML str.format prompts with per-row Jinja2 templates from prompt_templates
(migration 049), selected by EngagementContext tag-match with an
exact → partial → default fallback chain and optional weighted A/B sampling.

Authoritative spec: docs/SPEC_LLM_MANAGER.md §2.1 / §3.1.

role (Option A, ruled 2026-07-25): an LLM call renders a system prompt + a user
template. render() selects a SYSTEM template (tag-match / A-B — this is the
experimental variant, e.g. integrator factual/canonical/blended) and a USER
template (role='user', served as the deterministic default), and returns both.
The tracked (template_id, variant_id) is the system (experimental) variant.

Build interpretations (ruled): ab_allowed=False serves variant_id='default'
(§5 model-exploration path, Q1 ✓); calibration freezes the 'default' system
template + temperature (Q2 ✓ / Eval condition — the temperature freeze lives in
CallManager's CalibrationSnapshot since temperature is ConfigManager's).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.services.llm_manager_errors import PromptNotFoundError
from app.services.llm_manager_types import TAG_AXES, EngagementContext

logger = logging.getLogger(__name__)

_DEFAULT_VARIANT = "default"
_CACHE_TTL_SECONDS = 60  # hot-reload window (§3.1)


@dataclass(frozen=True)
class PromptTemplate:
    id: int
    module_key: str
    role: str                 # 'system' | 'user'
    variant_id: str
    variant_tags: dict[str, str]
    version: int
    template_body: str
    weight: float


@dataclass(frozen=True)
class RenderedPrompt:
    """PromptManager.render() output — a system+user pair plus the tracked
    (experimental) variant identity that flows into LLMResponse / llm_call_log."""

    system_prompt: str
    user_prompt: str
    template_id: int
    variant_id: str


@dataclass(frozen=True)
class RenderedComposition:
    """PromptManager.resolve_composition() output — the v2 block path (spec §5 /
    SPEC_PROMPT_BLOCK_PERSISTENCE §4). system_prompt is the assembled composition;
    composition_hash + manifest are what the caller upserts into
    prompt_composition_snapshots and stamps on llm_calls (dereferenceable
    attribution, §6)."""

    system_prompt: str
    composition_id: int
    composition_hash: str
    manifest: tuple          # ordered ((block_key, version), …) AFTER filter + authority-last
    variant_id: str


def _blocks_from_rows(member_rows: list, block_rows: list) -> tuple[list[str], dict]:
    """PURE (no DB): resolve each ordered composition member to a Block at its
    effective version — pinned_version when set, else the latest ACTIVE version.
    Returns (ordered_block_keys, {block_key: Block}). Raises PromptNotFoundError
    if a member can't resolve. Unit-tested offline; the DB fetch is separate."""
    from app.services.prompt_blocks import Block

    by_kv = {(r["block_key"], r["version"]): r for r in block_rows}
    latest_active: dict[str, dict] = {}
    for r in block_rows:
        if r["active"]:
            cur = latest_active.get(r["block_key"])
            if cur is None or r["version"] > cur["version"]:
                latest_active[r["block_key"]] = r

    ordered: list[str] = []
    blocks: dict[str, Block] = {}
    for m in member_rows:
        bk = m["block_key"]
        if m["pinned_version"] is not None:
            row = by_kv.get((bk, m["pinned_version"]))
            if row is None:
                raise PromptNotFoundError(f"composition pins {bk}@{m['pinned_version']} — absent in prompt_blocks")
        else:
            row = latest_active.get(bk)
            if row is None:
                raise PromptNotFoundError(f"composition references {bk} with no active version")
        va = row["validated_at"]
        ordered.append(bk)
        blocks[bk] = Block(
            block_key=row["block_key"], block_kind=row["block_kind"], role=row["role"],
            template_body=row["template_body"], version=row["version"],
            condition=row["condition"], is_authority=row["is_authority"],
            directives=frozenset(row["directives"] or ()), owner=row["owner"],
            validated_at=(va.isoformat() if hasattr(va, "isoformat") else va),
        )
    return ordered, blocks


_SYNC_COMP_CACHE: dict[str, tuple[float, "RenderedComposition | None"]] = {}


def resolve_composition_sync(
    module_key: str, *, conditions: dict[str, bool], template_vars: dict | None = None,
    ttl_seconds: int = 60,
) -> "RenderedComposition | None":
    """SYNC composition read path for sync call-sites (the sequential integrator runs
    in a sync context; awaiting the async resolver there would break the event loop).
    psycopg2 + a 60s TTL cache (§3.1). Returns None when no active composition exists
    (caller falls back to the 049/hardcoded prompt). Fail-soft: any DB/parse error
    returns None so a resolution problem can never break a live turn — it degrades to
    the existing prompt, never crashes."""
    import os

    now = time.monotonic()
    hit = _SYNC_COMP_CACHE.get(module_key)
    if hit and (now - hit[0]) < ttl_seconds:
        return hit[1]

    url = os.environ.get("CHAT_RAG_DATABASE_URL")
    if not url:
        return None
    result: "RenderedComposition | None" = None
    try:
        import psycopg2
        import psycopg2.extras

        conn = _connect_sync(url)
        try:
            conn.autocommit = True
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, variant_id, composition_hash, status FROM prompt_compositions "
                    "WHERE module_key=%s AND active=true AND variant_id='default' LIMIT 1",
                    (module_key,),
                )
                comp = cur.fetchone()
                if comp is not None:
                    cur.execute(
                        "SELECT position, block_key, pinned_version FROM prompt_composition_members "
                        "WHERE composition_id=%s ORDER BY position",
                        (comp["id"],),
                    )
                    members = cur.fetchall()
                    keys = [m["block_key"] for m in members]
                    cur.execute(
                        "SELECT block_key, version, block_kind, role, template_body, condition, "
                        "is_authority, directives, owner, validated_at, active "
                        "FROM prompt_blocks WHERE block_key = ANY(%s)",
                        (keys,),
                    )
                    block_rows = cur.fetchall()
                    ordered, blocks = _blocks_from_rows(members, block_rows)
                    from app.services.prompt_blocks import BlockAssembler

                    asm = BlockAssembler().assemble(
                        ordered, blocks, conditions=conditions, template_vars=template_vars or {}
                    )
                    result = RenderedComposition(
                        system_prompt=asm.text, composition_id=comp["id"],
                        composition_hash=asm.composition_hash, manifest=asm.blocks_used,
                        variant_id=comp["variant_id"],
                    )
        finally:
            conn.close()
    except Exception as e:  # fail-soft: never break a turn on a resolution error
        logger.warning("resolve_composition_sync(%s) failed, falling back: %s", module_key, e)
        result = None

    _SYNC_COMP_CACHE[module_key] = (now, result)
    return result


def _connect_sync(url: str):
    """psycopg2 connect using the app's OWN proven fallback-URL mechanism
    (app.db_client._get_fallback_url) rather than a bespoke parser — that function
    already does the regex password-injection Cloud Run requires (CHAT_DB_PASSWORD
    has no URL slot to fill via a separate connect() kwarg for a socket DSN; libpq
    needs it IN the URL/DSN string). ``url`` is accepted for API compat but ignored:
    _get_fallback_url reads CHAT_RAG_DATABASE_URL + CHAT_DB_PASSWORD from the
    environment directly, which is the same source this module's caller uses."""
    import psycopg2
    from app.db_client import _get_fallback_url

    full_url = _get_fallback_url("chat")
    if not full_url:
        raise RuntimeError("resolve_composition_sync: CHAT_RAG_DATABASE_URL not set")
    return psycopg2.connect(full_url, connect_timeout=10)


class PromptManager:
    def __init__(self, *, ttl_seconds: int = _CACHE_TTL_SECONDS, rng=None) -> None:
        import random as _random

        self._ttl = ttl_seconds
        self._rng = rng or _random.Random()
        self._cache: dict[str, tuple[float, list[PromptTemplate]]] = {}
        # autoescape=False (Tech Health flag 2) — prompts are not HTML.
        # keep_trailing_newline=True (AC-6) — Jinja's default strips a trailing
        # newline the current prompts keep; that would be a 1-byte-per-prompt drift.
        import jinja2

        self._jinja = jinja2.Environment(autoescape=False, keep_trailing_newline=True)

    # ── loading + cache ──────────────────────────────────────────────────────

    async def _load_active(self, module_key: str) -> list[PromptTemplate]:
        now = time.monotonic()
        cached = self._cache.get(module_key)
        if cached and (now - cached[0]) < self._ttl:
            return cached[1]

        rows = await self._fetch_rows(module_key)
        # Highest active version per (role, variant_id).
        best: dict[tuple[str, str], PromptTemplate] = {}
        for r in rows:
            t = _row_to_template(r)
            key = (t.role, t.variant_id)
            prev = best.get(key)
            if prev is None or t.version > prev.version:
                best[key] = t
        templates = list(best.values())
        self._cache[module_key] = (now, templates)
        return templates

    async def _fetch_rows(self, module_key: str):
        from app.services.pg_pool import get_pool

        pool = await get_pool()
        if pool is None:
            logger.warning("PromptManager: no PG pool; module_key=%s", module_key)
            return []
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT id, module_key, role, variant_id, variant_tags, version,
                       template_body, weight
                FROM prompt_templates
                WHERE module_key = $1 AND active = true
                """,
                module_key,
            )

    def invalidate(self, module_key: str | None = None) -> None:
        if module_key is None:
            self._cache.clear()
        else:
            self._cache.pop(module_key, None)

    # ── v2 composition path (migration 053, spec §5) ─────────────────────────

    async def _fetch_active_composition(self, module_key: str):
        from app.services.pg_pool import get_pool

        pool = await get_pool()
        if pool is None:
            logger.warning("PromptManager: no PG pool; composition module_key=%s", module_key)
            return None
        async with pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT id, variant_id, composition_hash, status
                FROM prompt_compositions
                WHERE module_key = $1 AND active = true AND variant_id = 'default'
                LIMIT 1
                """,
                module_key,
            )

    async def _fetch_members(self, composition_id: int):
        from app.services.pg_pool import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT position, block_key, pinned_version
                FROM prompt_composition_members
                WHERE composition_id = $1
                ORDER BY position
                """,
                composition_id,
            )

    async def _fetch_blocks(self, block_keys: list[str]):
        from app.services.pg_pool import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT block_key, version, block_kind, role, template_body, condition,
                       is_authority, directives, owner, validated_at, active
                FROM prompt_blocks
                WHERE block_key = ANY($1::text[])
                """,
                block_keys,
            )

    async def resolve_composition(
        self, module_key: str, template_vars: dict, *, conditions: dict[str, bool]
    ) -> "RenderedComposition | None":
        """v2 block path (spec §5). Returns None when no ACTIVE composition exists
        for module_key — the caller then falls back to render() (049 template path),
        which is how per-module cutover stays reversible. When a composition IS
        active, assemble it (authority-last, conditional-drop, validated_at gate all
        enforced by BlockAssembler) and return the system prompt + composition_hash
        + manifest for snapshot upsert + llm_calls attribution."""
        comp = await self._fetch_active_composition(module_key)
        if comp is None:
            return None
        members = await self._fetch_members(comp["id"])
        if not members:
            raise PromptNotFoundError(
                f"composition {comp['id']} (module_key={module_key!r}) is active but has no members"
            )
        block_rows = await self._fetch_blocks([m["block_key"] for m in members])
        ordered, blocks = _blocks_from_rows(members, block_rows)

        from app.services.prompt_blocks import BlockAssembler

        assembled = BlockAssembler().assemble(
            ordered, blocks, conditions=conditions, template_vars=template_vars
        )
        return RenderedComposition(
            system_prompt=assembled.text,
            composition_id=comp["id"],
            composition_hash=assembled.composition_hash,
            manifest=assembled.blocks_used,
            variant_id=comp["variant_id"],
        )

    # ── tag-match: exact → partial → default ─────────────────────────────────

    @staticmethod
    def _match_tier(templates: list[PromptTemplate], ctx: EngagementContext) -> list[PromptTemplate]:
        """Best-matching tier among a same-role candidate list.
        exact (all 5 axes) → partial (most-specific subset) → default."""
        ctx_tags = ctx.as_tags()

        def specified_and_matches(t: PromptTemplate) -> tuple[bool, int]:
            tags = t.variant_tags or {}
            keys = [k for k in TAG_AXES if k in tags and tags[k] is not None]
            if not keys:
                return (False, 0)
            ok = all(tags[k] == ctx_tags.get(k) for k in keys)
            return (ok, len(keys))

        exact = [t for t in templates if specified_and_matches(t) == (True, len(TAG_AXES))]
        if exact:
            return exact
        partial = [(t, n) for t in templates for (ok, n) in [specified_and_matches(t)] if ok and 0 < n < len(TAG_AXES)]
        if partial:
            top = max(n for _, n in partial)
            return [t for t, n in partial if n == top]
        return [t for t in templates if t.variant_id == _DEFAULT_VARIANT]

    def _pick(self, candidates: list[PromptTemplate], *, ab_allowed: bool) -> PromptTemplate | None:
        if not candidates:
            return None
        if not ab_allowed:
            for t in candidates:
                if t.variant_id == _DEFAULT_VARIANT:
                    return t
            return candidates[0]
        if len(candidates) == 1:
            return candidates[0]
        weights = [max(0.0, t.weight) for t in candidates]
        if sum(weights) <= 0:
            return candidates[0]
        return self._rng.choices(candidates, weights=weights, k=1)[0]

    def _select(self, role_templates: list[PromptTemplate], ctx: EngagementContext, *, ab_allowed: bool) -> PromptTemplate | None:
        return self._pick(self._match_tier(role_templates, ctx), ab_allowed=ab_allowed)

    def _render_body(self, t: PromptTemplate, template_vars: dict) -> str:
        return self._jinja.from_string(t.template_body).render(**template_vars)

    # ── render ───────────────────────────────────────────────────────────────

    async def render(
        self,
        module_key: str,
        ctx: EngagementContext,
        template_vars: dict,
        *,
        ab_allowed: bool = True,
        frozen_template: PromptTemplate | None = None,
    ) -> RenderedPrompt:
        """Select a system template (tag-match → fallback → A/B) + a user template
        (deterministic default) and Jinja2-render both. Returns the pair plus the
        system (experimental) variant identity.

        ``frozen_template`` (calibration) pins the SYSTEM template; the user
        template is still the deterministic default. Temperature freeze lives in
        CallManager's CalibrationSnapshot.
        """
        templates = await self._load_active(module_key)
        if not templates and frozen_template is None:
            raise PromptNotFoundError(f"PromptManager: no active templates for module_key={module_key!r}")

        system_t = frozen_template if frozen_template is not None else self._select(
            [t for t in templates if t.role == "system"], ctx, ab_allowed=ab_allowed
        )
        user_t = self._select([t for t in templates if t.role == "user"], ctx, ab_allowed=False)

        if system_t is None and user_t is None:
            raise PromptNotFoundError(
                f"PromptManager: no system or user variant matched for module_key={module_key!r}"
            )

        system_prompt = self._render_body(system_t, template_vars) if system_t is not None else ""
        user_prompt = self._render_body(user_t, template_vars) if user_t is not None else ""
        primary = system_t or user_t  # experimental identity = system if present
        return RenderedPrompt(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            template_id=primary.id,
            variant_id=primary.variant_id,
        )

    # ── calibration ──────────────────────────────────────────────────────────

    async def default_template(self, module_key: str) -> PromptTemplate:
        """The module's default SYSTEM template — what a calibration batch pins
        (Q2). Falls back to the default user template if the module is user-only.
        CallManager composes this with the frozen temperature (CalibrationSnapshot)."""
        templates = await self._load_active(module_key)
        systems = [t for t in templates if t.role == "system" and t.variant_id == _DEFAULT_VARIANT]
        if systems:
            return systems[0]
        users = [t for t in templates if t.role == "user" and t.variant_id == _DEFAULT_VARIANT]
        if users:
            return users[0]
        raise PromptNotFoundError(
            f"PromptManager.default_template: no 'default' variant for module_key={module_key!r}"
        )


def _row_to_template(r) -> PromptTemplate:
    tags = r["variant_tags"]
    if isinstance(tags, str):
        import json

        tags = json.loads(tags)
    return PromptTemplate(
        id=int(r["id"]),
        module_key=r["module_key"],
        role=r["role"],
        variant_id=r["variant_id"],
        variant_tags=dict(tags or {}),
        version=int(r["version"]),
        template_body=r["template_body"],
        weight=float(r["weight"]),
    )
