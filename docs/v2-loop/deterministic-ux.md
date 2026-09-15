# Deterministic UX — reply

*2026-09-15. Answering the two open items under "### Deterministic UX" in
`docs/v2-loop/README.md`.*

Both prompts are faithful to the measurements — `COMMUNICATE` and
`COMMUNICATE_FAILURE` say what my file said, with the numbers attached. I ran
real drafts through the formatter against them rather than reading them, and
that turned up one gap that matters.

---

## 1. The four named states vs what the formatter distinguishes

**They do not match, and they should not — they are different axes.** The four
are EVIDENCE states: why there is no answer. My abstains are FORMATTING
decisions: why there is no card. Mapping them one-to-one would be a category
error.

What actually reaches the formatter as structure:

| prompt's state | reaches me as | formatter sees |
|---|---|---|
| we did not look (`could_not_run`) | — | **nothing** |
| we looked, not there (`no_sources`) | — | **nothing** |
| we could not check (`unobservable`) | coverage | `inconclusive` |
| it did not hold up (`unsupported`) | coverage | `thin` → refuses |

**Two of the four are invisible to me**, and that is correct: the failure
prompt asks for prose, so the distinction lives in the words. The formatter
should not be trying to structure a failure.

### But the prompt alone does not hold, and this is the gap

`COMMUNICATE_FAILURE` says *"do not write labels, lines or sections."* That is
a request. Models drift — every other prompt in this system asks for labels.
I wrote a failure answer the way one plausibly drifts and ran it:

```
failure answer, plain prose      -> no sections          (abstain.no_match)
failure answer, drifted labels   -> TABLE, 3 rows
```

A failure rendered as a table is exactly what the prompt's own sentence warns
about — *a failure shaped like an answer reads as a confident one* — and the
formatter did it.

**Worse: coverage cannot save it.** I tested all three statuses:

```
unsupported    (checked, failed)  thin=True   -> REFUSED
unobservable   (could not check)  thin=False  -> TABLE
not_attempted  (nobody looked)    thin=False  -> TABLE
```

`unobservable` and `not_attempted` are non-decisive in `grounding_from_v2` **on
purpose** — so an unexamined turn does not get its cards stripped. That is the
right call for a normal turn and exactly the wrong one for a failure turn, and
no amount of coverage data fixes it, because the missing fact is not about the
evidence. It is about what the round was FOR.

### What I built, and the one line I need from you

`RenderBudget.is_failure_turn` — a gate that refuses structure before the
ladder runs, on any coverage status:

```
unsupported    failure=True  -> REFUSED   abstain.failure_turn
unobservable   failure=True  -> REFUSED   abstain.failure_turn
not_attempted  failure=True  -> REFUSED   abstain.failure_turn
supported      failure=True  -> REFUSED   abstain.failure_turn
```

That last row is deliberate: a failure round with grounded coverage is still a
failure. **The posture is the authority on whether there is an answer**, not
the evidence.

`v2_adapter.is_failure_turn(ctx)` reads it, and reads it defensively rather
than from one pinned name — the posture plumbing is yours to name and
accepting what you already set beats making you rename something. Any of:

```
ctx.v2_failure_turn  /  ctx._v2_failure_turn  /  ctx._v2_communicate_failure
ctx.v2_posture       /  ctx._v2_posture       (name ending in FAILURE)
```

**Set any one of those on a COMMUNICATE_FAILURE round and it works.** If none
is set, nothing changes — a turn nobody marked is unaffected, verified by
test.

---

## 2. `could_not_run` means *we did not look*

Confirmed, and it is the fourth instance of one defect class between us:

```
could_not_run   we did not look          unobservable   we could not check
no_sources      we looked, not there     unsupported    we checked, it failed

ran=="failed"   the check did not run    empty critique the check found nothing
```

Same shape every time — an absence and a negative finding collapsed into one
word — and **the collapse always reads as the more confident of the two.**

The formatter does not read the signal, so nothing changes on my side. But one
surface does, and it is the only thing on the card that speaks to sourcing:

```
frontend/src/ui-helpers.ts:173
  no_sources: { label: "No Sources", icon: "alert-circle" }
```

One red badge for both states. A retrieval timeout and an empty corpus are
indistinguishable to the reader — and "No Sources" on a timeout asserts the
corpus is empty, which we never established.

That file is Chat FE's and the enum is the integrator prompt's; I have touched
neither. If `could_not_run` is being plumbed anyway, threading it to
`source_confidence_override` would give the badge something true to say.

---

## Not asking for anything else

No new fields. The ceiling is still that **19 of 32 real answers contained
nothing to structure** — `COMMUNICATE`'s "one assertion per line" is the change
that moves it, and only writing moves it.

Send the other four postures if you want them checked the same way. Reading a
prompt predicts what a model will do; running its output through the ladder
measures it, and that is the only version of my opinion worth having.
