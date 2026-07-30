# ReAct Prompt → v2 Block Decomposition (Implementation Spec)

**Author:** ReAct Agent
**Status:** SIGNED ✅ by Chat Architecture, 2026-07-29 (read and verified firsthand, not a rubber stamp) — this is the implementation spec. Gate before code: 3 integration points to confirm with LLM Agent (§6a), then an implementation plan comes back here for review. No code changes made yet.
**Scope:** `app/pipeline/react_loop.py`, `app/pipeline/react/prompts.py`, `app/pipeline/tool_manifest.py`, `app/pipeline/react/round0.py` (in scope, build deferred), `app/pipeline/react/critic.py` (added to scope 2026-07-29)
**Builds on:** `SPEC_LLMMANAGER_V2.md` (block model, §3), `SPEC_PROMPT_BLOCK_PERSISTENCE.md` (schema, read path), `REACT_AGENT_ROLE_DERIVATION_DRAFT.md` (signed off — `agent_role` derivation + scope)

---

## 0. Verified call sequence (corrects/sharpens the schematic I was handed)

Read against the live code, not just the schematic:

- **System prompt** (`_react_reasoning_system`, `prompts.py:247`) is built **exactly once per turn**, at `react_loop.py:2061`, *before* the `for iteration in range(max_it)` loop. Confirmed static across rounds.
- **Context message** (`build_reasoning_context`, `prompts.py:495`) is rebuilt **every round**, at `react_loop.py:2135`, inside the loop.
- **Round 0** (`round0.py`) is a *third*, wholly separate system prompt (`ROUND0_SYSTEM_PROMPT`) for a one-shot short-circuit call (`stage="react_0"`) before the main loop starts. It is not a variant of the main system prompt — different rules, different tool list (hand-typed, see §4).
- The critic (`react/critic.py`, imported at `react_loop.py:2298`) is a **fourth** system prompt (`CRITIC_SYSTEM_PROMPT`, `stage="critique"`), also spliced with `user_profile`. **Now confirmed in scope (Chat Architecture ruling, §5 below)** — added to owned files.
- `agent_role` (explore/synthesize/draft) **did not exist as code before this pass.** `SPEC_LLMMANAGER_V2.md` §2 describes it as RoundManager-derived from round position; there was no `RoundManager` and no role-derivation function anywhere — only `_react_round_headline(iteration, max_it)` (a **UI label**, not a semantic role) and `is_guidance_round(iteration, max_it)` (a **boolean phase gate**). **Resolved:** see `REACT_AGENT_ROLE_DERIVATION_DRAFT.md` (signed off) — a pure function now exists, and its scope is deliberately narrow (§4 below).

---

## 1. System prompt — NOT four independent blocks today

The schematic proposed:
```
[1] identity+mode   → react.identity     (owner: Product)
[2] tool manifest   → react.tool_manifest (owner: ReAct Agent)
[3] response rules  → react.response_rules (owner: Chat Architecture)
[4] user profile    → react.user_profile (owner: User Manager)
```

**Reality (`prompts.py:327-420`):** `[1]`, `[2]`, `[3]` are one Python f-string (`_base_prompt_text`), not three things. The tool manifest is *interpolated in the middle* of it (`prompts.py:331`), and the CRITICAL RULES (§3) are hard-appended after the response-shape JSON examples in the same string. `[4]` is genuinely separate — applied as a post-hoc wrap via `splice_user_profile(_base_prompt_text, user_profile)` (`prompts.py:419-420`), which is the **cleanest existing seam** in the whole system prompt.

There's also a **fifth, undocumented static variant** the schematic missed: the **no-tools prompt** (`prompts.py:273-295`, gated on `mode == "task" or allowed_tools == []`). It is not a stripped-down version of blocks 1-3 — it's a completely different ~20-line prompt with its own JSON shape and its own 4 rules. Any block model that doesn't account for this branch will silently drop it.

Refined block list for the system prompt:

| Proposed block | Maps to | Independent? | Notes |
|---|---|---|---|
| `react.identity` | `"You are Mobius…"` opening (prompts.py:328) | Yes, textually | Currently glued to `mode_block` in the same f-string — splitting is a straightforward text cut, not a logic change |
| `react.mode_quality_bar` | `mode_block` (quick/copilot/agentic, prompts.py:300-326) | **No — 3-way variant, not one block** | This is itself 3 mutually exclusive templates keyed on `chat_mode`. Model as 3 `variant_id`s of one block (`quick`/`copilot`/`agentic`), not 1 block with an if/else Jinja body — matches how v2 already does variant selection |
| `react.tool_manifest` | `tool_manifest.get_tool_manifest()` call | **No — see §2, this is the hardest seam** | |
| `react.response_shape` | the two JSON-shape examples (prompts.py:333-353) | Yes | Static, mode-independent |
| `react.format_rules` | FORMAT RULES block (prompts.py:355-366) | Yes, but **references `mode_block`'s user-preference language** | Says "USER PREFERENCES (appended below...)" — the appended-below thing is block `[4]` (user_profile), applied *after* this text. Ordering dependency, not a content dependency — fine, but must preserve order |
| `react.critical_rules` | CRITICAL RULES 1-11 (prompts.py:371-413) | Yes, textually | This is the block Chat Architecture should own per the original schematic — it encodes RAG-first policy, PHI refusal (rule 6), NPI routing (rules 2-5), product-identity routing (3b) — all cross-cutting policy, not ReAct-mechanical. Recommend this stays Chat Architecture-owned as proposed |
| `react.user_profile` | `splice_user_profile` wrap | **Yes — already the cleanest seam** | No change needed to extract; it's already applied as a discrete append step |
| `react.no_tools` | the task-mode / allowed_tools=[] branch (prompts.py:273-295) | Yes, but **structurally disjoint** from all of the above | Not a composition of the other blocks — a parallel, self-contained composition. Needs its own `prompt_address` (`react.no_tools` or fold into `react.draft` with a `has_tools=false` condition — open question, §5) |

**Answering Q1 (independent vs. entangled):** `react.critical_rules`, `react.response_shape`, `react.user_profile` are cleanly independent. `react.identity` is independent but currently glued textually to the mode block. `react.mode_quality_bar` is not one block — it's three variants. `react.tool_manifest` is the one true entanglement (§2).

**Answering Q2 (varies by round vs. static):** Every system-prompt block is static **within a turn** — none of them read `iteration`. The only round-varying input to the *system* prompt is which of the 3 mode variants gets selected, and that's chosen once (from `chat_mode`, not from round number) — so even that's turn-static, not round-varying. All round-varying content lives in the context message (§3).

---

## 2. `react.tool_manifest` — the hardest seam

`tool_manifest.py` is **already a block composer**, just written in Python string concatenation instead of DB rows (`_compose_manifest`, `tool_manifest.py:325`). It splices ~15 sub-pieces in a fixed order:

- 2 hand-written prose primers (FL Medicaid routing gate, retrieval methodology)
- ~10 router-owned prose blocks (`_RAG_BLOCK`, `_REFUSE_BLOCK`, etc.) and registry-rendered skill blocks (`registry.manifest_text(...)`)
- 1 dynamically-discovered MCP section (`_auto_discovered_block`) — **rendered fresh per call**, not knowable at composition-authoring time, because MCP tools register at FastAPI startup (`tool_manifest.py:422-428`, the whole reason `get_tool_manifest()` is a function and not a module constant)
- 1 capabilities footer (`tool_capabilities_for_parser()`)
- all of it filtered by an `allowed: frozenset[str] | None` parameter (tool-policy restriction)

This does not fit the v2 `Jinja2 template_body over bound vars` model cleanly, for two reasons:
1. **Membership is dynamic at call time** (MCP registry state), not resolvable at composition-authoring time the way `prompt_composition_members` assumes (a fixed ordered list of `block_key`s).
2. **Filtering (`allowed`) changes which sub-pieces appear**, and that filter is a runtime parameter (`ctx.allowed_tools`), not a `variant_tags` axis known at composition build time.

Recommendation (for Chat Architecture / LLMManager to weigh in on, not a unilateral call): model `react.tool_manifest` as **one `derived` block** whose `template_body` is not Jinja prose but effectively `{{ tool_manifest_text }}` — i.e. push the entire existing `_compose_manifest` output in as a single pre-rendered template var, computed by Python exactly as today. Do **not** try to decompose the manifest's internal ~15 pieces into `prompt_blocks` rows — that would fight the registry/MCP dynamism for no real gain (nobody A/B-tests one tool's manifest prose independently of the others; the registry already owns that granularity via `SkillSpec.description`). This keeps `react.tool_manifest` a single addressable, versioned, owned block at the *outer* seam, while leaving the existing registry-driven internals untouched.

**RULED 2026-07-29 (Chat Architecture Q1): APPROVED, precedent-setting.** Confirmed clean against the actual `BlockAssembler`/schema code by LLM Agent (not from memory — they grepped it):
- `block_kind` (`derived` vs `per_turn`) is **purely categorical** — `assemble()`/`check_coherence()` never branch on it, only on `condition` and `is_authority`. Naming is for humans/the Studio UI, not enforcement. Recommended (non-binding): `tool_manifest` = `derived`, `tool_results` = `per_turn` (accumulates + truncates fresh every round).
- `{{ precomputed_text }}` as the entire `template_body` is safe: `_ENV = jinja2.Environment(autoescape=False, keep_trailing_newline=True)` passes the bound value through byte-verbatim, and Jinja substitutes the template structure once without re-parsing the substituted *value* as more Jinja — so externally-influenced content (a scraped page containing the literal string `{{7*7}}`) renders literally, never evaluated. Same safety property `turn_context.py`'s steer-sanitizer relies on; free with this pattern.
- `validated_at` is only gated when `is_authority=true` (`CHECK (is_authority = FALSE OR validated_at IS NOT NULL)`, mig 053). Both blocks should be `is_authority=False` (content composition, not a promise/refusal guarantee), so no sign-off gate applies to them specifically.
- `composition_hash` hashes `(block_key, version)` only, never `template_vars` — confirmed in `_hash()`. Volatile content (which MCP tools are registered, this round's accumulated tool_results) never changes the hash; only editing the block's template (a new version) does. Correct ablation semantic: the hash answers "which block definition," not "what data flowed through it."
- Integration detail: whichever caller invokes `assemble()`/`resolve_composition` passes the precomputed strings into `template_vars` at call time (e.g. `template_vars={"tool_manifest_text": ..., "tool_results_text": ...}`) — existing free-form dict param, nothing new to build.
- Ship as: `is_authority=False`, `condition=None` (presence isn't conditional — only content varies via `template_vars`), empty `directives`.

---

## 3. Context message — already block-shaped, cheap to decompose

`build_reasoning_context` (`prompts.py:495-818`) builds a Python `list[str]` (`parts`) and joins with `"\n\n"` at the end. Structurally, **this is already a block list** — each `if`/`append` pair is a de facto block. That makes this the *easy* half of the decomposition, with three real wrinkles:

| Section | Round-varying? | Independent? | Wrinkle |
|---|---|---|---|
| **[1] Guidance instruction** | Yes — `is_guidance_round(iteration, max_it)` | Yes | Fully static template text once the condition is true (`_react_guidance_instruction`, prompts.py:145-244) — good `conditional` block candidate, `condition="is_guidance_round"` |
| **[2] Jurisdiction** | No (turn-static, re-read every round from `ctx.merged_state`) | Yes | Simple `derived` block, one var |
| **[3] Thread uploads** | No | Yes | `derived`, list-shaped var (filenames/chunks) |
| **[4] Active context** | No | Yes | `derived`, 400-char truncation is a Python pre-step, not template logic |
| **[5] Prior failed query** | No | Yes | `derived` |
| **[6] Recent conversation** | No (but content varies by *turn*, not round) | **No — two incompatible variants selected by a runtime intent classifier** | `is_transform` keyword match (prompts.py:657) picks between a 3000-char "transform" preamble and a 400-char "compact" preamble — this is a live conditional branch, not a static template with a variable slot. Needs either two blocks selected by a `condition`, or one block with the branch pushed into the Python pre-render step (recommended — see below) |
| **[7] Rolling summary** | No | Yes | `derived`, 600-char cap already matches integrator's cap |
| **[8] Strategy bandit state** | **Yes — depends on `ctx._strategy_arms_tried`, which grows across rounds** | Yes, but **stateful across rounds within a turn** | Not a per-round *input* like tool_results, but its content differs round to round as arms get tried. `derived`, per-turn-mutable |
| **[9] Feedback cadence** | No | **No — 4 sub-variants** (nps/csat/targeted_miss/generic, prompts.py:740-752) | Same pattern as [6] — Python branch picks the instructional text. Recommend one `derived` block per `kind`, selected by a condition on `ctx.feedback_signal.kind`, rather than 4 rows or in-template branching |
| **[10] Tool results** | **Yes — accumulates every round** (`round N` context includes rounds `1..N`) | Yes as a *slot*, but **not Jinja-friendly content** | The real hard case besides the manifest. Per-tool truncation logic (result_summary preference, 320/400 head/tail split for >600 chars, per prompts.py:770-783) is control flow, not template substitution. Recommend: keep this as Python-rendered text handed to the block as ONE opaque var (same pattern as §2's tool_manifest), for consistency — do not attempt a Jinja `{% for %}` loop reproducing the truncation branches |
| **[11] Question** | No | Yes | Trivial |
| **[12] JSON footer** | No | Yes | Trivial, static |

**Answering Q2 precisely:** only **[1] guidance** (crosses a threshold), **[8] bandit state** (accumulates), and **[10] tool results** (accumulates) actually vary *within* a turn as rounds progress. Everything else is turn-static and just gets re-evaluated harmlessly each round (cheap, but worth noting — 9 of 12 sections could be computed once per turn rather than recomputed every round; that's a possible efficiency finding, not part of this task).

**Answering Q3 (hardest seams):** in priority order: (a) `react.tool_manifest` (§2, dynamic MCP membership), (b) `react.tool_results` ([10], accumulating + non-trivial truncation), (c) the two-variant sections ([6] recent-conversation, [9] feedback-cadence) where a Python classifier — not a template condition — currently picks the variant.

---

## 4. Round 0 (deferred) and `agent_role` (resolved)

1. **`round0.py`'s system prompt hardcodes a tool list** (get_top_orgs, get_entrant_analysis, etc., `round0.py:53-66`) that duplicates — and can drift from — the canonical list in `tool_manifest.py`. **DEFERRED** (Chat Architecture ruling) — Round 0 is out of scope for this pass entirely (it's a separate short-circuit composition, not part of the react.explore/synthesize/draft sequence). The tool-list drift is noted as a separate tracked item, not fixed here.
2. **`agent_role` derivation — RESOLVED.** Full spec + ruling in `REACT_AGENT_ROLE_DERIVATION_DRAFT.md` (signed off 2026-07-29). Summary: a pure function `react_agent_role(iteration, max_it)` — round 1 → `explore`, last possible round in the budget → `draft` (deterministic proxy for the spec's "completing round," which can't be known prospectively), else → `synthesize`. Lives in `prompts.py` next to `_react_round_headline`/`is_guidance_round`; no `RoundManager` class needed.

   **Scope ruling (load-bearing):** `agent_role` feeds **only** (a) the temperature schedule (§5 of `SPEC_LLMMANAGER_V2.md` — explore=higher, synthesize=lower, draft=lowest) and (b) the logged `agent_role` label (AC-v2-14) — for this pass. The system prompt's static blocks (§1's table) do **not** get role-keyed variants; `react.identity`/`react.tool_manifest`/`react.critical_rules` stay a single per-turn composition, exactly as today. **No behavior change** beyond adding temperature routing + logging — consistent with `SPEC_LLMMANAGER_V2.md` §9. Role-flavored prompt-*content* variants (an "explore" identity vs. a "draft" one) are explicitly **v2.1+ territory**, a separate future proposal with its own content authoring, Eval baseline, and sign-off — not bundled into this decomposition.

   Practical effect: `module_key` stays `f"react_{rn}"` (unchanged, round-number-keyed, arm-preserving per `SPEC_LLMMANAGER_V2.md` §2's own resolution). `prompt_address` for react's system-prompt blocks resolves once per turn, same as today. The one new per-round integration point is passing `agent_role=react_agent_role(iteration, max_it)` through `_call_llm_json` → `llm_generate` so LLM Agent's side can apply temperature + log the label — a coordination point at build time, not something built here.

---

## 5. Critic (`react/critic.py`) — newly in scope, own composition

Now confirmed in scope (Chat Architecture ruling on Q3, §6 ledger below). Reading it in full: `CRITIC_SYSTEM_PROMPT` (critic.py:234-286) is a single **static** string — no mode variants, no round variants, no `user_profile`-driven branching beyond the same `splice_user_profile` wrap react's main system prompt uses (`react_loop.py:2405-2406`). `build_critic_user_message` (critic.py:289-355) assembles the audit request (question, draft answer, numbered sources, tool outputs) — structurally similar to `build_reasoning_context` (an ordered list of sections), but built **once per critic invocation**, not accumulated across rounds the way react's tool_results are.

Block model: simpler than react's main prompt, no hard seams.
- `critic.audit_rules` — the whole `CRITIC_SYSTEM_PROMPT`, static, `is_authority=False`.
- `critic.user_profile` — **RULED (Chat Architecture, 2026-07-29): same block definition as `react.user_profile`, not a duplicate.** One `block_key`, one versioned definition, owned by User Manager, included as a member in both `react.*` and `critic.audit` compositions — when User Manager updates it, both inherit automatically.
- The audit request body (question/draft/sources/tool-outputs) is the same "opaque pre-rendered var" pattern as `react.tool_results` (§2) — sources aren't truncated here by design (critic.py:305-307 — "not truncated... callers upstream should already have chunk-size caps applied"), so it's simpler than react's version, but still Python-composed, not Jinja-templated.

The critic is gated by `critic_enabled()` (env flag, default OFF) and a deterministic `should_run_critic()` regex gate (critic.py:144-191) — meaning this composition may run **zero, one, or several times** within a single turn (once per completion attempt that trips the gate), unlike react's main prompt (built once) or context (built every round unconditionally). Its own `prompt_address` (e.g. `critic.audit`) is naturally disjoint from `react.*` — not a round-position variant of anything in §1-§4, confirming it belongs outside the explore/synthesize/draft tri-state (per the agent_role scope ruling in §4 — the critic isn't one of the three phases, it audits after one completes).

---

## 6. Ledger — all original open questions, resolved

1. ~~`react.tool_manifest` as a single opaque `derived` block~~ — **APPROVED**, mechanics confirmed by LLM Agent against the actual code (§2).
2. ~~`react.tool_results` — same pattern~~ — **APPROVED**, same confirmation covers this (§2).
3. ~~Is `react/critic.py` in my scope?~~ — **IN SCOPE**, added to owned files, decomposed in §5.
4. ~~Where does the no-tools prompt live in the addressing scheme?~~ — **OWN `prompt_address`** (`react.task`/`react.no_tools`), not a condition on `react.draft` — architecturally distinct.
5. ~~Build-order dependency: `agent_role` derivation doesn't exist yet~~ — **RESOLVED**, spec signed off, folded into §4.
6. ~~Round 0 — in scope or deferred?~~ — **DEFERRED.** Tool-list drift vs. `tool_manifest.py` noted as a separate tracked item, not fixed in this pass.

**The one item surfaced while folding critic.py in (§5) is also closed:** `critic.user_profile` = same block definition as `react.user_profile` (Chat Architecture ruling, folded into §5 above).

This document, plus `REACT_AGENT_ROLE_DERIVATION_DRAFT.md`, is **SIGNED** as the implementation spec (Chat Architecture, 2026-07-29). No code has been written.

---

## 6a. Gate before an implementation plan — coordinate with LLM Agent

Per Chat Architecture's sign-off, three integration points need confirming with LLM Agent (`local_bd8109e3-62b2-4972-a1a4-2a54d92e73c3`) before scoping a plan:

1. **`agent_role` passthrough param** on `_call_llm_json` → `llm_generate` — temperature routing + `AC-v2-14` logging live on LLM Agent's side; ReAct just needs to pass the label through. Confirm the param name/shape they want.
2. **`template_vars` dict pattern for opaque blocks** (`tool_manifest_text`, `tool_results_text`) — confirm the exact call-site signature for `assemble()`/`resolve_composition`.
3. **`prompt_address` namespace format** — whether `react.*` compositions should match the existing integrator compositions' namespace convention, or use a separate one.

No implementation plan until these three are confirmed. Once they are, a plan comes back to Chat Architecture for review before any code lands.

---

## 6b. BLOCKING — spec/shipped-schema gap found while confirming with LLM Agent (2026-07-29)

LLM Agent verified all three §6a items against the **live code**, not the design docs — and found the design docs (this one included, and `SPEC_LLMMANAGER_V2.md` upstream of it) rest on an assumption that was **never actually built**:

- `SPEC_LLMMANAGER_V2.md` §3.4 ratified `prompt_address ≠ module_key` as a decoupled field (`module_key` = bandit arm, unchanged; `prompt_address` = composition lookup key, role-based) — this is the premise this whole decomposition doc's addressing scheme (§4) is built on.
- **The shipped schema (mig 053) never added it.** `prompt_compositions` has only `module_key` (flat, exact-match TEXT, e.g. `integrator_enricher_factual`) — no `prompt_address` column, no dot-namespace parsing anywhere in the persistence layer.

This matters beyond naming: if react composition lookup uses `module_key` with **role-based** values (`react_explore`/`react_draft`) while `llm_calls.module_key` (the bandit arm) stays **round-number-based** (`react_2`) per the already-ratified arm-preservation decision (§2 of the v2 spec), that's the same column name carrying two different meanings across two different tables — precisely the "one field, one meaning" failure mode `SPEC_LLMMANAGER_V2.md` §0 was written to eliminate. It's not fatal (different tables), but it's exactly the kind of ambiguity that's caused every prior vocabulary bug in this system, and it means this doc's §4 addressing scheme needs re-grounding before an implementation plan can be scoped against it.

**RULED (Chat Architecture, 2026-07-29): Option (a) — build `prompt_address`. Not optional.** Reasoning on record: `llm_calls.module_key` must stay `react_{rn}` for bandit reward attribution; if composition lookup used `module_key="react.explore"` there'd be no join path from arm to composition, and the reward signal loses training attribution. Option (b) would paper over a real semantic break, not resolve it.

**Scope (as ruled):** one new nullable column, `prompt_address TEXT NULL` on `prompt_compositions`. `resolve_composition()` looks up by `prompt_address` when present, falls back to `module_key` otherwise — existing integrator compositions (no `prompt_address`) are unaffected. Small addendum migration. **This is LLM Agent + Database territory, not mine to implement** — looped in directly per Chat Architecture's instruction (see cross-session message to LLM Agent). Tracked here as a dependency: react's compositions can ship against `module_key`-only lookup in the meantime (Phase A, §7 below) and adopt `prompt_address` if/when role-based composition variance is ever built (Phase B+).

**RULED (Chat Architecture, 2026-07-29): Phase A / Phase B split.** Temperature routing is new plumbing and belongs in **Phase B**; it does not block **Phase A**.

- **Phase A — structural decomposition.** Blocks, compositions, versioning, A/B-testability. Fully independent of temperature/`agent_role` consumption — none of §1-§5's blocks vary by role (per the earlier §4 scope ruling: system prompt stays per-turn, no role-keyed content). **This is what I scope an implementation plan for** (§7 below).
- **Phase B — `agent_role` → temperature via `CallManager`.** Scoped separately, built once LLM Agent has the plumbing (`generate()`/`generate_sync()` gaining an `agent_role` param, `temperature_for_role()` gaining a live caller, reconciling with the existing `CallManager`/`llm_configs` temperature path). Its own sign-off, when the plumbing exists. The `react_agent_role()` derivation function itself (from `REACT_AGENT_ROLE_DERIVATION_DRAFT.md`) can land inert in Phase A if convenient (pure function, no dependencies) — but it has no caller and does nothing until Phase B wires it in.

**Doc status:** signed as the design. Both blocking gaps ruled. Implementation plan scoped for **Phase A only** — see §7.
