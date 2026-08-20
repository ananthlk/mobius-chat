# Proposal: authority indicator + CTA, default stays non-authoritative

**From:** Payor Policy/Retriever Agent (owns mobius-rag)
**To:** ReAct Agent / Chat Architecture (owns react_loop.py — hard sign-off gate, not proposing to ship this myself)
**Date:** 2026-08-14
**Trigger:** Ananth — live query "who is the ceo of aspire health partners florida" appeared to require
`citable_required`/authoritative mode to get any answer at all, and asked for a default-to-non-authoritative
flow with a "check authoritative sources only" CTA gated on a low authority score.

## What I found testing this query live (both mode branches, `/api/retriever/answer`)

- `authority_requirement=citable_required` (greedy dispatch): returned 10 chunks, `status=ok` — but they're
  keyword-matched provider-directory/spreadsheet rows that happen to contain "Aspire Health"/"Florida", not
  CEO information. Non-empty, but not actually a real answer.
- `authority_requirement=any` (portfolio dispatch): returned **0 chunks, `status=empty`**, despite fillers
  having legitimately delivered 12/12 planned chunks. This was a real bug (see below), not a legitimate
  "nothing found" result.
- Checked `react_loop.py:832-841` (`_citable_required`/`_CITABLE_TERMS`): this exact query matches none of the
  keyword terms, so chat's own baseline for this query is *already* `authority_requirement=any` on call 1. That
  means the default call hit the empty-result bug, and the only way to get *anything* back was reaching for
  citable_required (via the existing Task #41(a) `force_citable_required` CTA) — even though that answer was
  also wrong. This fully explains the "authoritative seems required" symptom without any change to the default
  posture logic — it was a RAG-side bug making the correct default look broken.

## Fixed on RAG's side (mobius-rag, `retriever-answer-engine` branch, commit `e35efd1`)

`synthesis.py`'s `_trim_to_token_budget` had no floor — unlike `mmr_select`'s own explicit "always keep at
least one selection" guarantee (`fusion.py`), it could trim a required slot's citations all the way to zero
when the global token budget was blown by a handful of oversized chunks (a few giant PDF-table-extraction
chunks were enough). Only portfolio dispatch hits this in practice, since greedy's per-slot MMR budget is
unbounded and only the (less aggressive) global trim ever runs there. Fixed: the trim loop now always
preserves the single strongest surviving citation. 88 targeted tests + 667 full retriever/router suite pass.
Deploying to dev now.

**Net effect once live:** the default `any` call for this kind of query should now return real (if imperfect —
still whatever the corpus actually has) content instead of an inexplicable empty result.

## New field: `authority_score` (mobius-rag `/api/retriever/answer` response → `contract.grounding_markers`)

Added deterministically from each citation's existing `authority` field (`"authoritative"` | `"external"` |
`"planned"` | `None`) — no new LLM call:

```json
"grounding_markers": {
  ...,
  "authority_score": 0.0,          // fraction of citations that are "authoritative"; null if zero citations
  "authority_level": "low",         // "high" (>=0.7) / "medium" (>=0.4) / "low" / null
  "authoritative_citation_count": 0,
  "total_citation_count": 6
}
```

Thresholds (0.7/0.4) are Retriever's own starting point, not Eval-calibrated — same posture as
`shape/structure.py`'s interim `_ACCURACY_NEED` cut. Easy to retune later; only these two constants need to
change.

## What I'm proposing on chat's side (NOT built — your call, your files)

1. **Default stays `any`** (non-authoritative) — this already matches `_citable_required`'s existing keyword
   rule for the large majority of queries; no change needed there. The RAG-side fix above is what actually
   closes the gap that made this look broken.
2. **Thread `authority_score`/`authority_level` through** to wherever chat surfaces RAG's contract response
   today (same path `citable_required_used`/`n_chunks` already travel via `rag_history`/telemetry — see
   `_compute_baseline_citable_and_relax_eligible`, `react_loop.py:1049-1082`).
3. **Render an indicator** on the answer (Chat FE's remit, not react_loop's) — a simple low/medium/high badge
   off `authority_level`.
4. **Reuse the existing CTA mechanism** rather than building a new one: Task #41(a)'s `force_citable_required`
   override (`react_loop.py:1052,1070,1329`) already does exactly "re-submit this turn with citable_required
   forced True" end-to-end. Gate the button's visibility on `authority_level == "low"` instead of (or in
   addition to) whatever currently triggers it — I didn't find an existing trigger condition for surfacing that
   button proactively (only that the mechanism exists once a user/FE fires it), so this may be a small net-new
   piece: deciding *when* to offer the button, not building the override itself.
5. **Be honest about the ceiling**: even with both of the above, this specific query (an org-leadership lookup)
   is fundamentally out of corpus for a payer/provider-policy RAG system — neither authority mode reaches the
   open web on call 1 (`filler_d`/web is turn-gated to `call_number >= 2` in Router). That's a separate,
   legitimate scope question or router-side gating that goes further if you also want.

Happy to pair on wiring #2/#3 once you're back — this doc is meant to be actionable on your side without
needing a live sync, since I don't currently have a session reference for you.
