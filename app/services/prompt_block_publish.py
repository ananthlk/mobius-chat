"""The one place a new prompt_blocks row gets written.

Governor seat, 2026-09-10: admin_prompts.create_block_version() (the
Composition Studio's HTTP path) and react_block_seed.py's seed() (the
deploy-time seed path) were two independent writers to the same table.
Migration 066 (token_counts) landed in one and not the other -- the seed
path silently wrote the default '{}', which is the DOCUMENTED "no stored
count for this tokenizer" contract, so it read as intentional absence
rather than a path that forgot. "The next feature added to one path is
the next thing the other forgets" -- the fix isn't reconciling two
writers that happen to agree today (agreement is not a property anything
enforces), it's having only one.

Both callers now go through publish_block_version(). The two callers
still differ in what they need:
  - The admin/Studio path always wants a fresh version (auto-incremented)
    and never expects the block to already exist.
  - The seed path is idempotent and safe-to-rerun by design: it passes an
    explicit version number (from its versioned BlockSpec list) and must
    skip re-publishing (and skip the token-count network call) when that
    exact (block_key, version) is already there.
One function, one branch on whether `version` was given explicitly,
covers both -- rather than two call sites each deciding independently
what a "new" block publish means.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Only these block kinds carry real, countable content -- derived/per_turn
# blocks (e.g. react.tool_manifest's "{{ tool_manifest_text }}" placeholder)
# have no content of their own to count; the runtime-variable part is
# priced by whoever renders it.
_COUNTABLE_BLOCK_KINDS = ("static", "conditional")


async def publish_block_version(
    conn: Any,
    *,
    block_key: str,
    template_body: str,
    owner: str,
    created_by: str,
    block_kind: str = "static",
    role: str = "system",
    condition: str | None = None,
    is_authority: bool = False,
    directives: list[str] | None = None,
    activate: bool = True,
    version: int | None = None,
    auto_validate: bool = True,
    validated_at: Any = None,
    validated_by: str | None = None,
) -> dict:
    """Insert one new prompt_blocks row, computing token_counts and the
    delta against the version it supersedes. Runs on a caller-supplied,
    already-acquired connection -- the caller owns the transaction (the
    admin path wraps this in its own `conn.transaction()`; seed() runs
    the whole seed in one transaction across many blocks).

    ``version=None`` (admin/Studio path): always publishes a new version,
    auto-incremented from the current max. ``version=<int>`` (seed path):
    idempotent -- if that exact (block_key, version) already exists,
    returns {"skipped": True} immediately without touching the tokenizer
    (seed() is documented safe-to-rerun; computing a fresh count before
    finding out the row already exists would cost a real API call on
    every rerun for every already-seeded block).

    ``auto_validate=True`` (default, matches the admin/Studio path's
    original behavior exactly): validated_at is stamped `now()` when
    ``activate or is_authority``, else left NULL -- a live editorial
    action really did just validate this block. ``auto_validate=False``
    (the seed path): the caller supplies an EXPLICIT ``validated_at``/
    ``validated_by`` (even if both are None), because a seeded block's
    validation timestamp -- when one exists -- describes a real historical
    review baked into its BlockSpec, not "whoever ran the seed script,
    just now." Stamping `now()` there would fabricate provenance for
    exactly the kind of field this session has spent all day insisting be
    real rather than asserted.
    """
    directives = directives or []

    if version is not None:
        existing = await conn.fetchval(
            "SELECT 1 FROM prompt_blocks WHERE block_key = $1 AND version = $2",
            block_key, version,
        )
        if existing:
            return {"skipped": True, "block_key": block_key, "version": version}
        next_ver = version
    else:
        next_ver = await conn.fetchval(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM prompt_blocks WHERE block_key = $1",
            block_key,
        )

    # Network call before any write -- never hold a transaction/connection
    # across it.
    token_counts: dict[str, int] = {}
    if block_kind in _COUNTABLE_BLOCK_KINDS:
        from app.services.prompt_token_counting import compute_token_counts
        token_counts = compute_token_counts(template_body)

    prev_row = await conn.fetchrow(
        """
        SELECT version, token_counts FROM prompt_blocks
        WHERE block_key = $1 AND active = true
        ORDER BY version DESC LIMIT 1
        """,
        block_key,
    )

    if auto_validate:
        validated_at = datetime.now(timezone.utc) if (activate or is_authority) else None
        validated_by = created_by if validated_at else None
    # else: use validated_at/validated_by exactly as passed in, including None.
    row = await conn.fetchrow(
        """
        INSERT INTO prompt_blocks
          (block_key, version, block_kind, role, template_body, condition,
           is_authority, directives, owner, validated_at, validated_by, active, created_by,
           token_counts)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb)
        ON CONFLICT (block_key, version) DO NOTHING
        RETURNING id, block_key, version, active, token_counts
        """,
        block_key, next_ver, block_kind, role, template_body, condition,
        is_authority, directives, owner, validated_at, validated_by, activate, created_by,
        json.dumps(token_counts),
    )
    if row is None:
        # ON CONFLICT DO NOTHING fired despite the pre-check above -- a
        # concurrent publisher won the race between the SELECT and the
        # INSERT. Report it as skipped rather than returning a fabricated
        # row; the caller that actually won already has the real one.
        return {"skipped": True, "block_key": block_key, "version": next_ver}

    token_delta: dict[str, dict[str, float]] = {}
    if prev_row is not None:
        raw_prev = prev_row["token_counts"]
        prev_counts = json.loads(raw_prev) if isinstance(raw_prev, str) else (raw_prev or {})
        for tokenizer, new_count in token_counts.items():
            old_count = prev_counts.get(tokenizer)
            if old_count is None:
                continue
            delta = new_count - old_count
            pct = round(100.0 * delta / old_count, 1) if old_count else None
            token_delta[tokenizer] = {"from": old_count, "to": new_count, "delta": delta, "pct": pct}

    if token_delta:
        grew = any(d["delta"] > 0 for d in token_delta.values())
        log_fn = logger.warning if grew else logger.info
        log_fn(
            "[prompt-block-publish] new block version block_key=%s version=%s (was %s) "
            "activate=%s token_counts=%s delta=%s by=%s",
            block_key, next_ver, prev_row["version"], activate, token_counts, token_delta, created_by,
        )
    else:
        logger.info(
            "[prompt-block-publish] new block version block_key=%s version=%s activate=%s token_counts=%s by=%s",
            block_key, next_ver, activate, token_counts, created_by,
        )

    out = dict(row)
    out["token_delta"] = token_delta
    out["skipped"] = False
    return out
