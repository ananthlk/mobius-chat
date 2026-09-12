# The profile statements, and the rule that selects each one

**Status:** draft for Ananth, 2026-09-12 · **Author:** Governor seat (orchestrator v2)
**Companion to:** `docs/governor-instruction-inventory.md` (what the units are)

Ananth: *"please get me the 20-40 profile statements and how we decide what
needs when.. that is the critical thing."*

The statements are the easy half. **The selector is the design.** A statement
with no selector is a constant nobody chose; a selector reading a field that
does not exist is a statement that never fires — which is how `gap_targeted`
was computed for 417 decisions and read by nothing.

---

## The selector vocabulary — everything a rule may read

**These exist and are computable today.** Nothing below may read anything else.

```
rn                       round index
tier                     fast | normal | thinking          (from chat_mode)
open_gaps                tuple[Gap]     — text, importance, opened_round
targeted_attempts(gap)   attempts KNOWN to aim at this gap  (tonight)
latest_closure(gap)      Closure(value, supported, why) | None
closure_trend(gap)       INCREASING | FLAT | DECREASING     (bands, not deltas)
stuck(gap, rn)           age>=3 AND >=2 distinct levers AND nothing returned
unreachable(gap, rn)     stuck OR (targeted attempts, none returned)
trend(history)           gap COUNT direction across rounds
spendable(state)         budget covers buy + act
may_overrun(state)       converging AND inside the band
worth_spending(state)    the gap, or None
exit_mode(state)         COMPLETE | BUDGET | CAPABILITY | ERROR
posture / branch         the machine's own call, never re-derived
model_proposes_complete  react's is_complete THIS round
kept                     chunks kept this round (tonight)
```

---

## THREE orderings, and conflating them is the bug

They are not the same list and they do not have the same shape. I wrote one and
called it both, which is how a "final round, do not call a tool" instruction can
end up rendered *after* "work this gap next".

### O1. TURN ORDER — which rounds a statement may fire in

```
round 1        ORIENT     ask the question as asked; name the parts
round 2        REVIEW     what came back; what is still missing
rounds 3..n    CLOSE      one gap at a time, with its history
any round      BOUND      ceiling / budget / error
final round    SETTLE     answer from what you have
```

Ananth: *"the old instruction actually works"* — round 1 gets ORIENT and FORM
and nothing else. Steering before evidence is steering from nothing.

### O2. EXECUTION ORDER — the sequence react performs inside one round

**This is the RENDER order.** The model reads top-to-bottom and acts in that
sequence, so a statement about formatting the answer must not sit above the
statement about what to search.

```
E1  BOUND        what constrains this round      (must be first: it changes everything below)
E2  ORIENT       what is being asked
E3  REVIEW       what I already have, what is missing
E4  SETTLE?      is this complete                (the handshake — BEFORE spending)
E5  TARGET       which gap this round
E6  APPROACH     new / retry / change lever / escalate
E7  ACT          choose tool, write query        (react's own, never dictated)
E8  CURATE       keep / discard
E9  RECORD       closure, gaps opened and closed
E10 SYNTHESISE   running answer, grounded, partial honestly
E11 FORM         shape, length, confidence, next steps
```

**E1 first is load-bearing.** "This is the final round" invalidates every
statement below it; rendering it last means the model has already planned a
tool call by the time it reads that it cannot make one.

**E4 before E5** is the whole dual-mode design: ask whether we are done before
deciding what to spend on. Reversed, the governor picks a gap and the
completion question never gets asked.

### O3. PRECEDENCE — which statement wins when two could fire

Inclusion only. **Never render order.** Within a group at most ONE fires:

```
1 SAFETY      ceiling, error          — arithmetic, never overridden
2 CONTROL     completion handshake
3 STRATEGY    what to do about a failing gap
4 EVIDENCE    what to do with what arrived
5 FRAMING     what is being asked
6 FORM        how to say it
```

A statement can win precedence and still render sixth. These are orthogonal.

### The mapping — every statement's slot

```
E1  BOUND        SAF-1 SAF-2 SAF-3 CTL-4 CTL-5
E2  ORIENT       FRM-1 FRM-2 FRM-3 FRM-4
E3  REVIEW       EVD-1 EVD-3
E4  SETTLE?      CTL-3 CTL-1 CTL-2
E5  TARGET       EVD-4
E6  APPROACH     STR-1 STR-2 STR-3 STR-4 STR-5 STR-6 STR-7
E7  ACT          EVD-2
E8  CURATE       (react's own; no governor statement)
E9  RECORD       EVD-5
E10 SYNTHESISE   FRM-5 FRM-6 FRM-7
E11 FORM         FRM-8 FRM-9 FRM-10 FRM-11 FRM-12 FRM-13
```

**CTL-3 renders before CTL-1**: the satisfaction question is asked before the
governor's dissent, so react answers it on its own evidence rather than
reacting to being challenged.

**S2. Contradictory statements must never stack.** This is not theoretical:
react_loop already uses `elif` deliberately so the final-round instruction and
the structural-exhaustion offramp cannot both land — *"the offramp's 'you're not
required to stop' framing would directly contradict the final-round
instruction's 'do NOT request another tool call'."* Any two statements that can
both fire must be proven non-contradictory or made mutually exclusive.

**S3. A cap of five per round.** More is a wall of text the model skims, and the
one that mattered is the one it skipped. Cap enforced at render, and a dropped
statement is RECORDED — silently dropping guidance is how a rule stops being
sent without anyone noticing.

**S4. Never on round 1 except FRAMING and FORM.** Round 1 has no evidence;
every other group would be steering from nothing. Ananth: *"the old instruction
actually works."*

**S5. Every statement is recorded per round.** Which fired, which were dropped
by the cap. A row that cannot reconstruct the prompt cannot explain the answer.

---

## The statements

`id · selector (executable) · statement`

### G1 SAFETY

| id | selector | statement |
|---|---|---|
| SAF-1 | `rn == max_it` | This is the final round. Answer from what you have; do not request another tool call. |
| SAF-2 | `exit_mode == ERROR` | A tool errored irrecoverably. Say what failed and answer from what you have. |
| SAF-3 | `extensions_used >= MAX_V2_EXTENSIONS` | The extension ceiling is reached. This is the last round regardless of open gaps. |

### G2 CONTROL — the completion handshake

| id | selector | statement |
|---|---|---|
| CTL-1 | `model_proposes_complete AND any(not targeted_attempts(g) for g in material)` | You marked this complete. My ledger shows **{gap}** was never searched this turn — no query named it. Confirm complete, or spend one round on it. Your call. |
| CTL-2 | `model_proposes_complete AND all(targeted_attempts(g) for g in open_gaps)` | You marked this complete and every open part was attempted. Proceeding. |
| CTL-3 | `always` (CONSTANT) | Before `is_complete=true`: are you satisfied with the level of answer and evidence you have — not "is the answer grounded", but "did I do enough to get it"? If a part is unanswered because you never looked, that is not complete. |
| CTL-4 | `not spendable(state) AND not may_overrun(state)` | The time budget is spent. Answer from what you have; say what is still open. |
| CTL-5 | `may_overrun(state)` | You are over the target but converging. One more round is authorised if it closes **{gap}** — not for polish. |

### G3 STRATEGY — a gap that is not moving

| id | selector | statement |
|---|---|---|
| STR-1 | `len(targeted_attempts(gap)) == 1 AND not closed` | **{gap}** was searched once and is still open. Previous query: {q}. Ask for the part it did not return. |
| STR-2 | `stuck(gap, rn)` | **{gap}**: {n} distinct levers, nothing returned. A reworded query returns the same evidence — change the approach or say it cannot be closed. |
| STR-3 | `closure_trend(gap) == INCREASING and not closed` | **{gap}** is closing — {prev}→{now}. Stay on it and ask for the part still missing. |
| STR-4 | `closure_trend(gap) == DECREASING` | **{gap}**: closure fell. The last attempt moved away from it. Return to what was working. |
| STR-5 | `unreachable(gap, rn) AND tier == thinking` | **{gap}** looks unreachable in this corpus. Before declaring it, try one materially different source class. |
| STR-6 | `trend(history) == INCREASING AND rn >= 3` | Open parts are growing, not shrinking. Stop widening; close one. |
| STR-7 | `gap_status == "stagnant"` | The last two rag calls converged on the same internal strategy and outcome. Another rag call with a similar query will not surface new information. |

### G4 EVIDENCE

| id | selector | statement |
|---|---|---|
| EVD-1 | `rn == 2 AND worth_spending is ROOT` | Review, do not re-ask. Compare what you have against what was asked. Name EACH part still missing as its own gap. If nothing is missing, say so. |
| EVD-2 | `rn > 1` (CONSTANT in-group) | Do not repeat the query you just ran. It returned what it returned; this round is for what it did not. |
| EVD-3 | `kept == 0 AND rn > 1` | Nothing was kept from the last call. Either the query missed, or this corpus does not hold it — say which you think it is. |
| EVD-4 | `len(open_gaps) > 1 AND worth_spending(state)` | Work **{gap}** this round. The others stay open and are not for this round. |
| EVD-5 | `always` (CONSTANT) | Report per-gap progress in `evidence_review.gaps`: closure 0-100 and why. *(LLM seat owns the field; not sent until it exists.)* |

### G5 FRAMING

| id | selector | statement |
|---|---|---|
| FRM-1 | `rn == 1` | Name every part this question asks for in `gaps_open`. That list is a REPORT of what was asked — not a plan to do them one at a time. |
| FRM-2 | `rn == 1` | Ask the question as asked. One query naming every part; rag decomposes across named entities better than sequencing does. |
| FRM-3 | `rn == 1 AND question is ambiguous` | This question has more than one reasonable reading. Ask the user rather than guessing — a clarifying question is a legitimate outcome. |
| FRM-4 | `tier == thinking AND rn == 1` | You have rounds to spend. Verify rather than accept the first plausible match. |

### G6 FORM

| id | selector | statement |
|---|---|---|
| FRM-5 | `always` (CONSTANT) | GROUNDING CONTRACT — every factual claim traces to something a tool returned this turn. |
| FRM-6 | `always` (CONSTANT) | On a multi-part question PARTIAL is the correct outcome. Report what you found; say plainly what you did not. |
| FRM-7 | **`part unanswered`** | For each part you could not answer, say WHICH of these it is: (a) searched, corpus is thin (b) searched, nothing relevant (c) not searched — ran out of budget. **Do not render all three as "not available in the provided documents."** |
| FRM-8 | `tier == fast` | At most one tool call. Bottom line plus 2-3 bullets. `is_complete=true` as soon as the answer is reasonable. |
| FRM-9 | `tier == normal` | A reasonable, practical, grounded answer is enough. Do not chase perfection. |
| FRM-10 | `tier == thinking` | Higher precision. Resolve avoidable ambiguity before completing when the user asked for definitive facts, numbers or policy detail. |
| FRM-11 | `user prefs say brief` | HARD CAP ~15 words. One verdict sentence. No bullets. |
| FRM-12 | `always` (CONSTANT) | State confidence and its limits: `high` only when tool evidence backs it. |
| FRM-13 | `answer has unanswered parts` | Give a concrete next step for what was not found — which authoritative source to check. |

**31 statements. 8 constants, 9 tier/profile, 14 dynamic.**

---

## What is missing today, ranked

| id | why it matters |
|---|---|
| **FRM-7** | **the sentence Ananth has read all night.** Needs `targeted_attempts` — shipped tonight |
| **CTL-3** | grades EFFORT; every existing criterion grades the ANSWER |
| **CTL-1** | the dissent. 440 decisions: 50 subtractions, 0 additions |
| **EVD-5 / STR-3 / STR-4** | blocked on the LLM seat's `closure` field |
| **FRM-3** | no ambiguity detector exists |
| **STR-5** | needs a source-class notion the governor does not model yet |

## The two rules that keep this honest

**Every dynamic statement needs a named consumer before it is written.** The
consumer is react, and the proof is the statement appearing in the rendered
prompt — asserted by a test, mutation-checked, or it proves nothing.

**Every statement that ships needs a gate that fails when it stops being sent.**
A rule that exists only as a row in this table is not a rule.
