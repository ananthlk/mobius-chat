# `fetch_document` — exact-filename short-circuit + multi-match `document_id` exposure

**Status:** PROPOSED for Download skill sign-off · Ananth-requested · 2026-08-17
**Owner:** Download skill (`app/skills/builtin/fetch_document.py` — ReAct does not own this file)
**Trigger:** live finding — "read this document 59G-4.150_Inpatient_Hospital_Services_Coverage_Policy_Final.pdf
and give me a detailed summary" burned all 3 copilot rounds and never attached content, despite the user
naming the exact filename and that filename being the first (best) candidate returned.

---

## 1. Root cause (two distinct bugs, same trace)

### 1a. Multi-match `env.text` never exposes `document_id`

`_corpus_match_envelope`'s multi-match branch ([fetch_document.py:1046-1051](../app/skills/builtin/fetch_document.py#L1046-L1051)):

```python
names = ", ".join(s.document_name for s in sources[:3])
text = (
    f"Found {len(sources)} possible matches: {names}. "
    "Pick the one you want from the cards below."
)
```

`env.text` is exactly what the model reads back as the tool observation next round
([react_loop.py:2757](../app/pipeline/react_loop.py#L2757): `"result": env.text or ...`). It lists **names
only** — the real UUIDs exist (`download_docs` / `ctx.react_document_download_data`, which is how the
disambiguation card correctly renders 3 real candidates), but that structured data never reaches the
model's own context.

**Observed effect:** round 2's `thought` correctly concluded "I need to call fetch_document again with the
specific document_id" — but had no ID to send, so it resent the filename. That hits the misroute-recovery
guard ([fetch_document.py:937-946](../app/skills/builtin/fetch_document.py#L937-L946), the "That's a name,
not an id — searching by name…" line), which just re-runs the same fuzzy match and returns the identical
3-candidate list. Repeated in round 3. Copilot's 3-round budget exhausted on two structurally-impossible
retries; the (correct) disambiguation card only appeared via the exhausted-rounds fallback, not cleanly.

### 1b. Ranking has no exact-match signal, so near-duplicate siblings always crowd out a verbatim filename

`_score_doc` ([fetch_document.py:345-364](../app/skills/builtin/fetch_document.py#L345-L364)) is pure
token-overlap count — no boost for an exact (or near-exact) filename/display-name equality. In the trace,
`59G-4.150_Inpatient_Hospital_Services_Coverage_Policy_Final.pdf` (the file the user typed verbatim) and
`59G-4.252_Diabetic_Supply_Services_Coverage_Policy_FINAL.pdf` (a same-family sibling) score nearly
identically — both share "Services / Coverage / Policy / Final" — so the exact match gets buried in a
3-way tie instead of resolving on its own.

**Consequence:** even after fixing 1a, this class of query still costs 3 rounds minimum (get candidates →
fetch by chosen id + attach → read + summarize) — right at copilot's ceiling, one hiccup from failing
again. Fixing 1a alone makes the bug *recoverable*; it doesn't make the common case *fast*.

---

## 2. Proposed fix

### 2a. (minimum) Put `document_id` in the multi-match `text`

Multi-match `text` should name each candidate WITH its id, e.g.:

```
Found 3 possible matches:
1. 59G-4.150_Inpatient_Hospital_Services_Coverage_Policy_Final.pdf (id: <uuid>)
2. 59G-4.252_Diabetic_Supply_Services_Coverage_Policy_FINAL.pdf (id: <uuid>)
3. …
Call again with document_id set to the one you want.
```

Closes 1a on its own — a follow-up round can now actually carry a real id.

### 2b. (real fix) Exact/near-exact filename short-circuit before ranking

Add an equality check in `_run_fetch_document` / `_fetch_candidates`, ahead of `_rank_matches`: if the
query string, normalized (lowercase, extension stripped, `_`/`-`/space collapsed), exactly equals one
candidate's `document_filename` or `document_display_name`, treat it as resolved — skip the pick-list
entirely and route straight into the SAME single-match path `document_id` resolution already uses
([fetch_document.py:918-927](../app/skills/builtin/fetch_document.py#L918-L927): `_corpus_match_envelope(call, [row], ...)`).

This is the same pattern already proven elsewhere in this codebase for "small file, attach directly, don't
make the model round-trip an id" (the single-match auto-attach path, Task #106, and the thread-upload exact/
strong-match short-circuit in `_thread_upload_matches`,
[fetch_document.py:644-680](../app/skills/builtin/fetch_document.py#L644-L680)) — not a new mechanism, an
extension of the existing one to the name-search tier.

**Effect:** round 1 = search by name, detect exact filename equality, auto-resolve to 1 row, attach content
(size-permitting) in that same tool call. Round 2 = read attached content, write the summary. **2 rounds
total**, no dependency on the model correctly round-tripping an id, and it stops 3 near-duplicate documents
from ever surfacing a pick-list when the user named one exactly.

Do 2a and 2b together — 2a is a small, low-risk safety net for every OTHER ambiguous case (real multi-doc
ambiguity where no candidate is an exact match); 2b is what actually removes the wasted round for the
common case this trace hit.

---

## 3. Non-goals / confirmed no react_loop.py changes needed

- Attachment plumbing (`ctx._pending_attachments` → `_call_llm_json(attachments=...)` →
  `_vertex_content_parts`, native Gemini document part) already works correctly today for any single-match
  resolution, including via `document_id`. No changes needed on the ReAct side once fetch_document emits a
  single match for the exact-filename case.
- Rule 12 (disambiguation-ask guidance, `prompts.py`) does not need to change — the model's decision to
  self-resolve on an exact filename match (rather than ask) was already the right call; it just couldn't
  execute it. Once 2b makes that resolution automatic in fetch_document itself, the model never even needs
  to make that decision for this case.

---

## 4. Test plan (ReAct will run this live once deployed, same pattern as the disambiguation fast-path pass)

| # | Case | Expect |
|---|---|---|
| 1 | Exact filename named, verbatim, unique in corpus | Round 1 resolves + attaches content; round 2 ships summary. No pick-list. |
| 2 | Exact filename named, but 2+ near-duplicate siblings exist (this trace's case) | Same as #1 — exact-match short-circuit wins regardless of sibling count. |
| 3 | Fuzzy/partial name, genuinely ambiguous (no exact match among candidates) | Falls through to today's multi-candidate path — now WITH ids in `text` (2a) — disambiguation card renders, resolves in ≤1 follow-up round if the model/user picks. |
| 4 | `document_id` passed directly (post-disambiguation-card resubmit) | Unaffected — already deterministic single-row resolve (§8.1 code path), regression-check only. |
| 5 | Exact filename match, but file is oversized for attachment | Resolves to 1 candidate (no pick-list), but content doesn't attach — falls back to page_count hint / download card, same as today's existing oversized-file behavior. Confirm text doesn't claim content was read when it wasn't. |
| 6 | Query has NO name-token overlap with any candidate (true no-match) | Unaffected — falls through to corpus_search / web registry tiers as today. |

Small-file cases (the majority of the corpus per Ananth) are the primary win — confirm #1/#2 end-to-end
against real dev data before calling this done, not just against a synthetic single-row test.

---

## 5. Open / to confirm together
1. Normalization rule for "exact" in 2b — case-insensitive + extension-stripped is proposed; confirm that's
   enough, or whether punctuation/underscore-vs-space also needs folding (the 59G-4.150 case has none of
   that ambiguity, but the corpus likely does elsewhere).
2. What happens when the query matches TWO candidates' filenames exactly (duplicate filenames, different
   documents) — should still fall through to the multi-candidate path rather than picking one arbitrarily.
3. Confirm 2a's id-in-text format doesn't blow up prompt size on a query that legitimately returns many
   (near the display cap of 3) candidates — should be negligible (one short uuid per line) but worth a
   glance.
