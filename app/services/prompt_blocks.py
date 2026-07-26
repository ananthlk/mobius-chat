"""Composable prompt blocks — the v2 assembler.

A prompt is an ordered assembly of typed, owned, validated blocks rather than one
monolithic template_body. Spec: docs/SPEC_LLMMANAGER_V2.md §3.

Structural guarantees this module enforces (so correctness doesn't depend on
whoever authored the composition):
  * AUTHORITY LAST — authority blocks (hipaa_context, forced_json, refuse_rules)
    always render after non-authority blocks, so a per-turn user-steer injection
    can shift emphasis but can never reach past a promise block. (§3.3, AC-1/AC-10)
  * CONDITIONAL DROP — a conditional block renders only when its condition holds
    (hipaa_on, emits_json, has_org). (AC-2)
  * composition_hash — the ordered (block_key, version) list that built the call,
    hashed, for ablation/attribution. (§6, AC-6)

The coherence gate (validate-time, not per-call) checks that individually-valid
blocks are also jointly-valid (§7 / AC-5): required blocks present, no conflicting
output directives.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import jinja2

_ENV = jinja2.Environment(autoescape=False, keep_trailing_newline=True)  # §3.2 / AC-6


@dataclass(frozen=True)
class Block:
    block_key: str
    block_kind: str            # 'static' | 'conditional' | 'derived' | 'per_turn'
    role: str                  # 'system' | 'user'
    template_body: str
    version: int = 1
    variant_id: str = "default"
    variant_tags: dict = field(default_factory=dict)
    condition: str | None = None   # None = always-present; else a key in `conditions`
    is_authority: bool = False     # immutable; must render after non-authority
    directives: frozenset = frozenset()   # e.g. {'output:json'} / {'output:prose'} — coherence gate
    owner: str | None = None
    validated_at: str | None = None   # owner sign-off timestamp; authority blocks REQUIRE it (TH-C4/CO-C4)


@dataclass(frozen=True)
class AssembledPrompt:
    text: str
    composition_hash: str
    blocks_used: tuple          # ordered ((block_key, version), …) after filter + authority-last


class CoherenceError(ValueError):
    """A composition of individually-valid blocks is jointly invalid (§7).
    Raised at composition WRITE time by the coherence gate."""


class AssemblyRefused(RuntimeError):
    """render() fail-closed refusal at assembly time (TH-C1/C4). A violation here
    means a mis-authored composition escaped the write-time coherence gate — we
    refuse to render rather than silently paper over it."""


class UnvalidatedAuthorityError(AssemblyRefused):
    """An authority block with no validated_at reached render (TH-C4/CO-C4)."""


class AuthorityOrderError(AssemblyRefused):
    """A non-authority block is ordered after an authority block at render (TH-C1)."""


def _hash(pairs: tuple) -> str:
    # FULL sha256 (not truncated). composition_hash is the PK of
    # prompt_composition_snapshots and the FK target of llm_calls.composition_hash;
    # a truncated hash + ON CONFLICT DO NOTHING would let a collision silently keep
    # the first manifest and mis-attribute the second — silent mis-attribution in
    # the one table whose only job is faithful attribution (Tech Health, 2026-07-26).
    h = hashlib.sha256()
    for bk, ver in pairs:
        h.update(f"{bk}@{ver}\n".encode())
    return h.hexdigest()


class BlockAssembler:
    def __init__(self, *, separator: str = "\n\n") -> None:
        self._sep = separator

    def _resolve(self, composition: list[str], blocks: dict[str, Block]) -> list[Block]:
        out = []
        for bk in composition:
            b = blocks.get(bk)
            if b is None:
                raise CoherenceError(f"composition references unknown block_key {bk!r}")
            out.append(b)
        return out

    def assemble(
        self,
        composition: list[str],          # ordered block_keys for (module_key, role, variant)
        blocks: dict[str, Block],        # block_key -> Block
        *,
        conditions: dict[str, bool],     # {'hipaa_on': True, 'emits_json': True, 'has_org': False}
        template_vars: dict,
    ) -> AssembledPrompt:
        resolved = self._resolve(composition, blocks)

        # CONDITIONAL DROP — FAIL-CLOSED for authority (CO-C1). A block with
        # condition=None is always present. A conditional block keeps only when
        # its condition is truthy — EXCEPT an authority block whose condition
        # can't be evaluated (missing from `conditions`) is INCLUDED, never
        # dropped: dropping refusal language on an eval error is a fail-OPEN
        # breach. (Non-authority unknown-condition → drop is acceptable.)
        kept: list[Block] = []
        for b in resolved:
            if b.condition is None:
                keep = True
            elif b.condition in conditions:
                keep = bool(conditions[b.condition])
            else:
                keep = b.is_authority  # fail-closed: authority stays on eval failure
            if keep:
                kept.append(b)

        # VALIDATED_AT — FAIL-CLOSED (TH-C4/CO-C4). render() refuses any authority
        # block whose resolved version has no validated_at. Without this, the
        # promise is a policy, not a property.
        for b in kept:
            if b.is_authority and not b.validated_at:
                raise UnvalidatedAuthorityError(
                    f"authority block {b.block_key!r} has no validated_at — "
                    f"refusing to render (fail-closed, TH-C4/CO-C4)"
                )

        # DUAL-LAYER ORDERING — FAIL-CLOSED (TH-C1). The write-time coherence gate
        # is the first layer; render RE-ASSERTS authority-last and RAISES on a
        # violation (does NOT silently reorder — a violation here is a coherence-
        # gate escape and must surface, not be masked).
        seen_authority = False
        for b in kept:
            if b.is_authority:
                seen_authority = True
            elif seen_authority:
                raise AuthorityOrderError(
                    f"non-authority block {b.block_key!r} is ordered after an "
                    f"authority block — refusing to render (fail-closed, TH-C1). "
                    f"Fix the composition ordering."
                )

        rendered = [_ENV.from_string(b.template_body).render(**template_vars) for b in kept]
        text = self._sep.join(r for r in rendered if r != "")
        pairs = tuple((b.block_key, b.version) for b in kept)
        return AssembledPrompt(text=text, composition_hash=_hash(pairs), blocks_used=pairs)

    # ── coherence gate (validate-time, §7 / AC-5) ────────────────────────────

    @staticmethod
    def check_coherence(
        composition: list[str],
        blocks: dict[str, Block],
        *,
        required: dict[str, str] | None = None,   # {'hipaa_on': 'hipaa_context'} → block required when condition set
        active_conditions: set[str] | None = None,
    ) -> list[str]:
        """Return a list of coherence problems (empty = coherent). Individually
        valid blocks can still conflict once composed — this catches that."""
        problems: list[str] = []
        present = set(composition)
        resolved = [blocks[bk] for bk in composition if bk in blocks]

        # 1. Required blocks present for the active conditions (e.g. hipaa_on → hipaa_context).
        for cond, block_key in (required or {}).items():
            if (active_conditions and cond in active_conditions) and block_key not in present:
                problems.append(f"required block {block_key!r} missing while {cond!r} is active")

        # 2. No conflicting output directives (e.g. prose block + forced_json).
        outputs = {d for b in resolved for d in b.directives if d.startswith("output:")}
        if len(outputs) > 1:
            problems.append(f"conflicting output directives in composition: {sorted(outputs)}")

        # 3. Authority hygiene: warn if a non-authority block is authored after an
        #    authority block (assembler fixes it, but the authoring is a smell).
        seen_auth = False
        for b in resolved:
            if b.is_authority:
                seen_auth = True
            elif seen_auth:
                problems.append(f"non-authority block {b.block_key!r} authored after an authority block")
                break
        return problems
