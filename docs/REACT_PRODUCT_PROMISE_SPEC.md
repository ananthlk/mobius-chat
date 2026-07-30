# SPEC — ReAct Product Promise Contract (module prep, draft)

**Author:** ReAct Agent
**Status:** DRAFT — proposed by Ananth 2026-07-30, live session. Spec'd out for a new agent Ananth is standing up ("Product Promise"). Not built, not scoped for implementation yet. Routing to Chat Architecture for awareness + input before this goes further, per Ananth's own instruction.
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
