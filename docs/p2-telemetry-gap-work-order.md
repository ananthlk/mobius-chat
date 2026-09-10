# P2 work order — the telemetry gap: 23 nodes with no structured signal

**Owner** Chat Master · **Ratifier** Eval + Tech Review · **Coordinator** Platform seat (mobius-f2)
**Opened** 2026-09-10 · Ananth: *"get the telemetry gap fixed across those 24 nodes"*

**It is 23, not 24** — `PHI gate` and `phi_gate` are the same node under two keys
(aliased in the catalogue so a correction lands once). Say 23 in any report.

---

## 1. What was measured, and what it means

`gen_readiness.py` counts two things **separately and never sums them**:

- **log sites** — `logger.<level>(`. Greppable by a human, after the fact, if they
  already suspect where to look.
- **telemetry sites** — `span/emit/record/track/observe`. A **structured** signal
  something can query, alert on, or join to a turn.

Across 31 live nodes: **224 log sites, 144 telemetry sites, and 23 nodes with
telemetry_sites == 0.** Those 23 are ~10,200 lines and carry **38 swallow handlers**.

**The distinction is the whole point of the phase.** A module with only log lines is
*diagnosable in hindsight by someone who already knows what went wrong*. It is not
*operable* — you cannot ask it a question, count its decisions, or notice it changed
behaviour. Every finding in this program came from the gap between those two.

---

## 2. 🔴 THE FAILURE MODE THIS ORDER EXISTS TO PREVENT

**Do not add emit calls to make a number go green.** A `record()` nobody reads is a
*producer without a consumer* — the exact defect this phase is named after. Adding 23
of them would raise the metric and change nothing, and it would do it in the phase
whose gate is "make absent producers detectable."

So the rule for this pass:

> **Instrument the DECISION the node makes, not the functions it has.**

If a node makes no decision worth recording, **say so and leave it alone.** A written
"nothing here decides anything, so nothing is instrumented" is a better outcome than a
span. I would rather this order close at 15 of 23 with 8 reasoned exemptions than at
23 of 23.

**Two nodes I expect to be exemptions** — check me, don't take it:
`stages` (11 lines) and `queue`'s `__init__` factory. Both may be pure wiring.

**And `queue` is already off the list on my side** — I had it mapped to
`app/queue/__init__.py`, a 19-line re-export, while the implementation next door
(`redis_queue.py`) has 11 log sites. Now measured across the package: 372 lines, 12
log sites, still **0 structured telemetry**, so it stays in scope — but on correct
evidence. I mention it because the same mis-mapping may hide elsewhere: **if a node's
LOC looks wrong for what it does, check the path before instrumenting it.**

---

## 3. The 23, with what I believe each one decides

Ordered by how much a missing signal costs us. **My guesses at the decision — argue
with them, you know the code.**

| node | loc | logs | swal | the decision I think it makes, unrecorded |
|---|---:|---:|---:|---|
| `model_registry` | 2462 | 21 | 10 | **which model, and why that one** — bandit arm choice, phase, circuit-breaker trips |
| `prompts` | 1368 | 4 | 1 | which blocks composed into the prompt actually sent |
| `critic` | 791 | 13 | 1 | ran / skipped, and the verdict — *the turns that most need auditing are the ones that skip it* |
| `tool_manifest` | 737 | **0** | 0 | which tools were offered. The Stage-0 funnel's missing stage |
| `POST /chat` | 714 | 5 | 4 | accepted / rejected / gated, before anything is queued |
| `llm_manager` | 469 | 1 | **8** | which config resolved, and 8 swallowed failures nobody sees |
| `message_resolver` | 425 | **0** | 0 | how the inbound message was interpreted |
| `worker` | 405 | 7 | 7 | pick-up, retry, abandon |
| `governor` | 382 | **0** | 1 | **extension granted or refused, and on what budget** — the known zero-logger node |
| `react_retry_guard` | 345 | **0** | 0 | which repeat call it refused — the guard that never reported firing |
| `capabilities` | 332 | **0** | 0 | which capabilities were declared to the model |
| `curator_tools` | 331 | 4 | 0 | **`keep` — which chunks the curator kept.** Never persisted. Filed months ago |
| `context` | 317 | **0** | 0 | what context was assembled, and what was dropped |
| `round0` | 198 | 1 | 0 | short-circuited or not |
| `parsing` | 167 | 2 | 2 | parse succeeded / recovered / failed — feeds `__unparseable_recovered__` |
| `jurisdiction` | 140 | **0** | 0 | which jurisdiction was determined. ABSENT coverage too |
| `personalization` | 131 | **0** | 0 | what was personalised |
| `retrieval_budget` | 118 | **0** | 0 | the budget set, and whether it bound |
| `PHI gate` | 106 | 5 | 2 | already writes an audit ROW — may be exempt on those grounds; your call |
| `feedback_signal` | 79 | **0** | 0 | the signal derived from feedback |
| `active_context` | 63 | **0** | 0 | writes two keys into the turn record — already a filed finding |
| `stages` | 11 | **0** | 0 | probably wiring — **expected exemption** |
| `queue`(init) | 19 | 0 | 0 | factory — **expected exemption** |

**Eight nodes emit nothing at all — no logs and no telemetry**: `tool_manifest`,
`message_resolver`, `governor`, `react_retry_guard`, `capabilities`, `context`,
`jurisdiction`, `personalization`, `retrieval_budget`, `feedback_signal`,
`active_context`. Those are invisible in both senses and are where I would start.

---

## 4. How to instrument, so the signal has a consumer

The convention already exists from P2b — **use it, do not invent a second one.**

1. **Bind the span to the schema node key** (`state_load`, `governor`, …), exactly as
   P2b did. Ananth's rule: *"the span should map to our schema in some regards, else
   what is the point of those modules."* A span keyed to a filename cannot be joined
   to the node it describes.
2. **Record the decision and its INPUTS.** The assertability rule from this program:
   *a decision is assertable iff the inputs it consumed are persisted alongside the
   outcome.* "governor granted an extension" is not assertable; "granted, rounds_used
   2, budget 4, soft_target 12s" is.
3. **Every count carries its target** (Eval's ruling). A number with no expected value
   cannot be read as good or bad.
4. **Instrument the swallows.** 38 handlers across these nodes log-and-continue. A
   swallow that records nothing structured is a failure the caller cannot see — and
   `llm_manager` alone has 8. These are the highest-value sites in the list.
5. **Prove the consumer exists in the same change.** After deploying, one query must
   return rows for the new signal. A span written and never queried is the defect,
   not the fix. `gen_readiness.py`'s `telemetry_sites` count is *not* that proof — it
   counts call sites, not rows.

---

## 5. Definition of done

- [ ] every node either **instrumented** or **exempted with a written reason**
- [ ] spans bound to schema node keys, joinable to a turn
- [ ] the 38 swallow handlers in scope each record something structured
- [ ] **one query per new signal returning real rows** on dev, post-deploy
- [ ] **and the field can take MORE THAN ONE VALUE.** Chat Master's addition,
      2026-09-10, from a live example: `llm_calls.is_fallback` exists, is read at
      `orchestrator.py:174`, and is `false` on **all 1,988 calls in 24h** because
      nothing ever writes `True`. It passes "producer exists", "consumer exists" AND
      "query returns rows" while carrying no information. **A distinct-value count of
      1 over a real window is the tell** — the same shape as a legal enum value with
      0–1 rows, generalised from enums to booleans. Report distinct values per new
      signal, not just row counts.
- [ ] counts carry targets
- [ ] suite clean against `docs/chat-test-baseline.json` — may shrink, never grow
- [ ] a **contract tag** where a guarantee is now enforceable, mutation-demonstrated
      per the ledger rule
- [ ] findings updated in `chat_node_content.py`; I re-run `refresh.sh` and the
      `observability` dimension moves on measured evidence
- [ ] deployed to dev, **verified by image digest**

**Do not do this in one commit.** One node per commit, or one cluster, so a
regression is attributable — the P2 gate now reads *attributed, not merely timed*,
which is Chat Master's own amendment and applies here first.
