# A/B harness — FE render + endpoint contract

**Status:** DRAFT for Governor ⇄ Chat-FE sign-off · Ananth-ruled A/B UX · 2026-09-10
**Companion to:** `../../docs/ab-harness-contract.md` (a7933b5, Governor) — that's the isolation+provenance
half (production routes / harness forks / what the harness may not do / the 20-question set). **This is the
FE-facing half:** the endpoint shape the page reads and how it renders. Governor asked "tell me the shape
you want and I'll build the endpoint to it" — this is that shape.
**Chat-FE hard no-code gate:** this is a shape spec. I build the page against it once the endpoint returns it.

---

## 0 · The one constraint that makes the whole thing valid

**The page judges NOTHING. A human judges by reading two renderings.** So the single most important
property is **rendering fidelity: box 1 must render v1's answer through the PRODUCTION renderer, byte-for-byte
the same path the live bubble uses** (`envelopeToAnswerCard` + `renderAnswerCard` / `renderEnvelope`), never a
re-implementation. Governor's §3 states the failure directly: *"if v1's answer renders worse here than in the
real bubble, the judgement is about your page, not the orchestrator."* Everything below serves that.

Corollary: **the endpoint returns v1's answer as the REAL `assistant_envelope`** — the exact object the
production turn produced and the bubble consumes — **not pre-rendered HTML, not markdown, not a summary.**
If I'm handed HTML I cannot guarantee parity with the bubble, and the comparison is compromised.

---

## 1 · Endpoint shape

```
GET /ab/runs?set=ab_question_set_v1   → { run_id, set_id, created, questions: [{ id, q, shape, status }] }
GET /ab/runs/{run_id}/q/{qid}         → ONE comparison (below). 200-with-empty-arms on a not-yet-run qid,
                                        never 404 (a not-run question and a failed run are both "no rows"; an
                                        HTTP code that also means "bad id" would collapse them — Governor's rule).
```

**Comparison payload — SYMMETRIC arms, so R1 is a data change not a schema change:**
```jsonc
{
  "question": { "id": "q01", "q": "what are the timely filing deadlines for sunshine health?",
                "shape": "lookup", "mode": "copilot" },
  "harness": true,                          // page renders the "NOT production" banner from this flag (§2)
  "experiment": {                           // Rule 5, data-driven → also the reusability mechanism (§4)
    "arm_a": { "id": "v1", "label": "v1 orchestrator" },
    "arm_b": { "id": "v2", "label": "v2 posture machine (shadow)" },
    "held_constant": ["question", "mode:copilot", "window:90d", "input"],
    "varied": ["orchestrator"]
  },
  "arms": {
    "v1": {
      "answer_envelope": { /* the REAL assistant_envelope — §0. Page renders via the production renderer. */ },
      "decision_trace": [                   // structured, round-by-round — NOT free-text log lines (row=truth)
        { "round_n": 1, "posture": "gather", "directive": "…", "gaps_opened": ["g1"], "gaps_closed": [], "rationale": "…" }
      ],
      "delivered": { "latency_ms": 17504, "cost_usd": 0.012, "exit_mode": "complete", "rounds": 9 },
      "promised":  { "latency_ms": 20000, "exit_mode": "complete" },
      "kept": true, "in_band": true
    },
    "v2": {
      "answer_envelope": null,              // ← null NOW (posture machine, shadow); a real envelope at R1,
                                            //   rendered IDENTICALLY to v1. box 2 = answer-mode when present,
                                            //   trace-mode when null. The switch is data-driven, no retrofit.
      "decision_trace": [ { "round_n": 1, "posture": "gather", "directive": "…", "gaps_opened": ["g1","g2"], "gaps_closed": [], "rationale": "…" } ],
      "delivered": { "latency_ms": null, "cost_usd": null, "exit_mode": "shadow", "rounds": 12 },
      "kept": null, "in_band": null         // null renders "—", never "false" (unset ≠ false; the count-vs-total lesson)
    }
  },
  "divergences": [                          // §5 payoff: the R0 view where the machines disagree, by round
    { "round_n": 9, "dimension": "exit", "v1": "complete", "v2": "gaps_increasing", "why": "v2 saw 2 gaps still open" }
  ]
}
```

Notes that are load-bearing, not stylistic:
- **`arms` is a map keyed by arm id, arms are symmetric.** Both carry `answer_envelope` + `decision_trace` +
  `delivered`. v2's `answer_envelope` is `null` today and a real envelope at R1 — same key, same render path.
  Designing for the two-answer end state now (Governor §5) means the schema is already symmetric.
- **`null` is not `false` and not `0`.** `kept:null`/`in_band:null`/absent latency render as **"—"**, never a
  clamped value. Same rule as `self:"n/a"` when parallel calls make it uncomputable. (This is the same defect
  family the program keeps removing — a UI that renders a value where the data holds "unknown".)
- **`gaps_opened`/`gaps_closed` are id arrays**, so the page shows *which* gaps and can diff them across arms —
  a count alone can't distinguish "closed the same gap it opened" from "closed a different one".

---

## 2 · Render — the page

**Distinct surface, unmissable harness banner.** Its own route (`/ab` or `/ab/{run_id}`), never the bubble.
A persistent top banner rendered from `harness:true`: **"A/B HARNESS — both arms ran on identical input.
Nobody was served. Production routes to exactly one orchestrator."** Governor §1: if a reader concludes
production may fork, the harness has done damage — so the page says it can't, in the page.

**Per comparison:**
```
┌─ experiment header (from `experiment`): Held: question · copilot · 90d   |   Varied: orchestrator ─┐   ← Rule 5, on the page
│  question text                                                                                     │
├──────────────────────────── box 1: ARM A ────────────────┬──────────── box 2: ARM B ──────────────┤
│  v1 answer, rendered via the PRODUCTION renderer          │  answer_envelope present → same renderer│
│  (byte-for-byte the bubble)                               │  answer_envelope null    → decision trace│
│                                                           │  (round-by-round, collapsible)          │
├─────────────────────── divergences strip (where they disagreed, by round) ─────────────────────────┤
│  ⚑ round 9 · exit: v1 "complete" vs v2 "gaps increasing" — v2 saw 2 gaps still open                 │
├─────────────────────── the machine's terms (diagnostic, NOT judgement) ─────────────────────────────┤
│  latency 17.5s vs — · cost $0.012 vs — · exit complete vs shadow · rounds 9 vs 12 · kept ✓ vs —     │
└─────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

**The rendering (box 1/2) is the judgement surface; the terms strip is diagnostic and clearly subordinate.**
Never an aggregate score. 20 comparisons = **0** data points for v2's exit criteria and **20** for human
judgement (Governor §2/§3); the page must not show a number that reads like the former.

---

## 3 · Reusability — the page is arm-agnostic

The page renders `experiment.arm_a/arm_b.label` + `held_constant`/`varied` verbatim and keys the boxes by
arm id. It does **not** know it's v1-vs-v2. So the same surface takes any two arms with zero page changes:
`{label:"prompt profile A"}` vs `{label:"prompt profile B"}`, varied `["prompt_profile"]`; `manifest 57` vs
`manifest 5`; `model X` vs `model Y`. When both arms produce answers (prompt/model A/B), both `answer_envelope`s
are non-null and box 2 is in answer-mode — no special case. v2's trace-mode is just the `answer_envelope:null`
branch, not a v2-specific code path.

---

## 4 · Open — needs Governor + Ananth
1. **`answer_envelope` = the real `assistant_envelope`, not HTML/markdown** (§0). Confirm you can return it —
   this is the one hard dependency; fidelity dies without it.
2. **Symmetric arm schema now** (§1): v2's `answer_envelope:null` today, real at R1. Confirm you'll build it
   symmetric so R1 is data, not a migration.
3. **Human-judgement capture — Ananth's call.** He said *"I can point at all the things working vs not."* Does
   the page CAPTURE his per-question verdict/notes (persisted where)? It's the only quality signal in the fleet,
   so it's worth persisting — but it is HUMAN judgement and must **never** be aggregated into a metric (that's
   the "20 comparisons look like 20 data points" trap). If yes, I need a tiny write endpoint (`POST /ab/runs/
   {run_id}/q/{qid}/verdict {better: "v1"|"v2"|"tie", notes}`) and it renders as an annotation, never a score.
4. **Decision-trace persistence** (Governor owes, §"what I owe you"): the v2 trace is a log line today. The
   structured shape in §1 (`{round_n, posture, directive, gaps_opened[], gaps_closed[], rationale}`) is what I
   render round-by-round. Persist it queryable (row=truth, never computed from logs — your own contract).
