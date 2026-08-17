# `disambiguation` — generic "pick one" fast-path contract

**Status:** DRAFT for ReAct ⇄ Chat-FE sign-off · Ananth-requested · 2026-08-11
**Owners:** ReAct Agent (emit + bypass + resubmit routing) · Chat FE/UX (block render + select wiring)
**Supersedes:** the document-specific draft — this is GENERIC (Ananth 2026-08-11: "we want a generic
disambiguation FE, not necessarily for just download").

**Purpose:** One reusable surface for any turn where the assistant needs the user to **pick one option
to proceed** — disambiguating documents, payers, CARCs, providers, NPIs, jurisdictions, anything. The
ReAct disambiguation-ask (rule 12) is the FIRST emitter, and it's a *fast path*: when the "answer" is
just "pick one," `integrate.py` bypasses the LLM integrator enrichment (integrator_a/critic/enrichment)
— there's nothing to enrich — and emits this lean envelope. But the block itself is a general envelope
block: any subsystem can emit it, not just ReAct, not just documents.

Relationship to existing `clarification_options`: that's the CHIP surface — good for short scalar
choices (jurisdiction FL/GA/TX). `disambiguation` is the richer CARD surface — title + subtitle +
metadata + snippet per candidate — for when the options need description, and it round-trips a
STRUCTURED selection instead of merging text into the composer.

---

## 1. The `disambiguation` envelope block (generic)

```jsonc
{
  "type": "disambiguation",
  "select_kind": "document",     // REQUIRED — WHAT is being picked. Extensible string enum; drives the
                                 // resubmit routing key + an optional leading icon. Known values:
                                 // "document" | "payer" | "carc" | "provider" | "npi" | "jurisdiction"
                                 // | "org" | "generic". Unknown kinds render fine (generic treatment).
  "query": "Molina provider manual",   // optional — the ambiguous input, for display + resubmit context
  "candidates": [
    {
      "id": "doc_abc123",        // REQUIRED — the select key + resubmit payload (domain-opaque to the FE)
      "title": "Molina Healthcare Provider Manual 2024",   // REQUIRED — primary display line

      // ALL below optional — render degrades gracefully if absent:
      "subtitle": "Provider Manual · Molina · FL · Medicaid",  // one-line context; else FE derives from meta
      "snippet": "Covers billing, prior auth, appeals, timely filing…",  // preview line
      "meta": { "payer": "Molina", "state": "FL", "program": "Medicaid", "authority_level": "official" },
                                 // optional flat string→string map → rendered as small chips (order preserved)
      "badge": "official",       // optional status/quality pill (e.g. authority tier)
      "action": null             // optional per-candidate OVERRIDE (see §3); null/absent = default resubmit
    }
    // … 2–N candidates, in display order (FE does not re-sort)
  ]
}
```

**Contract rules**
- `select_kind` (block) + `id` + `title` (per candidate) are the ONLY required fields. Everything else optional.
- `id` is **domain-opaque** to the FE — it's whatever the backend needs to route (`document_id`, payer slug,
  CARC code, NPI, …). The FE never interprets it; it just echoes it back in the selection.
- No domain-specific required fields (no `download_url`, etc.). That's what makes it generic.
- Keep it a "pick one" surface, not a search page. >~6 candidates → send top N and say so in `direct_answer`.

### 1.1 Generic base + per-kind specialization — "do both" (Ananth 2026-08-11)

ONE block, TWO levels of treatment, so a subsystem gets tool-specific richness OR the plain generic card
without a new block type each time:

- **Generic base (always):** the FE renders every `disambiguation` block as a uniform candidate card list
  — title + subtitle/meta chips + snippet + a primary **"Use this"** button → structured resubmit (§3).
  An unknown/simple `select_kind` (payer, carc, generic, …) gets exactly this and nothing more. Zero FE
  work per new kind — emit the block and it renders.
- **Per-kind specialization (opt-in):** when `select_kind` has a registered FE specialization, the base
  card is DECORATED — a leading icon, kind-specific chip formatting, and optional SECONDARY affordances
  beyond "Use this". The specialization reads optional kind-specific fields the backend puts in the
  candidate (in `meta` or a `kind_data` object) but never *requires* them.
  - **`document` specialization (v1 example):** 📄 icon + when the candidate carries a `download_url`
    (in `meta`/`kind_data`), a secondary **"Download"** button next to "Use this" (reuses the existing
    `downloadDocumentFile`). So one card offers both "read this one" (resubmit → `fetch_document`) and
    "just download it" — the tool-specific richness, on the generic base.
  - Future kinds register their own specialization the same way (payer logo, CARC badge, provider NPI
    formatting) without touching the generic path or the contract.

This keeps the EXISTING tool-specific blocks intact for their non-disambiguation jobs — e.g. pure
"here's your file, download it" still uses the `document_download` block (§ its own render). `disambiguation`
is specifically the *pick-one-to-proceed* surface; the two coexist and a subsystem picks per intent:
pure download → `document_download`; choose-to-continue → `disambiguation` (+ optional kind specialization).

---

## 2. The fast-path envelope (ReAct's first use)

On a disambiguation-ask round, `integrate.py` bypasses the integrator and emits:

```jsonc
{
  "status": "clarification",     // REQUIRED — reuses existing FE clarification handling (no confidence
                                 //  badge, renders as an ask). Do NOT use "completed".
  "assistant_envelope": {
    "version": 1,
    "blocks": [
      { "type": "direct_answer", "markdown": "I found 3 documents matching **\"Molina provider manual\"** — which did you mean?" },
      { "type": "disambiguation", "select_kind": "document", "query": "Molina provider manual", "candidates": [ /* §1 */ ] }
    ]
  }
}
```

`direct_answer` = the "which did you mean?" text (renders as the card lead). `disambiguation` = the choices.
The FE depends on ZERO integrator-only fields here (`thread_summary` / `next_questions_for_user` /
`citations` / `tldr` / `first_pass` all optional, non-load-bearing) — so skipping enrichment is safe.

Non-ReAct emitters can drop the same block on a normal turn (e.g. "3 payers match 'sunshine' — which
one?") without the fast-path — the block renders identically.

---

## 3. Select → resubmit contract (the round-trip)

On **"Use this"**, the FE resubmits via `POST /chat` (existing resubmit-as-user-turn mechanism), STRUCTURED:

```jsonc
{
  "message": "→ Molina Healthcare Provider Manual 2024",   // human echo for the transcript (title)
  "thread_id": "…",
  "selection": {                                            // NEW structured field the backend reads
    "kind": "document",                                     // == the block's select_kind
    "id": "doc_abc123",                                     // == the chosen candidate's id
    "in_reply_to": "<correlation_id of the disambiguation turn>"
  }
}
```

**Backend routes on `selection.kind`** — not by parsing `message`:
- `kind: "document"` → `fetch_document(document_id=selection.id)` directly (no react re-loop).
- `kind: "payer"` → set payer scope and continue.
- `kind: "carc"` → resolve to that CARC and continue.
- … each subsystem owns its `kind`'s routing. `message` is display-only.

Structured beats prose ("Use document X") because prose re-runs the whole react loop to re-derive the
choice, losing the speed win. **This structured `selection` field is the one cross-boundary dependency
to confirm** — it must be read by whatever handles `POST /chat` (chat backend / react entry).

**Per-candidate `action` override (§1)** — for the rare candidate whose select isn't a resubmit (e.g.
"None of these — search the web" or a candidate that opens a link). Shape mirrors the appeals-modes
action: `{ "kind": "resubmit" | "link" | "prompt", "url"?, "prompt"? }`. Absent → default structured
resubmit above. FE-static copy per action kind; data drives only availability + target.

---

## 4. Division of work
- **ReAct / chat backend:** ctx disambiguation flag → skip enrichment; `integrate.py` bypass emits §2;
  map candidates → §1 shape from `ctx.react_document_download_data`; for the `document` kind, pass the
  optional `download_url` in `meta`/`kind_data` so the FE can offer the secondary Download; read + route
  `selection` (§3) per `kind`.
- **Chat FE (me):** two layers —
  - *Generic base (~half-day):* render any `disambiguation` block as the uniform card list + "Use this"
    structured resubmit + transcript echo; disabled/loading on click; keyboard-accessible; per-candidate
    `action` override. Works for every `select_kind` immediately.
  - *Per-kind specialization (small, incremental):* a tiny FE registry keyed by `select_kind` that
    decorates the base card (icon + optional secondary affordances). Ship `document` in v1 (📄 + Download
    when `download_url` present); add others as they're needed — no contract change per kind.

## 5. Open / to confirm together
1. **Structured `selection` field** on `POST /chat` (§3) — confirm the backend reads it. ← the only hard dependency.
2. `select_kind` enum — seed values OK? Add any you know you'll need so I can pre-build their specialization.
3. For the `document` kind: pass `download_url` in candidate `meta`/`kind_data` so the FE can offer the
   secondary Download alongside "Use this" — confirm you can populate it (you already have it from fetch_document).
4. `subtitle` — you emit it, or FE derives from `meta`? (FE can derive.)
5. `status: "clarification"` (lowest-friction for me) vs an envelope-level `kind` — your call.
6. Per-candidate `action` override in v1, or defer (default resubmit only) until a real non-resubmit case?
