# Contract — the governor steers, react reports closure

**Version:** v1 (proposed 2026-09-12)
**Between:** Governor (orchestrator v2) ⇄ react round loop / `react.response_shape`
**Author:** Governor seat · **Requires:** LLM/prompt seat for the emitted field
**Raised by:** Ananth, 2026-09-12, after three runs of *"What is the care
management philosophy for Molina, Sunshine health and united healthcare"*
returned Molina only and declared the other two unavailable.

---

## The defect this exists to close

The governor selects, every round, the gap worth spending on. It writes that
selection to `turn_rounds.gap_targeted`.

**Nothing reads it.** Every consumer of `gap_targeted` is a governor module or
the A/B harness. The round prompt is never told which gap to close, so the
model picks a query unaided. On a three-payer question it picks one payer,
and the governor's central decision is inert.

Measured, 24h: 417 governor decisions, 0 that changed which gap was pursued.

And the return path is equally blind. `evidence_review` carries four fields:

```
keep · running_answer · gaps_closed[] · gaps_open[]
```

A gap is open or closed. **There is no gradient.** "Found the program name but
not the philosophy" and "found nothing" are the same signal — the string stays
in `gaps_open`. The governor cannot distinguish *near closing* from *stuck*,
which is the distinction every one of its rules turns on.

`returned_payload` is worse than binary: it is computed once **per round**
(`bool(gaps_closed) or bool(running_answer)`) and stamped onto every gap. That
is how Molina — which demonstrably returned content — recorded
`returned_payload=False`.

---

## The loop this contract enables

Ananth's words, 2026-09-12:

> *"the first round you start react with i have one gap — question — review.
> create more gaps.. target the first and most important.. try closing this..
> once you see it is near closing.. you instruct react. you have tried gap-1,
> if this round did not close it, keep at it, if closed pick the next gap.. if
> more gaps add.. so you govern it."*

```
R1   one gap (the question)  →  react decomposes  →  N gaps
R2+  governor names ONE gap  →  react works it    →  closure per gap
     ├─ closure rose, not closed   → keep at it, same gap
     ├─ closure flat 2 rounds      → change lever / reframe
     ├─ closed                     → next gap by importance
     ├─ new sub-questions named    → add as gaps
     └─ closure 0 after a targeted attempt → CAPABILITY, on evidence
```

Round 1 already works: `react.response_shape` v5 asks for `gaps_open` on
round 1 and a three-payer question yields three gaps at `opened_round=1`.
Steps 2 onward are what this contract adds.

---

## Direction 1 — governor → react (STEERING)

A block appended to the round context, beside the existing
`round_directive_text`. Not a system-prompt block: it is per-round state, and
it belongs where the other per-round state already is.

```
[Governor]
close this gap: "Sunshine Health care management philosophy"    (id S384280)
status: attempted 1 round, closure 60 (was 0)
instruction: closure rose but the gap is not closed. Stay on THIS gap.
             Your last query was "sunshine health care management" — the
             ledger says that returned the program name and eligibility.
             Ask for the part still missing.
do not: open a new topic, or answer the other gaps, until this one closes
        or you report it cannot be closed.
still open after this one: UnitedHealthcare care management philosophy
```

**Rules the wording must honour**, each earned:

1. **Name exactly one gap.** The observed failure is the model covering one
   payer and calling it done; a list restates the question and changes nothing.
2. **Carry the attempt history.** "You tried X and got Y" is the whole reason
   `REFORMULATE` was folded into `CLOSE` — the distinguishing fact is data the
   governor holds, not a name it asserts.
3. **Never dictate the query.** The governor decides WHERE to spend, never
   WHAT to say or ask. A governor writing queries is a second author of the
   answer.
4. **State what remains.** Suppressing the other gaps entirely invites a
   premature "complete"; the model needs to know the turn is not over.

## Direction 2 — react → governor (CLOSURE)

Added to `evidence_review`. **Additive: every existing field keeps its meaning
and its consumers.**

```json
"gaps": [
  {"id": "S384280",
   "text": "Sunshine Health care management philosophy",
   "closure": 60,
   "why": "found the ICM program and eligibility; the stated philosophy is missing"}
]
```

| field | meaning |
|---|---|
| `id` | echoed back from the `[Governor]` block; absent for a newly opened gap |
| `text` | the gap, unchanged in wording where possible — see id drift below |
| `closure` | 0–100, "how much of THIS gap the kept evidence now answers" |
| `why` | one clause naming what is present and what is missing |

`closure: 100` and membership in `gaps_closed` must agree. If they disagree the
governor records the disagreement and trusts `gaps_closed`; a field that can
contradict an older field silently is worse than no field.

---

## How the governor reads `closure` — and how it must not

**Bands and deltas only. Never the absolute number.**

```
BAND   none 0 · trace 1-24 · partial 25-59 · most 60-89 · closed 90-100
DELTA  sign and band-crossing, not magnitude
```

`62 → 68` and `60 → 70` mean the same thing. A rule that separates them is
tuned against a self-report's noise, which is how `[GUESS]` constants
calibrated on unnamed strings became load-bearing once already.

**`closure` is a CLAIM, not a measurement.** The model grades its own progress,
and this system has already shipped a model asserting "no sources" beside a
substantive answer — `self_report_contradicts_answer` exists for it. So every
closure value is cross-checked against evidence the governor can see itself:

| claim | check | on disagreement |
|---|---|---|
| closure rose | sources arrived, or `keep` grew | record `closure_unsupported`, do NOT advance the band |
| closure = 100 | gap is in `gaps_closed` | trust `gaps_closed` |
| closure = 0 | no sources returned | consistent; this is the CAPABILITY signal |

The cross-check result is persisted per round. A closure the governor accepted
and a closure the governor discounted must be distinguishable later, or the
first bad calibration becomes invisible.

---

## Gap identity

Gap ids are content-addressed (sha1 of normalised text), so **a rewording mints
a new id and resets age and lever counts** — which are exactly what make
`stuck` reachable. The `[Governor]` block echoes the id and asks for it back
specifically to stop that: an id echoed by the model survives a rewording of
the text.

`reworded_from` / `reworded_similarity` already record drift. **Their rate is
the acceptance test for this change**: if echoing ids works, drift falls; if it
spikes, the ids are being held together by a 0.70 similarity threshold rather
than by agreement, and this contract is not doing its job.

---

## What the governor will NOT do

- **Not write the query.** See rule 3.
- **Not close a gap itself.** Only react closes gaps; the governor reads.
- **Not rewrite closure.** It records, cross-checks and discounts — never edits
  a model's self-report into something more flattering or more pessimistic.
- **Not build a second decomposer.** Gaps come from react. A decomposition only
  the governor can see is a producer with no consumer, which is the defect this
  document exists to end.

---

## Acceptance — measured, not asserted

Each is a query against `turn_rounds`, not a claim in a report:

1. **Steering is consumed:** on a multi-gap turn, the gap named in the
   `[Governor]` block appears in the next round's query terms. Today: never.
2. **`gap_targeted` has a consumer:** the field is read on the prompt path.
   Today: read by no module outside the governor.
3. **Three-payer questions cover three payers:** Ananth's question returns all
   three, or names which it could not close and why. Today: one of three.
4. **CAPABILITY rests on per-gap evidence:** no CAPABILITY exit on a gap whose
   own closure was never observed. Today: fires off a round-level boolean.
5. **Id drift does not spike:** `reworded_similarity < 1.0` rate flat or lower
   after the change.

---

## Sequencing

| # | change | owner |
|---|---|---|
| 0 | BUDGET stops being a terminal — without it the turn dies at round 2 and no round exists to steer | Governor |
| 1 | per-gap closure ledger, bands, deltas, cross-checks | Governor |
| 2 | `[Governor]` steering block wired into round context | Governor |
| 3 | `closure` / `gaps[]` emitted in `evidence_review` | LLM / prompt seat |
| 4 | acceptance queries above, run on real turns | Governor |

Steps 0–2 ship dark: the governor emits the block and reads `closure` when
present, defaulting to today's binary behaviour when absent. **Step 3 is the
only step that changes what the model is asked for**, and until it lands the
governor's steering is measurable (does the named gap appear in the query?)
without any dependence on a field that does not exist yet.
