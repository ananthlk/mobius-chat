# SPEC — Chat Frontend v2 UX Changes

**For:** the **chat-front-end** agent (implements; owns frontend/UX going forward)
**Co-authored:** LLMManager (enricher redesign) + Chat Architecture (coordinator)
**Status:** Round 1 SIGNED (all five: Chat Master, UX, Product-Awareness, Technical Health, Compliance). **ROUND 2 RE-RATIFICATION REQUESTED (2026-07-26)** for two additions — **§1.4 tabbed-bubble layout** and **§2.1 progressive summary-first render**. AC-FE-1/FE-2 implemented (build-clean, not shipped); remaining ACs gated on round-2 sign-off + Tech Health per-AC acceptance. Nothing new ships until round 2 signs.
**Why now:** the chat enricher (parallel integrator Call A/B/C) is being rebuilt as v2 composable, envelope-selecting prompts. That changes *what the enricher emits*; the frontend must catch up. The prompt/backend work is validated (A/B same-or-better); the frontend is the lagging piece. This is that catch-up, specced.

---

## 0. Ground truth (what the frontend does today)

- `frontend/src/app.ts:1934` — an AnswerCard is **rejected (returns null)** unless `mode ∈ {FACTUAL, CANONICAL, BLENDED, RECITAL}`. `:1988` regex-extracts `mode`.
- `app.ts:2009–2010` — **section visibility is derived from `mode`**: `FACTUAL → hide all sections behind "Show details"`, `CANONICAL → show all`, `BLENDED → mixed`.
- `app.ts:2019 _renderSectionBody` — already renders section formats `table / stats / bars / steps / conditions / bullets` (classes `ac-fmt-*`).
- `app.ts:471, 8353, 8772` — `ui_blocks`: `chart` (image_base64), `callout` (info/warning/tip), `detail`, `table` render today.
- `app.ts:487, 1772, 3542` — `markdown_report` type renders + downloads (a real "report" surface).

---

## 1. AnswerCard schema changes — REQUIRED to deploy the enricher

### 1.1 `mode` becomes OPTIONAL (modes dropped)
v2 drops `FACTUAL / CANONICAL / BLENDED` (product decision — replaced by output-intent, §3). `RECITAL` **stays** (a distinct verbatim output).
- **Change — two gate sites, both must be touched:**
  - `:1934` `parseOne` — currently returns null if mode ∉ {FACTUAL,CANONICAL,BLENDED,RECITAL}. Make mode-absent a valid path.
  - `:1988` fallback extractor — also regex-gates on the same mode enum. Must also accept absent mode.
  - `:740` `AnswerCard` TypeScript interface — `mode` is currently a required union type. Make it optional (`mode?`).
  - Add `visibility: "primary" | "detail"` (optional) to the `section` type.
- **Loosening `mode` does NOT loosen card validity.** `direct_answer` (string, non-empty) remains the required anchor for any non-RECITAL card; RECITAL additionally requires `recital.verbatim`. "Accept a no-mode card" means exactly that — not "accept any JSON." `:1935` already enforces `direct_answer` today; this change protects that invariant.
- **Invalid-value handling:** an unrecognized `mode` value (not in {FACTUAL,CANONICAL,BLENDED,RECITAL}) is treated the same as absent — falls through to the v2 path. A missing or unrecognized `visibility` value (not "primary"/"detail") is treated as absent — falls to the §1.2 absent-value fallback (first section primary, rest detail). This ensures mid-ramp enricher bugs produce defined behavior, not undefined renderer falls.
- **AC-FE-1:** a card with no `mode` renders normally at both `:1934` and `:1988`; a `RECITAL` card still renders as a recital; TypeScript types compile with `mode` optional and `section.visibility` present; **and a card with neither a valid `mode` nor a `direct_answer` still returns null (malformed-card rejection preserved).**

### 1.2 Per-section `visibility` replaces mode-driven visibility
Because mode is gone, the enricher now sets **`section.visibility: "primary" | "detail"`** per section (it chooses what leads vs what's tucked).
- **Change:** replace the mode→visibility logic (`:2009`) with: `visibility="primary"` → visible by default; `visibility="detail"` → behind "Show details".
- **Absent value fallback — RULED by UX (2026-07-26):** first section defaults to `"primary"`; subsequent sections default to `"detail"`. Explicit `visibility` values override. Deterministic — no implementation ambiguity.
- **Invalid value fallback:** any `visibility` value outside {"primary","detail"} is treated as absent — falls to the absent-value fallback for that section (renders behind "Show details"). An unrecognized value must never dark-render content with no path to reach it.
- **AC-FE-2:** a card with mixed `primary`/`detail` sections shows primary inline and tucks detail behind "Show details". Absent `visibility`: first section primary, rest detail.

### 1.3 Envelope formats fire more often — keep them robust
v2 actively **selects** `table/stats/bars/steps/conditions` (fewer default-bullets). `_renderSectionBody` already handles these — verify each renders well and is **responsive** (tables scroll horizontally on mobile, no body overflow).
- **AC-FE-3:** each of the 5 typed formats renders correctly + responsively for a representative card.

### 1.4 Answer card is a TABBED bubble — content maps to tabs (NEW, added 2026-07-26)
The AnswerCard renders as a **tabbed bubble**, not one long scrolling prose column. Content lands in the tab that owns it; the tab bar appears once any non-Answer tab has content (`app.ts:2712–2760`). This is the intended information architecture — do not stack another tab's content into Summary.
- **Tabs (today):** `Summary` (default, always present) · `Citations` · `Corrections` · `Follow-up` · `Tasks` · `Diagnostics` (admin/QA). The bar renders only when a non-Answer tab has content; badges show per-tab counts.
- **Field → tab mapping:**
  - **A (core):** `direct_answer` + `sections` → **Summary** tab. `section.visibility` (§1.2) decides lead-vs-tuck *within* Summary (primary inline, detail behind "Show details") — orthogonal to, and unaffected by, the tab structure.
  - **B (critic):** `citations` → **Citations** tab (count badge); `takeaways` + `gaps` + the source/authority badge render in **Summary** as review context — ruled by UX (2026-07-26). Rationale: critic content belongs alongside the answer so readers see "here's the answer + what sources covered + what they didn't" in one context. The locked architecture's gaps→Corrections-tab routing is superseded by this ruling for v2.
  - **C (enrichment):** `next_questions_for_user` → **Follow-up** tab; next-step task items → **Tasks** tab; `suggested_actions` → action chips.
  - **Telemetry / bandit / calibration** → **Diagnostics** tab only (Eval-Architect guardrails: system-confidence ≠ answer quality; arm = `(model, module_key, variant_id)`; grounded badge driven by the graded check, never structural presence).
- **AC-FE-8:** each populated field renders in its owning tab **per the §1.4 field→tab map** (`takeaways` deliberately renders in Summary as review context — that is the map, not a violation; `gaps` renders in Summary as review context per the UX ruling); the tab bar shows only tabs with content; badges reflect counts; Summary is the default active tab.

---

## 2. Parallel integrator merge (Call A/B/C)

Field ownership (server-merged at `final_parallel.py:6-8` + merge layer `:273–300`; frontend renders the merged card):
- **A (core):** `direct_answer` · `sections` · `thread_summary` · `correction`
- **B (critic):** `citations` · `confidence` · `takeaways` · `gaps`
- **C (enrichment):** `next_questions_for_user` · `next_steps` · `suggested_actions`
- **CONFIRMED emit shape (LLM Agent, verified in code 2026-07-26) — do NOT extend `parseOne`:** `final_parallel.py:265–306` merges A/B/C into **one AnswerCard JSON**. But the server SPLITS it for the client: `integrate.py:77` copies the `_ANSWER_CARD_ENVELOPE_KEYS` fields onto the client card, and **`assistant_envelope.py` builds typed envelope blocks from the B/C fields** — `next_steps` + `next_questions_for_user` → followup blocks (`assistant_envelope.py:417`), `suggested_actions` → action chips, `takeaways`/`gaps` → `detail` markdown, `thread_summary`/`correction` carried on the message. So the B/C fields reach the frontend via the **assistant_envelope typed-block path you already render** (`app.ts:485–486` next_steps/suggested_questions, `:470` detail), **NOT via `parseOne`.** `parseOne` extracting only the core (mode/direct_answer/sections/citations/followups/confidence_note/required_variables) is BY DESIGN — leave it.
- **`next_steps` — resolved:** it is NOT in the card allowlist, but it DOES reach the client via the assistant_envelope followup builder (`:417`). No backend fix needed.
- **AC-FE-4 (re-scoped to a VERIFY):** with parallel mode on, confirm the assistant_envelope path renders every B/C field (citations, confidence, takeaways, gaps, next_steps, next_questions, suggested_actions) with none silently dropped — **no `parseOne` rewrite.** If a specific field is found dropped, flag LLM Agent (backend fix), not a parser change.

### 2.1 Progressive summary-first render (NEW, added 2026-07-26)
The v2 answer is **not one atomic payload painted once** — delivery is progressive, summary-anchored:
1. **Phase 1 — summary streams first.** As soon as the pipeline is off the ReAct loop, a short **summary answer (≤5 lines, formatted as the summary)** streams and renders immediately into the Summary tab. It is the **stable anchor**.
2. **Phase 2 — parallel agents chime in.** A/B/C outputs (sections, citations, takeaways/gaps, next steps, questions, tasks) arrive and **enrich around** the summary — populating the Summary tab's sections and the other tabs' panels + badges **additively**.
3. **The streamed summary is never replaced or re-rendered.** No flash, no swap. Enrichment fills in; the anchor holds.
- **Frontend requirement:** render the streamed summary the instant it arrives (before A/B/C exist), then merge each parallel-agent field in additively as it lands — without disturbing the summary or anything already painted.

**Additive-merge contract (Tech Health round-2, 2026-07-26) — the render rules that make progressive merge race/flash-free:**
1. **Chrome + slots mount at anchor time, not on arrival.** At Phase-1 (summary render), mount the FULL tab-bar + panel STRUCTURE with empty, hidden slot containers (via the existing count=0/`data-empty` mechanism at `app.ts:2712`). Enrichment FILLS slots; it never mounts chrome mid-render (a tab bar appearing when B's citations land is a layout shift the "no flash" promise otherwise misses). This is the same mechanism as Chat Master's "no-op shell" ruling below.
2. **Merge is FILL-your-slot, never append → commutative.** A/B/C arrive in nondeterministic order and two of them write into Summary (A's sections, B's takeaways). Each field fills a FIXED, pre-created slot (sections slot, takeaways slot, citations panel, …), never appends to a shared container. Result: final layout is order-independent — permuting arrival order yields identical DOM.
3. **Idempotent, and completion reconciles (never repaints).** (a) SSE replay/reconnect can deliver a field twice — per-slot write is idempotent (interim: content-hash dedup per slot; bind to ClientChannel `seq_no` when that service lands). (b) Under v2 progressive, the completed handler **reconciles** — a no-op over already-filled slots — and never `replaceChild`-repaints an enriched panel (the `:2712` comment notes today's completed handler replaces panels; under progressive that path must not repaint filled slots, or it re-introduces the flash one level below the summary).

- **Scope — RULED now-slice (Chat Master, 2026-07-26):** the additive-merge shell IS the production renderer from day 1, running at N=2 today — A's `direct_answer` already streams as the anchor (phase-1, live: `integrate.py:461` → `final_parallel.py:268`), and the one merged A/B/C card lands as a single enrichment event (degenerate phase-2). Not a dormant shell — build the real slot-fill renderer for N chunks; it runs at N=2 until the off-ReAct migration adds phases. No backend change from LLM Agent this increment.
- **True-progressive (ReAct draft as phase-1 stream + B/C as separate chunks) is DEFERRED** to the off-ReAct migration, owned by LLM Agent (two emit changes). **TRIPWIRE:** 60 days from frontend-v2 ship — if phase signals aren't delivered by then, Chat Architecture re-reviews.
- **Mental model:** one slot-fill renderer serves all cases — legacy single-paint = N=1, today's now-slice = N=2, true-progressive = N>2. vitest exercises synthetic N>2 while prod runs N=2, so the future path stays proven before it ramps.
- **AC-FE-9:** (1) the summary renders before enrichment arrives and is never re-rendered/replaced during enrichment; (2) the anchor's viewport position does not shift when enrichment arrives (CLS assertion — chrome mounted at anchor time); (3) permuting parallel-field arrival order yields identical final DOM (commutative slot-fill); (4) duplicate delivery of a field is idempotent (no doubled content); (5) the completed handler is a no-op over already-filled slots (no repaint); (6) a legacy sequential card (all slots fill at once) still renders correctly.

---

## 3. Output-format surfaces (NEW — product + UX decision)

The enricher can now produce multiple **deliverables** from the same facts (validated in the format artifacts). Current status:
- **read** (default) — AnswerCard — *live UI*.
- **report** / **payor report** — `markdown_report` — *live, downloadable*.
- **NEW, no renderer today:** `email`, `SMS`, `EMR note`, `appeal letter`, `presentation`.

Needs (scope + priority with Product/UX):
- **(a)** a renderer per adopted surface; **(b)** export/copy/download affordances (email→copy/.eml, EMR→copy, report→.md/.pdf); **(c)** a **format selector** — default `read` + chips to switch (`read` | `report` | `email` | …). Intent *detection* is a **separate module**; the chips + switch UI are frontend.
- **Adopted surfaces — UX ruling (2026-07-26), PA confirmed (2026-07-26):** ✅ `read`, `report`, `email` (copy/export only; no interactive compose). Deferred to v1.1+: `appeal_letter`, `EMR_note`, `SMS`, `presentation` (require further coordination).
- **AC-FE-5 (per adopted surface, list to be named by UX + PA in sign-off):** the surface renders, the selector switches between formats **without re-running retrieval** (chip click = format-transform on existing facts), and every export/copy/download/send affordance enforces the PHI gate: (a) classify the **final rendered payload** via POST /classify (full pass) before the affordance is enabled; (b) gate=='clean' → export freely; (c) PHI-detected → BLOCK for SMS/clipboard/external channels; permit-with-audit for EMR note/appeal letter when MOBIUS_HIPAA_ALLOWED=true (BAA-signed); HARD BLOCK on SMS regardless of BAA; (d) indeterminate/error/timeout → fail-closed, affordance disabled; (e) every PHI export writes a disclosure audit row (content_sha256 + identifier categories + destination-type + purpose + user + org + timestamp) to compliance.hipaa_analysis_log, gate_source='export:\<surface\>'. The gate fires on **egress action** (copy/download/send), NOT on format-selector chip switch.

### 3.1 Transform return contract — PINNED (LLM Agent, 2026-07-26; owns generation + no-retrieval + HIPAA gate)
The format-transform endpoint returns the payload **plus a server-decided per-channel egress state** so the frontend renders affordances without ever classifying PHI itself.

```
transform response = {
  payload: <rendered format text/struct> | null,   // populated under (a); NULL when server withholds body (b)
  surface: "email" | "emr" | "appeal" | ...,
  egress: {
    overall: "allow" | "permit_with_audit" | "block",   // summary for affordance styling
    channels: {                                          // authoritative, per-affordance
      clipboard: "allow" | "block",
      download:  "allow" | "block",
      email:     "allow" | "permit_with_audit" | "block",
      emr:       "allow" | "permit_with_audit" | "block",
      sms:       "block"                                 // PHI never permitted; clean may allow
    },
    reason?: string,                                     // e.g. "phi_detected:default_deny"
    decision_token: string                               // opaque; binds decision to content_sha256
  }
}
```

**Display/egress rule by gate state — FINAL (Compliance ruling 2026-07-26):**
- `gate=="clean"` → payload renders, all export affordances enabled.
- `gate=="phi"` (known PHI) → **(a) payload renders on-screen** (in-boundary use; same facts already visible in read view); egress affordances gated per `egress.channels`. Under default-deny: every egress channel blocks. Under HIPAA-allowed: EMR/appeal unlock with audit; SMS always hard-blocked.
- `gate=="indeterminate"` (classifier error/timeout/unverifiable) → **(b) WITHHOLD payload** — `payload=null`, all channels block, frontend shows blocked notice. Fail-closed on uncertainty; (a) is only safe when classification was positive.

**Three non-negotiable conditions for (a) to be in-boundary (Compliance, 2026-07-26):**
1. **Ephemeral — no PHI at rest.** Transform result never persisted/logged/cached/telemetered server-side. Compute-and-stream; client renders in memory. /classify logs categories+hash only, never raw payload.
2. **Export enforcement is server-side, not just a disabled button.** Export endpoints gate on `decision_token` + re-verify (closes TOCTOU). Disabled UI is the UX; endpoint refusal is the control.
3. **Single-source egress verdict.** `egress.channels` computed by classifier is the ONE source both client (gray affordances) and server endpoints (refuse) read. No client-side re-derivation.

**Honest caveat (state this, don't oversell):** "disabled export" is a USE-boundary control, not hard DLP — select-all-copy/screenshot defeat a grayed button. Accepted residual: same already exists for the read view. Spec it as "egress affordances gated," not "PHI cannot leave."

**Audit is server-side at the export endpoint,** transactionally bound to the egress call (append-only, fail-closed, chat mig 042). Client passes `decision_token`; client writes no audit.

**`egress.channels` is a RENDER HINT, not the security boundary.** The export endpoint re-verifies (content hash vs `decision_token`) and re-gates at egress time, then audits — closing the TOCTOU (classify-clean → swap-PHI → export). A stale/tampered hint at worst causes the endpoint to reject; it can never cause an unaudited disclosure.

### 3.2 Format chip (Task #10) — SHIPPED 2026-08-04 (dev rev mobius-chat-00646-gj4)
A read-only **indicator** at the top of the answer card that surfaces the enricher's `output_intent` classification — *what kind of deliverable* this answer is. This is an indicator, **not** the interactive format *selector* of §3(c)/AC-FE-5 (that remains future work); the chip does not switch formats.

- **Vocabulary = the REAL enricher `output_intent` enum** (`app/stages/integrate.py` allowlist), which is a **deliverable type, not a format**: `read` (Answer) · `report` (Report) · `email` (Email) · `sms` (Text) · `emr` (EMR Note) · `appeal` (Appeal) · `payor_report` (Payor Report). This **supersedes an earlier draft** that proposed a format taxonomy (`report/quick/list/table/structured`) — that vocabulary was never emitted by the backend and is dropped (Chat Master ruling 2026-08-04: reconcile spec to the deliverable enum; fabricating labels the backend never emits is worse than shipping against the real enum).
- **`output_intent` is frequently `None` end-to-end.** Unknown/absent intent → **no chip rendered** (never a fabricated label). Each known value maps to an icon + plain-language label + a semantic per-intent hue (orthogonal to accent/success/warn/error); color is never the only signal.
- **Indicator vs adopted-surface note:** the chip may display a *deferred*-surface intent (e.g. `emr`, `appeal`) because it only reports what the enricher classified — this does **not** contradict §3's v1 adopted-surfaces ruling (`read`/`report`/`email`), which governs interactive/export surfaces, not this read-only label.
- **Impl:** `buildFormatChip()` in `frontend/src/render/bubble.ts` (exported, unit-tested); the AnswerCard type carries `output_intent?`/`display_summary?`.

---

## 4. Envelope catalogue alignment (with UX)

The enricher's `envelope.selection` block is authored from **UX's authoritative catalogue**. Frontend rendering must be **1:1** with that catalogue.
- **Frontend rendering confirmed from code:** `app.ts:471, 8353, 8772` renders `chart` (image_base64), `callout` (info/warning/tip), `detail`, `table` ui_blocks today for server-supplied blocks. The frontend renderer is already there.
- **Envelope catalogue — RULED by UX (2026-07-26):** Enricher can actively SELECT: `bullets/table/stats/bars/steps/conditions` (section formats) + `chart` (via Timing) + `callout` (embedded). Server-injected only (enricher cannot request): `document_download`, `task_list`, `correction`, `action_chips`, `sources`, `next_steps`.
- **AC-FE-6:** every envelope the enricher can emit has a matching, tested frontend renderer; none silently falls back. UX's authoritative catalogue is the input; this AC is the verification.

---

## 5. Rollout & backward-compatibility

Behind the existing `MOBIUS_INTEGRATOR_MODE=parallel` flag (currently **0%**). Frontend must be **backward-compatible during rollout**: old sequential cards (with `mode`) **and** new v2 cards (no `mode`, `visibility` flag) both render.

**Renderer path selection — single source of truth, read by both renderers so they cannot drift during the flag ramp:**
- **v2 path** when ANY section carries a `visibility` field, OR `mode` is absent, OR `mode` is unrecognized (any value outside {FACTUAL,CANONICAL,BLENDED,RECITAL} → treated as absent → v2 path → §1.2 fallback).
- **Legacy path** when `mode ∈ {FACTUAL,CANONICAL,BLENDED}` AND no section carries `visibility`.
- **RECITAL** renders as a recital in both worlds (keyed on `mode==='RECITAL'` or `recital.verbatim` present).
- **Collision** (card carries BOTH a legacy mode AND per-section visibility): per-section `visibility` wins (more specific signal); legacy mode is ignored for visibility. Any section lacking `visibility` in this case falls to the §1.2 absent-value fallback.

- **AC-FE-7:** both the legacy card shape and the v2 card shape render correctly with the flag at any rollout %; the discriminator logic above is the single source of truth for path selection.

---

## 6. Sign-off gate

**Round 1 (§1.1–1.3, §2, §3, §3.1, §4, §5): SIGNED by all five.** Round-2 column tracks re-ratification of the two additions (§1.4 tabbed layout, §2.1 progressive render).

| Reviewer | Gates | Round 1 | Round 2 (§1.4 + §2.1) |
|---|---|---|---|
| **Chat Master** | architecture / merge-contract; tab IA + progressive-merge contract | ✅ | ✅ (§2.1 now-slice ruling + §1.4 architecture verified) |
| **UX architect** | §1.2 fallback, §3 email, §4 catalogue; **§1.4 tab layout + field→tab map**; §2.1 summary format | ✅ | ✅ (2026-07-26; gaps→Summary confirmed; §2.1 anchor format confirmed) |
| **Product-Awareness** | §3 surfaces = product-truth; **§2.1 visible "summary-first" experience gated until ReAct same-day** | ✅ | ✅ (2026-07-26; condition: if ReAct slips, gate stays OFF until ready — applies to the visible Phase-1 experience, not the additive-merge renderer which is live from day 1) |
| **Technical Health** | frontend structure, backward-compat (§5), no RED; **§2.1 progressive-merge vs AC-FE-7**, AC-FE-8/9 coverage | ✅ | ✅ (three merge-contract conditions encoded; §1.4 confirmed; 60-day tripwire) |
| **Compliance/PHI** | §3 export affordances (egress gate, tiered action, audit row) | ✅ | n/a (additions don't touch egress) |

Round-2 additions do not change any Round-1 ruling. No NEW frontend code ships until Round 2 signs; the already-built AC-FE-1/FE-2 (unaffected by the additions) awaits Tech Health per-AC acceptance as before.

---

---

## Chat Architecture sign-off (co-author review, 2026-07-25)

**Architecture / merge-contract: APPROVED with three edits applied above.**

- Code refs verified against actual files: `:1934` mode-gate, `:2009` mode→visibility logic, `final_parallel.py:6-8` field ownership, `app.ts:471/8353/8772` ui_block renderers — all confirmed correct.
- Backward-compat posture (AC-FE-7) is correct: sequential-with-mode and parallel-no-mode must both work at any rollout %.
- Three open items added as explicit gates (not left as implementation-time decisions): §1.2 visibility fallback, §3 adopted-surfaces list, §4 enricher-request capability. None are blocking sign-off routing — they are required outputs of the UX + PA sign-offs.

*Drafted by LLMManager + Chat Architecture for the chat-front-end agent. Backend/prompt changes are validated (A/B same-or-better); this closes the frontend gap. Spec → sign-off → code.*

---

## Chat Architecture Round-2 assessment (2026-07-26)

**§1.4 Tabbed bubble — CONSISTENT with code and locked architecture. RESOLVED.**
- Verified against `app.ts:2712–2760`: the 5-tab implementation (Summary/Citations/Corrections/Follow-up/Tasks) already exists. §1.4 adds Diagnostics as tab 6. The locked 9-tab architecture (Detailed/Artifacts/Workflows as additional tabs) shows those three as future build scope — no conflict with shipping 6 now.
- Field→tab mapping verified against §2 field ownership and existing code: consistent.
- **`gaps` routing — RESOLVED (UX ruled 2026-07-26):** `gaps[]` renders in Summary as review context alongside `takeaways`. The locked architecture's gaps→Corrections-tab routing is superseded for v2. See §1.4 line 49 and UX Round-2 sign-off.

**§2.1 Progressive render — ARCHITECTURE RULING (Chat Master, 2026-07-26). SUPERSEDED by now-slice ruling below.**
- Original "no-op shell" framing corrected: `direct_answer` already streams today — phase-1 is live. The additive-merge shell IS the production renderer from day 1. See §2.1 scope ruling for the authoritative now-slice description.

**Round-2 status: ALL CLOSED.** UX gaps routing resolved; §2.1 now-slice ruled; all five reviewers signed. Build is unblocked.
