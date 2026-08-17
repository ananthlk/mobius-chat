# `document_candidates` — disambiguation fast-path contract

**Status:** DRAFT for ReAct ⇄ Chat-FE sign-off · Ananth-requested · 2026-08-11
**Owners:** ReAct Agent (emit + bypass + resubmit routing) · Chat FE/UX (block render + select wiring)
**Purpose:** When ReAct needs the user to pick among multiple document candidates (rule 12), skip the
LLM integrator enrichment (integrator_a/critic/enrichment) — there's nothing to enrich — and emit a
lean envelope that renders the candidates fast. Same backend-computed-flag pattern as the rest of the
week's ReAct work.

---

## 1. The fast-path envelope

On a disambiguation-ask round, `integrate.py` **bypasses** the full integrator and emits:

```jsonc
{
  "status": "clarification",              // REQUIRED — reuses existing FE clarification handling
                                          // (suppresses confidence badge, renders as an ask, not a
                                          //  confident answer). Do NOT use "completed".
  "assistant_envelope": {
    "version": 1,
    "blocks": [
      { "type": "direct_answer", "markdown": "I found 3 documents matching **\"Molina provider manual\"** — which did you mean?" },
      { "type": "document_candidates", "query": "Molina provider manual", "candidates": [ /* see §2 */ ] }
    ]
  }
}
```

- `direct_answer` carries the clarifying text (renders as the card lead via the FE's envelope→card path). REQUIRED.
- `document_candidates` carries the choices (§2). REQUIRED.
- **Nothing else is needed.** The FE depends on ZERO integrator-only fields for this render —
  `thread_summary`, `next_questions_for_user`, `citations`, `tldr`, `first_pass`, `reasoning_trace`
  are all optional and non-load-bearing here. Omitting them (the whole point of the bypass) is safe.

---

## 2. `document_candidates` block shape

New envelope block type. A **sibling** of `document_download` — NOT the same block. Reuses the candidate
data ReAct already writes (`ctx.react_document_download_data` via `_attach_download_payload()`), minus the
`download_url` requirement (a read-candidate is re-fetched by `document_id`, not downloaded).

```jsonc
{
  "type": "document_candidates",
  "query": "Molina provider manual",       // optional — original ask, for display + resubmit context
  "candidates": [
    {
      "document_id": "doc_abc123",          // REQUIRED — the select key + resubmit payload
      "title": "Molina Healthcare Provider Manual 2024",   // REQUIRED — display

      // ALL below optional (render degrades gracefully if absent):
      "subtitle": "Provider Manual · Molina · FL · Medicaid", // optional one-line; else FE derives from meta
      "snippet": "Covers billing, prior auth, appeals, timely filing…",  // optional preview line
      "meta": {                             // optional chips (rendered as small tags)
        "payer": "Molina", "state": "FL", "program": "Medicaid",
        "authority_level": "official", "host": "molinahealthcare.com",
        "effective": "2024"
      },
      "resolved_via": "title_match"         // optional provenance (diagnostics only)
    }
    // … 2–N candidates
  ]
}
```

Rules:
- `document_id` + `title` are the only required per-candidate fields.
- No `download_url` dependency (unlike `document_download`, which skips entries without it).
- Order = display order (FE renders top-to-bottom, no re-sort).
- Keep it small — this is a "pick one" surface, not a search-results page. If there are >~6 candidates,
  send the top N and say so in `direct_answer` (FE won't paginate).

---

## 3. Select → resubmit contract (the round-trip)

When the user clicks **"Use this"** on a candidate, the FE resubmits via `POST /chat` (the existing
resubmit-as-user-turn mechanism — same as think-mode escalation / appeals packet). **Structured, not
prose** — so the backend routes STRAIGHT to `fetch_document(document_id)` and does NOT re-run the react
loop to re-derive the choice (matches `fetch_document`'s own "call again with `document_id`" pattern).

**FE sends** (`POST /chat` body):
```jsonc
{
  "message": "→ Molina Healthcare Provider Manual 2024",   // human-readable echo for the transcript
  "thread_id": "…",
  "selection": {                                            // NEW structured field the backend reads
    "kind": "document_candidate",
    "document_id": "doc_abc123",
    "in_reply_to": "<correlation_id of the disambiguation turn>"
  }
}
```

**Backend (ReAct/chat) reads** `selection.kind === "document_candidate"` → calls
`fetch_document(document_id=selection.document_id)` directly, skipping planner/react re-derivation.
The `message` field is display-only (renders as the user's turn bubble so the transcript reads sensibly);
the backend routes on `selection`, not on parsing `message`.

If ReAct would rather NOT add a structured `selection` field and prefers a prose round-trip, the fallback
is the FE sending `message: "Use document doc_abc123"` and react re-parsing — but that re-runs the loop
and loses the speed win, so structured is strongly preferred. **This is the one field to confirm on your
side.**

---

## 4. FE behavior (Chat-FE, ~half-day)

- Render `document_candidates` as a compact card list: title (primary) + optional subtitle/meta chips +
  optional snippet, each with a **"Use this"** primary button (violet, Mobius token). No download action.
- On click → `POST /chat` with the §3 structured payload; render the echo as the user's next turn; the
  disambiguation card stays in history (like clarification chips do).
- Disabled/loading state on the clicked button during the round-trip.
- Accessible: real `<button>` per candidate, keyboard-focusable, `aria-label` = "Use " + title.

## 5. Backend behavior (ReAct)

- ctx flag: disambiguation round → skip integrator enrichment.
- `integrate.py` bypass: emit the §1 envelope (status=clarification + direct_answer + document_candidates)
  instead of running integrator_a/critic/enrichment.
- Populate `candidates` from `ctx.react_document_download_data` (already structured) — map to §2 shape.
- Route the §3 `selection` payload straight to `fetch_document`.

## 6. Open / to confirm together
1. **Structured `selection` field** on `POST /chat` (§3) — confirm ReAct/chat backend will read it (vs prose fallback). ← the one real dependency.
2. `subtitle` — do you emit it, or should the FE always derive it from `meta`? (FE can derive; emit only if you have something better.)
3. Candidate cap / ordering — confirm you'll pre-trim to a sane N.
4. Does the disambiguation turn need `status: "clarification"` specifically, or is an envelope-level `kind` cleaner on your side? (FE already handles `status: "clarification"` — lowest-friction.)
