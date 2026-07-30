# `agent_role` Derivation for ReAct

**Author:** ReAct Agent
**Status:** SIGNED ✅ by Chat Architecture, 2026-07-29 (verified firsthand, not a rubber stamp). Parent implementation spec (`REACT_PROMPT_BLOCK_DECOMPOSITION_DRAFT.md`) also signed. Next: coordinate 3 integration points with LLM Agent before scoping an implementation plan (tracked in the parent doc's ledger).
**Builds on:** `SPEC_LLMMANAGER_V2.md` §2 (`agent_role` definition), §5 (`agent_role`→temperature)

**RULING (Chat Architecture, 2026-07-29):** All of §5's asks approved as proposed.
- **§2 scope guard — APPROVED.** `agent_role` feeds ONLY temperature + the logged label (AC-v2-14) for this pass. The system prompt stays a single per-turn composition; blocks 1-3 do **not** get role-keyed variants. Reasoning on record: `SPEC_LLMMANAGER_V2.md` §9's "no behavior change on cutover" is load-bearing — role-flavored content bodies would conflate structural decomposition (this pass) with a behavioral change (a separate proposal needing an Eval baseline + product sign-off + explicit measurement). Temperature routing is the correct first use of the signal: additive, testable, no content authoring required. **Role-flavored prompt-body variants are v2.1+ territory** — a distinct future proposal, not this spec.
- **§1.1 prospective proxy (last round in budget = draft) — APPROVED.** Correct approximation, consistent with `is_guidance_round`'s own round-position logic.
- **§4 exclusions (react.task, Round 0, critic) — CONFIRMED.**

Cleared to fold into `REACT_PROMPT_BLOCK_DECOMPOSITION_DRAFT.md` (that doc is now the implementation spec, itself still gated on a final Chat Architecture sign-off pass before any code).

---

## 0. The problem

`SPEC_LLMMANAGER_V2.md` §2 requires `agent_role` (`explore`/`synthesize`/`draft`) as a **RoundManager-derived, per-round signal** that (a) drives temperature and (b) selects which `module_context` flavor composes. Per my [decomposition draft](REACT_PROMPT_BLOCK_DECOMPOSITION_DRAFT.md) §0, **no such derivation exists in code today** — there is no `RoundManager` class, only `_react_round_headline` (a UI label string) and `is_guidance_round` (a boolean phase gate), neither of which is `agent_role`. This spec proposes the derivation and — more importantly — scopes what it's allowed to change, because a naive reading of §2 has a real behavior-change trap (§2 below).

---

## 1. The derivation

```python
def react_agent_role(iteration: int, max_it: int) -> str:
    """Deterministic, round-position-only. iteration is 0-indexed (matches
    is_guidance_round / _react_round_headline's convention)."""
    if iteration == 0:
        return "explore"
    if iteration >= max_it - 1:
        return "draft"
    return "synthesize"
```

Lives in `react/prompts.py`, next to `_react_round_headline` and `is_guidance_round` — same file, same pattern, no new module or class. There is no need to invent a `RoundManager` component; the round bookkeeping already lives inline in `run_react`'s `for iteration in range(max_it)` loop (`react_loop.py:2108`), and this function slots into that loop the same way the existing two do.

**Per-mode sequences this produces** (concrete, so the mapping is checkable):

| Mode | max_it | Round 1 | Round 2 | Round 3 | ... | Last round |
|---|---|---|---|---|---|---|
| quick | 2 | explore | draft | — | — | (round 2 = draft) |
| copilot | 3 | explore | synthesize | draft | — | |
| task | 3 | n/a — see §4, no role split applies | | | | |
| agentic | 10 | explore | synthesize | synthesize | ×7 more synthesize | draft (round 10) |

### 1.1 Reconciling "the completing round" ambiguity

`SPEC_LLMMANAGER_V2.md` §2 says *"the completing round = draft"* and separately acknowledges phase maps to *"a round's position in the run (first / middle / last-completing), which is dynamic."* Read literally, "the completing round" is **retrospective** — you only know a round was the one that finished the turn *after* the LLM sets `is_complete=true`, which could happen at round 2 of a 3-round budget. But `agent_role` has to be known **before** the LLM call, to build that round's prompt and pick its temperature. You cannot prospectively know if this round will complete.

Resolution: use **"last possible round of this turn's budget"** (`iteration == max_it - 1`) as the deterministic, prospectively-computable proxy for "draft," not "the round that actually completed." This is the same kind of approximation the spec's own §2 already accepts for `module_key` (`react_{rn}`, round-number-keyed, blending phases across different run lengths) — round-number is a good-enough proxy, logged so Eval can revisit it later (§2's own reasoning, reused here). Consequence: if a copilot turn (max_it=3) completes at round 2, that round was prompted/temperature'd as `synthesize`, not `draft` — it never gets relabeled after the fact. That's fine; it's a labeling approximation, not a correctness issue, and it's consistent with how `react_2`'s arm history already blends synthesize/draft per §2's own C1 analysis.

### 1.2 Relationship to `is_guidance_round` — a different axis, not a duplicate

`is_guidance_round(iteration, max_it)` (prompts.py:93) fires at the ceil(0.8×max_it) threshold and governs a **content injection** (the "shift from search to synthesis" instruction, Context §[1] in the decomposition draft). `agent_role` governs **temperature + (potentially) which system-prompt flavor composes**. These overlap in agentic mode (rounds 8-9 are `synthesize` *and* in the guidance band; round 10 is `draft` *and* in the guidance band) but that's not a conflict — they're different levers reading the same round-position signal for different purposes. No dedup needed; flagging so the overlap doesn't get mistaken for a bug later.

---

## 2. Scope guard — what `agent_role` does NOT change (this is the important part)

Naive reading of §2/§3: `prompt_address = application.agent_role` (e.g. `react.explore`) selects which blocks compose, and since `agent_role` changes every round, that implies **re-resolving the composition every round** — including the system-prompt blocks (`react.identity`, `react.tool_manifest`, `react.critical_rules`, etc.) that today are built **once per turn** (`react_loop.py:2061`, confirmed in the decomposition draft §0).

If `react.explore`/`react.synthesize`/`react.draft` compositions actually contain *different content* for those blocks, this is a **real behavior change**: the system prompt would need rebuilding every round, and someone has to author three flavors of identity/rules text that don't exist today. `SPEC_LLMMANAGER_V2.md` §9 is explicit that cutover should be **"no behavior change... just decomposed."** A round-varying system prompt with new role-flavored prose is not a decomposition of the existing prompt — it's new content.

**Proposal:** for this cutover, `agent_role` for react feeds exactly two things, both additive and non-content-changing:

1. **Temperature** — `ConfigManager` applies a temp schedule keyed on `agent_role` (§5 of the v2 spec: explore=higher, synthesize=lower, draft=lowest). React's job is only to *pass the label through* to whatever call wraps `llm_generate`.
2. **The logged label** — `agent_role` is written to `llm_calls` per call (AC-v2-14), independent of whether it changes what was rendered. This is what keeps the phase signal available for Eval to later decide *whether* model-choice or prompt content should actually condition on it (§2's own "log it so you can evaluate it later" principle).

**What does NOT happen in this pass:** react's `module_context` (the identity/rules/manifest content) does **not** grow three role-flavored variants. `prompt_address` for react's static system-prompt blocks stays a single value per turn (not per round) — the existing "build once" behavior is preserved. If product/Chat Architecture later wants genuinely different system-prompt framing for explore vs. draft rounds, that's a **new, separate proposal** with its own content-authoring and sign-off — not something bundled into "decompose the existing prompt into blocks."

**This is the one judgment call in this spec I want explicit sign-off on** — I could be wrong that content-invariance is what's wanted; if Chat Architecture wants role-flavored prompt bodies as part of this phase, that changes the shape of blocks 1-3 in the decomposition draft (they'd need `role`-keyed variants, not just `mode`-keyed ones) and the "build once per turn" code structure in `run_react` would need to move the system-prompt build inside the loop. Worth deciding explicitly before implementation starts either way.

---

## 3. Integration touchpoint (coordination, not something I build solo)

Passing `agent_role` through to logging requires a new parameter somewhere in the `_call_llm_json` (`prompts.py:433`) → `llm_generate` (`app/services/llm_manager.py`, LLM Agent's file) call chain — today that call only takes `stage=f"react_{rn}"`. Adding `agent_role=react_agent_role(iteration, max_it)` as a passthrough param is a small, additive change on the react side, but the receiving end (temperature lookup + `llm_calls.agent_role` column write) is LLM Agent's territory. Flagging as a coordination point for when implementation starts — not proposing to touch `llm_manager.py` myself.

---

## 4. Where this does NOT apply

- **`react.task` / no-tools composition** (Q4 ruling — its own `prompt_address`). Task mode's context builder already skips guidance/jurisdiction/bandit-state entirely (`build_reasoning_context`'s `_is_no_tools` branch, prompts.py:510-539) and the loop finalizes on the first tool-free decision — there's no meaningful multi-round explore→draft arc to label. Recommend: no `agent_role` for this composition; it's a single flat call, not phased.
- **Round 0** (`round0.py`, deferred per Q6). Not part of the round loop this derivation targets.
- **The critic** (`react/critic.py`, now in scope per Q3). It runs *after* a draft is produced, auditing rather than reasoning-forward — it's not one of the explore/synthesize/draft phases at all. Not proposing to extend the tri-state model to it; it stays its own thing (own system prompt, own `stage="critique"`).

---

## 5. Status — signed off, folded forward

All three asks (derivation in §1, the §2 scope guard, the §4 exclusions) are ruled and approved per the box at the top of this doc. Folded into `REACT_PROMPT_BLOCK_DECOMPOSITION_DRAFT.md`, which is now the implementation spec awaiting a final Chat Architecture sign-off pass before any code lands.
