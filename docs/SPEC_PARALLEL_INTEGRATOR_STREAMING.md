# Parallel Integrator: Factory-Model Prompts, Latency Budget, and Progressive Streaming

2026-08-08. Ananth's directives (relayed via Chat Master), this doc covers all three:

1. Parallel prompts (core/critic/enrichment) must consume the factory-model's
   enriched ctx (`reasoning_ledger`/`tool_outputs`/`rag_chunks`) — not the retired
   `source_texts` field — same contract the sequential integrator already uses.
2. Get Call A's ~12.5s wall-clock down to 1-3s. Model choice is one lever, not the
   only one (Ananth, direct) — see "Latency approach" below for why this spec uses
   a latency budget + tighter max_tokens instead of a hardcoded model pin.
3. Stream each of the 3 calls' contributions to the client as soon as THAT call
   completes, not after all three finish — Ananth: "I want these tabs to be
   streaming as completed so that we can make the feeling that this real
   progress." The current code already runs the 3 calls concurrently but blocks
   on `as_completed()` yielding all three before parsing/emitting anything, so the
   user sees nothing until the SLOWEST call lands — no better perceived latency
   than sequential, despite the wall-clock win.

## 1. Prompt refresh

`chat_config.py`'s `integrator_parallel_{core,critic,enrichment}_system` are
hardcoded strings, separate from the DB-composition prompts the sequential path
uses (`module.enricher` / `enricher.answercard_schema_and_rules`, currently v8/v14).
They were last touched before today's factory-model work and still:

- Reference `source_texts` throughout (citations rule, field rules) — that field
  was renamed to `rag_chunks` in `final.py::_build_consolidator_input_json` this
  morning (Task #58). `format_response_parallel` calls the SAME builder, so the
  parallel prompts were instructing the model to look for a field that no longer
  exists in its own input JSON.
- Describe FACTUAL/BLENDED as two distinct modes with separate section-count
  rules — predates the FACTUAL/BLENDED merge (commit 4d773a6).
- Have none of today's other fixes: no grounding-floor instruction, no
  hedge-mirroring, no appeals-table-format handling, no "every field required"
  requirement, no RECITAL bleed-hardening.

Fix: rewrite all three to the same "formatter, not synthesizer" contract as
`module.enricher` v8 — read `reasoning_ledger` (react's per-round
learned/running_answer/gaps_closed/gaps_open) as the primary content source,
`rag_chunks` for citation text, `tool_outputs` for typed non-rag content. Each
prompt keeps its own narrow field scope (Call A: card structure; Call B: citations
+ takeaways + gaps; Call C: next_steps + suggested_actions + next_questions), but
now correctly named against the real input shape.

## 2. Latency approach

Ananth's steer: model choice is one lever, not the only one. Two changes, applied
together, both already-built mechanisms — no new infrastructure:

**a) `latency_budget_ms` hard pre-filter** (`ModelRouter.select()`, built earlier
this session, previously unused pending this exact decision). Trims model
candidates to those whose tracked `ema_latency_ms` fits the budget before the
Thompson draw runs; falls back to "closest thing available" rather than hard-
failing if nothing fits. Preferred over hardcoding a model name: stays inside the
existing quality/circuit-breaker system, and self-corrects if the fast model of
today degrades or a faster one becomes available — a hardcoded pin does neither.
Values: Call A 3000ms, Call B 2000ms, Call C 1500ms (loosely tracking each call's
existing relative share of the 12.5/5.5/3.0s split, compressed toward the 1-3s
target).

**b) Tighter `max_tokens`.** Call A's 4096-token budget was sized for the OLD
"full synthesis from raw sources" job. Under the factory model, react already did
the reasoning — Call A is formatting a pre-structured answer, not generating one
from scratch, so its real output should be substantially smaller. Reduced to
2048. B/C stay at their existing 1024/512 (already narrow-scope, no evidence they
need to shrink further).

These compound: a smaller max_tokens reduces generation time on ANY model: Pro,
Flash, or whatever the latency-budget filter picks. Model choice alone (Option A,
hardcoding Flash) was considered and set aside — see the "model change is one not
the only option" note above.

## 3. Progressive streaming

New progress-event function, same shape/pattern as `append_draft_answer` /
`append_detail_answer` (`app/storage/progress.py`):

```python
def append_integrator_partial(correlation_id: str, part: str, data: dict) -> None:
    """part: "core" | "citations" | "enrichment" """
```

Fires event `integrator_partial` with `{"part": ..., "data": {...}}`. One event
type, `part`-discriminated, rather than three distinct event names — simpler for
Chat FE to handle with one listener + switch.

Per-part payload:
- `core` (Call A): `mode`, `direct_answer`, `sections`, `thread_summary`,
  `correction` — fires as soon as Call A parses, same as today's behavior, just
  no longer gated on B/C also being done.
- `citations` (Call B): `citations`, `cited_source_indices`,
  `source_confidence_override`, `confidence_note`, `takeaways`, `gaps`.
- `enrichment` (Call C): `next_questions_for_user`, `next_steps`,
  `suggested_actions`.

`final_parallel.py`'s `as_completed()` loop currently only logs a checkmark per
call; the parse-and-emit work happens AFTER the loop, once all three are in. Fix:
parse and emit `integrator_partial` for each call INSIDE the loop, the moment
its future resolves — no longer wait for the other two. The final merge into one
`card` dict (for `ctx.response_payload`/persistence) still happens after the loop
exactly as today — this only changes when the CLIENT hears about each piece, not
what gets persisted or how the pieces get combined.

Frontend rendering of `integrator_partial` events (populating tabs incrementally)
is Chat FE's side — this doc covers the backend event contract only; coordinate
before ramping so partial events aren't silently dropped by a client that doesn't
know the event type yet.

## Rollout

Same as the original ramp plan: 0% (build+verify) → 10% → 50% → 100% via
`MOBIUS_INTEGRATOR_PARALLEL_PCT`. Verify wall-clock via a real test batch before
each step, same as the original latency ask required.
