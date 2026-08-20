# Prior Auth Form Surfacing — Product Spec

**Status:** Roadmap · Not started  
**Owner:** TBD (Chat + Payor Policy)  
**Filed by:** Chat Architecture · 2026-08-18  
**Scope:** Detection + routing only. No pre-fill. No PHI in pipeline.

---

## Problem

When a Mobius user asks about a prior authorization — "what do I need to submit for ABA therapy with Sunshine?" or "I need to request prior auth for this member" — the right form is buried on the payor's website. The user leaves chat, searches manually, often grabs the wrong form or an outdated one, and faxes cold with errors. Mobius knows the payor, the service type, and the intent. It should just surface the right form.

---

## What this is NOT

- **No pre-filling PHI.** Member data (ID, DOB, diagnosis) never enters our pipeline as part of this feature.
- **No PDF parsing at query time.** Forms are catalogued at ingest, not re-parsed per request.
- **No form submission.** Mobius surfaces and optionally renders; the user fills and sends via whatever channel the payor accepts (fax, portal, email).

---

## User flow (target)

```
User: "I need to submit a prior auth for ABA therapy for my Sunshine member"

Mobius: "Here's the Sunshine Health Behavior Analysis PA Request form.
         [Open form ↗]  It requires BCBA credentials, assessment scores
         (Vineland-3, BASC-3), and a signed treatment plan."
```

One response. Correct form. Context on what to prepare.

---

## Detection layer

Identify prior auth intent from chat context. Signal sources (in priority order):

| Signal | Example | Weight |
|--------|---------|--------|
| Explicit phrase | "prior auth", "PA request", "preauthorization" | High |
| Payor + service type | "Sunshine" + "ABA" / "behavioral health" | High |
| Procedure code mention | CPT 97151, 97153, H0036 | Medium |
| Outcome-adjacent phrase | "they denied", "I need approval for" | Low |

Detection runs in the Router/intent classification layer — same path as service-type tagging. Not a new model; add `PRIOR_AUTH_REQUEST` as a recognized intent signal in the existing classify_query flow.

Out of scope for detection: distinguishing initial vs. concurrent auth requests. Surface the form; let the user decide request type on the form itself.

---

## Form catalogue

A lightweight lookup table: `(payor, service_category) → form_record`.

```
form_record {
  form_id:       str           -- internal ID
  form_name:     str           -- human-readable
  payor_id:      str           -- FK to payor fact store
  service_cat:   str           -- e.g. "behavioral_health", "behavior_analysis", "physical_therapy"
  form_url:      str           -- canonical source URL (payor website)
  fax_number:    str | null    -- where to send
  notes:         str | null    -- "expedited: call 1-844-477-8313"
  last_verified: date          -- freshness gate
}
```

Seed with known Sunshine forms to start:

| form_id | form_name | service_cat | fax |
|---------|-----------|-------------|-----|
| SH-BH-ILOS | Behavioral Health PA Fax (ILOS) | behavioral_health | 1-844-208-9113 |
| SH-BA-PA | Behavior Analysis PA Request | behavior_analysis | 1-844-208-9113 |

Expand per payor + service category as onboarding adds payers.

Maintenance: re-verify form URLs on each payor corpus refresh. Flag stale (`last_verified > 90d`) in the Maintaining Agent's nightly sweep.

---

## Routing logic

```
classify_query output includes PRIOR_AUTH_REQUEST intent
  → extract payor_id from turn context (Payor Policy Agent)
  → extract service_category from turn context
  → lookup form_catalogue WHERE payor_id = :payor AND service_cat = :cat
  → if 1 match: surface form card
  → if 0 matches: "I don't have a catalogued form for [payor]+[service] yet — here's their provider portal"
  → if N>1 matches: surface all N with brief differentiation
```

No LLM call in the routing step itself. Pure lookup after intent + context are already resolved by existing layers.

---

## Chat output — form card

A structured card type (new `answer_type: prior_auth_form`), rendered by the FE alongside the normal answer.

Fields:
- Form name
- Payor name
- Fax number (if applicable)
- Link to form (opens in new tab or modal)
- Notes field (expedited path, what to attach)

The form card does NOT replace the answer — it augments it. The answer text can include context like "this form requires an assessment completed within 6 months" pulled from the payor corpus.

---

## Out-of-scope (explicitly deferred)

| Item | Reason deferred |
|------|-----------------|
| Pre-filling member fields | PHI scope; not ready |
| Pre-filling provider fields | Credentialing module integration TBD |
| Inline PDF render / fill modal | Useful later; adds FE complexity |
| Submission tracking | Requires workflow state management |
| Multi-payor form normalization | Maintenance burden; tackle per-payor |

---

## Dependencies

| Dependency | Status |
|-----------|--------|
| Payor Fact Store — payor_id resolution | LIVE |
| classify_query intent layer | LIVE (add new intent constant) |
| Form catalogue table (new) | Not built |
| Form card FE component (new) | Not built |
| Maintaining Agent — form freshness sweep | Needs new gate |

---

## Success criteria (v1)

1. When a user asks about prior auth for a catalogued payor + service, Mobius surfaces the correct form card in ≤1 turn.
2. Form URL is verified current (last_verified within 90 days).
3. Zero PHI flows through the form-surfacing path.
4. At least 3 payors × 2 service categories catalogued at launch.

---

## Open questions

- **Where does the form catalogue live?** Options: (a) extend payor fact store with a `forms` table, (b) separate `prior_auth_forms` table in mobius_rag DB, (c) flat config file per payor. Recommendation: (b) — keeps it queryable and Maintaining Agent can sweep it.
- **Who owns catalogue maintenance?** Sourcing Agent ingests payor docs; Payor Policy Agent owns the fact store. Form catalogue sits at the seam — needs an owner decision.
- **Expedited auth path?** Sunshine's expedited path is a phone call, not a form. Should Mobius surface that signal? Probably yes — low effort, high value.
