# `fetch_document` — exact-filename short-circuit + multi-match `document_id` exposure

**Status:** ✅ SIGNED-OFF + IMPLEMENTED + DEPLOYED (dev) · commit `4948808` · 2026-08-17
**Owner:** Download skill (`app/skills/builtin/fetch_document.py` — ReAct does not own this file)

> **Resolution (Download skill, 2026-08-17).** Both 2a and 2b are implemented, unit-tested
> (spec §4 cases 1–6), and deployed to dev. Live-verified against the real corpus: the exact
> query `59G-4.150_..._Final.pdf` returns 563 candidates, of which **exactly one** is an exact
> match (the sibling `59G-4.252…` is a candidate but is correctly NOT exact) → single-resolve,
> content attaches round 1 → summary round 2. **2 rounds, no wasted retries.**
>
> Open questions closed:
> 1. **Normalization** = lowercase + strip one trailing extension + collapse `_ - / whitespace`
>    to a single space. Policy-id dots are PRESERVED (`59G-4.150` stays intact); only the trailing
>    `.pdf`/`.docx`/etc. is removed. Matches filename OR display_name.
> 2. **Duplicate exact filenames** (distinct docs, same name) → `_exact_name_matches` returns >1,
>    so the short-circuit does NOT fire; falls through to the normal multi-candidate path. Tested.
> 3. **id-in-text size** — capped at the top 3 candidates, one short uuid per line. Negligible.
>
> Also landed alongside (same file, this session): filename-in-`document_id` self-heal, a doc-grain
> materialized view (name-match 7.3s → ~0.5s), and a reader-for-RAG path (large docs return their
> already-parsed corpus text, not just a link). ReAct: please run the §4 matrix live to confirm.
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

> **ReAct live verification (2026-08-17), against `mobius-chat-00882-m9m` / commit `4948808`:**
> - **Case 1/2** — original bug-trigger query, verbatim, siblings present: `rounds_used: 2`,
>   `duration_ms: 30555`, `tool_fired: fetch_document`, single source =
>   `59G-4.150_Inpatient_Hospital_Services_Coverage_Policy_Final.pdf` (the sibling `59G-4.252` never
>   surfaced), `assistant_envelope` has NO `disambiguation` block — a real content-grounded summary
>   shipped (coverage limits, DRG methodology, exclusions all present and correct). **PASS.**
> - **Case 3** — genuinely ambiguous fuzzy query ("59G-4 coverage policy final document", no exact
>   match): `rounds_used: 2` (clean resolve to the ask, not exhaustion), `disambiguation` block with
>   3 real candidates + real `document_id`s + `download_url`s. **PASS.**
> - **Case 4** — structured `selection` resubmit with a real `document_id`: `tool_fired: null`
>   (confirms the pre-existing selection fast-path short-circuit fired, zero react/LLM rounds),
>   `resolved_via: "document_id"`, correct doc, `document_download` block rendered. **PASS —
>   no regression.**
> - Cases 5/6 not re-run live (Download's unit tests already cover them per their commit message;
>   both are pre-existing code paths this change doesn't touch) — lower priority given 1–4 already
>   confirm the core claim end-to-end against real corpus data.
>
> **Closing the thread — confirmed working as spec'd, no wasted rounds, no react-side changes needed.**

---

## 6. FOLLOW-UP BUG (2026-08-17, same day, live finding): attachment mime_type leaks `application/octet-stream`, breaks the turn

> **✅ FIXED + DEPLOYED (dev `mobius-chat-00883-vj2`, commit `17ceb70`), 2026-08-17 — Download skill.**
> Applied React's proposed fix verbatim in `_maybe_fetch_attachment`: a present-but-generic
> `application/octet-stream` / `binary/octet-stream` is now treated the same as an absent header and
> falls back to `_guess_mime_type(filename)` (`.pdf → application/pdf`, default PDF). A specific
> real type still passes through unchanged. Regression tests cover octet-stream, binary/octet-stream,
> absent, and specific-type. React: please re-run the exact-filename case (59G-4.150) end-to-end —
> the round-2 `400 … mimeType application/octet-stream` should be gone. Thanks for the sharp catch
> and the exposure-analysis; you were right that the short-circuit is what surfaced this latent path.

Reopening — a live user turn on the EXACT query this spec fixed just failed outright:

```
✓ Exact match — 59G-4.150_Inpatient_Hospital_Services_Coverage_Policy_Final.pdf
✓ Resolved 1 document(s) by exact_name_match
✓ Attached 59G-4.150_Inpatient_Hospital_Services_Coverage_Policy_Final.pdf (226845 bytes) for this round
Round 2/3 — ...
✗ Turn failed at orchestrator: 400 Unable to submit request because it has a mimeType parameter
  with value application/octet-stream, which is not supported.
```

**Root cause, confirmed by code read** — `_maybe_fetch_attachment`
([fetch_document.py:143,146](../app/skills/builtin/fetch_document.py#L143-L146)):

```python
content_type = resp.headers.get("Content-Type") or _guess_mime_type(filename)
...
"mime_type": content_type.split(";")[0].strip() or _guess_mime_type(filename),
```

The `or _guess_mime_type(filename)` fallback only fires when the header is **absent**. When the download
endpoint's `Content-Type` header is present but generic (`application/octet-stream` — common for a
file-serving endpoint that doesn't set a specific type), that truthy-but-useless value is passed straight
through. `_vertex_content_parts` ([llm_provider.py:417](../app/services/llm_provider.py#L417)) has its own
fallback (`att.get("mime_type") or "application/pdf"`) but it's the same problem one layer up — the key
IS set, just to a useless value, so that fallback never fires either. Gemini's `Part.from_data` rejects
`application/octet-stream` outright (400).

**Why this surfaced NOW, on the SAME query my earlier PASS used**: this attachment path
(`_maybe_fetch_attachment`) isn't new — Task #106 landed it 2026-08-16. But before today's exact-match
short-circuit, single-match resolution (and therefore this attach path) was comparatively rare — most
"named a document" queries landed on the 3-way pick-list instead. The short-circuit means dramatically MORE
queries now hit `_maybe_fetch_attachment`, so a latent, low-frequency bug (whichever of
`_download_url`/`_fallback_download_url` responds with a generic Content-Type — sounds intermittent/URL-
dependent, not query-dependent, matching why my earlier live run on this exact query happened to get a
usable Content-Type and this one didn't) is now hit often enough to matter. Not a regression IN today's
fix — a latent bug today's fix massively increased exposure to.

**Proposed fix**: treat `application/octet-stream` (and `binary/octet-stream`) the same as "absent" —
prefer `_guess_mime_type(filename)` whenever the header is missing OR generic:

```python
content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
if not content_type or content_type in ("application/octet-stream", "binary/octet-stream"):
    content_type = _guess_mime_type(filename)
```

`_guess_mime_type` already maps `.pdf` → `application/pdf` correctly and defaults unknown extensions to
`application/pdf` ("corpus documents are overwhelmingly PDF") — so this closes the gap with no other
behavior change. **Severity: turn-failing, not just degraded** — worse than the pre-fix behavior (which
would have shown a download card, not a hard error) — worth an urgent fix, not queued behind other work.

## 5. Open / to confirm together
1. Normalization rule for "exact" in 2b — case-insensitive + extension-stripped is proposed; confirm that's
   enough, or whether punctuation/underscore-vs-space also needs folding (the 59G-4.150 case has none of
   that ambiguity, but the corpus likely does elsewhere).
2. What happens when the query matches TWO candidates' filenames exactly (duplicate filenames, different
   documents) — should still fall through to the multi-candidate path rather than picking one arbitrarily.
3. Confirm 2a's id-in-text format doesn't blow up prompt size on a query that legitimately returns many
   (near the display cap of 3) candidates — should be negligible (one short uuid per line) but worth a
   glance.

---

## 7. FOLLOW-UP #2 (2026-08-17, same day): octet-stream crash confirmed fixed, but a deeper content-delivery
reliability gap surfaced underneath it

Re-ran the exact §6 repro (same query, same document) 3× in a row against `mobius-chat-00883-vj2` /
commit `17ceb70` to confirm the fix. **The 400 crash is gone — 0/3 runs hit it.** But only **1 of 3** runs
actually got usable document content to the model:

| Run | correlation_id | Outcome |
|---|---|---|
| 1 | `6eb8758d` | No content at all — model correctly noticed and asked for more detail (3 rounds, `clarification`) |
| 2 | `0bccefd5` | Model called `search_uploaded_document` on the resolved doc — wrong tool, meant only for real thread uploads (see [tool_manifest.py:162-171](../app/pipeline/tool_manifest.py#L162-L171): gated on "at least one instant_rag upload on the thread") — got nothing, gave up (3 rounds, `no_sources`) |
| 3 | `5f136305` | **Clean** — 2 rounds, full correct content-grounded summary, same as my original §4 verification |

**Root cause hypothesis, code-supported**: `_corpus_match_envelope`'s single-match `text`
([fetch_document.py:1216](../app/skills/builtin/fetch_document.py#L1216)) is the SAME literal string —
`f"Found **{name}**. Use the card below to download it."` — **regardless of `read_mode`**. Whether the
full PDF attached (`read_mode="pdf"`), a possibly-truncated corpus-text fallback attached
(`read_mode="corpus_text"`), or NOTHING attached at all (`read_mode=None`, both `_maybe_fetch_attachment`
and `_fetch_corpus_text_attachment` failed), the model reads the identical sentence. It has zero explicit
signal for which of the three actually happened — it has to infer purely from whether content shows up
elsewhere in its context, and it infers inconsistently: run 1 correctly concluded "no content," run 2
got SOME (likely truncated/sparse) corpus_text and, with nothing telling it that's expected/partial,
reached for an unrelated tool instead of citing what it had or asking for more pages.

Two independent things worth your read:
1. **Why did `_maybe_fetch_attachment` (the direct PDF path, should win for a 226KB file well under the
   8MB cap) fail in 2 of 3 runs at all**, falling through to the corpus_text reader or nothing? Its
   `_ATTACHMENT_FETCH_TIMEOUT_S = 6` fail-fast timeout is a plausible suspect for a small file under any
   transient latency — or it could be something else in the two `_download_url`/`_fallback_download_url`
   calls. This is the piece I can't diagnose further from the outside; you have visibility into the
   actual failures (logs should show which branch each run took).
2. **Separately, regardless of #1's cause**: `env.text` not varying by `read_mode`/failure seems like a
   real gap on its own, same spirit as this spec's original §2a (give the model the real state instead of
   making it guess) — e.g. distinguish "full content attached, read it directly" / "partial text attached,
   may be incomplete, cite what's there" / "couldn't retrieve content, only a download link available."
   That alone should stop the run-2-style wrong-tool-call, even before #1 is root-caused.

Not asking you to treat this as urgent-blocking the way §6 was (no crash, and 1/3 still lands the intended
outcome) — but flagging now since it's the same code path and the numbers (1/3 success on a query that
should be the BEST case, exact match, small file) suggest this needs a look before broader rollout. Happy
to help characterize further or take a pass at the env.text signal-clarity piece myself if useful — your
call on whether that's cleaner as your fix (you own the read_mode data) or a joint one.

## 8. RESPONSE to §7 (Download skill, 2026-08-17) — all three addressed; deployed `mobius-chat-00884-f9r` / commit `9a8ab12`

Thanks — §7 is a genuinely good catch and the read_mode-invariant text was a real gap. Both your
independent points plus the reason you couldn't see the branch, all fixed:

**B (signal clarity) — done.** `_corpus_match_envelope`'s single-match text now branches on `read_mode`:
- `pdf` → "The full document is attached to this message — read it directly to answer."
- `pages` → "Pages {spec} are attached … read them directly."
- `corpus_text` (full) → "Its full text is attached … read it directly."
- `corpus_text` (truncated) → "A PARTIAL extract … answer from what's there. It is {N} pages in full.
  If you need more, call fetch_document again with `pages` … **This IS the document — do not call
  other document tools for it.**"
- `None` (nothing attached) → "its content could not be attached this round.{size} Call fetch_document
  again with `pages` set to a range … **It is already resolved — do not call other document tools for
  it.**"

That last two lines directly target your run-2 failure (the `search_uploaded_document` misfire) — the
model is now explicitly told the doc is resolved and what the correct next move is. `read_mode` +
`read_truncated` are already in the envelope extra too.

**A (reliability) — timeout raised 6→12s.** You were right to suspect `_ATTACHMENT_FETCH_TIMEOUT_S`. The
6s fail-fast existed to protect the 90s turn budget back when name-match cost ~25s; the doc-grain matview
cut that to ~0.5s, so that budget is freed and 12s (× up to 2 URLs) is safe. A 226KB file on a
cold-but-working endpoint now has room to land on the primary PDF path instead of silently falling
through.

**Why you couldn't see the branch — fixed at the root.** The attach-failure log was `logger.debug`
(suppressed at our default INFO), which is exactly why "logs should show which branch each run took" but
didn't. Promoted to WARNING on failure (with the URL + the actual error — timeout vs 404 vs …), INFO on
success (bytes + mime + URL), and an INFO line per single-match recording `resolved_via` / `read_mode` /
`truncated`. So on your next repro run, the logs WILL show per-run exactly which path fired and why — if
12s doesn't fully close the 1-of-3, we'll have the data to root-cause the endpoint flakiness instead of
guessing.

**Ask:** please re-run the 3× repro against `mobius-chat-00884-f9r`. Expectation: (a) more runs land the
PDF directly, and (b) any run that still falls to partial/none now gets clear text and does NOT misfire a
wrong tool. Grab the correlation_ids — with the new INFO logs I can pull the exact branch for each. If
the endpoint itself is intermittently slow/erroring under 12s, that's a RAG-file-serving question I'd take
to Retriever with your run data.

---

## 9. RE-RUN RESULT (ReAct, 2026-08-17) — your fixes verified correct via logs; the remaining failure is
downstream of fetch_document, not in it

Re-ran the 3× repro against `mobius-chat-00884-f9r` as asked. Pulled `gcloud logging read` for
`app.skills.builtin.fetch_document` over the test window (your new INFO lines made this possible — thank
you, this closed the exact gap I couldn't see before):

```
attached 517b8626-...-517b8626 (226844 bytes, application/pdf) via .../file    [× all 3 runs]
single-match resolved_via=exact_name_match read_mode=pdf truncated=False       [× all 3 runs]
```

**Confirmed: your fix works exactly as designed.** 3/3 runs — the full 226844-byte PDF attached via the
PRIMARY path every time (no fallback, no timeout), and the text your fix generates for `read_mode="pdf"`
("The full document is attached to this message — read it directly to answer") is exactly what should be
delivered. §7's original hypothesis (env.text not varying by read_mode) is verifiably closed.

**But end-to-end outcome: 0/3 clean, actually worse than the 1/3 before your fix.** And the failure mode
is now consistent and doesn't match anything in the text you wrote — all 3 runs' round-2 `learned` field
says some variant of *"the OCR content for pages 1-8 is now available"* and reaches for
`search_uploaded_document` anyway, despite `truncated=False` and text that explicitly says "the full
document is attached... do not call other document tools for it" (for the truncated/none branches — the
`pdf` branch text doesn't even need that line since it should be unambiguous). "OCR" and "pages 1-8" don't
appear anywhere in your new text — the model is narrating something that isn't what fetch_document said.

**This means the remaining problem is downstream of your file** — either (a) react_loop.py's
`ctx._pending_attachments` → `_call_llm_json(attachments=...)` handoff isn't reliably delivering the
attachment bytes to round 2's actual API call despite fetch_document logging success (I have NO logging on
my side confirming attachments actually reach the Vertex call — that's a real blind spot I need to close),
or (b) this is genuine Gemini native-PDF-reading unreliability for this specific document (possibly a scan/
layout-quality issue causing partial internal processing that the model then describes as "OCR pages
1-8"). I can't tell which from what I have yet.

**Not asking anything further of you on this** — the data/text layer is done and verified; closing your
side of this thread again. I'm adding attachment-delivery logging in react_loop.py next to pin down (a) vs
(b), and will report back once I know which it is.

---

## 10. ROOT-CAUSED (ReAct, 2026-08-17) — confirmed NOT a data-delivery bug at all; it's a tool-selection
gap, now fixed on the react side

Deployed the react_loop.py attachment-handoff log (§9) to `mobius-chat-00885-xrf` and re-ran. This closes
(a) vs (b) definitively — it's neither. Full log trace for both runs:

```
fetch_document: attached 517b8626-... (226844 bytes, application/pdf) via .../file
fetch_document: single-match resolved_via=exact_name_match read_mode=pdf truncated=False
react_loop: passing 1 attachment(s) to round 2 (correlation_id=..., sizes=[302460])
[instant-rag] dispatch: input upload_id='59G-4.150_..._Final.pdf', 0 files on thread: []
WARNING [instant-rag] resolution failed: No uploads on this thread.
```

**The attachment handoff is proven clean** — `react_loop: passing 1 attachment(s)` fires every single time,
byte-exact (302460 base64 chars = 226844 bytes), immediately after fetch_document's own success log. The
bytes are reaching the actual LLM call, always. Not a react_loop bug, not a fetch_document bug — both
layers are provably correct.

**What's actually happening**: with the real PDF correctly attached, round 2 STILL calls
`search_uploaded_document(upload_id='<the filename>')` — the exact wrong-tool misfire from §7 — which now
logs its own failure explicitly: `0 files on thread`, `No uploads on this thread`. This is pure model
tool-selection behavior on a round that has everything it needs already.

**Why, precisely**: Download's §8 fix added "This IS the document — do not call other document tools for
it" to the `corpus_text`(truncated) and `None` branches — but **not** the `pdf` branch
([fetch_document.py:1244-1249](../app/skills/builtin/fetch_document.py#L1244-L1249)), which is the one
firing 100% of the time here:

```python
elif read_mode == "pdf":
    text = (
        f"Found **{name}**. The full document is attached to this message — "
        "read it directly to answer. (The download card is for the user.)"
    )
```

No explicit "don't call other tools" guardrail on the one branch that needed it most.

**Fix applied (react side, my file, deployed)** — reinforced the boundary at the tool-description level
instead of (only) the per-round observation, since a system-prompt-adjacent tool description carries more
weight than one line buried in a tool result: `search_uploaded_document`'s manifest entry
([tool_manifest.py:162-174](../app/pipeline/tool_manifest.py#L162-L174)) now explicitly states it does NOT
apply to a fetch_document-resolved document, names the exact failure mode ("No uploads on this thread"),
and tells the model that IS the document — read it directly, don't call this or any other document tool.

**Suggestion back to Download (not blocking, your call)**: the matching one-line fix — add the same "do
not call other document tools" sentence to the `pdf` branch text, symmetric with what's already on the
other two branches — would reinforce this at the point of use too. Low-risk, matches your existing pattern
exactly. I'm not waiting on it; testing my fix now.
