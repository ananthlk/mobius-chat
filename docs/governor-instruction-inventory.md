# The laundry list — every modular thing the governor asks react to do

**Status:** draft for Ananth, 2026-09-12
**Author:** Governor seat (orchestrator v2)
**Why now:** Ananth — *"i think it's a set of 20 modular things you want react
to do... we just have a few constants and some profile based and some dynamic..
you need to get these right, that's it."*

---

## What we have today

```
module=react_explore   52 uses
module=critic_audit     8 uses
```

**Six postures resolve to one prompt.** `PROMPT_MISMATCH` already records that
EXPLORE extends into v1's *remediation* prompt — declared rather than fixed, so
an A/B divergence stays attributable to the decision. That declaration is now
the oldest unpaid debt in this module.

## The three kinds, and why the distinction is load-bearing

| kind | changes | fails as |
|---|---|---|
| **CONSTANT** | never | silently dropped — nobody notices a rule that stopped being sent |
| **PROFILE** | per posture / mode | the wrong profile for the round, and the row still says the right posture |
| **DYNAMIC** | per round, from the ledger | a producer with no consumer — computed, never read (this is where `gap_targeted` sat for 417 decisions) |

A unit in the wrong column is the defect, not a style choice: a dynamic fact
frozen into a constant becomes a lie the day it changes, and a constant
recomputed per round is a rule that can silently stop being sent.

---

## The list

Legend — **have**: shipped · **partial**: exists, incomplete · **missing**.
Every unit names the evidence that earns it; a unit with no failure behind it
is a unit nobody needs yet.

### A. Framing — what is being asked

| # | unit | kind | state | earned by |
|---|---|---|---|---|
| 1 | **Name every part the question asks for** — a report, never a work queue | PROFILE (round 1) | have (v6) | 3/3 runs named three payers then searched one: *"starting with Molina Healthcare"* |
| 2 | **Ask the question as asked** — one query naming all parts; let the tool decompose | PROFILE (round 1) | have (v6) | broad query: 3 payers ~30s · single-payer query: 1 payer 73s |
| 3 | **Classify the ask** — factual / comparison / procedural / ambiguous | PROFILE | missing | a comparison and a lookup have different completion bars and share one prompt today |
| 4 | **Ask the user instead** when the question cannot be answered as posed | CONSTANT | have | grounding contract: clarifying is a legitimate outcome |

### B. Evidence — getting and curating it

| # | unit | kind | state | earned by |
|---|---|---|---|---|
| 5 | **Choose the tool for this round** | DYNAMIC | partial | static per-turn `allowed_tools`; `estimate()` built and unwired |
| 6 | **Write the query** — react's job, never the governor's | — | have | the governor decides WHERE to spend, never WHAT to ask |
| 7 | **Keep / discard retrieved chunks** | CONSTANT | have | `keep` drives 80267→35762 char curation |
| 8 | **Say what arrived** — kept count, distinct from what was used | DYNAMIC | have (tonight) | 15 sources returned read as "nothing came back" → CAPABILITY at 25s of a 95s promise |
| 9 | **Review evidence against the question** and name what is still missing | DYNAMIC (round 2+) | have (tonight) | the `[Governor]` discover block |
| 10 | **Report per-gap closure** 0–100 + why | DYNAMIC | missing (LLM seat) | binary open/closed cannot separate *near closing* from *stuck* |
| 11 | **Distinguish searched-and-not-found from never-searched** | DYNAMIC | have (tonight) | one Molina query marked all three gaps attempted → CAPABILITY on two never searched |

### C. Strategy — what to do when a round fails

| # | unit | kind | state | earned by |
|---|---|---|---|---|
| 12 | **Reformulate** a query that returned nothing | DYNAMIC | partial | `gap_status: stagnant` exists; carries no attempt history |
| 13 | **Change lever** — a different tool, not a reworded query | DYNAMIC | partial | REFORMULATE folded into CLOSE: the distinguishing fact is `attempted_by` |
| 14 | **Escalate source class** — corpus → web → authoritative | PROFILE | partial | RAG under-selects `d` (web), its strongest arm |
| 15 | **Declare a gap unreachable**, with the reason | DYNAMIC | partial | CAPABILITY fires off a round-level boolean, not per-gap evidence |
| 16 | **Do not repeat the query you just ran** | DYNAMIC | have (tonight) | in the discover block |

### D. Synthesis — building the answer

| # | unit | kind | state | earned by |
|---|---|---|---|---|
| 17 | **Maintain a running answer** across rounds | CONSTANT | have | `running_answer` |
| 18 | **Ground every claim** in something a tool returned | CONSTANT | have | `react.grounding_contract` |
| 19 | **Report partial honestly** — found X, did not find Y | CONSTANT | have | the contract's own worked example is our three-payer question |
| 20 | **Say WHY a part is missing** — not searched / searched and thin / not in corpus | DYNAMIC | **missing** | *"not available in the provided documents"* renders identically for all three. **This is the sentence Ananth keeps seeing.** |
| 21 | **Format to the card** — bottom line, bullets, length | PROFILE | have | mode blocks |
| 22 | **State confidence and its limits** | PROFILE | have | mode blocks |
| 23 | **Offer next steps** the user can act on | PROFILE | have | fallback block |

### E. Control — when to stop

| # | unit | kind | state | earned by |
|---|---|---|---|---|
| 24 | **Satisfaction check** — *"are you satisfied with the level of answer and evidence you have?"* | CONSTANT | **missing** | every criterion grades the ANSWER; none grades the EFFORT |
| 25 | **Declare `is_complete`** — react's call, always | CONSTANT | have | react has the evidence; it is better placed |
| 26 | **Governor's dissent** — "my ledger says part X was never attempted; still complete?" asked ONCE | DYNAMIC | **missing** | 440 decisions: 50 subtractions, **0 additions** |
| 27 | **Respect the budget directive** — consolidate / stop | DYNAMIC | have | product promise |
| 28 | **The hard ceiling** — arithmetic, not judgement | CONSTANT | have | 98-round runaway |

---

## The shape of the fix, in one line

**Today the governor's only exercised power is #27-as-termination. The units
that would make it useful — #10, #20, #24, #26 — are the four that are
missing.** 48h: 440 decisions, 50 turns cut short, 32 tool calls killed (27 of
them `rag`), 0 turns extended.

## Ordering, by evidence rather than by appetite

1. **#20 — say WHY a part is missing.** Cheapest, and it is the sentence Ananth
   has been reading all night. Needs #11, which shipped tonight.
2. **#24 + #26 — the satisfaction check and the governor's single dissent.**
   The handshake: react keeps the completion call, the governor contributes the
   one fact react cannot hold. Runs through the accelerator — the half that has
   never fired — not the stop path that has fired 50 times and been wrong each
   time.
3. **#10 — per-gap closure.** LLM seat owns the field. Unblocks the `[GUESS]`
   constants, which can only be re-derived against real per-gap data.
4. **#5 — per-round tool selection.** `estimate()` is built, priced and
   verified; the Dockerfile COPY is the remaining step.
5. **#3, #14 — real profiles.** Last, because the posture→prompt mismatch is
   declared and attributable today, which makes it the *safest* of these debts
   rather than the most urgent.

## What must not happen to this list

- **A unit nobody reads.** Every DYNAMIC row needs a named consumer on the
  prompt path before it is built. `gap_targeted` was computed for 417
  decisions and read by nothing.
- **A unit in the wrong column.** See the table above.
- **A rule that exists only as a comment.** Each shipped unit needs a gate that
  fails when the unit stops being sent — mutation-checked, or it proves nothing.
