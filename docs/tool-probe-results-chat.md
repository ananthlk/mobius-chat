# In-process tool probe — chat-owned tools

**From:** Orchestrator (mobius-chat)
**To:** Tool Manifest
**Run:** 2026-09-15, against deployed service URLs read from the running
`mobius-chat` Cloud Run config (not local defaults).

Every tool below was invoked through its **real dispatch gate** — registry
`dispatch()` or `react_loop._execute_tool()` — not a reimplementation.

## Result

| tool | result | evidence |
|---|---|---|
| `recall_evidence` (seeded) | **PASS** | returned the seeded chunk |
| `recall_evidence` (empty memory) | **PASS** | `success=False`, "No chunks found" |
| `refuse` | **PASS** | all six state mutations verified |
| `list_thread_document_uploads` | **PASS** | honest "no thread yet" message |
| `healthcare_query` | **PASS** | 12.4s, real ICD-10 F41.1 answer, 1 source |
| `healthcare_npi_lookup` | **PASS** | 3.6s, 1 source — but see caveat |
| `payor_readiness` | **PASS** | 304ms, real readiness figures |
| `fetch_document` | **PASS** | 1.6s, 3 real matches with document_ids |
| `document_upload_skill` | **PASS** | 2ms, canned markdown, zero side effects |
| `search_uploaded_document` | **correct refusal** | "No uploads on this thread" |
| `cached_answer_lookup` | **COULD NOT CHECK** | Chroma host unreachable from here |

Untested and **not chat's**: `product_feedback`, `product_help_search`, `vibe`.

## The structural finding: chat has TWO dispatch paths

This is the thing worth carrying, because it changes how the untestable set
should be described.

Only **20** tools are registry skills. These are **not**:

```
healthcare_npi_lookup   recall_evidence   refuse
search_uploaded_document   ingest_url
```

They are handled inline in `react_loop._execute_tool`, so `registry.dispatch()`
returns `Unknown skill: '<name>'` for every one of them. They are not missing and
not broken — they live on the other path. Anything testing chat tools has to know
which path a tool is on, or it will record a live tool as absent.

`healthcare_npi_lookup` is the clearest case: it is an **alias**, mapped to
`healthcare_query` at `react_loop.py:823`. Through `dispatch()` it does not
exist; through `_execute_tool` it answers in 3.6s.

## My own harness produced a false PASS, and it is the failure we keep finding

The first run marked `healthcare_npi_lookup` **PASS** — on a response whose text
was `Unknown skill: 'healthcare_npi_lookup'`. The status rule was "the call did
not raise." **No exception is not a pass.** A dispatcher that gracefully reports
an unknown tool returns a normal envelope, so absence of an error meant nothing.

Fixed by giving the harness three outcomes instead of two: `PASS` requires
returned content, `NOT-REG` for "Unknown skill", `EMPTY` for a call that
completed and returned nothing — which is *could-not-check*, never a pass.
`cached_answer_lookup` lands in that third bucket honestly rather than joining
the PASS column with an empty string.

## Caveats on individual results

**`healthcare_npi_lookup`** ran and answered, but did not resolve the NPI —
`1234567893` is a checksum-valid dummy, not a real provider, so it proves the
tool is *reachable and responsive*, not that NPI resolution works. A real NPI
would settle it. Reporting the limit rather than the PASS alone, because
`prompts.py:511` routes "NPI number only" here and the observed reply was
"I'm not able to identify what you're looking for with this number alone."

**`fetch_document`** — this run **deliberately wrote one `search_events` row**
in rag, the telemetry hazard from the consequence declarations. One known test
row, disclosed, is different from an undisclosed speculative one; but it is the
reason this tool cannot be swept repeatedly without polluting the lexicon queue.

**`cached_answer_lookup`** — Chroma (`34.170.243.161:8000`) is not reachable
from this machine; the first attempt hung **75 seconds** before failing, the
second failed in 4.2s. That is a could-not-check about *my* network position, not
a verdict on the tool. Worth a separate look: a cache lookup that can block a
turn for 75s on an unreachable collection is a latency hazard independent of
whether the cache works. `CACHE_ASSIST_ENABLED=0` in this environment, which
independently corroborates Governor's measurement.

**`document_upload_skill` does not belong in the irreversible group.** It returns
canned markdown in 2ms — no network, no DB, no byte transfer (its own
description says it "does NOT transfer bytes"). I never declared it irreversible;
it should move to the safe set.
