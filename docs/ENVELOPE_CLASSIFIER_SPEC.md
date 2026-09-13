# Envelope Classifier Spec

**Status:** Phase 1 shipped · Phase 2 superseded · upstream proposer mounted
**Written:** 2026-09-12 · **Rewritten against the build:** 2026-09-13
**Area:** mobius-chat — answer card presentation
**Author:** Ananth · **Seats involved:** UX formatter (this), Governor (orchestrator v2)

> **This document was rewritten to match what exists.** The original was a
> forward-looking PRD; by the end of its first build day it described a system
> that no longer existed. A spec behind its build is worse than no spec — it
> is a confident description of something gone, and the next reader has no way
> to tell which half is real. Everything below is either in the tree or
> explicitly marked as not built. §9 records what changed and why.

---

## Problem statement

The answer card picked its presentation envelope (`table`, `stats`, `bars`,
`steps`, `conditions`, `bullets`) through **three independent implementations
that did not share a rulebook**: the regex fast path, natural-language shape
rules inside the enricher prompt, and the tool-hint builder. They disagreed on
their thresholds, so identical content rendered differently depending on which
path served the turn. Users read that as random formatting; it was a
reproducible fork.

That is fixed. What the work uncovered is a larger problem the spec did not
anticipate, and §8 is now the more important half of this document.

---

## 1. What shipped

| Module | Lines | Job |
|---|---|---|
| `app/responder/envelope_classifier.py` | 746 | The ladder, gates, budget, trace. One pure function decides every envelope. |
| `app/responder/deterministic_format.py` | 611 | Extraction only — prose → typed payload. Decides nothing. |
| `app/responder/v2_adapter.py` | 281 | v2's grounding verdict → the formatter's budget. |
| `app/responder/answer_template.py` | 349 | A suggested answer shape, computed **before** react writes. |
| `mobius_contracts/taxonomies/envelope_thresholds.py` | — | Every threshold, once. |
| `scripts/gen_envelope_thresholds.py` | — | Generates the frontend mirror; `--check` fails CI on drift. |
| `app/pipeline/v2/blocks.py` | — | `Slot.ANSWER_SHAPE = 42`, `owner="ux"` (Governor's file, their ruling). |

**209 tests** across five suites. Repo total 3,525 passing; 18 pre-existing
failures unrelated to this work (logging/tracing config and LOC ratchets),
verified failing identically with these changes stashed.

---

## 2. The ladder, as built

First match wins. **Rule ids are stable strings, not indexes** — inserting a
rule must never renumber the others, because `rule_id` is what the decision
trace is queried by.

| `rule_id` | Predicate | Envelope |
|---|---|---|
| `intent.explicit` | The user named a format in their message | that format |
| `hint.typed` | A tool supplied a typed `section_hint` | the hint's — never re-classified |
| `shape.table` | A parsed pipe table, or records sharing keys | `table` |
| `shape.chart` | Ordinal axis + numeric values | `chart` — **flag off, no renderer** |
| `shape.bars` | Items carry a `weight` | `bars` |
| `shape.conditions` | Items carry `condition` + `result` | `conditions` |
| `shape.bullets` | Explicit list, ≥3 items, **avg ≤25 words** | `bullets` |
| `abstain.prose_list` | Explicit list whose items are paragraphs | — prose |
| `shape.steps` | Ordered items, ≥2 | `steps` |
| `shape.stats` | ≤4 pairs, values ≤30 chars | `stats` |
| `shape.table.pairs` | Pairs that overflow the stats caps | `table` |
| `abstain.no_match` | Nothing matched | — prose |

Gates run before the ladder and return an abstain:
`abstain.raw_excerpt` · `abstain.thin_evidence` · `abstain.single_fact`.

**Three orderings are load-bearing and were not free choices:**

1. **bullets > steps > stats** preserves the fast path's existing precedence,
   pinned by three named tests in `test_deterministic_format.py`. The
   rationale is confidence, not structural specificity: a draft with a real
   3-item list and a stray `"Deadline: 180 days"` line is a list whose prose
   contains a colon, not a stats card. *The draft spec had stats above
   bullets. That was wrong against the existing tests.*

2. **bars and conditions above bullets**, because `weight` and
   `condition`/`result` are unambiguous typed signals prose extraction never
   produces. No prose payload can reach them, so their position cannot change
   fast-path behaviour — but a typed payload carrying weights must not fall
   through to bullets.

3. **`abstain.prose_list` sits inside the bullets rule, not after it**, so the
   trace distinguishes "there was no structure here" from "the structure was
   too heavy to bullet". Only the second says the **answer** should change
   rather than the formatter.

---

## 3. Abstain is a verdict, and the reasons are distinguishable

Governor's contract rule — *"empty is not absent"* — applied to this surface.
All four abstains once rendered identically (prose, no card, nothing said),
with the `rule_id` in the trace and invisible in the product. *"We declined to
format this because nothing grounds it"* and *"this content had no structure"*
are opposite statements to a reader.

The card now carries a `presentation` key (additive; a frontend that ignores
it is unaffected) with the `rule_id`, the reason, and user-facing wording
where there is any worth saying. `abstain.no_match` and `abstain.single_fact`
deliberately carry none — "there was nothing to format" is not worth telling
anyone.

**The visual treatment is the frontend's.** `abstain.thin_evidence` is this
surface's version of coverage's `unobservable` and should reuse whatever grey
that gets, rather than inventing a second visual language for one state.

---

## 4. Thresholds: one source, CI-enforced

Every bound lives in `mobius_contracts`. The frontend consumes a generated
mirror; `test_envelope_threshold_drift.py` fails if they diverge, and a
further test fails if a bare numeric literal reappears in the classifier.

| Constant | Value | Why |
|---|---|---|
| `STATS_MAX_ITEMS` | 4 | The frontend renders four tiles |
| `STATS_MAX_VALUE_CHARS` | 30 | A tile is a value, not a sentence |
| `BULLETS_MIN_ITEMS` | 3 | Fewer could be a stray dash |
| `BULLETS_MAX_AVG_WORDS` | 25 | **react's own cap**, see §8 |
| `STEPS_MIN_ITEMS` | 2 | Two ordered items is a procedure |
| `PAIRS_MIN` / `MAX` | 2 / 6 | Below: coincidence. Above: regex over-matching prose |
| `MAX_RICH_BLOCKS_PER_TURN` | 2 | Judged on a rendered long answer |

This caught a real drift on day one: `bubble.ts` capped stat tiles at a
hardcoded `slice(0, 4)` while the backend had `4` in a comment. Neither knew
about the other.

**`BULLETS_MAX_AVG_WORDS` is not our number.** It was set from first
principles about scannability and only afterwards found to equal react's own
stated cap in `REACT_FORMAT_RULES_TEXT`. The guard is therefore *enforcement
of an existing contract*, not a new opinion — which is a much stronger claim,
and checkable against a file rather than against taste.

---

## 5. Multi-section and the `direct_answer` split

**Not in the original spec.** Added after measuring shape loss on long drafts:
multi-shape answers lost **2 of 3 shapes** — a numbered submission procedure
and a contact block, both explicitly structured by the author, kept only as
prose. The dominant failure mode was **under**-formatting, not the
over-formatting these systems are usually accused of.

`segment_draft()` walks a draft into contiguous structural blocks and
classifies each; `MULTI_SECTION_ENABLED` defaults **on**. The render budget is
what makes that safe — blocks past the cap degrade to bullets rather than
stacking cards.

Two bugs surfaced here, both worth recording:

**Content loss.** `prose` was computed from what segmentation *consumed*
rather than what actually *rendered*, so a block that classified and then
declined a card had its lines stripped from the answer **and** no section to
appear in. A table plus a long-bullet block dropped three paragraphs entirely.
`Segmentation` now carries the draft's lines and each block's span;
`prose_excluding(rendered_blocks)` subtracts only what rendered. **A block
that abstained was declined a card, not deleted.**

**Greedy pair runs.** The label:value regex is the loosest of the four —
`"Step 1: Gather documentation"` satisfies it as readily as
`"Deadline: 180 days"` — so a run started on a genuine pair swallowed the
procedure that followed. Checking patterns in order at the top of the loop is
not enough; the greed happens *inside* the run.

`direct_answer` on a segmented turn is the prose *between* the blocks, so
facts appear once. Three cases, and the third keeps it safe: no sections →
full draft; sections + surviving prose → the prose; **sections + nothing
surviving → full draft**, because `direct_answer` is the streamed anchor and
blanking it leaves the user watching an empty bubble until the card lands.

---

## 6. The v2 seam

`ctx.v2_integration` is available at responder time (`orchestrator.py` runs
`react_loop` then `integrate` on a shared `ctx`; `react_loop.py:4125` sets it
during finalize). `budget_from_v2()` feeds `is_thin_evidence` from the
integrator's verdict, so the formatter and the integrity surface cannot
disagree about whether a turn was grounded. The local heuristic survives only
for turns v2 never touched.

**The correction that matters.** The first wiring read "nothing grounded" from
coverage and suppressed formatting — which, on today's traffic (react still
returning v1 shape, `facts` empty, every part `unobservable`), **stripped
every table, stat tile and step list from every card**. Measured, not
predicted; it did not ship.

That is could-not-check rendered as checked-false — the same defect Governor
was simultaneously fixing in their critic, which given no facts marked
correctly-grounded claims `unsupported`. Theirs on the critic, ours on the
formatter.

`Grounding` now separates **conclusive** from **inconclusive**:

- `DECISIVE_STATUSES = {supported, partial, unsupported}`. `unobservable` is
  excluded — it is an admission, not a verdict. `not_attempted` is excluded —
  nobody looked, which is a fact about us, not about the part.
- An empty `critique` with `ran == "ok"` does **not** count as the critic
  having run.
- `thin` requires conclusive **and** nothing grounded **and** no citations.

Consequence: `thin` rarely fires today. That is the deliberate failure
direction — formatting an ungrounded answer is a smaller harm than silently
stripping structure from a grounded one. When the critic is reliable the gate
starts working with no change here.

---

## 7. What is NOT built

- **`chart`** — ladder slot reserved, `CHART_ENABLED = False`, no renderer.
- **`pages_per_entity`** — wired as `()` in `facts_from`; per-arm retrieval
  depth is not on `ctx`, so the template block drops its two evidence lines.
- **R10, the planner intent prior** — `IntentSignals.question_intent` exists
  and is unread.
- **`ENVELOPE_CONTRACT`** — a shared declaration of the format vocabulary and
  required `data` keys per format, so the classifier cannot emit what the
  frontend cannot render. Proposed to the UX seat; **no reply**. Deliberately
  not built: freezing a vocabulary before the renderer owner agrees is how you
  get a contract everyone works around.
- **Envelope CSS on tokens** — `.ac-fmt-bar-fill` uses `var(--mobius-violet)`
  but `.ac-fmt-stat-tile` hardcodes `rgba(0,0,0,0.08)` and `.ac-fmt-stat-value`
  uses `--sidebar-text`. Against `STYLE_GUIDELINES.md`, and likely why these
  read wrong in dark theme. Owned by Chat front end, not UX — `mobius-design`
  owns only the tokens.

---

## 8. The ceiling: the formatter cannot fix the answer

**This is the most important thing learned and it was not in the original
spec.**

A deterministic formatter has exactly three moves: **detect** structure the
model already emitted, **rearrange** what is already delimited, and **refuse**.
Turning three paragraphs into a comparison is none of them. It is writing.

Found on a live three-payer question. `REACT_FORMAT_RULES_TEXT`
(`react/prompts.py:447`) hard-codes one answer shape — a bold line plus "2–4
short bullet points (each 10–25 words)" — and **never mentions a table**. So a
three-entity comparison is *structurally unable* to come back as one. The
shipped answer had bullets of 61, 64 and 69 words against react's own 25-word
cap, forced into a list because no instruction permitted anything else.

That is not the model choosing badly. It is the model having no option to
choose. And the quality audit passed it as "clear, well-grounded" — it was
well-grounded, and it never answered the comparison that was asked. **The
audit cannot see framing.**

### The move: determinism goes upstream

Everything needed to know the answer *should* be a comparison exists before a
word is written. The planner already splits "compare X, Y and Z" into one
sub-question per entity — a count, not a judgement.

`answer_template.py` computes a **suggested** shape from that and from
retrieval depth, and `Slot.ANSWER_SHAPE = 42` renders it into react's prompt
on the communicating round. Pure: `re`, `dataclasses`, two constants modules.
Fifty calls and a fresh interpreter produce one output hash.

**It suggests; it does not enforce.** The block says so in its own text —
react has read the evidence and this has not, so evidence that contradicts the
shape wins. If react ignores it, `abstain.prose_list` catches the violation
downstream and names it in the trace rather than rendering it. **Suggestion
upstream, detection downstream, the same threshold at both ends.**

### Honest about the model

The 28 English strings in that module were written by an LLM, once, offline.
The model moved from the loop to the authoring step — not eliminated,
relocated. Per-turn authoring gives 47 near-identical variants whose
differences are invisible, unattributable, and free to drift; authoring once
gives byte-identical guidance in a file that can be read, diffed and rejected.

The decision to *emit* each string is computed, and every number in them was
measured: the 25-word cap is react's own, "evidence is uneven" is
`max ≥ 2 × min` distinct pages, "split into 3 parts" is a count.

**One design opinion remains**, and it is one line: that a table beats bullets
for a comparison. Governor declined to overrule it.

---

## 9. What changed since the draft, and why

| Draft said | Built instead | Why |
|---|---|---|
| Ladder with stats above bullets | bullets > steps > stats | Three named existing tests pin it; the draft was wrong |
| No `shape.table.pairs` | Added | Five short pairs had nowhere to go |
| Single section per turn | Multi-section, default on | Long drafts lost 2 of 3 shapes |
| Pairs capped at 6, always | Ceiling lifts when contiguous | Nine genuine deadlines in a run abstained to prose. Count was standing in for evidence it does not carry; contiguity is the real signal |
| `direct_answer` untouched | Split to inter-block prose | Every fact rendered twice once sections multiplied |
| No bullet length rule | `BULLETS_MAX_AVG_WORDS` | 61/64/69-word bullets shipped |
| **R4: strip `format` from the enricher prompt** | **Superseded** | Governor is *deleting* the enricher, not fixing it. v2 replaces it with deterministic `assemble` + a critic now off the blocking path |
| Formatter-only scope | Upstream template proposer | §8 |

### Decisions of Governor's that shaped this

Recorded at their request.

- **The enricher is being deleted.** R4 as written is moot. Worth stating
  plainly because the original spec's Phase 2 reads as live work and is not.
- **The answer body remains prose.** `facts[]` carries statements with
  provenance, not shape, so `segment_draft` is the permanent path for content
  envelopes rather than the bridge it was built as.
- **`unobservable` is a first-class verdict**, deliberately not collapsed into
  a negative. §6 exists because of that ruling.
- **`_communicating(f)`, not `f.finalising`.** The extended answer keys on
  `role_communicate` rendering; the `exact_tool` posture fires it on round 1
  with no finalising round at all. Written as a shared helper so two seats
  read one definition.
- **`owner="ux"` on the answer_shape block**, and their allowed-owner test
  widened to accept it: a block whose sentences another seat wrote should not
  carry governor's name.
- **Delete the fallback parser rather than deprecate it** — "a fallback
  exercised only when the good path fails, and known buggy, makes the failure
  worse and hides it." Replaced with set difference over the planner's own
  sub-questions, which has no heuristics to be wrong about. Every defect class
  the surface parser had is now structurally unreachable.

---

## 10. Next

1. **Wire `pages_per_entity`** — the only unwired input on the template block.
2. **`ENVELOPE_CONTRACT`** — blocked on the UX seat replying.
3. **`abstain.prose_list` as a metric, not a log line.** Every firing is a turn
   where the upstream under-structured. That count is the input to fixing the
   prompt, and the missing input to a quality audit that currently cannot see
   framing.
4. **Re-verify against real v2 envelopes** when react returns v2 shape. The
   §6 regression was found by running against the shape Governor *described*
   rather than the one assumed — worth repeating with real bytes.
