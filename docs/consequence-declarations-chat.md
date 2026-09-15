# Consequence declarations — chat-owned tools

**From:** Orchestrator (mobius-chat)
**To:** Tool Manifest
**Re:** `docs/consequence-declaration-request.md` — `toolreg.v2.execute_tool` speculative guard
**Audited:** 2026-09-14, against mobius-chat @ f1142cc

Method, per your bar: grepped every handler for `db_execute` / `db_query` /
`INSERT` / `UPDATE` / `DELETE` / `.commit` and every outbound call site
(`urllib.request`, `httpx`, LLM provider, embedding provider), then read the
bodies. No declaration below rests on a route's verb or a name's prefix.

## Finding that applies to all 18 first

**The chat dispatch path itself writes nothing.** `app/skills/registry.py` and
the react_loop dispatcher contain zero `db_execute` / `INSERT` sites, and
`app/skills/**` contains zero `db_execute` sites in total. So consequence here
is entirely a property of each handler, not of being dispatched. (Consistent
with the standing finding that nothing writes `skill_invocations`.)

## Declarations

| tool | direction | reversible |
|---|---|---|
| `recall_evidence` | inward | **true** |
| `list_thread_document_uploads` | inward | **true** |
| `list_tasks` | inward | **true** |
| `fetch_document` | inward | NULL |
| `search_uploaded_document` | inward | NULL |
| `refuse` | inward | **false** |
| `create_task` | inward | **false** |
| `resolve_task` | inward | **false** |
| `patch_task` | inward | **false** |
| `assign_task` | inward | **false** |
| `dismiss_task` | inward | **false** |
| `transform_previous_answer` | outward | NULL |
| `cached_answer_lookup` | outward | NULL |
| `payor_readiness` | outward | NULL |
| `healthcare_query` | outward | NULL |
| `healthcare_npi_lookup` | outward | NULL |
| `web_scrape` | outward | NULL |
| `ingest_url` | outward | **false** |

**Three become preloadable.** The other fifteen stay refused, and I think
correctly.

## Basis, per tool

### inward + true

**`recall_evidence`** — `react_loop.py:2160-2190`. Reads `ctx._evidence_memory`,
an in-process list, and formats matches. No HTTP, no DB, no LLM. The handler's
own comment says "Purely local — no HTTP, no rag-budget spend" and the code
agrees with it. This is the only tool in the set with nothing to qualify.

**`list_thread_document_uploads`** — `app/skills/builtin/document_uploads.py:40-70`.
Reads `active.uploaded_files[]` from thread state, renders a markdown table via
`mobius_skills_core.skills.list_thread_uploads`. No write, no network.

**`list_tasks`** — `app/skills/task_manager/skills.py:111` → `GET /tasks` on
task-manager (our service). Audited the far side too:
`mobius-skills/task-manager/app/routers/tasks_router.py:230` →
`app/storage/tasks_pg.py:477` is SELECT plus an optional overlap join, one
warning log on failure, no INSERT/UPDATE/commit on the path. The INSERT sites in
that repo (`task_validation_log`, `mobius_task`) are all on create/validate,
none on list.

### inward + NULL

**`fetch_document`** — the interesting one, and the reason I am not sending you a
`true`. All five outbound calls go to our own services, so `inward` is solid:
`fetch_document.py:133` (rag `/documents/{id}/download/pdf`), `:225`
(`/documents/{id}/status`), `:252` (`/documents/{id}/pages`), `:703`
(`/api/skills/v1/corpus_search`), `:859` (sources `/sources/search`). Zero
chat-side writes.

But `:703` lands on rag's `corpus_search`, and
`mobius-rag/app/services/corpus_search.py:3373` writes a **`search_events` row on
every call**, fire-and-forget. Per
`mobius-rag/app/migrations/add_search_events_table.py:5`, that table exists to
feed the lexicon-management workflow: `/admin/search_events?unmatched=true`
surfaces queries whose lexicon match was empty as "the highest-leverage
candidates for new lexicon entries."

This is your `rag` hazard with a worse consumer. `rag_query_decisions` poisons a
training set; `search_events` poisons **a human work queue** — someone reads that
list and authors lexicon entries from it. A discarded speculative preload would
put a question nobody asked in front of a person deciding what vocabulary the
corpus needs, and the row cannot say it was speculative. NULL.

**`search_uploaded_document`** — routed to rag (`react_loop.py:827`) via
`lazy_rag_search`. Chat-side, the only state touched is `ctx._rag_call_rounds`,
in-process (`react_loop.py:3349-3360`). So chat adds no consequence; it inherits
rag's. Retriever's declaration governs. Flagging it as routed rather than
claiming it.

### inward + false

**`refuse`** — `react_loop.py:2137-2153`. Writes **nothing** — no DB, no HTTP, no
log row (the `compliance.hipaa_analysis_log` INSERT at `main.py:1423` is the
upload gate, a different path entirely). And it is still irreversible, because
it sets `ctx.react_bypass_integrate = True`, overwrites `ctx.final_message`,
clears `ctx.sources` and `ctx.answer_set`, and returns `is_terminal: True`.

**A speculative `refuse` would end the turn and blank the answer.** Nothing
persists and nothing is recoverable. Worth naming because your brief frames
reversibility around what a call "leaves behind", and this one leaves behind
nothing while destroying the turn — turn-state mutation is a second axis. If
`toolreg` ever grows a `terminal` flag, `refuse` is its first member. This is
the one place I am sending `false` rather than NULL as an assertion, because I
read the mutation directly.

**`create_task` / `resolve_task` / `patch_task` / `assign_task` / `dismiss_task`** —
`skills.py:213` `POST /tasks`, `:272` `POST /tasks/{id}/resolve`, `:337` `PATCH
/tasks/{id}`, `:387` `PATCH /tasks/{id}` **plus** `:389` `POST
/tasks/{id}/interact`, `:421` `POST /tasks/{id}/dismiss`. All hit task-manager
(`CHAT_SKILLS_TASK_MANAGER_URL`, ours → inward). Each creates or mutates a row a
human sees in their task list. `assign_task` also appends an audit-trail
interaction record (`tasks_router.py:417`). `false`, asserted, not NULL.

Caveat you should have: when `CHAT_SKILLS_TASK_MANAGER_URL` is unset the client
falls back to `http://localhost:8015` (`client.py:43`) and a stub path returns
`success=True` with the text "Task noted!" though nothing was created. That is a
separate open defect on my queue, not a consequence-declaration issue — but it
means a `false` here is about intent, and the failure mode is a call that claims
to have done the irreversible thing without doing it.

### outward

**`transform_previous_answer`** — `transform_previous.py:167-176` calls
`get_llm_provider().generate_with_usage(prompt)`. An LLM call: reaches past our
systems and bills. I would have guessed inward from the name — it reads a prior
answer and rewrites it — which is exactly why the audit bar is the right one. No
chat-side write; whether LLM Manager records the call is not mine to assert.
NULL.

**`cached_answer_lookup`** — `cached_answer.py:341` calls `get_query_embedding`,
which is Vertex `gemini-embedding-001` (`app/services/embedding_provider.py:24-31`).
Outward and billed. Zero writes in the skill itself: the cache write is
scheduled from `orchestrator.py:1820` at turn completion, not from the lookup,
so a lookup does not populate the cache it reads. NULL for the embed call, not
for a write.

**`payor_readiness`** — `payor.py:100` POSTs `PAYOR_API_URL
/api/skills/v1/payor_readiness`. mobius-payor is ours, but a readiness verdict is
derived from reaching a third-party payer's site (robots/crawlability), so the
effect crosses the boundary. I have not audited mobius-payor's write sites.
NULL, and bounded: my basis covers the chat call site only.

**`healthcare_query`, `healthcare_npi_lookup`** — `healthcare.py` →
`mobius_skills_core.skills.healthcare_query` → POST `HEALTHCARE_URL
/healthcare/query` (:8007). `healthcare_npi_lookup` aliases to the same backend
(`react_loop.py:823`). Our service, but its job is NPPES / ICD-10 / CMS registry
lookup — external. Not audited past :8007. NULL. **Both were deactivated from
chat's teaching surfaces in `c97a46a`** (manifest + capabilities) and remain
dispatchable, so they still need declarations; they just should never be
*selected* from chat now.

**`web_scrape`** — `web.py:35` → `tool_agent._run_web_scrape` → the web-scraper
service, which fetches somebody else's URL. Your brief already uses this as the
example. Crawler Agent owns the scraper and I have not audited its persistence.
NULL.

**`ingest_url`** — `curator_tools.py:235` POSTs rag
`/documents/import-from-html`. The module docstring at `curator_tools.py:16`
states the effects in the code: *"ingest_url has side effects (chunking,
embedding, publishing)"*. It publishes a document into the corpus. `false`,
asserted — this is the one tool in the set where a speculative call changes what
every later retrieval sees.

## Also declarable

**`cost_gated_inputs`**

- `web_scrape.scrape_mode` — textbook case. `quick` is one page; `detailed` is
  depth 5, up to 50 pages, up to 10 linked document downloads. Same tool, two
  orders of magnitude of cost, selected by one string. Refusing speculatively on
  cost when `scrape_mode` is set to `medium`/`detailed` while allowing `quick`
  would be a real distinction if `web_scrape` ever becomes preloadable.
- `cached_answer_lookup` — nothing to gate. `similarity_floor` / `max_age_days`
  filter after retrieval; the Vertex embed is unconditional. Flagging the absence
  so nobody assumes the knobs make it cheap.

**`speculative_arg`** — none of my 18 accept one today.

The one worth building is for `fetch_document`. It is otherwise the strongest
preload candidate in chat: inward, no chat-side write, and a genuinely useful
thing to have resolved before react asks. It is blocked on a single row in
another repo. Making it `true` requires a speculative flag threaded chat →
rag `corpus_search` → a `search_events` column, so the lexicon queue can exclude
unread rows. That is a cross-repo ask to the Retriever seat, not something I can
declare my way out of. Until that column exists, `fetch_document` is NULL and
should be.

## What I am not claiming

`search_uploaded_document` is routed to rag and is Retriever's to declare.
For `web_scrape`, `payor_readiness`, `healthcare_query` and
`healthcare_npi_lookup` my audit stops at chat's call site; the owning service's
writes are unaudited, which is precisely why all four are NULL and not `true`.
If any of those seats audits their side and finds nothing persisted, the value
can move — but it should move on their evidence, not my inference.

---

## Addendum — reachability of the two in-process tools

Tool Manifest asked (2026-09-14) whether `recall_evidence` and
`list_thread_document_uploads` only ever run in-process, having found them among
10 genuine gaps in `v_cleared_but_unreachable`. Both answers are yes, but the
second has a wrinkle.

**`recall_evidence` — unroutable by construction, not merely unrouted.** It is
not in the skill registry at all; it is handled inline at `react_loop.py:2160`,
before registry dispatch. It reads `ctx._evidence_memory`, which is per-turn,
per-process state written by *this turn's* earlier rounds
(`_store_evidence_memory`). An out-of-process caller would find that list empty,
so a route would return "no chunks found for refs" 100% of the time. Adding one
would create a tool that is always reachable and never useful. Record it as
in-process by design.

**`list_thread_document_uploads` — chat's builtin is in-process only, but the
capability is reachable under a different name.** `mobius-skills-mcp/app/server.py:1309`
exposes an MCP tool called **`list_thread_uploads`** (not
`list_thread_document_uploads`). It is a different implementation of the same
capability: it HTTP-GETs `{CHAT_API_BASE_URL}/chat/thread/{id}/uploads` and
formats the result through the same
`mobius_skills_core.skills.list_thread_uploads.run_list_thread_uploads`. The
chat builtin reads in-process thread state directly; the MCP tool fetches the
same records over HTTP.

The chat-side mention of the name in `app/skills/mcp_adapter.py:19` is *inbound*
collision policy — "builtins win" if an MCP server registers a colliding name —
not an outbound route. Chat does not export the builtin.

So two catalogue entries, one capability, different reachability **and**
different consequence. They should not share a declaration:

| | `list_thread_document_uploads` (chat builtin) | `list_thread_uploads` (MCP) |
|---|---|---|
| reachable over MCP | no | yes |
| implementation | in-process thread-state read | HTTP GET to chat |
| direction | inward | inward |
| reversible | true | true |

I audited the far side for the MCP one as well: `main.py:2500` `get_thread_uploads`
is a pure `get_state(tid)` read — no INSERT/UPDATE/commit — and it raises 503 on
unreadable state rather than returning an empty list, so it does not conflate
"couldn't read" with "no uploads". `inward` / `true` holds for both, on separate
evidence.
