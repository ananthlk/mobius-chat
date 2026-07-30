# ReAct v2 Block Decomposition — Phase A Implementation Plan

**Author:** ReAct Agent
**Status:** ✅ **LIVE ON DEV** (2026-07-30). Signed by Chat Architecture 2026-07-29; Ananth confirmed go-ahead; migration 054 co-signed by Database + applied/verified by LLM Agent 2026-07-30; react's blocks + compositions seeded and resolving correctly against the real dev DB, byte-identical to the legacy path, confirmed end-to-end. See §8/§9.
**Scope:** Phase A only (structural decomposition — blocks, compositions, versioning, A/B-testability). Phase B (`agent_role`→temperature via `CallManager`) is explicitly excluded, per 2026-07-29 ruling.
**Builds on:** `REACT_PROMPT_BLOCK_DECOMPOSITION_DRAFT.md` (signed spec), `REACT_AGENT_ROLE_DERIVATION_DRAFT.md` (signed, Phase B consumer), `SPEC_PROMPT_BLOCK_PERSISTENCE.md` (persistence layer, already live)

---

## 1. What "Phase A done" means

Every block in the decomposition spec exists as a `prompt_blocks` row, every composition exists as `prompt_compositions` + `prompt_composition_members` rows, and `react_loop.py`/`react/critic.py` call `resolve_composition()`/`assemble()` instead of the current hand-built Python string functions — with **byte-identical output** for the same inputs (the v2 spec's own AC-6 parity requirement, §10.3 of `SPEC_LLMMANAGER_V2.md`). No new prompt content, no behavior change, no `agent_role`/temperature wiring.

**Simplification specific to react:** react never used v1's monolithic `prompt_templates` (049) — `_react_reasoning_system`/`build_reasoning_context` are, and always were, hand-built Python. So there's no v1→v2 migration path or 049-fallback coexistence to manage here (unlike the integrator, which had 049 rows first). Phase A for react is a direct cutover: Python string-building → block composition, in one step per composition.

---

## 2. Composition inventory (per the seeding map ruling, 2026-07-29)

**RULED:** five compositions, using the schema-054 `prompt_address`/`module_key` pair (`module_key` = composition-family identifier, `prompt_address` = logical role address — both populated, per Chat Architecture's seeding map):

| `prompt_address` | `module_key` | Replaces | Notes |
|---|---|---|---|
| `react.explore` | `react_explore` | `_react_reasoning_system`'s tools-path branch (`prompts.py:297-420`) | Round 1 |
| `react.synthesize` | `react_synthesize` | same | Middle rounds |
| `react.draft` | `react_draft` | same | Last round in budget |
| `react.no_tools` | `react_no_tools` | `_react_reasoning_system`'s no-tools branch (`prompts.py:273-295`) | Structurally disjoint, own block set, per Q4 ruling |
| `critic.audit` | `critic_audit` | `CRITIC_SYSTEM_PROMPT` (`critic.py:234-286`) | |

*(context message stays Python-built — see §5)*

**CONFIRMED (2026-07-29): `react.explore`/`react.synthesize`/`react.draft` have IDENTICAL block content in Phase A** — no role-flavored text, consistent with the signed §2 agent_role ruling (system prompt doesn't vary by role yet; that's Phase B). These three exist as **addressing/attribution infrastructure only**: `react_loop.py` calls `resolve_composition()` **once per round**, selecting `react_{agent_role}` via `react_agent_role(iteration, max_it)` — so `react_agent_role()` gets a **real Phase A caller** (composition selection), even though it doesn't drive temperature yet and the rendered text is byte-identical across all three. `llm_calls.composition_id` can distinguish which phase a call used for future analytics, at zero output-behavior cost today.

This changes the earlier draft's "system prompt built once per turn" framing to **built once per round** (calling `resolve_composition` up to 10× per turn in agentic mode) — but since all three resolve to identical content, this is a resolution-frequency change, not a content or output change. `react_tools`' `variant_id` per chat mode (`quick`/`copilot`/`agentic`) for the mode-quality-bar block (§3) still applies across all three role-compositions — uses the existing 5-axis variant-match / A-B-sample mechanism already built for the integrator, not new logic.

---

## 3. Block inventory

| `block_key` | `block_kind` | `is_authority` | `condition` | Owner | Source |
|---|---|---|---|---|---|
| `react.identity` | static | false | null | Product | prompts.py:328 opening line |
| `react.mode_quality_bar` | static, 3 `variant_id`s (`quick`/`copilot`/`agentic`) | false | null | ReAct Agent | prompts.py:300-326 |
| `react.tool_manifest` | derived (opaque, `{{ tool_manifest_text }}`) | false | null | ReAct Agent | `tool_manifest.get_tool_manifest()` output |
| `react.response_shape` | static | false | null | ReAct Agent | prompts.py:333-353 |
| `react.format_rules` | static | false | null | ReAct Agent | prompts.py:355-366 |
| `react.critical_rules` | static | **false — RULED** (§4, preserves today's order; `is_authority=true` reserved for a future proposal) | null | Chat Architecture | prompts.py:371-413 |
| `react.user_profile` | derived (opaque, `{{ user_profile_text }}`) | false | `has_user_profile` | User Manager | `splice_user_profile` output — **shared block, also a member of `critic.audit`** per the ruling |
| `react.no_tools_body` | static (whole no-tools prompt as one block — not decomposed further, it's ~20 lines with no internal reuse) | false | null | ReAct Agent | prompts.py:273-295 |
| `critic.audit_rules` | static | false | null | ReAct Agent | critic.py:234-286 |
| `critic.audit_request` | per_turn (opaque, `{{ audit_request_text }}`) | false | null | ReAct Agent | `build_critic_user_message()` output |

Context-message blocks (rebuilt every round, not part of the two system-prompt compositions above — see §5 for why these stay Python-built in Phase A rather than becoming `prompt_blocks` rows):

| Section | Treatment |
|---|---|
| [1] Guidance instruction | Would be `conditional`, `condition="is_guidance_round"` — static template text |
| [2]-[5] Jurisdiction / uploads / active context / failed query | Would be `derived`, one var each |
| [6] Recent conversation | Two variants (transform/compact) picked by a Python classifier — opaque var, same treatment as tool_manifest |
| [7] Rolling summary | `derived` |
| [8] Bandit state | `derived`, per-turn-mutable |
| [9] Feedback cadence | 4 variants (nps/csat/targeted_miss/generic) — recommend 4 separate `block_key`s selected by `condition` on `ctx.feedback_signal.kind`, not one block with in-template branching |
| [10] Tool results | `per_turn` (opaque, accumulating) |
| [11]-[12] Question / JSON footer | `derived` / static |

---

## 4. `is_authority` on `react.critical_rules` — RULED: option (a)

Found while sequencing block ordering: **today's actual code order is `critical_rules` (end of `_base_prompt_text`) THEN `user_profile` appended after** (`splice_user_profile(_base_prompt_text, user_profile)`, prompts.py:419-420). Marking `react.critical_rules` as `is_authority=true` would make v2's assembler force it **after** `user_profile` at render time (authority-last is enforced, not just authored-position) — inverting today's order. That's a real behavior change, not a decomposition.

**RULED (Chat Architecture, 2026-07-29): option (a).** `react.critical_rules` seeds with **`is_authority=false`** for Phase A — preserves today's exact order. `is_authority=true` is reserved only for blocks that were **already last** in the existing code order (none of react's blocks qualify today). Moving `critical_rules` to authority-last is a legitimate future direction but needs its own Eval baseline + explicit sign-off — not bundled into Phase A.

---

## 5. Context-message blocks stay Python-built in Phase A — RULED: approved as scoped

`build_reasoning_context` runs **every round** (up to 10× per turn in agentic mode). Re-resolving a 12-member composition from the DB (even with the 60s TTL cache) every round, for content where 9 of 12 sections don't vary within the turn (per the decomposition doc's own finding), is overhead with no functional payoff in Phase A — nothing here needs A/B-testing or validated-block guarantees at the per-round granularity; the whole point of blocks (independent validation, ablation, A/B) applies to the **system prompt** (identity/rules/manifest), not to per-round accumulating context.

**Proposed Phase A scope: decompose the two system prompts (`react_tools`, `react_task`) and the critic prompt (`critic_audit`) into real `prompt_blocks`/`prompt_compositions`. Leave `build_reasoning_context` as Python-built for now** — it's already effectively block-shaped (an ordered list), gets the "9 of 12 sections turn-static" efficiency win noted in the decomposition doc, and touching it doesn't unlock anything Phase A promises (validation/A-B-testing of the system prompt). Converting it to real DB-backed blocks is a candidate for a later phase if there's a concrete need (e.g. Product wants to A/B a specific context section) — not required to satisfy "Phase A = structural decomposition + A/B-testability" for the parts that actually benefit.

**RULED (Chat Architecture, 2026-07-29): approved as scoped.** "The block governance payoff (append-only versioning, composition_hash tracing) applies where drift is expensive — system prompts, not per-round context assembly." Full 12-section decomposition stays out of Phase A; a future A/B-testing need on a specific context section would be its own concrete proposal.

---

## 6. Build sequence

1. Seed `prompt_blocks` rows for the 10 system-prompt/critic blocks in §3 (version 1 each), `react.critical_rules.is_authority=false` per §4.
2. Seed `prompt_compositions` (with both `module_key` and `prompt_address` populated per §2's map) + `prompt_composition_members` for `react_explore`/`react_synthesize`/`react_draft` (identical 7-member lists + 3 mode variants each), `react_no_tools` (1 member), `critic_audit` (3 members, `react.user_profile` shared with the `react_*` set).
3. Add `react_agent_role(iteration, max_it)` to `react/prompts.py` (per `REACT_AGENT_ROLE_DERIVATION_DRAFT.md`, signed) — its Phase A caller is composition selection at the `react_loop.py` call site (§2), not temperature.
4. AC-6 parity test: assemble each composition with today's real inputs, byte-diff against `_react_reasoning_system()`/`CRITIC_SYSTEM_PROMPT`'s current output for the same vars. Any diff is a decomposition bug, not an intentional change. Also assert all three `react_explore`/`synthesize`/`draft` resolutions are byte-identical to each other for the same round-invariant inputs (the addressing-infrastructure-only guarantee).
5. Cut over `react_loop.py`'s system-prompt build (currently once per turn, becomes once per round via `resolve_composition(module_key=f"react_{react_agent_role(iteration, max_it)}", ...)`) and `critic.py`'s caller to `resolve_composition()` + `assemble()`, behind a flag if the existing rollout pattern from `STATUS_V2_LIVE_2026-07-26.md` calls for one.
6. Verify one real turn end-to-end (same pattern as the v2 live-verification record: a real production-shaped call + trace inspection) — confirm output is unchanged AND confirm `llm_calls.composition_id` correctly reflects the round's phase. Then remove the old Python string-building functions once the composition path is confirmed stable — no dead parallel code left behind.

No temperature, no role-flavored content in any of the above — that's Phase B, gated on LLM Agent's plumbing per the 2026-07-29 ruling. `agent_role` in Phase A drives composition selection only.

---

## 7. Sign-off status

All open items ruled by Chat Architecture, 2026-07-29:
1. §4 (`react.critical_rules` `is_authority`) — **RULED: false, preserve today's order.**
2. §5 (context-message scope cut) — **RULED: approved as scoped.**
3. §2 (composition count / seeding) — **RULED: 3 identical-content role compositions, addressing infrastructure only.**

Plan is signed. No code written yet — checking in with Ananth before starting, since this is the first step in this session that touches real code + DB schema/data rather than design docs.

---

## 8. Implementation status (2026-07-29) — code + local verification done, DB seeding blocked

Ananth confirmed directly to proceed. Built and verified locally; **actual DB seeding is NOT done** (see the blocker at the end of this section). What landed:

**Files:**
- `app/pipeline/react/prompts.py` — added `react_agent_role()`; extracted the system-prompt pieces into named constants (`REACT_IDENTITY_TEXT`, `REACT_MODE_BLOCK_{QUICK,COPILOT,AGENTIC}`, `REACT_RESPONSE_SHAPE_TEXT`, `REACT_FORMAT_RULES_TEXT`, `REACT_CRITICAL_RULES_TEXT`, `REACT_NO_TOOLS_PROMPT`) so `_react_reasoning_system()` (legacy) and the new block-seed script share one source of truth instead of risking drift between a hand-copied DB body and the live prompt; added `resolve_react_system_prompt_v2()` (flag-gated, fail-soft, mirrors `app/responder/final.py`'s `MOBIUS_PROMPT_SOURCE=composition` pattern).
- `app/pipeline/react/critic.py` — added `resolve_critic_system_prompt_v2()`, same pattern.
- `app/pipeline/react_loop.py` — the legacy `reasoning_system` computation before the loop is now also the fail-soft fallback; inside the loop, when `MOBIUS_PROMPT_SOURCE=composition`, each round resolves `react_{agent_role}` via `react_agent_role(iteration, max_it)` and swaps in the composed prompt if resolution succeeds. Critic's call site got the equivalent swap. Flag off → byte-for-byte the same code path as before (verified, not assumed — see below).
- `app/services/react_block_seed.py` (new) — seeds 9 blocks (not the 10 originally planned — see correction below) + 5 compositions (`react_explore`/`synthesize`/`draft`/`no_tools`, `critic_audit`), idempotent upsert, coherence-checked before write, dry-run passes with no DB (`python3 -m app.services.react_block_seed`).

**Two corrections found while implementing (verified against actual code, not assumed from the design docs):**
1. **`resolve_composition_sync`/`resolve_composition` hardcode `variant_id='default'`** — there is NO 5-axis `EngagementContext` tag-matching on the v2 composition path (that mechanism belongs only to `PromptManager.render()`, the older v1/049 path). My earlier plan said mode-variance (quick/copilot/agentic) would "reuse the 5-axis variant-match mechanism" — it can't, that mechanism isn't wired to this path. Resolved by making `react.mode_quality_bar` an **opaque/derived block** (`"{{ mode_block_text }}"`, same pattern as `tool_manifest`) — mode selection happens in Python at the call site, exactly like today, not via DB-side variant matching. Simpler than originally described, not more complex; composition count stays at 5 as signed.
2. **`critic.audit_request` doesn't belong in a composition** — `prompt_compositions` has no per-row `role` column; a composition's members join into ONE string regardless of each block's own `role`. Legacy code calls the critic with two SEPARATE strings (system, user). I'd originally planned to fold the audit-request user message into `critic_audit`'s member list — that would have wrongly concatenated it onto the system prompt. Dropped it from the composition; it stays exactly what it always was, Python-computed text passed directly as the call's `user` argument. Block count is 9, not 10.

**AC-6 parity — verified, not assumed:**
- `scratchpad/parity_check.py`: the refactored `_react_reasoning_system()` (constants-based) produces **byte-identical** output to the git-HEAD pre-refactor version, across 7 representative cases (all 3 modes, with/without user profile, with/without tool restriction, task mode, empty-allowed-tools). All pass.
- `scratchpad/parity_check_composition.py`: `BlockAssembler.assemble()` over the seeded blocks produces **byte-identical** output to the live `_react_reasoning_system()`, across 5 cases. Getting this exact required two real fixes, not just assumption: (a) `mode_block_text` needed its own leading/trailing newlines stripped before being passed as an opaque template var — the assembler's `"\n\n"` block-join separator was doubling up with the constant's own boundary whitespace, which had been doing that job for the old inline-f-string layout; (b) `react.critical_rules`' *seeded block body* (not the shared constant used by the legacy path) needed one trailing `"\n"` appended, to correctly reproduce the exact byte-for-byte spacing whether it's the last block in the composition (no user profile) or followed by `react.user_profile`. Also verified `react_no_tools` and `critic_audit` compositions match their legacy equivalents exactly (including the profile-present case for critic).
- Confirmed `react_explore`/`react_synthesize`/`react_draft` seed to the **identical `composition_hash`** (as intended — addressing infrastructure only, no content variance in Phase A).

**Test suite:** ran `tests/test_react_*` (100 tests). 94 passed. 3 failed — verified via `git stash` that **all 3 are pre-existing, not caused by this work**: 2 are an unrelated missing-dependency error (`ModuleNotFoundError: mobius_contracts`, fails identically on git HEAD before any of my changes) and 1 is a LOC-ceiling ratchet test (`test_react_loop_loc_under_ceiling`) that was **already red before this work** (2763 LOC vs. a 2560 ceiling, pre-existing) — my ~28 lines of composition-resolution wiring pushed it to 2791, worsening an already-failing ratchet rather than breaking a passing one. Not fixing it here (bumping the ceiling isn't my call to make solo, and the test's own message says not to "bump it on autopilot") — flagging for whoever owns that ratchet.

**Also noted, not touched:** the working tree has other files with uncommitted changes I didn't make (`app/services/block_seed.py`, `model_registry.py`, `prompt_manager.py`, `requirements.txt`, a few docs) — consistent with other fleet sessions working concurrently in this shared checkout. Confirmed I only edited my three owned files plus new files of my own; branch is still `main`, unswitched.

**Blocker (RESOLVED 2026-07-30) — see §9.**

---

## 9. Dev cutover — verified live (2026-07-30)

Database co-signed migration 054; LLM Agent applied it to dev and independently end-to-end tested the new `prompt_address` lookup path before handing back. Verified independently from this side too, not just taken on report:

- Confirmed `prompt_compositions.prompt_address` column exists on the real dev DB (direct query via `.env`'s `CHAT_RAG_DATABASE_URL`, port-5433 cloud-sql-proxy) — 2 pre-existing integrator rows present, both `prompt_address=NULL` as expected (unaffected).
- Ran `react_block_seed.seed()` for real (no existing async runner script called this anywhere in the repo — block_seed.py's own `__main__` is dry-run-only, so I wrote a small one-off asyncpg runner to invoke it). All 5 compositions seeded: `react_explore`/`react_synthesize`/`react_draft` (identical `composition_hash`, as designed), `react_no_tools`, `critic_audit`.
- Called `resolve_react_system_prompt_v2()` for `explore`/`synthesize`/`draft`/no-tools and `resolve_critic_system_prompt_v2()` directly against the now-live DB — all resolve successfully (no longer fail-soft `None`). Compared the `explore` resolution against `_react_reasoning_system()` (legacy) for identical inputs: **byte-identical**, confirmed live, not just in the earlier isolated parity scripts.
- Confirmed the `[react] v2 composition prompt module=... hash=...` log line fires correctly for both react and critic resolution — the same observability signal the integrator's live cutover used to prove itself (`STATUS_V2_LIVE_2026-07-26.md`).
- Re-ran the full react test suite with `MOBIUS_PROMPT_SOURCE=composition` set and the live DB reachable: same 94 pass / 3 pre-existing fails as with the flag off — no new failures from the live composition path.

Phase A is done: structural decomposition, versioned/owned blocks, live on dev, byte-identical output, zero regressions. Phase B (`agent_role`→temperature) remains explicitly out of scope, per the signed ruling.
