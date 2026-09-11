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
Passthrough is verbatim `{version, blocks}`, not re-wrapped, `version` not stripped — Governor asserts byte
identity with `/chat/response/{cid}` in the endpoint test rather than trusting the composer.

**The A/B page is a container for two instances of the production renderer, not a rendering path of its own**
(Ananth asked "same UI both boxes?" — yes, and this is why). Same renderer both sides means any visible
difference between box 1 and box 2 is a real difference in *what the orchestrator produced*, never a difference
in the viewer. That has a consequence Governor now imposes on the arms, not the page: **the renderer accepts
exactly ONE contract, so v2's COMMUNICATE must emit the same `assistant_envelope` shape or box 2 falls back to
trace-mode permanently.** The shape isn't a convention the two arms agree to (agreement splits the moment one
side wants a variant) — it's a constraint the single renderer imposes on both.

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
      "decision_trace": [                   // v2's rows carry the SHADOW HALF — the compare() output, stored
        { "round_n": 1, "posture": "gather", "directive": "…", "gap_targeted": "g1",
          "gaps_opened": ["g1","g2"], "gaps_closed": [], "rationale": "…",
          "v1_directive": "…", "v1_reason": "…", "v1_maps_to": "gather",
          "verdict": "agree" }              // verdict ∈ {agree, diverge, unmapped}; = turn_rounds.shadow_verdict,
                                            //   AUTHORED server-side in compare(). Read it; NEVER recompute it FE-side.
      ],
      "delivered": { "latency_ms": null, "cost_usd": null, "exit_mode": "shadow", "rounds": 12 },
      "kept": null, "in_band": null         // null renders "—", never "false" (unset ≠ false; the count-vs-total lesson)
    }
  }
  // NO separate divergences[] array. The strip is a FILTERED VIEW of v2.decision_trace
  // (rows where verdict != "agree"), never a second representation — §2. One source.
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

**Sources are named and confirmed (Governor 2f6b912 / 05bfdc5, 2026-09-10) — both §0/§4 hard deps resolved:**
- `answer_envelope` ← **`GET /chat/response/{correlation_id}`.`assistant_envelope`**, the verbatim object the live
  turn produced ({version, blocks}). No pre-render, no markdown, no reconstruction — box 1 runs the bubble's code.
- `decision_trace` ← **`turn_rounds`** (migration 068, dev-applied, idempotent). Column map: `round_n`←`round_index`,
  `posture`←`posture`, `directive`←`directive`, `rationale`←`rationale`, `gap_targeted`←`gap_targeted`,
  `gaps_opened/closed`←JSONB of same name. v2's rows also carry the shadow half: `v1_directive`, `v1_reason`,
  `v1_maps_to`, and `verdict`←`shadow_verdict`. NOT `turn_spans` — that table has a `sampled`/`sample_rate` column
  that can make a row absent by config; `turn_rounds` has none and that absence is the row-is-truth guarantee.
  Trace is a persisted row now, never computed from a log line.
- **ONE source, no sibling array (Governor's call, my §2 one level up).** There is no pre-composed `divergences[]`.
  A second array carrying facts already in `decision_trace[]` drifts the first time someone edits one and not the
  other — the exact defect family this program removed repeatedly this week. The divergence strip is a **filtered
  view** of `v2.decision_trace` (rows where `verdict != "agree"`), computed at render time, never stored twice.

---

## 2 · Render — the page

**Distinct surface, unmissable harness banner.** Its own route (`/ab` or `/ab/{run_id}`), never the bubble.
A persistent top banner rendered from `harness:true`: **"A/B HARNESS — both arms ran on identical input.
Nobody was served. Production routes to exactly one orchestrator."** Governor §1: if a reader concludes
production may fork, the harness has done damage — so the page says it can't, in the page.

**Default = the near-production view; the machine internals expand (Ananth 2026-09-10).** A first-time reader
must land on *what a user would actually see* — the two rendered answers side by side, clean, exactly the
bubble — not a wall of postures and latencies. Everything diagnostic is **collapsed by default and expandable**,
so the near-production comparison is the resting state and the machine detail is one click away.

**Per comparison:**
```
┌─ [banner: NOT production — harness forks]                                                           ┐
├─ experiment header (from `experiment`): Held: question · copilot · 90d   |   Varied: orchestrator ──┤   ← Rule 5, always on
│  question text                                                                                      │
├──────────────────── box 1: ARM A [▾] ────────────────────┬──────────── box 2: ARM B [▾] ───────────┤
│  v1 answer, rendered via the PRODUCTION renderer          │  answer present → same renderer          │  ← DEFAULT view:
│  (byte-for-byte the bubble)                               │  answer null    → decision trace         │    just the answers,
│  [›] round-by-round trace  (collapsed)                    │  [›] round-by-round trace  (collapsed)   │    near-production
├─────────────────────── [›] Divergences (collapsed) — where the two machines disagreed ──────────────┤   ← expand for detail
├─────────────────────── [›] Terms (collapsed) — latency · cost · exit · rounds · kept ────────────────┤   ← diagnostic, subordinate
└──────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Collapse/expand rules (FE-only — no endpoint change):**
- **Each box `[▾]` collapses/expands independently** — collapse a long v1 answer to eyeball v2, or focus on one
  arm. Same measured-max-height animation the bubble's other collapsibles use; keyboard-focusable, `aria-expanded`.
- **On load, the resting state is near-production:** both answer boxes expanded (the answers ARE the point), and
  the trace / divergences / terms all **collapsed**. A new user sees a clean side-by-side of two rendered answers
  that looks like the product; a power reader expands the internals.
- **R0 degradation:** while v2 has no answer (`answer_envelope:null`), box 2's *content* is the decision trace —
  it isn't a collapsible extra there, it's what v2 has, so it shows. At R1 the answer becomes the box content and
  the trace drops to the collapsed `[›]` under it, matching box 1. So the same layout carries both stages.
- **A per-comparison "expand all / collapse all"** toggle in the experiment header, and the last choice is
  remembered (localStorage, per the standard try/catch-guarded pattern) so a reviewer working through 20 questions
  isn't re-collapsing on every one.

**Divergences = a filtered view of `v2.decision_trace`, keyed on the STORED `verdict` (Governor's condition,
load-bearing):** the strip is `v2.decision_trace.filter(r => r.verdict !== "agree")` — no separate array. Each
row's `verdict` gets a visibly different treatment; never one lumped "they disagreed" row:
- `agree` → filtered out (the null result; no divergence to surface).
- `diverge` → v1 and v2 reached different postures on the same, mapped dimension. The real signal — neutral
  "⚑ round N · v1 X vs v2 Y" row, the thing a human judges.
- `unmapped` → v1 said something the shadow mapping doesn't cover. **A finding about the MAPPING, not evidence
  against v2** — distinct, clearly-not-a-conflict style (muted, "mapping gap", e.g. "⊘ round N · v1 X — not
  covered by mapping"), kept out of any "where v2 differs" framing. Lumping it into `diverge` would make
  Governor's mapping coverage look like v2 errors.

> **🔴 Read the verdict; NEVER recompute it FE-side.** The page filters on the stored `verdict` field. It must
> **not** derive divergence by comparing `v1_maps_to`/`posture` values itself — that would be a *second mapping*,
> and it would disagree with the server's exactly where the server's is most interesting: `extend` resolves by
> **reason**, not by name. Two `extend` rounds with identical directives map to different postures depending on
> whether the reason is "quality issue flagged" vs "round budget exhausted"; a posture-vs-posture diff can't see
> that and would flag correct behaviour as divergence. `compare()` authors the verdict once, server-side; the page
> is a viewer. If a verdict ever looks wrong, the move is to tell Governor his mapping is wrong (which the FE is
> well placed to spot — that's the whole point of `unmapped` being its own state), NOT to out-vote it in render.

**The rendering (box 1/2) is the judgement surface; the terms are diagnostic and clearly subordinate** — which
is exactly why they collapse and the answers don't. Never an aggregate score. 20 comparisons = **0** data points
for v2's exit criteria and **20** for human judgement (Governor §2/§3); the page must not show a number that
reads like the former.

---

## 3 · Reusability — the page is arm-agnostic

The page renders `experiment.arm_a/arm_b.label` + `held_constant`/`varied` verbatim and keys the boxes by
arm id. It does **not** know it's v1-vs-v2. So the same surface takes any two arms with zero page changes:
`{label:"prompt profile A"}` vs `{label:"prompt profile B"}`, varied `["prompt_profile"]`; `manifest 57` vs
`manifest 5`; `model X` vs `model Y`. When both arms produce answers (prompt/model A/B), both `answer_envelope`s
are non-null and box 2 is in answer-mode — no special case. v2's trace-mode is just the `answer_envelope:null`
branch, not a v2-specific code path.

---

## 4 · Open

**RESOLVED (Governor 2f6b912 / 05bfdc5, 2026-09-10):**
1. ✓ **Real `assistant_envelope`** — `GET /chat/response/{cid}`.`assistant_envelope` returns it verbatim (§1). Box 1
   runs the bubble's code; §0 fidelity holds with zero reconstruction.
2. ✓ **Symmetric arms** — taken as specified: `answer_envelope:null` for v2 today, real at R1, box 2 switches by
   data. R1 is a data change, not a migration.
4. ✓ **Decision-trace persistence** — `turn_rounds` migration 068, dev-applied, row-is-truth (no sampling column,
   unlike `turn_spans`). Shadow columns present for the divergence view; `unmapped`≠`diverge` (§2).

**STILL OPEN:**
3. **Human-verdict capture — Ananth's call.** He said *"I can point at all the things working vs not."* If he wants
   it, I need a tiny write endpoint (`POST /ab/runs/{run_id}/q/{qid}/verdict {better:"v1"|"v2"|"tie", notes}`),
   rendered as a per-question annotation. **Governor's condition, which I adopt:** nothing in the system ever SUMS
   it, and the page says so where the verdict is entered. 20 comparisons = 0 data points for v2's exit criteria,
   20 for judgement — the instant a "13/20" exists, that distinction stops being observed and it gets quoted as an
   exit criterion. So: capture verdict + notes, never aggregate, state that on the page.
5. **Harness endpoint itself** — Governor has both composing sources (`/chat/response`, `turn_rounds`) + the
   question set (`eval/ab_question_set_v1.json`); it exists as this shape, not yet as code. He builds to this doc.
   I build the page once it returns.
