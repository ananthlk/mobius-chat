"""Seed prompt_blocks + prompt_compositions + prompt_composition_members
(migrations 053 + 054) for ReAct's v2 block decomposition.

Phase A (docs/REACT_PHASE_A_IMPLEMENTATION_PLAN.md, signed 2026-07-29):
structural decomposition only — no agent_role-driven content, no temperature
(that's Phase B). Mirrors app/services/block_seed.py's idempotent-upsert
pattern (ON CONFLICT DO NOTHING), extended to also populate the mig-054
`prompt_address` column, which block_seed.py's integrator seed does not use.

Design notes:
  * Block bodies are imported directly from app.pipeline.react.prompts /
    app.pipeline.react.critic — the SAME constants the legacy
    _react_reasoning_system()/CRITIC_SYSTEM_PROMPT use — so seeded blocks
    can never drift from the live prompt (single source of truth, not a
    re-typed copy). Verified byte-identical via
    scratchpad/parity_check.py (AC-6 parity, all cases pass).
  * react.mode_quality_bar and react.tool_manifest are `derived` (opaque)
    blocks: template_body is literally "{{ mode_block_text }}" /
    "{{ tool_manifest_text }}" — the caller computes the real text in
    Python (exactly as today) and passes it via template_vars. This
    avoids in-template Jinja conditionals for mode selection and sidesteps
    resolve_composition()/resolve_composition_sync()'s hardcoded
    `variant_id='default'` (verified in prompt_manager.py — there is no
    5-axis EngagementContext tag-matching on the v2 composition path,
    only on PromptManager.render()'s v1/049 path; an earlier draft of the
    Phase A plan assumed the composition path reused that mechanism —
    it doesn't, and this opaque-var approach avoids needing it).
  * react.critical_rules keeps ONE Jinja var ({{ max_iterations }}, rule 9)
    inline — a static, versioned block that still needs max_iterations in
    template_vars every render, same as the legacy path.
  * react.user_profile is `derived`/opaque ("{{ user_profile_text }}"),
    condition="has_user_profile" — SHARED: a member of react_explore/
    synthesize/draft AND critic_audit (Chat Architecture ruling,
    2026-07-29: same block_key, not a duplicate).
  * react_explore / react_synthesize / react_draft have IDENTICAL member
    lists (addressing/attribution infrastructure only, per the signed
    agent_role scope ruling — Phase B is what would make them diverge).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from app.services.prompt_blocks import Block, BlockAssembler, _hash

_VALIDATED_AT = None  # none of react's Phase A blocks are is_authority — no gate applies


def _ts(s: str | None):
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
    owner: str = "react-agent"
    validated_at: str | None = None
    version: int = 1  # bump when `body` content changes — prompt_blocks is append-only


def _react_block_specs() -> list[BlockSpec]:
    """Built lazily (not at import time) so importing this module never
    requires app.pipeline.react.prompts / critic to already be importable
    in contexts that don't need them (mirrors block_seed.py's lazy
    chat_config import in resolve_body)."""
    from app.pipeline.react import prompts as react_prompts
    from app.pipeline.react import critic as react_critic

    return [
        BlockSpec("react.identity", "static", "system", react_prompts.REACT_IDENTITY_TEXT,
                  owner="product"),
        BlockSpec("react.mode_quality_bar", "derived", "system", "{{ mode_block_text }}",
                  owner="react-agent"),
        BlockSpec("react.tool_manifest", "derived", "system", "{{ tool_manifest_text }}",
                  owner="react-agent"),
        # v3 (2026-07-30): v1's original content (no output_intent/
        # display_summary in the example). v2 briefly added those fields for
        # SPEC_REACT_OUTPUT_INTENT.md; REVERTED per Chat Architecture's
        # 2026-07-30 ruling — classification moves to the enricher (LLM
        # Agent, running on Claude) instead of react (Gemini Flash proved
        # non-compliant across 3 verified prompt-tightening attempts; see
        # docs/REACT_PHASE_A_IMPLEMENTATION_PLAN.md). v3 is a NEW version
        # matching v1's content exactly (bodies are append-only — can't
        # un-write v2), not a reactivation of v1 itself, to keep the
        # version history forward-only and auditable.
        # v4 (2026-08-08, Ananth directly): running_answer schema-example
        # hints now note "**Bold** the key fact" -- same formatting-for-
        # readability fix as react.critical_rules v2 below, applied to the
        # inline JSON-shape examples the model sees at generation time.
        BlockSpec("react.response_shape", "static", "system", react_prompts.REACT_RESPONSE_SHAPE_TEXT,
                  owner="react-agent", version=4),
        # react.output_intent_instruction (v1/v2/v3) REMOVED from the
        # composition (2026-07-30 revert, see react.response_shape's note
        # above) — no longer a member of any active composition. Its
        # BlockSpec is deleted along with REACT_OUTPUT_INTENT_TEXT (the
        # constant it referenced) rather than left as dead/unreferenced
        # code; git history has the full text if this needs reviving.
        BlockSpec("react.format_rules", "static", "system", react_prompts.REACT_FORMAT_RULES_TEXT,
                  owner="react-agent"),
        # is_authority=False for Phase A (Chat Architecture ruling, 2026-07-29 —
        # docs/REACT_PHASE_A_IMPLEMENTATION_PLAN.md §4): marking this authority
        # would make the assembler force it after react.user_profile, inverting
        # today's actual order (critical_rules, then user_profile appended).
        # +"\n" here (not on the shared constant): the legacy f-string's OWN
        # line layout already contributes this trailing newline via a
        # separate mechanism — adding it to the shared constant would double
        # it on the legacy path. Restored 2026-07-30 (was moved to
        # react.output_intent_instruction for v3's reorder; that block is
        # now removed, so critical_rules is last-before-user_profile again,
        # same as the original Phase A layout).
        # v2 (2026-08-08, Ananth directly): the running_answer bullet (rule
        # 1c-2) now instructs bolding the key fact + a 1-3 sentence cap, so
        # the per-round progression (card.reasoning_trace, Chat FE feature)
        # and the Summary tab render as scannable markdown instead of a
        # dense unformatted paragraph.
        # v3 (2026-08-08, Task #77 CI-guard finding): v2 was seeded from a
        # read of REACT_CRITICAL_RULES_TEXT taken BEFORE a concurrent commit
        # (a6f5dfb, Observer verdict/reason for the reframe decision, #41a)
        # landed in this shared checkout -- `git add` by filename picked up
        # that growth when this file was committed, but the DB seed was
        # never regenerated against the post-growth content, so v2's DB row
        # silently mismatched the v2 Python source from the moment it
        # shipped. test_prompt_block_seed_drift.py (Task #77) caught it same
        # day. v3 is a plain re-seed at the CURRENT content, read fresh
        # immediately before this commit -- no wording change of its own.
        #
        # v5 (2026-08-17, Ananth, directly, live finding): adds rule 12 --
        # when a tool result signals ambiguity/insufficient info (multiple
        # fetch_document candidates, missing jurisdiction, etc.) and it
        # can't be resolved from context, use the EXISTING final-answer
        # shape (is_complete=true, tool=null, answer=<real question>)
        # instead of inventing a nonexistent "clarification options"
        # mechanism -- caught live when a model burned 4 rounds trying to
        # call one before falling back to an unrelated tool. Jumps 3->5
        # (not 3->4) because the DB's highest active version was already 4
        # when this was written -- test_prompt_block_seed_drift.py caught
        # that as pre-existing drift, unrelated to this change (confirmed
        # via git stash: identical failure without this edit). Not
        # investigating the 3->4 gap's origin here -- v5 supersedes it.
        # v6 (2026-08-17, Ananth, directly): rule 12 now branches by
        # {% if mode == "agentic" %} -- agentic is told to actually try
        # resolving ambiguity itself first (it has the rounds), copilot/
        # quick are told to ask promptly rather than deliberate (small
        # round budget), and both get "act on your conclusion the SAME
        # round you reach it" -- a live agentic run showed the model
        # correctly deciding to ask in round 2, then re-deriving that
        # same conclusion for 3 more rounds before acting on it in round
        # 5. Requires "mode" in the render's template_vars on BOTH paths
        # (resolve_react_system_prompt_v2 and _react_reasoning_system) --
        # same lesson as v5's rag_call_ceiling miss, applied proactively.
        BlockSpec("react.critical_rules", "static", "system", react_prompts.REACT_CRITICAL_RULES_TEXT + "\n",
                  owner="chat-architecture", version=6),
        # Shared with critic.audit (Chat Architecture ruling, 2026-07-29): one
        # block_key, member of both react_* and critic_audit compositions.
        BlockSpec("react.user_profile", "derived", "system", "{{ user_profile_text }}",
                  condition="has_user_profile", owner="user-manager"),
        BlockSpec("react.no_tools_body", "static", "system", react_prompts.REACT_NO_TOOLS_PROMPT,
                  owner="react-agent"),
        # v2 (2026-08-12, Chat Master directive, Task #89): rule 1 now
        # explicitly names ALL labeled evidence sections (Retrieved
        # sources, Tool outputs, Prior turn context and sources) as valid
        # grounding, not just the first section literally titled
        # "sources" -- closes the false-negative where a correctly
        # carried-forward fact (build_critic_user_message's new prior-
        # turn section) got flagged as unsupported. build_critic_user_
        # message's own body is plain Python, not a block (see the NOTE
        # below) -- only this system-prompt constant needed a version bump.
        BlockSpec("critic.audit_rules", "static", "system", react_critic.CRITIC_SYSTEM_PROMPT,
                  owner="react-agent", version=2),
    ]
    # NOTE: critic's user-turn message (build_critic_user_message() output) is
    # deliberately NOT a block. prompt_compositions has no per-composition
    # `role` column — a composition's members join into ONE string regardless
    # of each member's own `role`. Legacy code calls _call_llm_json(system,
    # user, ...) with two SEPARATE strings; folding the user message into this
    # composition would wrongly concatenate it onto the system prompt. It stays
    # exactly what it always was: Python-computed text passed directly as the
    # call's `user` argument, no DB/BlockAssembler involvement needed.


# module_key -> (prompt_address, ordered block_keys). prompt_address is the
# mig-054 phase-address lookup key (react.explore/synthesize/draft — Chat
# Architecture's seeding map, 2026-07-29); module_key is the composition's
# own identity column (react_explore/...), NOT the bandit arm — the arm
# stays llm_calls.module_key = react_{rn}, unchanged, bridged via
# llm_calls.composition_id, not by matching strings (different tables,
# different meanings, confirmed by Chat Architecture + verified against
# prompt_manager.py's actual resolver code).
# v2/v3 (2026-07-30): added + repositioned react.output_intent_instruction
# for SPEC_REACT_OUTPUT_INTENT.md (see git history / the doc for the full
# attempt record — 3 verified prompt-tightening iterations, none got Gemini
# Flash to comply).
# v4 (2026-07-30): REVERTED per Chat Architecture ruling — classification
# moves to the enricher (Claude) instead. Matches v1's original 7-member
# list exactly (no output_intent_instruction), as a new forward version
# (member lists are fixed per composition_id, can't be edited in place).
_REACT_TOOLS_MEMBERS_V4 = [
    "react.identity",
    "react.mode_quality_bar",
    "react.tool_manifest",
    "react.response_shape",
    "react.format_rules",
    "react.critical_rules",
    "react.user_profile",
]

COMPOSITIONS: dict[str, dict] = {
    "react_explore": {"prompt_address": "react.explore", "members": _REACT_TOOLS_MEMBERS_V4, "version": 4},
    "react_synthesize": {"prompt_address": "react.synthesize", "members": _REACT_TOOLS_MEMBERS_V4, "version": 4},
    "react_draft": {"prompt_address": "react.draft", "members": _REACT_TOOLS_MEMBERS_V4, "version": 4},
    "react_no_tools": {"prompt_address": "react.no_tools", "members": ["react.no_tools_body"], "version": 1},
    "critic_audit": {"prompt_address": "critic.audit",
                      "members": ["critic.audit_rules", "react.user_profile"], "version": 1},
}


def _to_block(spec: BlockSpec) -> Block:
    return Block(
        block_key=spec.block_key, block_kind=spec.block_kind, role=spec.role,
        template_body=spec.body, version=spec.version, condition=spec.condition,
        is_authority=spec.is_authority, directives=frozenset(spec.directives),
        owner=spec.owner, validated_at=spec.validated_at,
    )


def build_plan() -> tuple[list[BlockSpec], dict[str, dict]]:
    """Coherence-check every composition BEFORE any DB write (real
    check_coherence() gate, at seed time) — mirrors block_seed.py's
    build_plan(). Raises on an incoherent composition. Blocks are keyed by
    (block_key, version) for the hash/member resolution below, so a
    composition's `members` list always resolves against the SPECIFIC
    versions defined in _react_block_specs() at plan time — not
    "whatever's active in the DB" — matching pinned_version=NULL's actual
    runtime semantics (float to latest ACTIVE) only once these versions
    are the ones marked active by seed()."""
    specs = _react_block_specs()
    blocks = {s.block_key: _to_block(s) for s in specs}

    asm = BlockAssembler()
    plan: dict[str, dict] = {}
    for module_key, info in COMPOSITIONS.items():
        members = info["members"]
        problems = asm.check_coherence(members, blocks, required=None, active_conditions=None)
        if problems:
            raise ValueError(f"react_block_seed: composition {module_key!r} is incoherent: {problems}")
        pairs = tuple((blocks[bk].block_key, blocks[bk].version) for bk in members)
        plan[module_key] = {
            "prompt_address": info["prompt_address"],
            "members": members,
            "version": info.get("version", 1),
            "composition_hash": _hash(pairs),
        }
    return specs, plan


_UPSERT_BLOCK = """
INSERT INTO prompt_blocks
  (block_key, version, block_kind, role, template_body, condition, is_authority,
   directives, owner, validated_at, validated_by, active, created_by)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::timestamptz,$11,true,'react_block_seed')
ON CONFLICT (block_key, version) DO NOTHING
"""
# Includes prompt_address (mig 054) — block_seed.py's _UPSERT_COMPOSITION
# predates 054 and doesn't set it; react's compositions need it (phase
# addressing, decoupled from module_key/the bandit arm).
_UPSERT_COMPOSITION = """
INSERT INTO prompt_compositions
  (module_key, prompt_address, variant_id, version, status, active, composition_hash,
   coherence_checked_at, created_by)
VALUES ($1,$2,'default',$3,'validated',true,$4, NOW(),'react_block_seed')
ON CONFLICT (module_key, variant_id, version) DO NOTHING
RETURNING id
"""
_INSERT_MEMBER = """
INSERT INTO prompt_composition_members (composition_id, position, block_key, pinned_version)
VALUES ($1,$2,$3,$4)
ON CONFLICT (composition_id, position) DO NOTHING
"""
# `uq_active_composition`/`uq_active_composition_by_address` (mig 053/054) allow
# at most ONE active row per (module_key, variant_id) — regardless of version.
# Bumping a composition's version (a member-list change) requires deactivating
# every other version of that module_key first, in the same transaction as the
# new version's insert, or the new INSERT violates that constraint.
_DEACTIVATE_OTHER_VERSIONS = """
UPDATE prompt_compositions SET active=false
WHERE module_key=$1 AND variant_id='default' AND version <> $2 AND active=true
"""


async def seed(conn) -> dict:
    """Idempotent seed against a live 053+054 schema. Coherence is checked
    before any write. Returns a per-module summary. Safe to re-run: blocks
    upsert ON CONFLICT DO NOTHING (append-only per version); compositions
    at an unchanged version also no-op; a composition whose `version` in
    COMPOSITIONS is NEWER than what's active deactivates the old version
    and activates the new one, atomically, in the caller's transaction."""
    specs, plan = build_plan()
    for spec in specs:
        blk = _to_block(spec)
        await conn.execute(
            _UPSERT_BLOCK, blk.block_key, blk.version, blk.block_kind, blk.role,
            blk.template_body, blk.condition, blk.is_authority, list(blk.directives),
            blk.owner, _ts(blk.validated_at), blk.owner if blk.validated_at else None,
        )
    out = {}
    for module_key, info in plan.items():
        version = info["version"]
        await conn.execute(_DEACTIVATE_OTHER_VERSIONS, module_key, version)
        row = await conn.fetchrow(
            _UPSERT_COMPOSITION, module_key, info["prompt_address"], version, info["composition_hash"]
        )
        if row is not None:  # freshly inserted; write its ordered members
            for pos, bk in enumerate(info["members"], start=1):
                # pinned_version=NULL — a 'validated' composition floats to each
                # block's latest active version (same reasoning as block_seed.py:
                # pinning here would silently defeat live block edits).
                await conn.execute(_INSERT_MEMBER, row["id"], pos, bk, None)
            out[module_key] = {"seeded": True, "composition_hash": info["composition_hash"],
                                "prompt_address": info["prompt_address"], "version": version}
        else:
            out[module_key] = {"seeded": False, "reason": "already present", "version": version}
    return out


if __name__ == "__main__":  # pragma: no cover — dry-run without a DB
    _specs, _plan = build_plan()
    print("react_block_seed dry-run — plan is coherent:")
    for mk, info in _plan.items():
        print(f"  {mk} (prompt_address={info['prompt_address']}): "
              f"{len(info['members'])} blocks  hash={info['composition_hash'][:16]}…")
