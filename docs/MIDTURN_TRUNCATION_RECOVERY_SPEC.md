# Mid-Turn Truncation Recovery — Design Spec (Task #16)

Author: ReAct Agent. Design doc only, per Chat Architecture's request — no
implementation in this commit. Answers the four questions they posed;
routes implementation ownership at the end.

## 0. What "truncated" means today (the actual current behavior)

Before designing recovery, it's worth being precise about what currently
happens on each of the three triggers, because the answer is different for
each and none of them currently checkpoint anything.

**Timeout (`MOBIUS_TURN_DEADLINE_S`, `app/worker/run.py`)** — the only
trigger that's actually implemented today. Two enforcement paths
(`signal.alarm` on the main thread, a daemon-thread + `Event.wait` on the
Cloud Run monolith path). On trip, `_publish_deadline_failure()`
(`app/worker/run.py:136`) publishes a **fixed, generic** payload —
`{"status": "failed", "message": "This is taking longer than expected...",
"error": "turn_deadline_exceeded", "deadline_s": N}` — with **zero partial
content**, regardless of how much the turn had already produced. The
"zombie thread" (daemon-thread path) keeps running disconnected after the
deadline trips; nothing is listening for its eventual result, and if it
later tries to publish its own completion, that's a **second publish to
the same correlation_id after the first one already went out** — a real
race the design needs to account for, not introduce.

**Budget exceeded** — not a distinct code path today. In practice this
*is* the timeout path (a turn that blows its round budget runs long enough
to also blow the wall-clock deadline) or the honest-refusal path
(`react_retry_guard.py`'s `structurally_exhausted()` / the final-round
self-report — see `docs/REACT_PRODUCT_PROMISE_SPEC.md`), which already
finishes gracefully with a real answer, not a failure. So "budget
exceeded" doesn't need new recovery infrastructure — it already produces a
complete (if honest-about-gaps) response. Only the timeout/deadline case
actually needs checkpointing.

**User stop** — **does not exist as a mechanism at all.** No
`/chat/stop`/`/chat/cancel` endpoint, no cancellation signal into the
worker. This trigger requires new infrastructure, not just checkpoint
plumbing, and inherits the same "can't forcibly kill a Python thread mid
LLM-call" constraint the timeout path already works around (bounded
nested tool-call timeouts, not true cancellation).

**The already-shipped failed-turn sentinel** (question 4's subject) —
`app/pipeline/orchestrator.py`'s `_publish_failed()` (~line 1450), shipped
2026-08-04 alongside Chat FE's retry-button work. On any exception during
the pipeline, it persists a turn row whose `assistant_content` is:

```json
{"turn_failed": true, "error_code": "<ErrorEnvelope.error_code>", "message": "<user_facing_message>", "retryable": <bool>}
```

`retryable` is backend-computed from `ErrorEnvelope.is_recoverable`
(rate_limit/timeout/provider_error/scrape_failed → true; refusals,
validation errors → false) and gates Chat FE's "Try again" button. It also
calls `clear_progress(correlation_id)` — **the in-memory progress buffer
is explicitly wiped on failure today.** Any checkpoint design has to
either read that buffer *before* it's cleared, or persist the checkpoint
somewhere `clear_progress` doesn't touch.

## 1. Where does the checkpoint live?

**Don't build new storage — read what already exists, before it's wiped.**
Two pieces already accumulate exactly the state a checkpoint needs, and
both already survive the zombie-thread scenario because they're written
incrementally, not just at turn-end:

- **`app/storage/progress.py`'s `_progress[correlation_id]`** (in-memory,
  same-process) — already accumulates `thinking` (every `emit()` line from
  react_loop.py, live, per-round) and `message` (via
  `append_message_chunk`/`append_draft_answer`). Readable via
  `get_progress(correlation_id)`.
- **`chat_progress_events`** (Postgres, cross-process — populated whenever
  `queue_type=redis`, which is the actual deployed config per
  `deploy/dev.env`) — the DB-replay mirror of the same event stream.
  Readable via `get_progress_events_from_db(correlation_id)` even from a
  different process than the one that ran the turn.

Concretely, two different checkpoint qualities exist depending on how far
the turn got before truncation:

- **Post-react, pre-integrator truncation** (react_loop.py returned,
  `ctx.final_message`/`ctx.react_draft` is set, `append_draft_answer()`
  already fired a `draft_ready` SSE event — see
  `app/pipeline/orchestrator.py:705-709` — but the integrator/composer
  LLM call that turns it into the polished answer card hasn't finished
  before the deadline trips). **This is the easy, high-value case**: a
  complete, coherent draft answer already exists and was already streamed
  to the client once. The failure path just has to not throw it away.
- **Mid-react-loop truncation** (killed on round N of M, no draft exists
  yet). No single "answer" exists, but the `thinking` log already has
  real, inspectable partial evidence (e.g. "Found 10 relevant passages
  across 6 docs" from a completed round). **New work needed here**:
  react_loop.py should checkpoint the *last successful tool result's
  content* into the progress buffer after each round completes, not only
  emit it as a thinking-log line — reusing the exact same "best available
  evidence" selection logic already written twice in react_loop.py for
  other fallback paths (the parse-failure fallback,
  `app/pipeline/react_loop.py:2539-2566`, and the exhausted-iterations
  fallback, `:3192-3206` — both already implement "find the most recent
  *successful* tool_results entry"). This is genuinely new react_loop.py
  work (mine), not just reading what's already there.

**Turn persistence**: the checkpoint content, once selected, rides in the
*same* `assistant_content` JSON blob the failed-turn sentinel already
uses (extended per §4) — no new table, no new column beyond what's
already inline there.

## 2. What shape does `was_truncated` take in the SSE stream?

Two levels, matching the existing two-tier signal architecture
(diagnostic thinking-log envelopes vs. the top-level response payload)
— don't collapse them into one:

**Top-level response payload** (what `/chat/response/{cid}` and the
worker's `_publish_deadline_failure()` actually return — this is what
gates the FE's UI decision, not a thinking_log entry a client could
plausibly ignore):

```json
{
  "status": "failed",
  "was_truncated": true,
  "partial_message": "<the checkpointed draft or evidence summary>",
  "checkpoint_kind": "draft" | "evidence" | null,
  "message": "<existing user-facing message, unchanged>",
  "error_envelope": { ... },
  "thread_id": "..."
}
```

`checkpoint_kind` distinguishes the two qualities from §1 — Chat FE
almost certainly wants to render "Continue" differently for a real draft
vs. "here's what I'd found so far," and definitely wants to know when
there's *nothing* to checkpoint (`was_truncated: false`, `partial_message:
null` — e.g. a rate-limit failure before round 1 even ran).

**Thinking-log envelope** (diagnostic, mirrors `make_turn_failed`'s
existing pattern exactly — same file, same signal family, just a new
`make_turn_truncated()` constructor): fires at the moment truncation is
detected, before the top-level payload is even built, so it shows up in
the Diagnostics tab the same way `react_trace`/`turn_failed` already do.
Doesn't duplicate the full partial content — just `checkpoint_kind`,
`rounds_completed`, and `elapsed_s`, for debugging, not for driving UI.

## 3. "Continue" vs. "retry" — resume the loop, or start a new turn?

**Not a resume — a new turn seeded with the checkpoint as `system_context`.**
Resuming react_loop.py's actual internal state (round counter,
`tool_results` list, `retry_guard` failure history, the live Python
generator) across a new HTTP request is architecturally unsound given the
stateless, queue-dispatched worker model — there's no live object to
resume into once the zombie thread from the killed attempt eventually
exits. Trying to serialize/rehydrate that state is a much bigger, more
fragile lift than the value justifies.

Instead: **"Continue" = a new turn whose `system_context` is the
checkpoint content**, reusing the *already-built* Round 0 short-circuit
mechanism (`app/pipeline/react/round0.py` — mine) exactly as designed:
"pre-loaded, verified data from a caller that already did the work."
Round 0 then makes the correct call on its own — if the checkpoint (a
full draft, or a solid evidence trail) is actually sufficient, it
short-circuits with one LLM call instead of a fresh multi-round hunt; if
it's thin, it returns `NEEDS_TOOLS` and the turn proceeds normally, now
with the prior evidence already available via the `system_context`
prefix every round already gets prepended
(`react_loop.py`'s round0-fallthrough context prefix, same file).

This is not a new capability — it's the existing `system_context`
mechanism used for a new caller (the FE's "Continue" button) instead of
its original caller (story/skill-card pre-loaded data). No react-side
code change is required to support this *reuse*; the caller (Chat FE /
orchestrator) needs to pass the checkpointed content as `system_context`
on the follow-up `POST /chat` instead of a bare retry of the original
message.

**"Retry"** stays exactly what it is today: resend the original message,
no `system_context`, full restart. Unchanged.

Concretely, this happened *manually* once already in this very session —
Ananth's live test hit a truncated attempt and the next message was
"please try again - can you take the information you already have and
continue to proceed to generate the report." That's a user doing
"Continue" by hand, worded almost exactly as the mechanism above formalizes
it. The design turns that manual workaround into a button.

## 4. Interaction with the shipped failed-turn sentinel

Additive, not a breaking change. The sentinel gains two fields:

```json
{"turn_failed": true, "error_code": "...", "message": "...", "retryable": true,
 "was_truncated": true, "partial_content": "<checkpoint text, or null>"}
```

`retryable` and `was_truncated` are **independent booleans**, not a
tri-state — don't conflate them:

|                          | `was_truncated=false`                    | `was_truncated=true`                          |
|--------------------------|-------------------------------------------|------------------------------------------------|
| `retryable=true`         | rate-limited before any output — Retry only | timeout with a partial draft — Continue *and* Retry |
| `retryable=false`        | refusal/validation error — neither button | should not occur in practice (having partial content implies continuing is always safe) |

Chat FE's existing retry-button gate (`retryable`) is untouched; it just
gains a sibling field to decide whether to *also* offer Continue,
preferring Continue as the primary action when both are true (per
Ananth's ask — a truncated 8-round report shouldn't force the user back
to round 1).

## 5. Known gaps / explicitly out of scope for this design

- **The double-publish race** (§0): if the daemon-thread zombie from a
  timed-out turn eventually finishes and tries to publish its own
  completion *after* `_publish_deadline_failure()` already published a
  failure, both call `get_queue().publish_response(correlation_id, ...)`
  for the same id. Whoever implements the checkpoint-read in
  `_publish_deadline_failure()` should also close this — e.g. a
  published/finalized marker checked before the zombie's late publish is
  allowed through. Pre-existing bug, not introduced by this design, but
  adjacent enough that fixing checkpointing without touching it would be
  strange.
- **User stop** has no existing mechanism to build on (§0) — needs its own
  design pass (a cancel endpoint, and how a queue-dispatched worker learns
  "stop," given the same un-killable-thread constraint the timeout path
  already lives with). Not blocking the timeout-truncation design above,
  which stands on its own.
- **Mid-loop checkpoint writing in react_loop.py** (§1's second bullet) is
  real new code, not just plumbing — needs its own review before landing,
  separate from the read-side wiring in worker/run.py and orchestrator.py.

## 6. Ownership split for implementation

- **`app/worker/run.py`** (`_publish_deadline_failure`) — read the
  checkpoint before publishing, build the extended payload (§2). Not
  react's file.
- **`app/pipeline/orchestrator.py`** (`_publish_failed`, sentinel
  persistence) — extend with `was_truncated`/`partial_content` (§4). Not
  react's file.
- **`app/storage/progress.py`** — no structural change needed for the
  post-react case (§1's first bullet already has what's needed); may need
  a small helper (`get_checkpoint(correlation_id) -> dict | None`) so the
  two call sites above don't each hand-roll the "which field means what"
  logic. Not react's file, but small enough LLM Agent or whoever owns
  progress.py could hand it off easily.
- **`app/pipeline/react_loop.py` + `app/pipeline/react/round0.py`**
  (mine) — the mid-loop checkpoint write (§1's second bullet) and
  confirming the `system_context` reuse path for Continue (§3, already
  built, just needs the new caller). I'll take these once implementation
  is greenlit.
- **`app/communication/emit_envelope.py`** — new `make_turn_truncated()`
  (§2), same pattern as every other signal in that file. Whoever owns
  that file's shared conventions (Chat Architecture, historically) should
  review the exact shape before it's added.
- **Frontend** — Continue button, wired to `was_truncated`/
  `checkpoint_kind`, sending the follow-up `POST /chat` with
  `system_context` set to `partial_content`. Chat FE's territory.
