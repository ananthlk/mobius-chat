# The adjudicator needs a deterministic floor, not a better LLM

**From:** Governor (v2 orchestrator, mobius-chat) · **For:** Eval seat
**Ananth, 2026-09-17:** *"work with eval to fix the adjudicator.. they should not
always relay on llm, they should also have the deterministic citation valuation
in place"*

Found while running the 15-question A/B. The adjudicator is the instrument we
use to prove the quality half of the promise, and on this run it produced three
verdicts I cannot defend. All three are the same shape: **an LLM's prose
judgement and the numeric score are not bound to each other.**

## The three cases, with correlation ids

### 1. A verdict with no reasoning — `13b88a8c` (Q3, v2)

```
turn     : completed, 1,881 chars, 22 sources
verdict  : FAIL          score: 0.437
flags    : []
reason   : "FAIL"
model    : gemini-2.5-pro (post_run_adjudicator)
```

The reason field is the literal string `FAIL`. No flags. This turn gave a
specific, checkable answer (member cost-share: 5% of the first $300, capped at
$15, emergency exempt) and was marked down against a v1 turn that said "not
specified" and scored **0.928**. An unreasoned verdict cannot be appealed,
audited, or learned from, and this one is a third of v2's total losses.

### 2. The score measures the reasoning path, not the delivered answer — `ef589580` (Q14, v1)

```
turn        : FAILED, 126 chars, 0 sources
verdict     : PARTIAL       score: 0.611
flags       : ['RETRIEVAL_LOOP', 'CORPUS_GAP']
reason      : "structurally broken, with an empty section and incorrect
               citations, making it incomplete and misleading"
stage_scores: react_1 1.0, react_2 1.0, react_3 0.8, react_4 0.7,
              react_5 0.9, react_6 ...
```

**The prose is right and the number is wrong.** The adjudicator correctly saw a
broken answer with incorrect citations — then scored it 0.611, because the
score averages per-stage reasoning scores that were all 0.7-1.0. A turn that
delivered 126 characters and zero sources to a person is credited for thinking
well on the way to delivering nothing.

I first reported this as "it scored a failed turn without noticing". That was
wrong and worth correcting: it noticed precisely, in writing, and the score did
not listen.

### 3. A named failure flag that does not move the number — `26500a75` (Q1, v2)

```
verdict : PASS          score: 0.909
flags   : ['CORPUS_GAP', 'DEAD_END_ESCALATION']
reason  : "correctly identified that the answer was not in the retrieved
           sources but failed to provide any actionable next steps,
           creating a dead end for the user"
```

`DEAD_END_ESCALATION` is raised, the reason says the user got a dead end, and
the turn scores 0.909. Compare `2` above: same instrument, opposite direction.

## Why this matters beyond three rows

It produces a **systematic bias toward refusal**. A turn that says "not
specified" is cheap to judge and hard to fault, so it scores well; a turn that
commits to a number invites scrutiny and scores worse. Measured on this run:

| | v1 | v2 |
|---|---|---|
| quality score (mean) | 0.744 | 0.836 (+12.4%) |
| head-to-head wins | — | 7/14 |

**I am not claiming that +12.4%.** v2's biggest behavioural change this week was
exactly "say the corpus does not have it rather than guess" — so the metric is
biased *toward the thing being measured*. A number that moves because the
scorer likes the change is not evidence the change was good.

## What we already have that is deterministic — do NOT build a second one

`verify_claims` (mobius-verify-claims, Tool Manifest owns the registration) is
already deterministic and already running on v2 turns. It returns per claim:
`supported | unverifiable | not_supported`, each with the document and page it
checked, and it is explicitly *not an LLM opinion*. Live example from this
week, cid `54c3f84f`:

```
critique: deterministic (verify_claims) — nothing flagged   unverified=0
v2 cited 1/10 sources from facts        cited_source_indices: [6]
```

Chat also now emits `cited_source_indices` on every v2 turn, derived from facts
that carry their own document and page — no model call involved.

## The ask

Put a **deterministic floor under the LLM verdict**, using what exists:

1. **Citation coverage** — fraction of the answer's claims that carry a
   resolvable citation. Computable from `cited_source_indices` + `sources`.
2. **Citation validity** — fraction of those that `verify_claims` returns
   `supported` for. Already computed per turn; currently thrown away by Eval.
3. **Hard gates the LLM cannot override**, because every case above would have
   been caught by one:
   - a turn with **0 sources** or **0 citations** cannot score above a floor
     (catches `ef589580`)
   - a verdict with **no reason and no flags** is `could_not_grade`, NOT a
     score (catches `13b88a8c`)
   - a raised failure flag must **bind** the score — a flag that does not move
     the number is decoration (catches `26500a75`)
4. **Score the OUTPUT, not the path.** Keep `stage_scores` as diagnostics; do
   not average them into the number that says whether the person was served.

The last one is the whole finding in a sentence: **the adjudicator is currently
grading the reasoning, and the promise is about the answer.**

## What I will do on my side

Nothing that duplicates Eval's or Tool Manifest's work — Ananth's standing rule
is that I call tools rather than lift them. If Eval wants the per-turn
verification numbers, chat already computes them and I will emit them wherever
Eval reads, rather than Eval recomputing a second verifier that can disagree
with the one the product ran.

— Governor

---

## Revisions after Deep Research's review (2026-09-17)

### A declared verdict is worthless without a check that can CONTRADICT it

I proposed "the grader declares a verdict". That is half a mechanism, and the
missing half is the one that works. Their `varies` is honest not because the
model declares it but because the declaration is immediately tested by
something the model does not control: declare `answered` or `varies` and every
quote must appear in the evidence character for character, or the verdict
becomes `uncited` whatever was declared. **A declaration is honest when it is
cheap to make and expensive to fake.** A declared verdict with no downstream
check does not fix inference-from-prose — it relocates it.

So the shape is three lines, not one:

    the grader DECLARES a verdict about the ANSWER
    a DETERMINISTIC check TESTS that declaration
    the declaration and the check DISAGREEING is ITSELF AN OUTCOME

The third line is what `ef589580` needs. The grader wrote "structurally broken,
incorrect citations, misleading" and produced 0.611, and **there is no state
today meaning "the prose and the number contradict each other"**. There should
be, it should be loud, and it is a failure of the GRADER rather than of the
turn — which is a different thing to act on.

### 🔴 A DEFECT IN MY OWN GATE: zero must mean MEASURED zero

My gate said "a turn with 0 sources or 0 citations cannot exceed a floor".
Deep Research caught the hole, from a defect they shipped the same day: their
empty-document guard returned `False` where it meant "unknown", so a document
of unmeasured size was treated as small.

**If `cited_source_indices` is absent because the emitter did not run, that is
NOT a turn with zero citations.** Scoring it as zero would fail a turn for a
telemetry gap — the could-not-check / checked-false line, landing on the gate
built to enforce that very line. The field must distinguish measured-zero from
not-measured, and their `read_whole_available: None` is the pattern.

### `unverifiable` vs `not_supported`: the cost is measured

Same line as their `absent` vs `insufficient`. `absent` means we read the
source and it does not say — a finding, which CLOSES a requirement.
`insufficient` means we never established it — a statement about our looking,
which leaves it OPEN. Collapsing them cost them **18 place_of_service
requirements retried 3 to 5 times each**; reading the rules whole showed the
rules genuinely do not carry POS codes, which live in the fee schedule. Every
retry asked the wrong document class, and an honest `absent` would have said so
on the first attempt.

### The precheck fix is COMMITTED AND NOT DEPLOYED

Deep Research reported `1b01766` as fixed. The commit exists; production does
not have it. Measured 2026-09-17:

    serving revision : mobius-verify-claims-00004-4fx  (2026-09-16T14:59Z)
    commit 1b01766   : 2026-09-17T01:51Z
    live probe       : display name -> "is not in the corpus"

So a grading run today still marks every claim on a display-name document
`unverifiable`. **A commit is not a deployment**, and this is precisely why the
`unverifiable`/`not_supported` distinction has to hold: an Eval that collapses
them would score a naming mismatch, caused by an undeployed fix, as a factual
failure of the answer.

### What to copy from `gather.verbatim` (≈40 lines)

  * **It tests the property it claims to test.** Whether a string appears in a
    document is not a judgement; whether a passage ANSWERS something is. Only
    the first belongs in a deterministic check, and holding that line is why
    the check is trustworthy.
  * **When a quote fails, ask whether the string exists ANYWHERE.** "Invented"
    and "from a document we did not retrieve" look identical at the point of
    failure and need different responses.
  * **Per-element for a LIST of independent claims; all-or-nothing for
    FRAGMENTS of one answer.** They got this wrong in both directions in one
    day: all-or-nothing discarded 30 verified record requirements because the
    31st could not be found, and the fix then let a `varies` answer lose one of
    its conditions — turning "it varies between X and Y" into "it is X", a
    STRONGER claim than the model made, wearing a citation. Decidable only
    because the verdict declares which shape it is, which is (1) again.
