# Chat turn latency — where the time actually goes

**Owner:** Chat Master (P2b telemetry)
**For:** Product Awareness — input to picking which modules get a latency spec
**Status:** measured live on dev, `mobius-chat-00969-b8j`, 2026-09-09
**Surface:** `/traces` (admin) · per-query "Module timing" in the chat diagnostics tab
**Raw:** `GET /chat/matrix/{correlation_id}` (per-turn, ungated)

## The headline

On a representative agentic turn (`7897f928`, "Florida Medicaid rates for CPT 90837",
3 ReAct rounds, 56.5s wall):

| Where | ms | % of wall |
|---|---:|---:|
| **Our own processing** | **1,514** | **2.7%** |
| Waiting on models (LLM) | 40,742 *purchased* | — |
| External tool (RAG over HTTP) | 30,368 | 54% |
| Database | 140 | 0.2% |

**Our code is 2.7% of the turn.** The other 97% is spent waiting on something
else — a model or the RAG service. Any latency programme that targets chat's own
modules first is optimising the 2.7%.

"Purchased" LLM time exceeds wall because three integrator calls run in parallel;
the wall column already accounts for the overlap.

## Per-process breakdown (same turn)

| Process | wall | processing | llm | db | tool |
|---|---:|---:|---:|---:|---:|
| `react_loop` | 44,275 | 100 | 0 | 0 | 0 |
| ├ round_2 | 37,123 | 13 | 0 | 0 | 30,100 |
| │ ├ `tool:healthcare_query` *(external)* | 30,100 | 0 | 0 | 0 | 30,100 |
| │ └ `_call_llm_json` | 7,011 | 199 | 6,812 | 0 | 0 |
| ├ round_3 | 4,845 | 111 | 0 | 0 | 0 |
| │ └ `_call_llm_json` | 4,734 | 145 | 4,589 | 0 | 0 |
| └ round_1 | 2,206 | 145 | 0 | 0 | 268 |
| `integrate` | 11,727 | **240** | 0 | 0 | 0 |
| ├ `llm:integrator_a` *(parallel)* | 11,486 | 0 | 11,486 | 0 | 0 |
| ├ `llm:integrator_critic` *(parallel)* | 8,253 | 0 | 8,253 | 0 | 0 |
| └ `llm:integrator_enrichment` *(parallel)* | 8,025 | 0 | 8,025 | 0 | 0 |
| `state_load` | 485 | 345 | 0 | 140 (5r/2w) | 0 |
| **TOTAL** | **56,486** | **1,514** | **40,742** | **140** | **30,368** |

## What to attack, in order

1. **`tool:healthcare_query` — 30,100ms, 54% of the turn.** One RAG call. Not
   chat's code; belongs to the RAG/Retriever seat. This is the single biggest
   item on the board by a wide margin.
2. **`llm:integrator_a` — 11,486ms.** Sets `integrate`'s floor: critic (8,253)
   and enrichment (8,025) run underneath it and cost nothing extra. Only
   integrator_a is worth attacking, and the lever is model routing / prompt size,
   not our code. Note it ran *well past* its 3,000ms `latency_budget_ms`, which is
   worth a look on its own — the budget is a router pre-filter on tracked EMA
   latency, so either the filter is not binding or the estimate is stale.
3. **ReAct `_call_llm_json` — 6,812 + 4,589 + 1,577ms of model time**, plus
   ~560ms of *our* non-model overhead across the three rounds. The overhead is
   real but small.
4. **`state_load` — 345ms processing, 140ms db (5 reads / 2 writes).** Warm.
   Cold-start turns reach 400–500ms; that is a connection-pool warmth problem,
   not a query problem. No read loop — I previously suspected one and retract it.

## Retractions — numbers I previously reported that were wrong

I am flagging these explicitly because they were used to reason about priorities.

- **"`integrate` is ~1,079ms/turn of PURE processing (no llm, no db) — the
  largest block of code-we-control."** Wrong. `integrate`'s job is to format the
  response *via the integrator LLM*, and those calls had no spans, so their wall
  fell into the processing column. Measured at 12,087ms of "our processing" on
  one turn; it is actually **240ms**. Fixed — the calls are now spanned.
- **"`state_load` has a read loop."** Wrong; it was an artefact of the smoke test
  reusing fixed correlation ids, so four runs counted as one turn (~14× inflation).
- **A 5,770ms RAG call reported as 5,770ms of our processing.** Same class: a
  `tool:*` span is a leaf with no llm/db counts, so the generic
  `wall − children − llm − db` formula charged its whole wall to us.

## The pattern underneath all three

**An un-instrumented external wait is indistinguishable from our own work.** Every
one of these was a *producer without a consumer* variant: the time was real, the
span was missing, and the fallback attribution was silently plausible. Nothing
failed loudly — the number just quietly named the wrong module, which is the
expensive kind of wrong, because it sends someone to optimise code that is idle.

Two structural guards now exist:
- `turn_spans.concurrent` (db/schema/064) — concurrent siblings contribute
  `max()`, not `sum()`, so parallel calls cannot drive a parent's processing to a
  clamped 0. A column, not a naming convention: a convention is a rule with no
  enforcement and breaks silently on the first rename.
- `turn_matrix()` takes injectable spans, so the shape rules are tested with no
  database. The previous version of this module shipped a broken write precisely
  because every test patched `db_execute`.

**Still un-instrumented** (they read as 0ms, which means "not measured", not
"free"): `feedback_signal`, `retrieval_budget`, `jurisdiction`, `personalization`,
and the sequential `format_response` path. If a latency spec targets any of these,
instrument first — do not trust a 0.

---

# Schema findings (sent to Product Awareness 2026-09-09)

## `credentialing_envelope` is stale — the only confirmed one
Verified against **git history, not grep**:
- Module deleted by P1d, commit `298830c` ("remove the credentialing/roster
  surface, 26,390 lines").
- Its documented core, `resolve_step3_roster_merge_context`, has **zero**
  remaining references.
- `scripts/platform/chat_node_content.py` still rates it **green, depth="code"**
  with a full behavioural description.

Named in `app/telemetry/spans.py:NODES_WITHOUT_CODE` so a reader comparing the
36-node schema to a live trace does not read "no span" as "fast". Their file is
not edited from here.

## `completion_extension_gate` — I was wrong, the schema was right
I nearly reported this as a second stale entry. It is live code: ~60 lines
**inline** in react_loop's main loop (completion critic + the `max_it += 1`
extension bump), with no module, class or function — exactly as the schema says.

My existence test was a search for a **symbol** of that name. A nameless block
has no symbol, so the empty result proved nothing and I read it as absence.

> **A name-based search answers "is there a symbol called X", never "does X
> happen."** Reserve absence claims for git history.

The false claim had already shipped in `NODES_WITHOUT_CODE`; corrected in the
same commit that instrumented the node.

This node was the most valuable one in the whole set to instrument, precisely
because it is nameless: with nothing to decorate its LLM call was charged to the
enclosing round's processing, *and* anyone searching for it concluded it was
absent. Two independent ways of being invisible, stacked.

## Coverage
35 of 36 schema nodes have code; 26 emit spans. Uninstrumented remainder is
infrastructure (`queue`, `worker`, `POST /chat`, `orchestrator`, `stages`) plus
`plan`/`resolve`, deleted by the refactor.
