# The A/B eval harness

Runs the fixed 15-question bank and reports the Product Promise's four terms
together — **latency, cost, quality, and whether the promise was met** — because
any one of them alone can be read as a win while another regresses.

## Why this lives in the repo

It lived in `/tmp` until 2026-09-15 and cost real data twice in one night:

1. **A fixed output path.** Launching a second run truncated the first's file.
   The summary survived; the `correlation_id`s did not — and they are the only
   way back to a turn, so round counts for that arm became unrecoverable.
   Output is now scoped per profile/tag.
2. **The question bank itself** existed only in a conversation and had to be
   reconstructed from a session transcript. A bank that moves between runs
   makes every comparison meaningless. It is now `ab-15-question-bank.json`.

## Two shapes, and the fork is the better one

`ab_bank_run.py` runs the bank sequentially against one or more profiles.
`fork_compare.py` forks each question so both arms run **in the same seconds**
on the same corpus, varying only the model (`ab_shadow_profile`).

Prefer the fork. On a shared dev instance sequential runs cannot be trusted:
measured 2026-09-14, an unrelated `mobius-lexicon-maintenance` sweep produced a
**3.5× latency spread on an identical query shape** — larger than any model
difference we have measured. Forking makes that noise land on both arms and
cancel in the within-pair difference.

## Three traps this harness has already fallen into

- **`clarification` is terminal.** Polling only for `completed`/`failed` recorded
  **604.7s** for a turn the service measured at **10.5s**, and that number was
  reported upward as "anthropic is slow". It was the harness waiting. Every run
  now records the pipeline's own `total_latency_ms` beside wall clock so the two
  cannot silently disagree.
- **Adjudication lands 30–70s AFTER a turn publishes.** A score read at turn-end
  is legitimately absent, and absent is **not** "scored badly". There is a second
  pass for this.
- **A failed turn is data, not a gap.** Never drop one from a mean.

## The caveat that gates every quality number here

**The adjudicator does not reproduce.** Re-scoring identical text with the same
judge: mean |delta| **0.241**, max 0.621 (n=8). That is larger than most
differences this harness can measure, including the v1-vs-v2 gap. Treat any
single score as ±0.24 and only large gaps as real. Latency and cost are
measured and do reproduce; quality is not yet a usable term.

## Running it

    AB15_PROFILES=gemini,anthropic AB15_TAG=compare python3 scripts/eval/ab_bank_run.py
    python3 scripts/eval/fork_compare.py          # all 15, forked
    python3 scripts/eval/fork_compare.py 1,4,13   # a subset

`cache_assist=false` is always sent: the answer cache serves a repeated question
with **no pipeline at all** (status=completed, zero thinking entries, no arm), so
a re-run of this bank without it would compare cache hits.
