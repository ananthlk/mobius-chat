# v2 → deterministic UX: the render contract

*From: Governor seat (orchestrator v2). 2026-09-12.*
*Every payload below is COPIED FROM A REAL LOCAL RUN, not written from the
schema. Where a field has never been observed populated, it says so.*

---

## What you are replacing

The enricher made a model call to write prose about an answer it could not
check. v2 replaces it with three things, and the first needs no model:

| | what | needs a model? |
|---|---|---|
| **assemble** | coverage per part, provenance per claim, open gaps | no |
| **critic** | is each claim supported by the facts given? | yes (cheap) |
| **next steps** | what would close what is still open | yes (cheap) |

Your surface is the first one plus the trace. **The deterministic half is the
part you can render without waiting for anything.**

---

## 0. What changed on 2026-09-13 — read this first

Two defects made §1 unrenderable, and they were **both on my side of the
seam**. If you tried this contract on 2026-09-12 and saw nothing, that is why.
Both are now fixed and deployed.

**(a) `emit_step` published the dataclass, not the dict.**
`make_v2_trace` returns an `EmitEnvelope`. `on_thinking` gates the structured
path on `isinstance(chunk, dict) and is_envelope(chunk)`, so it failed that
check, fell to the bare-string branch, and every v2 step reached the user as
`str(envelope)` — the dataclass repr. Nothing raised, so the fallback warning
never logged. Every peer emitter already called `.to_dict()`; this was the one
site that did not. Fixed in `app/pipeline/v2/trace.py`; gated by
`tests/test_v2_trace_envelope_shape.py`.

**(b) The envelope never reached the live stream — for ANY signal.**
`on_thinking` has passed `{"type": "thinking", "content": …, "envelope": …}`
since Sprint A.1 (2026-04-19). `send_to_user` read only `content` and dropped
the envelope on the floor; `append_thinking` then published `{line, ts}`.

So **no structured signal has ever been renderable live** — not `v2_trace`,
not `react_trace`, not `retrieval_trace`. They worked only *after* the turn,
off `chat_turns.thinking_log`, which is why the Diagnostics-tab panels look
fine and the live thinking log looks empty. That asymmetry was the bug, not a
design choice.

Fixed in `app/communication/gate.py` + `app/storage/progress.py`; gated by
`tests/test_thinking_envelope_reaches_sse.py`.

### What this means for you

The SSE `thinking` event now carries an optional `envelope`:

```json
{"event": "thinking",
 "data": {"line": "✓ Found 15 passage(s) across 3 document(s) in 8.2s",
          "ts": 1757..., "ts_readable": "…",
          "envelope": { …the full v2_trace envelope from §1… }}}
```

Three properties I have tested and will keep true:

1. **`line` stays authoritative.** It is the headline, always present. A client
   that ignores `envelope` renders exactly what it rendered before this change.
   Nothing you ship has to change on my account.
2. **`envelope` is attached to the FIRST line only.** One envelope describes
   one step; on a multi-line chunk the later lines carry no envelope, so you
   never get N rows for one step.
3. **`envelope` is always a dict or absent** — never a string, never a repr.

The FE currently reads `data.line` and stops (`frontend/src/app.ts`, the
`ev === "thinking"` branch). Reading `data.envelope` when present is the whole
change; `note`/`data.detail`/`data.key`/`data.state` then work as §1 describes.

**This is a request, not a commit — the FE is yours.** I have not touched
`frontend/`. If the shape is wrong for how you render, tell me and I will move
the backend rather than ask you to work around it.

---

## 1. `v2_trace` — the live stream

One signal per step. `note` is the headline, `data.detail` is what expands.
Same envelope shape as `retrieval_trace`, which you already render as a panel.

```json
{
  "signal": "v2_trace",
  "correlation_id": "local-a329f4549b",
  "note": "⚠ react replied: shape=mixed, 1 problem(s) — fact with no document",
  "data": {
    "stage": "react",
    "detail": [
      "  ✓ replied: shape=mixed · 3 fact(s) · 0 gap(s)",
      "       complete=true",
      "       • Molina provides a comprehensive Integrated Care Management (ICM) program [molina_fl_provider_manual_2026.pdf p111]",
      "       • UnitedHealthcare Community Plan's Care Model program ⚠ NO SOURCE",
      "       ⚠ fact with no document"
    ],
    "shape_seen": "mixed", "facts": 3, "ungrounded": 1, "gaps": 0,
    "is_complete": true, "next_round_worth_it": null,
    "problems": ["fact with no document"]
  },
  "round": 1, "thread_id": "t-79220173", "report_to_task_manager": false
}
```

### Stages, in the order they fire

| `data.stage` | fires | headline says |
|---|---|---|
| `memory` | once, before any spend | what we already knew, or that the store was down |
| `preload` | once | which tools ran before react spoke |
| `trim` | per preloaded tool | per fan-out arm kept/had, and **any starved arm** |
| `react` | per round | shape, fact count, unsourced count, problems |
| `memory_write` | per round | what was remembered, what was **refused** |
| `integrator` | once, at the end | how many parts are unverified |

### Two guarantees you can build on

**The headline stands alone.** If something is wrong, it is in `note` — a
starved fan-out arm, an unsourced fact, a failed call. You never have to expand
to find out whether to draw attention to it.

**`detail` is pre-rendered strings, deliberately.** This is human-facing text
and a second formatter would be a second author of what a step means. Render
them as lines; do not parse them. Everything you might parse out is already a
typed sibling key in `data`.

---

## 2. `ctx.v2_integration` — the answer's checked structure

```json
{
  "coverage": [
    {"part": "Molina care management philosophy", "status": "supported",
     "why": "1 grounded fact(s)", "evidence": ["molina_fl_provider_manual_2026.pdf p111"]},
    {"part": "Sunshine care management philosophy", "status": "unobservable",
     "why": "no grounded fact mentions this part — cannot tell whether it was answered from evidence or from memory", "evidence": []}
  ],
  "citations": ["molina_fl_provider_manual_2026.pdf p111"],
  "unsupported_claims": 1,
  "open_gaps": [],
  "critique": [], "critique_summary": "",
  "next_steps": [],
  "ran": {"assemble": "ok", "critique": "ok", "next_steps": "ok"},
  "prompt_sources": {"v2.integrator.critic": "fallback (missing)"},
  "problems": ["critique was not JSON"]
}
```

### `coverage[].status` — five values, and they are not a severity scale

| status | means | render as |
|---|---|---|
| `supported` | a grounded fact mentions this part | green, show the citation |
| `partial` | critic's judgement only | amber |
| `unsupported` | critic says the facts do not support it | red |
| `not_attempted` | still an open gap — **nobody looked** | red, and offer the reopen |
| `unobservable` | **we could not tell** | grey — never green, never red |

**`unobservable` is the one that matters.** It means the answer talks about
this part and nothing grounds it — we cannot distinguish "answered from
evidence we failed to record" from "answered from the model's memory".
Rendering it as a pass is the single most damaging thing this contract can do,
because it is exactly the state a confident wrong answer produces.

### `ran` — three values per section

`ok` · `skipped` (decided not to) · `failed` (tried, didn't answer).
**`failed` must never render as "no issues found."** An empty critique after a
failed critic is an absent check, not a clean one.

---

## 3. `RoundReport` — per round, every key always present

`report.to_dict()`. Keys are **always** present, including empty ones, so you
never test for existence and never invent a default — a default you invent
becomes a second author of the answer.

```
round_index · roles[] · thread_summary · turn_summary · findings[] ·
open_gaps[] · next_tools[] · evidence[] · set_aside[] · expanded_answer ·
is_complete · complete_why · next_round_worth_it · next_round_why ·
elapsed_s · promise_s · rounds_left · unobservable[]
```

Three rules that are load-bearing:

- **`is_complete: null` ≠ `false`.** Null means react did not say. Rendering it
  as "incomplete" asserts a judgement nobody made.
- **`expanded_answer` is only populated on a COMMUNICATE round.** Other rounds
  carry `turn_summary` instead. Publishing the expanded answer every round
  shows a half-written answer as final, three times on a three-round turn.
- **`next_round_worth_it` is asked, not obeyed.** It is the model's opinion,
  recorded so we can measure whether it was ever right. Do not render it as a
  commitment.

---

## 4. Empty vs absent — the rule under all of it

Every surface distinguishes **"we know this and it is empty"** from **"we do
not know this"**, because collapsing them is the defect this whole rebuild
exists to remove.

- `next_tools: []` renders as *"no tools offered — selector unavailable"*, not
  as nothing. An absent line reads as "no tools needed"; it usually means the
  selector never answered.
- A `memory` headline distinguishes *new thread* from *store unreachable* —
  both have zero facts and carry opposite advice.
- `unobservable[]` on the round report is what the round could not determine.
  It is not an error list and not an empty result.

---

## 5. What is not ready yet — do not build against these

- **`critique` / `critique_summary` are usually empty today.** The critic's
  reply is not reliably JSON and the prompt blocks are not in the prompt DB
  (`prompt_sources` says `fallback (missing)`). Render the section only when
  `ran.critique == "ok"` AND `critique` is non-empty.
- **`next_steps` is often empty** for the same reason.
- **`thread_summary` / `set_aside`** populate only on multi-turn threads; a
  first turn legitimately has neither.

Build against `coverage`, `citations`, `unsupported_claims`, `ran` and the
`v2_trace` stream. Those are populated on every run I have measured.

---

## How to see it yourself

Nothing here should be taken from this document. Run the question against dev
with `MOBIUS_V2_PCT=100 MOBIUS_V2_STEER=1 MOBIUS_V2_SHADOW=1
MOBIUS_V2_PRELOAD=1` and read the real envelopes — if a field here disagrees
with what the service emits, the service is right and this file is stale.
