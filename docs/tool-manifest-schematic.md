# `tool_manifest` — schematic

**Status: IN PROGRESS. Section 1–4 written 2026-09-10. Not reviewed by Chat Master yet.**
**Author: Platform seat. Every claim carries a line reference and how it was established.**

---

## 0. Why this document exists, and how to read it

Ananth, 2026-09-10: *"we will deal with tools_manifest methodically by going line by
line and creating a ux and understanding what it does and what it does not.. we need a
schematic developed first — I CANT TRUST YOU GUYS.. so we will do that with proper
documentation."*

**The distrust is earned.** In one day I reported stale deploy state three times, shipped
a blank schema page, closed two findings on a mechanism that was never exercised, and
had four of my own detectors wrong — a telemetry regex that missed the very convention
being measured, a swallow detector that counted a hand-up as a swallow, a signals path
pointing at a dead scratchpad, and a reachability check built as transitive closure.

So this document is written to be checkable without me. **Every claim is tagged:**

| tag | means |
|---|---|
| **[READ]** | I read the code at the cited line. Says nothing about runtime. |
| **[LIVE]** | I observed it from the deployed service. |
| **[MEASURED]** | A number I computed, with the method stated. |
| **[ESTIMATE]** | An approximation, with its method and error stated. |
| **[UNVERIFIED]** | I believe it and have not established it. Do not act on these. |
| **[REPORTED]** | Another seat told me. Not independently checked. |

**No fixes are proposed in this document.** Ananth's instruction is understanding first.
Where I see a defect I record it and stop.

---

## 1. What the file is

`mobius-chat/app/pipeline/tool_manifest.py` — **737 lines** [MEASURED: `wc -l`].

It builds **one string**: the catalogue of dispatchable tools that goes into the
planner's prompt. It dispatches nothing, calls no tools, and makes no decisions. Its
entire output is prose the model reads.

**This is the first thing to hold onto:** the node is a *prompt fragment generator*. Its
failure mode is not a crash or a wrong return value — it is **the model being told
something misleading**, which surfaces three layers away as a wrong tool call. Nothing in
the file can be unit-tested for correctness of meaning.

Public surface [READ, `:664`–`:737`]:

| symbol | line | what it does |
|---|---|---|
| `get_tool_manifest(allowed)` | `:664` | returns the composed manifest string |
| `get_manifest_tool_names(allowed)` | `:690` | returns the tool names it *rendered* |
| `__getattr__(name)` | `:716` | module-level dynamic attribute access |
| `ENTITY_TOOLS` | `:733` | registry set ∪ hand-listed |
| `FOLLOW_UP_CAPABLE` | `:737` | registry-derived |

`get_manifest_tool_names` exists because a parallel list would drift from what the model
actually sees [REPORTED — Chat Master built it during the P2 telemetry pass].

---

## 2. 🔴 THE PRODUCTION MANIFEST IS TWICE THE SIZE OF THE ONE IN THE CODE

**This is the most important thing in the document and it is invisible from the repo.**

The deployed service serves its own manifest at `GET /chat/skills-manifest` [LIVE — 200,
54,376 bytes]. Reading it:

| | tools | chars | ~tokens |
|---|---:|---:|---:|
| **static** — hand-written blocks in this file | **28** | 27,624 | ~6,906 |
| **auto-discovered from MCP** at startup | **29** | 26,080 | ~6,520 |
| **total in production** | **57** | 53,705 | **~13,426** |

[MEASURED: tool signatures counted as lines matching `^[a-z][a-z0-9_]*\(`, split at the
`── Auto-discovered tools (from MCP) ──` header at manifest line 413.]
[ESTIMATE for tokens: chars/4. Chat Master's `count_tokens` measurement of the static
half was 8,137 against a chars/4 estimate of 8,110 — 0.3% apart — so chars/4 is a sound
approximation here, but these are **not** measured token counts.]

**Consequences, and each is independently significant:**

**(a) Half the catalogue does not exist in the repository.** The 29 MCP tools are
registered at chat startup from a remote server. Nobody reading this file — or grepping
the codebase — can see them. Chat Master measured 28 locally and 58 in prod and could
not reconcile it until they looked at a live turn [REPORTED].

**(b) The P6 baseline of 8,137 tokens / 35.8% of the planner's prompt is a FLOOR, not
the figure.** It was measured against the static half. The real manifest is ~1.65× that
[MEASURED, ESTIMATE]. I have already corrected the finding; the honest position is that
the true share is unknown until someone runs `count_tokens` on the production render.

**(c) A prompt-cost decision cannot be made from the code.** Any judgement about
"the manifest is too big" that reads only this file is reasoning about 51% of the object.

---

## 3. 🔴 THE `get_*` MARKET TOOLS WERE DELETED FOR ALWAYS 404-ING. THEY ARE BACK.

`:40`–`:56` is a comment recording a deliberate removal [READ]:

> `_FL_MEDICAID_DATA_ROUTING_BLOCK` removed 2026-08-04 (Chat Architecture, live-testing
> regression: agent burned 5 rounds trying `get_published_rates`/`get_rate_benchmarks`,
> both "tool not found"). This block promised ~10 `get_*`/`search_orgs` tools as a
> "read this FIRST" priority routing table, but **NONE of them are backed by a
> registered MCP tool** … A prominent routing table for tools that always 404 is worse
> than no routing table at all. **Restore this … once that service is actually wired
> into `EXTRA_MCP_URLS`.**

**The production manifest now contains 26 of those `get_*` tools** — `get_published_rates`
at manifest line 691, plus `get_rate_benchmarks`, `get_market_size`,
`get_org_profile`, `get_service_mix`, `get_entrant_analysis` and ~20 more [LIVE].

They arrive through the **MCP auto-discovered section**, not through the deleted static
block. So the removal held and the tools returned by a different route.

**What I do NOT know, and this is the crux [UNVERIFIED]:**
- whether they now **dispatch successfully**, i.e. whether the MCP server that publishes
  them is actually reachable and functional from the deployed service
- or whether we have re-created the exact regression the comment documents — a
  prominent catalogue of tools that 404 — at **twice the prompt cost** and with the
  routing table's warning removed

**This is the single highest-value question in the node** and it is answerable with one
dispatch attempt against the live service. I am not guessing at it. **It belongs in the
comprehensive test.**

---

## 4. The composition path — line by line

[READ throughout this section.]

### 4.1 Static blocks (`:57`–`:461`)

Module-level string constants, one per tool or tool family:

| block | line | note |
|---|---|---|
| `_RETRIEVAL_METHODOLOGY_PRIMER` | `:57` | read once, applies to all search tools |
| `_RAG_BLOCK` | `:73` | |
| `_RECALL_EVIDENCE_BLOCK` | `:118` | |
| `_SEARCH_CORPUS_BLOCK` | `:135` | **`= ""`** — retired, backend routes to `rag` |
| `_RECALL_SEARCH_BLOCK` | `:138` | **`= ""`** — merged into `search_corpus(mode="recall")` |
| `_PRECISION_SEARCH_BLOCK` | `:141` | **`= ""`** — merged into `search_corpus(mode="precision")` |
| `_HEALTHCARE_NPI_LOOKUP_BLOCK` | `:143` | |
| `_SEARCH_UPLOADED_DOCUMENT_BLOCK` | `:162` | |
| `_REFUSE_BLOCK` | `:202` | terminal short-circuit |
| `_APPEALS_BLOCK` | `:231` | **one gate key, five callable tools** — see §5 |
| `_LOOKUP_AUTHORITATIVE_SOURCES_BLOCK` | `:289` | |
| `_INGEST_URL_BLOCK` | `:315` | |
| 8 × `_SERVICE_LINE_*_BLOCK` | `:346`–`:407` | |
| `_AUTO_DISCOVERED_HEADER` | `:464` | header for the MCP section |

**Three blocks are empty strings kept as named constants.** `_SEARCH_CORPUS_BLOCK`,
`_RECALL_SEARCH_BLOCK`, `_PRECISION_SEARCH_BLOCK` are all `""` with a retirement comment.
They render nothing. Whether anything still references them is [UNVERIFIED] — a named
empty constant is exactly the surface that reads as live to a grep.

### 4.2 `_auto_discovered_block(allowed, ...)` — `:473`

Renders the MCP section. This is the function that produces half the production manifest
and zero of the local one.

### 4.3 `_compose_manifest(allowed, ...)` — `:541`

Assembles the blocks in a fixed order. The module docstring states the order is
deliberately preserved because *"any drift in the planner prompt is a behavior change"*
[READ, `:24`–`:27`].

### 4.4 The two entry points — `:664`, `:690`

`get_tool_manifest` returns the string; `get_manifest_tool_names` returns the names
rendered. They must agree, and nothing in the file enforces that they do [READ —
no assertion or shared derivation between them; [UNVERIFIED] whether a test covers it].

---

## 5. What the node does NOT do

Stated explicitly because Ananth asked for it, and because several of the day's wrong
conclusions came from assuming otherwise.

- **It does not dispatch.** Dispatch is `react_loop.py`. A tool appearing here is not
  evidence it is callable.
- **It does not gate.** `allowed` narrows what is *rendered*; the actual permission
  resolution is `orchestrator.py:95` `_resolve_allowed_tools`, reading
  `user_tool_subscriptions`.
- **It does not know whether a tool works.** §3 is the live consequence.
- **It does not prevent two entries from claiming the same query.** [READ — this is the
  L2 cause found 2026-09-10: `appeals_lookup_rules` at manifest `:250`–`:256` claims
  *"how do I appeal CARC 22"* and *"…from Sunshine Health"*; `appeals_get_playbook` at
  `:258`–`:268` says only *"PREFER THIS over rag"*, never over its sibling. Ananth's
  query matched the sibling's examples near-verbatim, payor included, and the model
  obeyed the manifest.]
- **It emits nothing.** Zero log sites, zero telemetry sites on 737 lines [MEASURED,
  `gen_readiness.py`]. There is no record of what it rendered on any given turn.
- **`_APPEALS_BLOCK` is gated on one key while documenting five callable tools**
  [REPORTED — Chat Master, during the telemetry pass: recording only the gate key would
  have shown `appeals_get_playbook` as never-offered on turns the model called it].

---

## 6. Open questions for the review

For Chat Master, as the person who knows the dispatch side:

1. **Do the 26 `get_*` MCP tools dispatch, or do they 404?** (§3 — the crux.)
2. Which MCP server publishes them, and is it in `EXTRA_MCP_URLS` on the deployed
   revision?
3. Do the three empty `""` blocks still have referents anywhere?
4. Is there a test that `get_tool_manifest` and `get_manifest_tool_names` agree?
5. What is the **measured** `count_tokens` figure for the *production* render, so §2's
   estimate can be replaced?

---

## 7. Not yet written

- the UX Ananth asked for (what a maintainer needs to see and change)
- per-tool trigger-phrase overlap map across all 57 entries
- the `allowed`-narrowing path end to end
- what a reader of the planner prompt actually sees, in order
