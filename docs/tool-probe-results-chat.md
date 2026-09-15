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

---

## Round 2 — the tools Ananth authorized

| tool | result | evidence |
|---|---|---|
| `transform_previous_answer` | **PASS** | 17.1s, produced a real 1,336-char formal appeal letter from a seeded prior answer, 1 source |
| `web_scrape` | **PASS** | 1349ms against **our own** `/health`; returned `{"status":"ok"}`, 1 source, `signal=google_only` |
| `ingest_url` | **NOT RUN** | the authorization's condition is not satisfiable — see below |

### `ingest_url` was authorized *into a disposable corpus*. There isn't one.

`ImportFromHtmlRequest` (`mobius-rag/app/main.py:9180`) takes `url`, `html`,
`title`, `payer`, `state`, `program`, `authority_level`, and run-provenance
fields. There is **no dry-run flag and no corpus/collection selector**: every
import lands in the one live corpus. So the conditional authorization does not
attach to any reachable configuration, and the tool stays untested rather than
being run against the corpus on a generous reading of the condition.

To test it for real, one of these has to exist first: a dry-run mode on
`/documents/import-from-html`, or a scratch corpus the importer can target.
Both are rag-side changes, not chat's.

### My harness produced a **second** false PASS

`web_scrape` first reported **PASS** on text reading
`Error: WEB_SCRAPER_URL not configured`. The round-1 fix had made `PASS` require
*non-empty text* — and a graceful error message is non-empty text. The first
false pass came from "it didn't raise"; the second from "it returned something".

Both are the same mistake: **classifying on the shape of the response instead of
on what the response says.** The rule now checks the content (`Error:`,
"not configured", "failed") and the signal, not merely that bytes came back.
After setting `WEB_SCRAPER_URL` from the deployed config, `web_scrape` passed
for real — 1349ms, 1 source, actual page content.

Worth stating plainly because it happened twice in one session, in a harness
written by someone who spent the same session telling other seats that absence
of an error is not a result.

## Safe scope for the five task tools — what the code supports

No test org exists: all 200 tasks currently in the service are `org_name
= _shared_`, and there is no `-al` marker convention in the task-manager code
(it is a fleet convention, not enforced there).

Two candidate scopes, with their real costs:

1. **`audience="developer"` on `_shared_`** — chat's user-facing Tasks surface
   (`mobius-chat/app/api/tasks.py:56`) defaults to `audience="user"` and only
   returns developer rows when a caller explicitly asks for `developer` or
   `all`. So a developer-audience task does not appear on the surface a human
   reads. **Caveat that must not be dropped:** run-scoped reads (`run_id` set)
   *drop the audience filter*, so such a task is still visible in pipeline/run
   views.
2. **A throwaway `org_name`** — accepted, because `org_registry.check_and_stamp`
   fails open for unknown orgs. But it calls `_log_unresolved`, which INSERTs an
   `org_not_registered` row into `task_validation_log`. That trades a visible
   task for a polluted diagnostic table; it is not the cheaper option it looks
   like.

Recommendation: (1), with the pass condition being **the row is present when
queried back from the real service**, never the call returning `success=True` —
the stub returns `success=True` with nothing created, so a test that trusts the
return value would pass against a service that did nothing.
