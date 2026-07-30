"""Seed prompt_blocks + prompt_compositions + prompt_composition_members
(migration 053) from the validated v2 block set.

STAGED — ready to run the instant 053 lands (gated on the tables existing). This
is step 2 of docs/RUNBOOK_V2_DEPLOY.md. Idempotent: blocks upsert ON CONFLICT
(block_key, version) DO NOTHING; a composition is (re)written only when its
coherence gate passes, so an incoherent composition is never seeded active.

Design notes:
  * Long/authoritative bodies (the AnswerCard schema+rules block) are sourced
    from the single existing source of truth — chat_config — at seed time via
    `resolve_body`, NOT duplicated here. Short structural blocks are inline.
  * composition_hash is computed with the SAME prompt_blocks._hash the assembler
    uses, so the seeded hash matches what runtime will produce.
  * check_coherence() runs before an active composition is written — the real
    write-time gate, at seed time.
"""
from __future__ import annotations

from dataclasses import dataclass

import datetime as _dt

from app.services.prompt_blocks import BlockAssembler, Block, _hash

_VALIDATED_AT = "2026-07-26"          # v2 gate closed 6/6; blocks validated at sign-off


def _ts(s: str | None):
    """ISO date/datetime string → tz-aware datetime for a timestamptz column (asyncpg
    validates the Python type, so a bare string is rejected)."""
    if not s:
        return None
    return _dt.datetime.fromisoformat(s).replace(tzinfo=_dt.timezone.utc) if len(s) <= 10 \
        else _dt.datetime.fromisoformat(s)


@dataclass(frozen=True)
class BlockSpec:
    block_key: str
    block_kind: str
    role: str
    body: str
    condition: str | None = None
    is_authority: bool = False
    directives: tuple = ()
    owner: str = "llm-agent"
    validated_at: str | None = None    # authority blocks REQUIRE this (053 ck_authority_validated)


# ── the validated enricher block set (first module migrated; matches the A/B
#    decomposition proven BETTER against real Claude in v2_migrate_enricher.py) ──
_ENRICHER_PREAMBLE = (
    "You are the ENRICHER for a retrieval-based Q&A system.\n\n"
    "The user has ALREADY seen react_draft (provided in the input JSON). Your job is NOT to "
    "restate or rephrase it. Your job is to:\n"
    "  1. Correct any factual error in the draft (if sources contradict it)\n"
    "  2. Pull verbatim evidence from source_texts to back up key claims\n"
    "  3. Distill what matters into takeaways\n"
    "  4. Specify concrete next actions\n"
    "  5. Flag what the sources did not cover"
)
_ENRICHER_FACTUAL = (
    "Mode-specific rules for FACTUAL:\n"
    "- Set mode = 'FACTUAL'.\n"
    "- sections: 2–3 sections, each with 3–6 substantive bullets (8–25 words each). "
    "Intent must be one of: process, requirements, definitions, exceptions, references. "
    "No stub bullets. FACTUAL hides sections behind 'Show details' — they must carry real detail.\n"
    "- direct_answer is ONE sentence — the single operative fact.\n"
    "- required_variables: if the answer depends on an unknown (service code, plan subtype), "
    "list it. Add one followup question only if the user must clarify to get a definitive answer."
)

BLOCK_SPECS: list[BlockSpec] = [
    # The enricher blocks are a FAITHFUL, byte-preserving decomposition of the live
    # chat_config integrator_{factual,blended,canonical}_system prompts (single source
    # of truth) — sliced at section markers, not re-typed. intro+schema are IDENTICAL
    # across all three modes (verified: only the mode-rules tail differs), so they're
    # shared blocks; each mode gets its own rules leaf.
    # concat(intro, schema, <mode>_rules) == chat_config.integrator_<mode>_system.
    BlockSpec("module.enricher", "static", "system", "@enricher:intro", owner="llm-agent"),
    BlockSpec("enricher.answercard_schema_and_rules", "static", "system",
              "@enricher:schema", owner="llm-agent"),
    BlockSpec("module.enricher.factual", "static", "system", "@enricher:factual", owner="llm-agent"),
    BlockSpec("module.enricher.blended", "static", "system", "@enricher:blended", owner="llm-agent"),
    BlockSpec("hipaa_context", "conditional", "system",
              "HIPAA: never surface or invent a patient identifier (name, MRN, DOB, address). "
              "Never fabricate a clinical fact. If PHI is required to answer and none is grounded, "
              "say so — do not guess.",
              condition="hipaa_on", is_authority=True, owner="compliance", validated_at=_VALIDATED_AT),
    BlockSpec("forced_json", "conditional", "system",
              "Return ONLY valid JSON. No markdown, no commentary, no extra text.",
              condition="emits_json", is_authority=True, directives=("output:json",),
              owner="llm-agent", validated_at=_VALIDATED_AT),
]

# module_key → ordered block_keys (authority blocks last; assembler re-asserts).
COMPOSITIONS: dict[str, list[str]] = {
    "integrator_enricher_factual": [
        "module.enricher",
        "enricher.answercard_schema_and_rules",
        "module.enricher.factual",
        "hipaa_context",
        "forced_json",
    ],
    "integrator_enricher_blended": [
        "module.enricher",
        "enricher.answercard_schema_and_rules",
        "module.enricher.blended",
        "hipaa_context",
        "forced_json",
    ],
}


_ENRICHER_MODE_ATTR = {
    "factual": "integrator_factual_system",
    "blended": "integrator_blended_system",
    "canonical": "integrator_canonical_system",
}
_SCHEMA_MARKER = "AnswerCard schema"


def _decompose_enricher(strict: bool = True) -> dict:
    """FAITHFUL byte-preserving split of each chat_config.integrator_<mode>_system into
    (intro, schema, <mode>) at the shared schema marker + the per-mode rules tail.
    concat(intro, schema, mode) == the original prompt for EVERY mode — no re-typing,
    no drift. intro/schema are asserted IDENTICAL across modes (else seeding refuses —
    a divergence there would mean the modes no longer share the schema, and silently
    seeding one mode's intro/schema for all three would be wrong). strict=False
    tolerates absence (dry-run)."""
    try:
        from app.chat_config import ChatPromptsConfig
        cfg = ChatPromptsConfig()
    except Exception:
        cfg = None

    out: dict[str, str] = {}
    intro = schema = None
    for mode, attr in _ENRICHER_MODE_ATTR.items():
        s = (getattr(cfg, attr, "") or "") if cfg else ""
        a = s.find(_SCHEMA_MARKER)
        rules_marker = f"Mode-specific rules for {mode.upper()}"
        b = s.find(rules_marker)
        if not s or a <= 0 or b <= a:
            if strict:
                raise ValueError(
                    f"block_seed: could not decompose chat_config.{attr} at "
                    f"{_SCHEMA_MARKER!r} / {rules_marker!r} — prompt structure changed."
                )
            out[mode] = f"<<{mode}>>"
            continue
        this_intro, this_schema, this_rules = s[:a], s[a:b], s[b:]
        if intro is None:
            intro, schema = this_intro, this_schema
        elif strict and (this_intro != intro or this_schema != schema):
            raise ValueError(
                f"block_seed: {attr}'s intro/schema diverges from the other modes' — "
                f"they're supposed to be shared blocks. Re-check before seeding a split."
            )
        out[mode] = this_rules
    out["intro"] = intro or "<<intro>>"
    out["schema"] = schema or "<<schema>>"
    return out


def resolve_body(spec: BlockSpec, *, strict: bool = True) -> str:
    """Single-source-of-truth resolver. '@enricher:<part>' bodies are sliced from the
    live chat_config.integrator_factual_system (faithful decomposition) so the seeded
    blocks never drift from the live prompt. Inline bodies pass through unchanged.
    strict=True (real seed): a failed decomposition RAISES (no blind seed);
    strict=False (dry-run): placeholders, since body TEXT doesn't affect coherence."""
    if not spec.body.startswith("@enricher:"):
        return spec.body
    part = spec.body.split(":", 1)[1]
    return _decompose_enricher(strict=strict)[part]


def _to_block(spec: BlockSpec, body: str, version: int = 1) -> Block:
    return Block(
        block_key=spec.block_key, block_kind=spec.block_kind, role=spec.role,
        template_body=body, version=version, condition=spec.condition,
        is_authority=spec.is_authority, directives=frozenset(spec.directives),
        owner=spec.owner, validated_at=spec.validated_at,
    )


def build_plan(*, strict: bool = True) -> tuple[list[tuple[BlockSpec, str]], dict[str, dict]]:
    """Resolve bodies + coherence-check every composition BEFORE any DB write.
    Returns (resolved_blocks, compositions_plan). Raises on an incoherent
    composition — the real check_coherence() gate, at seed time. strict=False
    lets the plan validate without a wired chat_config attr (dry-run)."""
    resolved = [(s, resolve_body(s, strict=strict)) for s in BLOCK_SPECS]
    blocks = {s.block_key: _to_block(s, body) for s, body in resolved}

    asm = BlockAssembler()
    plan: dict[str, dict] = {}
    for module_key, comp in COMPOSITIONS.items():
        problems = asm.check_coherence(
            comp, blocks,
            required={"hipaa_on": "hipaa_context"},
            active_conditions={"hipaa_on", "emits_json"},
        )
        if problems:
            raise ValueError(f"block_seed: composition {module_key!r} is incoherent: {problems}")
        pairs = tuple((blocks[bk].block_key, blocks[bk].version) for bk in comp)
        plan[module_key] = {"blocks": comp, "composition_hash": _hash(pairs)}
    return resolved, plan


# ── DB write (idempotent). Executed only post-053; guarded so import is safe. ──
_UPSERT_BLOCK = """
INSERT INTO prompt_blocks
  (block_key, version, block_kind, role, template_body, condition, is_authority,
   directives, owner, validated_at, validated_by, active, created_by)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::timestamptz,$11,true,'block_seed')
ON CONFLICT (block_key, version) DO NOTHING
"""
_UPSERT_COMPOSITION = """
INSERT INTO prompt_compositions
  (module_key, variant_id, version, status, active, composition_hash,
   coherence_checked_at, created_by)
VALUES ($1,'default',1,'validated',true,$2, NOW(),'block_seed')
ON CONFLICT (module_key, variant_id, version) DO NOTHING
RETURNING id
"""
_INSERT_MEMBER = """
INSERT INTO prompt_composition_members (composition_id, position, block_key, pinned_version)
VALUES ($1,$2,$3,$4)
ON CONFLICT (composition_id, position) DO NOTHING
"""


async def seed(conn) -> dict:
    """Idempotent seed against a live 053 schema. Coherence is checked before any
    write (build_plan raises otherwise). Returns a per-module summary."""
    resolved, plan = build_plan()
    for spec, body in resolved:
        blk = _to_block(spec, body)
        await conn.execute(
            _UPSERT_BLOCK, blk.block_key, blk.version, blk.block_kind, blk.role,
            blk.template_body, blk.condition, blk.is_authority, list(blk.directives),
            blk.owner, _ts(blk.validated_at), blk.owner if blk.validated_at else None,
        )
    out = {}
    for module_key, info in plan.items():
        row = await conn.fetchrow(_UPSERT_COMPOSITION, module_key, info["composition_hash"])
        if row is not None:  # freshly inserted; write its ordered members
            for pos, bk in enumerate(info["blocks"], start=1):
                # pinned_version=NULL (not a hardcoded version): a 'validated' (not yet
                # 'frozen') composition is meant to FLOAT to each block's latest active
                # version — that's the whole point of the live-editable block system (a
                # new block version takes effect with no redeploy, no composition edit).
                # Pinning here would silently defeat that (caught live: a v2 block edit
                # had no effect until this was found and fixed).
                await conn.execute(_INSERT_MEMBER, row["id"], pos, bk, None)
            out[module_key] = {"seeded": True, "composition_hash": info["composition_hash"]}
        else:
            out[module_key] = {"seeded": False, "reason": "already present"}
    return out


if __name__ == "__main__":  # pragma: no cover — dry-run without a DB
    # `python3 -m app.services.block_seed` validates the plan (coherence + hashes)
    # WITHOUT touching a DB, so the seed can be checked pre-053.
    _resolved, _plan = build_plan(strict=False)
    print("block_seed dry-run — plan is coherent:")
    for mk, info in _plan.items():
        print(f"  {mk}: {len(info['blocks'])} blocks  hash={info['composition_hash'][:16]}…")
