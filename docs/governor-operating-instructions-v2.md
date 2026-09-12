# Governor — operating instructions (v2)

*Owner: Governor seat. Written 2026-09-12 against what is running, not what was
planned. Every number here is measured; where it is not, it says so.*

## What the governor decides

| Decision | Owned by | Where |
|---|---|---|
| Which posture this round takes | governor | `v2/posture.py` |
| Whether the round is affordable | governor | `posture.decide(..., affordable=)` |
| Which gap this round targets | governor | `posture.select()` |
| Which roles the prompt carries | governor | `v2/blocks.py` |
| Which tools preload before react speaks | governor + Tool Manifest | `v2/preload.py` |
| Whether to enrich after the answer | governor | `v2/enrich.should_enrich()` |
| Whether a critic verdict buys another round | governor | `v2/enrich.should_reopen()` |
| What the turn remembers, and for how long | governor | `v2/memory.py` |

## What the governor must never decide

These are not style preferences. Each one has cost a live defect.

- **What is true.** The governor never grades evidence. `assemble()` checks
  whether a claim is *grounded*, never whether it is *right*.
- **How the question decomposes.** react names the gaps; rag fans out. When I
  re-derived parts by parsing the question, I became a second author of a
  decomposition the system had already made. Ananth, on an earlier attempt:
  *"dont break it RAG already does that."*
- **Which facts matter.** The memory manager decides which facts *survive*, in
  arrival order. A memory that ranks facts by importance is a second author of
  the answer.
- **The wording of another seat's section.** `react/prompts.py`'s
  `response_shape` is the LLM seat's. v2 adds §11 *alongside* it and never
  rewrites it.

## Order of operations, per turn

```
1  memory.recall          what we already know — emitted FIRST, it is the
                          cheapest evidence there is
2  preload.plan/execute   tools BEFORE react speaks; rag always
3  fair_share             per fan-out arm, never a global top-K
4  frame.render           §5 gaps · §6 roles · §8 complete · §9 tools
                          · §10 evidence · §11 v2 response shape
5  react                  one call
6  contract.parse         facts, gaps, acks → react_v2_rounds (append-only)
7  memory.remember        grounded facts + rejections → thread_evidence
8  [repeat 2-7 while a gap is open AND affordable]
9  _finalize_response     → _v2_integrate: assemble + critic ∥ next steps
```

## Standing rules (each one paid for)

**BUDGET is not "out of money."** It is the fall-through label for *gaps
remain*. Treating it as terminal cost 50 turns with 0 additions.

**Could-not-check is not checked-false.** UNOBSERVABLE is a first-class
verdict. Eight instances of that collapse in one session.

**Never-searched and searched-empty are different facts** and carry opposite
advice. Say which, always — a/b/c.

**Emit the decision and its inputs, never just the outcome.** And say what did
*not* happen: a silent skip and a step that ran and found nothing produce the
same silence.

**A headline must stand alone.** If a reader must expand it to know whether
something is wrong, it is a label.

**Degrade, never fabricate.** A failed critic is recorded as failed. An
invented critique reads as a check that happened.

**Every ack needs a checker.** An ack with no consumer is a producer with no
consumer wearing a schema.

**Mutation-check every gate**, and strip comments before matching source — six
gates in one day passed by matching their own prose.

## Measured numbers the governor budgets against

Production, 60 days, 3,342 turns (`scripts/bench_round_cost.py`):

| rounds | turns | input tokens | LLM latency | $/turn |
|---|---|---|---|---|
| 1 | 1,124 | 14,863 | 2.6s | 0.00174 |
| 2 | 908 | 36,117 | 7.3s | 0.00568 |
| 3 | 697 | 57,133 | 12.6s | 0.01260 |
| 7 | 77 | 223,133 | 56.0s | 0.13224 |

**A second round costs 3.26× in dollars and 2.82× in latency.** It is
superlinear because every round re-carries the accumulated context. 66.2% of
turns take 2+ rounds today — that is preload's addressable population.

Corollary the governor must not forget: **prompt size is not the latency
lever.** Trimming the round-1 prompt 59% moved wall clock by less than
run-to-run noise. Rounds are the cost.

## Open dependencies

- **`facts[]` is asked for and not returned.** §11 provably reaches the model;
  react still replies `shape=v1`. Until resolved with the LLM seat, the thread
  ledger stays empty and the integrator's critic has no compressed evidence.
- **`delivered_quality` is NULL on every row.** The objective function's
  quality term has no writer. The critic's verdict is the candidate.
- **Nothing above is deployed.** Dev revision `01069` predates all of it.
