# SPEC — ReAct Product Promise Contract (module prep)

**Author:** ReAct Agent
**Status:** §1–§7 below are the ORIGINAL DRAFT (2026-07-30) — kept as-written for the historical record of the design reasoning. As of 2026-08-04 the governor itself is BUILT and LIVE on dev (`app/pipeline/react/governor.py`, gated behind `MOBIUS_PRODUCT_PROMISE_ENABLED`) — see §8 for the model-bandit selection criteria extension, the newest piece of "react prep" built on top of it. §6's "none of this is built" no longer applies to the core contract/evaluate()/directive machinery; it now applies only to the specific items still listed there (checkpoint/recovery).
**Origin:** Ananth — "we need a react module prep module... the first of this is getting a new module called product promise that can tell us what good looks like for the entire react loop... react executes... the agent then updates based on react... new loop runs."

---

## 1. The gap this addresses

Today, the policies that define "what a good ReAct turn looks like" are real but **scattered, implicit, and mostly hardcoded**, not owned by any single explicit contract:

| Dimension | Where it lives today | How it's set |
|---|---|---|
| How long to spend (rounds) | `react_max_iterations_for_mode()` — quick=2, copilot=3, task=3, agentic=10 | Static per-`chat_mode` constant, code-level, not tunable per-turn |
| How long to spend (wall clock) | `MOBIUS_TURN_DEADLINE_S` (~300s, Cloud Run request timeout) | Env var, infra-level hard ceiling, not turn-aware |
| Max tokens | `max_tokens` param per `_call_llm_json` call (800/1400 depending on stage) | Hardcoded per call site, no explicit "budget" concept — confirmed no `BudgetLedger` class exists anywhere in the codebase despite being referenced aspirationally in `SPEC_LLMMANAGER_V2.md` |
| Accuracy | `confidence` (self-reported by the model), the critic's post-hoc groundedness audit | No explicit target — "high/medium/low" is qualitative, not a number the loop optimizes toward |
| Tone | `user_profile.rendered_prompt` (personalization), FORMAT_RULES' "USER PREFERENCES win" carve-out | Per-user, not per-task; no per-turn override concept |
| Format | `REACT_RESPONSE_SHAPE_TEXT`/`REACT_FORMAT_RULES_TEXT` (static), `output_intent`/`display_summary` (new, enricher-side, Claude) | Static rules + one new classified field; no unified format contract |
| Decision authority to extend | **None.** Rounds are a fixed ceiling per mode; if `is_complete` never fires, the loop exhausts and falls back to an honest-escalation message (`react_loop.py`'s final fallback path) | No mechanism exists today to dynamically grant more rounds/time based on interim progress |

Product Promise's proposed job: make these an **explicit, versioned, inspectable contract** that React reads at the start of a turn (or phase) and reports progress against — rather than a pile of independent hardcoded constants nobody can see or govern as one thing.

## 2. Proposed contract shape (draft — for the new agent + Chat Architecture to refine, not final)

```
ProductPromiseContract:
  time_budget:
    soft_target_s: <float>       # aspirational, e.g. "most turns should finish in 8s"
    hard_ceiling_s: <float>      # MUST be ≤ MOBIUS_TURN_DEADLINE_S (infra wall — see §5)
  token_budget:
    target_tokens_per_round: <int>
    max_tokens_per_turn: <int>
  accuracy_target:
    min_confidence: "low" | "medium" | "high"   # or a numeric bar, if this maps onto
                                                  # Eval's existing quality_score scale (see §4 — NOT
                                                  # a new metric invented independently of Eval)
  tone: <ref to user_profile, or an explicit per-task override>
  format: <ref to output_intent taxonomy (read/report/email/sms/emr/appeal/payor_report),
           or a more granular per-task override>
  decision_authority:
    can_extend_rounds: bool
    max_extension_rounds: <int>          # a range, not unlimited — "how many more, at most"
    extension_trigger: <what interim signal justifies asking for more — e.g. "guidance-mode
                        fired but still no is_complete", "critic flagged but rounds exhausted">
```

This is a first cut, not a ratified schema — the real design work (which fields are load-bearing, which are advisory, how they compose with existing per-mode `chat_mode` behavior) belongs to whoever builds Product Promise, with Chat Architecture's sign-off, the same gate everything else in this file's ownership area has gone through.

## 3. The loop, and the fork in it that needs deciding

Ananth's description: "the agent sets the contract, react executes, the agent then updates based on react, new loop runs." Read literally, this is a supervisory control loop — but there are **two structurally different ways to implement it**, with very different cost/latency profiles, and Ananth's own phrasing ("what if the answer is not reached, can we spend more time — what is the max time, kind of range") points toward wanting the *second* one:

**(a) Between-turns / per-session.** Product Promise sets the contract once (per turn, or per session), React runs to completion within it exactly as today, and Product Promise observes the outcome and adjusts the contract *for the next turn*. This is structurally similar to how Eval already turns calibration runs into priors — cheap, no new latency inside a turn, but it can't answer "should THIS turn get more rounds" — only "should the NEXT turn's contract be different."

**(b) In-the-loop / mid-turn.** Product Promise is consulted **during** a turn — specifically when React is about to exhaust its round budget without `is_complete` — to decide whether to grant an extension, and how much. This is what "can we spend more time" actually requires. It's a real, new decision point with its own latency/cost (a call or a lookup, live, inside the turn), not free, and it needs to compose with the existing 80/20 guidance-mode split (`is_guidance_round`) — the loop already has a "shift from search to synthesis" behavior at 80% of budget; Product Promise's extension logic would need to either supersede or coordinate with that, not silently conflict with it.

**Not deciding this myself.** This is the single most consequential open question in this spec — it determines whether Product Promise is a calibration-adjacent, between-turns component (lower risk, smaller build) or a live, in-the-loop supervisor (bigger build, real latency/cost tradeoff, needs its own latency budget accounted for against `MOBIUS_TURN_DEADLINE_S`). Flagging for the new agent + Chat Architecture to rule on before scoping a build.

## 4. Overlaps to reconcile — not architecture in a vacuum

Three existing, owned components already cover pieces of this ground. Product Promise needs to be positioned *relative to* them, not built as if they don't exist:

- **Eval** already owns calibration, priors, and the reward/quality signal (`project_eval_owns_qa`, `project_router_prior_miscalibration`). "Accuracy target" is Eval's domain — Product Promise should consume Eval's existing quality_score/calibration output as an input, not invent a parallel accuracy concept.
- **Router** already exists as "a constrained optimizer over Eval priors" (`project_router_reasoning_strategy`) — deciding strategy/behavior per turn. Some of what Product Promise proposes (how long, what format) already overlaps with Router's stated territory. These two need a clear division of labor: is Router the mechanism that *executes* Product Promise's contract, or are they now redundant?
- **`agent_role`** (explore/synthesize/draft — signed, not yet built, explicitly scoped as Phase B territory: driving temperature and potentially content per round position) is the existing, already-designed extension point for "different behavior at different points in a turn." Product Promise's "how long to spend" / "can we extend" logic maps naturally onto agent_role's round-position signal rather than requiring a wholly separate new mechanism.

Building Product Promise without reconciling these is how the fleet ends up with four overlapping policy layers (Eval, Router, agent_role/Phase B, Product Promise) instead of one coherent one. This reconciliation is exactly the kind of design work that should happen with Chat Architecture (and probably Eval + Router's sessions directly) before implementation starts — flagging, not gatekeeping.

## 5. Hard infra constraint

`MOBIUS_TURN_DEADLINE_S` (currently ~300s in dev, per the live deploy config) is a Cloud Run request timeout — a real, hard ceiling, not a policy React or Product Promise can override. Any `time_budget.hard_ceiling_s` the contract sets must be **≤** this value, and any "can we spend more time" extension logic must account for whatever time has already elapsed in the turn, not just "add N more rounds" blindly (a round's wall-clock cost varies a lot by tool called — a single `web_scrape` round can take much longer than a cached `rag` lookup).

## 6. What this would require on React's side (once §3/§4 are resolved — not scoped yet)

- Replace the static `react_max_iterations_for_mode()` lookup with a contract-driven value (still needs a sane default/fallback when no contract is set — same fail-soft posture as everything else in this file's ownership area).
- Expose a structured "progress report" React emits at natural checkpoints (round completion, guidance-mode transition, critic verdict) for Product Promise to read — some of this already exists informally in `thinking_log`/emit-envelope signals and could likely be reused rather than rebuilt.
- If (b) (in-the-loop) is the chosen design: a real call site inside the round loop where React asks "am I allowed to continue," with its own fail-soft default (extension denied / current behavior unchanged) so a Product Promise outage never breaks a turn — matching the fail-soft posture used everywhere else in this codebase's v2 work.

None of this is built. This section exists so whoever picks up the implementation has a concrete starting list, not so it gets built before §3/§4 are settled.

## 7. Summary for Chat Architecture

Ananth is standing up a new agent ("Product Promise") to own an explicit contract for what a good ReAct turn looks like (time/token budget, accuracy target, tone, format, and — the genuinely new piece — decision authority to grant more time/rounds when the answer isn't reached). This spec lays out the concept, a draft contract shape, and flags three things that need resolving before a build starts: (1) between-turns vs. in-the-loop mechanics — a real cost/latency fork, (2) reconciling scope with Eval (accuracy), Router (strategy), and agent_role/Phase B (per-round behavior) so this doesn't become a fourth overlapping policy layer, (3) the hard `MOBIUS_TURN_DEADLINE_S` ceiling any time budget must respect. Not built, not implemented — routing for awareness and input per Ananth's instruction.

---

## 8. Model-bandit selection criteria (built 2026-08-04)

**Origin:** Ananth, continuing the "react prep" framing this whole doc started from — "the next part of our react logic is to specifically direct the bandit to give us a clear model that fits a criteria... the things that drive this are latency, token amount, level of reasoning required... this is part of the react prep because we need to account for the react call itself." Explicitly instructed to coordinate with Chat Architecture and LLM Agent to scope before building — not built unilaterally.

### 8.1 What already existed (verified against code, not assumed)

Before this work, react's LLM calls already went through `ModelRouter.select()` (Thompson sampling, `app/services/model_registry.py`) via `llm_manager.generate()`. Of Ananth's 3 criteria:

- **Token amount** — already solved. `router.select()` took `estimated_prompt_tokens`/`expected_output_tokens` as a hard pre-filter (candidates whose context window/TPM budget can't fit the request are dropped before Thompson sampling runs).
- **Reasoning depth** — existed, but coarsely. `app/services/bandit_weights.py`'s `fast`/`normal`/`thinking` composite-weight profiles (latency-vs-quality-vs-cost-vs-reliability) were derived ONLY from session-level `chat_mode` (quick→fast, copilot→normal, agentic→thinking) plus a few hardcoded per-stage floors. No per-call signal existed.
- **Latency** — only tracked as an OUTCOME (`router.update_ema(model_id, latency_ms, cost_usd)` after each call completes), never as a selection-time INPUT constraint.
- A `complexity: str | None` parameter existed on `generate()`'s public signature but was silently dropped before reaching `router.select()` — a dead parameter, not a working mechanism.

### 8.2 The two real gaps, and what got built

**Ownership split:** LLM Agent owns the actual selection mechanics (`model_registry.py`/`bandit_weights.py`/`llm_manager.py` — the router side). ReAct owns the caller side (deriving the two new signals from state react already has, and passing them through). Eval signed off on the calibration-risk question (per-call `reasoning_depth` variance doesn't corrupt the Beta posterior, since it's recomputed fresh per call from mode-invariant raw PG stats — nothing mode-weighted is ever persisted).

**LLM Agent's side (already shipped independently):** `router.select()`, `generate()`, and `generate_sync()` (partial — see below) now accept two new optional kwargs, both `None` by default (zero behavior change for any existing caller):
- `reasoning_depth: str | None` — a SOFT nudge. Overrides the chat_mode-derived `bandit_mode` for that one call, reusing the existing `fast`/`normal`/`thinking` weight tables (no new weight math).
- `latency_budget_ms: int | None` — a HARD pre-filter, same architectural shape as the existing token-budget filter. Trims candidates whose `ema_latency_ms` exceeds the budget before Thompson sampling runs; if that would empty the candidate pool, keeps the single fastest candidate rather than hard-failing.

**ReAct's side** (`app/pipeline/react/governor.py`):

```python
def agent_role_to_reasoning_depth(agent_role: str | None) -> str | None:
    # explore -> "fast" (a lookup round, latency-favoring model is fine)
    # synthesize -> "thinking", draft -> "thinking" (real synthesis work)
    # None -> None (governor off, or role not resolved this round)

def latency_budget_ms(contract, elapsed_s, directive) -> int | None:
    # Only set when directive is "consolidate"/"finalize" — most rounds
    # are unconstrained (None). Reuses the SAME time-accounting the
    # governor uses for its own hard-stop (hard_ceiling_s, FINALIZE_MARGIN_S)
    # rather than an independently-invented number, so the model-selection
    # deadline and the governor's own emit-now deadline can't drift apart.
```

Wired into `react_loop.py`'s main reasoning-round call site (`_call_llm_json(..., stage=f"react_{rn}", ...)`), both derived per-round from the governor's own live state and passed straight through to `generate()`. Both stay `None` whenever `MOBIUS_PRODUCT_PROMISE_ENABLED` is off — same fail-soft posture as every other governor signal in this codebase.

### 8.3 A real bug caught during implementation, not by inspection

The first wiring attempt derived `reasoning_depth` from the composition-selection block's `_agent_role` local variable — which only populates when `MOBIUS_PROMPT_SOURCE=composition` is *also* set (an unrelated flag, needed for DB composition lookup, not for knowing explore/synthesize/draft). That silently left `reasoning_depth` `None` whenever the composition flag was off, even with the governor fully on. Caught by mocking `_call_llm_json` and directly inspecting the kwargs it actually received on a scripted turn — not by re-reading the code and assuming it was correct. Fixed by deriving the bandit's agent_role directly from `_pp_pre_directive` via `directive_to_agent_role()`, independent of the composition-selection path. Locked in permanently by `tests/test_governor_bandit_criteria.py::test_reasoning_depth_reaches_call_llm_json_without_composition_flag`.

### 8.4 Correction (2026-08-04)

Earlier revisions of this section flagged `generate_sync()` as not yet accepting `reasoning_depth`/`latency_budget_ms`. That was wrong at the time of writing and LLM Agent corrected it: both kwargs were added to `generate_sync()` in the same commit (`bb0fb0e`) that added them to `generate()`/`router.select()` — verified directly against the current signature, not just taken on their word. No gap; both the sync and async paths carry the full criteria.

### 8.5 Tests

- `tests/test_governor_bandit_criteria.py` — 15 tests: pure-function coverage for both derivations (all `agent_role`/`directive` mappings, boundary cases on the latency formula) plus 3 end-to-end tests through `run_react()` proving the actual kwargs reach `_call_llm_json` correctly (the governor-off fail-soft floor, the consolidate-triggers-a-latency-budget case, and the regression above).

## 9. Continuous groundedness heuristic (scaffolded 2026-08-04, flag OFF)

**Why.** `CritiqueResult.has_blocking_issues` (§critic.py) is boolean — a turn either has a high-severity ungrounded claim or it doesn't. The Bandit Agent reward contract wants a continuous groundedness signal per turn instead, closer to what mobius-rag's `fact_checker.check_facts(grounding_only=True)` already returns as a `.score` in `[0, 1]` for RAG's own producer arms. Calling that fact_checker live from react's round loop would mean a cross-service network hop plus a second judge-model call per scoring round — directly working against the same latency budget §8's `reasoning_depth`/`latency_budget_ms` criteria exist to protect. So this is an in-process approximation computed from data the critic already produces, not a new judge call.

**The formula.** `app/pipeline/react/critic.py::compute_groundedness_heuristic(issues, weights=None)`:

```
score = clamp(1 − Σ penalty[issue.severity] for issue in issues, 0.0, 1.0)
penalty = {"high": 0.5, "medium": 0.2, "low": 0.05}   # provisional priors
```

This is a penalty sum, not a ratio — `CritiqueResult.issues` only contains *flagged* (ungrounded) claims, there's no total-claims denominator to divide by (Eval caught this on the first draft of the formula, which had assumed a `1 − unsupported/N_claims` shape that isn't computable from the critic's actual output).

**Direction vs. magnitude.** Eval gave a structural argument (2026-08-04, not an empirical sample) that the *direction* is safe to build on without waiting for calibration: both this penalty sum and fact_checker's `.score` are monotonic in the same underlying construct — unsupported-claim mass in the answer — so a sign inversion would require the critic and fact_checker to disagree on what "grounded" means, which is implausible for two source-grounding judges applying near-identical rubrics. *Magnitude* (the actual weight values) is explicitly NOT safe yet — that's what Eval's locked-judge GCP calibration run resolves. Two shape risks flagged ahead of that run, both worth knowing before reading any value out of this function today:

1. **Saturation** — `high=0.5` means two high-severity issues already floor the score at `0.0` (`0.5 × 2 = 1.0` penalty). A turn with 2 fabrications and a turn with 8 both score `0.0`; fact_checker's continuous score would still discriminate between them. Eval expects the fit to want `high` lower than `0.5`.
2. **Granularity** — this is a coarse discrete sum standing in for a continuous score. The calibration run should *fit* the three weights from real (answer, chunks) pairs against fact_checker's score, not just validate the `{0.5, 0.2, 0.05}` priors as correct.

**Why gated, and why config not code.** `groundedness_heuristic_enabled()` (env `MOBIUS_REACT_GROUNDEDNESS_HEURISTIC`, same on/off value set as `critic_enabled()`) is OFF by default — this is a scaffold, not wired into any decision path. `has_blocking_issues` alone still decides whether a round loop continues; nothing about turn behavior changes whether this flag is on or off. The three penalty weights are env-overridable (`MOBIUS_REACT_GROUNDEDNESS_PENALTY_{HIGH,MEDIUM,LOW}`), not hardcoded, so when Eval's GCP run produces fitted values, dropping them in is a config change, not a code change or a redeploy of the formula itself.

**Where it surfaces.** When the flag is on and either critic call site (the Product Promise mandatory floor, or the legacy `critic_enabled()`-gated path) runs, `ctx.react_groundedness_score` is set from the round's `CritiqueResult.issues` and threaded into the `react_trace` diagnostics envelope (`make_react_trace(..., groundedness_score=...)`, `app/communication/emit_envelope.py`) alongside the existing boolean `groundedness_passed`. `None` when the flag is off or the critic never ran that turn.

**Tests.** `tests/test_react_critic.py::TestGroundednessHeuristicFlag` (flag on/off values) and `::TestComputeGroundednessHeuristic` (pure-function coverage: zero issues, each severity alone, the exact 2-high saturation case Eval flagged, mixed severities, env-override, invalid-env fallback, explicit-weights-param precedence). `tests/test_react_trace.py::test_trace_groundedness_score_none_when_heuristic_flag_off` / `::test_trace_groundedness_score_populated_when_heuristic_flag_on` — end-to-end through `run_react()`, not just the pure function.

**Not done here:** nothing reads or acts on `react_groundedness_score` yet — no gating logic, no bandit reward wiring, no dashboard. That's the next decision once Eval's calibration run lands, not part of this scaffold.
