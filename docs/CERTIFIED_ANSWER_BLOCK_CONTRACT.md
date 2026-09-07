# `certified_answer` — service-line certified-fact answer contract

**Status:** DRAFT for Service-Line-Registry ⇄ Chat-Master ⇄ Chat-FE sign-off · Ananth-requested · 2026-08-11
**Owners:** Service Line Registry (API envelope) · Chat Master (tool-response → assistant_envelope mapping)
· Chat FE/UX (block render). **Companion:** DISAMBIGUATION_FASTPATH_CONTRACT.md (blocking caveats reuse it).

**Purpose:** Render answers that come from CERTIFIED service-line rows (status/answer/caveats/citations/
data/meta) — including the two honest not-a-failure states (`known_absent`, `unknown`) that are the common
case. The key principle: **all three states share identical card chrome; only a neutral provenance pill and
which caveats show ever change.** "We don't know" is a real answer and looks like one.

---

## 1. The turn is COMPOSED typed blocks, not one monolith

A service-line answer turn decomposes into existing + one new envelope block. **Chat Master's tool-response→
assistant_envelope layer does the decomposition** (the Service Line API returns the flat {status,answer,
caveats,citations,data,meta}; Chat Master maps it to these blocks):

| From the service-line envelope | → assistant_envelope block | Renderer |
|---|---|---|
| status + answer + material/context caveats + inline provenance | **`certified_answer`** (NEW, §2) | new, mine |
| a `blocking` caveat's `choices[]` | **`disambiguation`** (select_kind = the caveat's domain) | EXISTING (shipped) |
| `data` limits/coverage/codes | **`table`** or **`stats`** | EXISTING |
| `citations[]` | **`sources`** | EXISTING |

So the ONLY new render is `certified_answer`. Blocking caveats become a `disambiguation` block (candidates =
`caveat.choices`, id passed straight back as the re-query modifier — loop closes per that contract). Data
maps to my table/stats. Citations to the Sources surface.

---

## 2. The `certified_answer` block

```jsonc
{
  "type": "certified_answer",
  "status": "found" | "known_absent" | "unknown",   // → provenance pill (§3). NEVER an error state.
  "answer": "H0031 is 4 different billable services…",  // REQUIRED — the verbatim, quotable headline sentence
  "caveats": [                                   // material + context ONLY (blocking → disambiguation block)
    { "code": "caps_not_additive", "kind": "material", "text": "…" },
    { "code": "standard_not_payor", "kind": "context", "text": "…" }
  ],
  "provenance": [                                // optional — the compact inline "sourced: …" line under the answer
    { "document": "2025 Community Behavoir Health Fee Schedule.pdf", "page": 2, "sourced": true }
  ]
}
```

- `answer` is the star — rendered as the headline in ALL three states, same treatment.
- `caveats` here carry only `material` and `context` (a test on your side already asserts blocking never
  rides here — good; blocking goes to the disambiguation block).
- `provenance` is the small inline citation line; the FULL citation list still goes to the `sources` block.
  Render the AHCA filename **verbatim** (the "Behavoir" typo is the real filename — correcting it breaks the
  join). A `provenance`/citation entry with `sourced:false` renders **flagged, not as a citation** (it's our
  placeholder wording, not policy — pairs with the `unsourced_placeholders` caveat; 75/153 reqs are unsourced,
  so this path is live).

---

## 3. Render rules (Chat FE)

**One card chrome, three provenance pills — nothing else structural changes between states:**
| status | pill label | pill tone | meaning |
|---|---|---|---|
| `found` | **Sourced** | subtle green | certified from a read source |
| `known_absent` | **Source silent** | amber-neutral | we hold + read the doc; it's silent |
| `unknown` | **Not held** | grey-neutral | no source held (the COMMON case) |

None red. No empty state, no error tint, ever. `unknown` with an empty `sources` block is correct and
looks like a confident answer — the sentence + material caveats carry the honesty.

**Caveat placement is by `kind`, never by reading text:**
- `material` → a ⚑ line immediately under the answer, one per caveat. Load-bearing, never collapsed.
- `context` → footer, muted (standard_not_payor lives here — it's on every answer).
- `blocking` → never rendered here; it's the `disambiguation` block.

**Layout:**
```
[provenance pill]  <answer sentence, verbatim>
                   · sourced: 2025 …Fee Schedule.pdf p.2        (provenance, when sourced:true)
⚑ <material caveat>                                             (one line each, under the answer)
[ disambiguation block ]                                        (only when a blocking caveat exists)
[ table / stats: limits as amount · period · unit ]            (from data, when it helps)
────
<context caveat> (footer, muted)   ·   Sources (full citations)
```

---

## 4. Division of work
- **Service Line Registry:** the API envelope (done) — status/answer/caveats(+choices on blocking)/citations/
  data/meta. Bend it if the FE needs a field (you've offered).
- **Chat Master:** the tool-response → assistant_envelope decomposition (§1) — emit `certified_answer` +
  `disambiguation` (from blocking `choices`) + `table`/`stats` (from `data`) + `sources` (from `citations`).
  ALSO: the current dispatch error on the code-based tools ("internal tool error") — E2E can't render until
  that's fixed (already flagged to you).
- **Chat FE (me):** the `certified_answer` block render (provenance pill + verbatim answer + material/context
  caveat placement + inline provenance + sourced:false flag). Everything else reuses shipped renderers.

## 5. Open / to confirm
1. **Data → table vs stats:** limits (amount·period·unit rows) → `table`; a single headline number (e.g. one
   rate) → `stats` tile. Chat Master picks per payload shape, or send a hint. Confirm you're OK with that.
2. **select_kind for blocking:** modifier_ambiguous → `select_kind: "modifier"`; bad_filter (the /search
   typo→suggestion) → `select_kind: "service_line"`. Seed the kinds so I can pick icons.
3. **Does `certified_answer` need `meta` (endpoint/jurisdiction)?** I'd surface jurisdiction (FL Medicaid) as a
   small footer tag next to the context caveat. Confirm `meta.jurisdiction` is stable.
4. E2E render verification is jointly gated on Chat Master's dispatch fix — none of us calls it done until a
   real question ("how many units of H2019 HR…") renders through.
