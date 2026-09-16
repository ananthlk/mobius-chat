# The renderEnvelope cutover — scope, for the Deterministic UX seat

*Written by Governor (orchestrator v2), 2026-09-15, at Ananth's instruction:
"lets do the renderEnvelope cutover.. when there is a tool and a preferred UX
we should just use it IMO".*

**I have not started it.** This is your module, your commit comment is the one
that named the cutover, and you are working in it now. What follows is the
scope as I measured it while chasing a missing block, so you inherit the
measurement rather than repeat it.

## What I did do

One line, `app.ts`, committed: removed `next_steps` from `_suppressedChrome`.
Measured on cid `bfcf3385` — three next_steps emitted, three showing as the
"Tasks 3" badge, nothing the reader could see, while `suggested_questions`
rendered inline as chips. Same card, opposite treatment.

That is a symptom of the thing below, not a fix for it.

## Why the second path still exists

`renderEnvelope` (bubble.ts:1068) is already the intended single consumer and
already correct: walks blocks in backend order, renders each ONCE, peels
`sources`, drops off-contract types with telemetry, **no `_hasTabs`
heuristic**. It is used by `ab.ts` and by the A/B panel fallback.

The main completed-handler does not use it, and the reason is structural:

| | supplies |
|---|---|
| `renderAnswerCard` | the SHELL — tabs, Sources tab, Tasks panel, actions, diagnostics injector, mode classes, streaming-shell reuse |
| `renderEnvelope` | a body `<div>` + the peeled `sources` block |

So today the handler goes `envelopeToAnswerCard()` → `renderAnswerCard()` →
**then a second additive pass** over the same blocks with `_suppressedChrome`
deciding which ones the card already drew. That set is a heuristic standing in
for "did the shell render this", and it is where blocks go missing.

## What the cutover actually requires

1. **Move the shell.** Either `renderAnswerCard` takes a pre-built body from
   `renderEnvelope`, or the shell splits out of it. This is the whole job; the
   rest is small.
2. **A complete `renderExtraBlock`.** `renderEnvelope` routes non-leaf blocks
   through it. Today's A/B fallback handles exactly ONE type
   (`tool_attribution`). The main path needs: `next_steps`,
   `suggested_questions`, `action_chips`, `callout`, `correction`, `task_list`,
   `document_download`, `credentialing_card`, `pipeline_human_gate`,
   `takeaways`, `chart`, `disambiguation`, `attachments`, `certified_answer`.
   The per-block renderers already exist in `app.ts` — this is routing, not new
   rendering.
3. **Delete `_suppressedChrome` and `envelopeToAnswerCard`.** Both exist only
   to reconcile two renderers. One renderer needs neither.
4. **Keep the contentless-envelope guard.** `envelopeHasContent` stops a
   clarification/error turn rendering as a blank Research card (Ananth,
   2026-08-08). That is not dual-read and must survive.

## The two live block sets, measured today

    emitted   action_chips · bullets · direct_answer · first_pass · mode_badge
              next_steps · sources · suggested_questions · tool_attribution

    renderEnvelope handles directly:  sources mode_badge first_pass
                                      direct_answer tldr markdown_report detail
                                      + FORMAT_TYPES (table stats bullets steps
                                        bars conditions domain_card)
    → via renderExtraBlock:           action_chips next_steps
                                      suggested_questions tool_attribution

So for TODAY's traffic the gap is four types, all of which already have
renderers. The shell move is the real work.

## One thing I would argue for

`renderEnvelope` already counts what it drops (`onUnknownBlock`). Wire that to
telemetry before the cutover, not after: a block the backend emits and the
renderer silently discards is exactly the failure we have just spent a day
chasing, and it is the one failure this architecture can detect for free.
