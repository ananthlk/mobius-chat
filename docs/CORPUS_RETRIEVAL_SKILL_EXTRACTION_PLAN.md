# Corpus Retrieval Skill Extraction — Alignment Plan

**Status:** DRAFT — for alignment with the rag agent before code moves.
**Author:** chat agent · 2026-04-27
**Related:** Today's pgvector cutover (rag), Chroma retirement (chat),
chunk-id-backfill hotfix (chat). This plan is the long-term shape we
should land *after* those triage fixes are verified green.

## 1. Context — why we're doing this

The pgvector cutover surfaced an architectural debt that's been latent
since Chroma was the only store. Ownership of the read path is split:

* **mobius-rag** owns ingestion (chunking, embedding, write to pgvector).
  ✓ Clean.
* **mobius-rag** *also* exposes `/api/query` which does ANN + resolves
  source_id → text + returns chunks. ✓ Lives in rag.
* **mobius-chat** owns BM25, hybrid RRF fusion, confidence labeling,
  blend selection, and the published_rag *Chroma* path that talked
  directly to the chat-owned Chroma VM. ✗ Retrieval logic on the
  consumer side, with its own embeddings store, in a different repo.

Concrete debt this produced today:

1. **Two stores, two truths.** chat's published_rag Chroma had
   phantoms (doc_ids deleted from Postgres but still in the index).
   pgvector signoff caught that.
2. **TCP hangs eating turn budgets.** chat's direct Chroma path hung
   2m18s per call when the Chroma VM was unstable, which alone burnt
   the 300s ReAct deadline. We retired those paths today.
3. **ChunkOut/source_id vs id** silent drop bug. chat's RRF dropped
   every chunk because `mobius-rag/api/query` returns `source_id` and
   chat's RRF keys on `id`. Patched today (chunk-id backfill).
4. **distance/similarity polarity lie.** ChunkOut reuses Chroma's
   `distance` field name but the value is now `1 - cosine_distance`
   (similarity). Drop-in compat at the cost of semantic clarity.

All four are symptoms of the same thing: chat is doing retrieval work.
The fix is to move retrieval entirely into mobius-rag and expose it
to chat as a *skill*, dispatched the same way as every other skill.

## 2. Target architecture

```
┌──────────────────────────────────┐         ┌──────────────────────────────────┐
│ mobius-chat                      │         │ mobius-rag                       │
│                                  │         │                                  │
│  pipeline / ReAct                │         │  ingestion (chunk + embed)       │
│       │                          │         │       ↓                          │
│       │ skill dispatch           │         │  pgvector (writes only here)     │
│       ↓                          │         │       ↑                          │
│  skills.corpus_search ───HTTP──→ │         │  /api/skills/corpus_search       │
│       │                          │         │       │ (reads only here)        │
│       ↓                          │         │       ↓                          │
│  doc_assembly + integrator       │         │  chunks → calibrate → respond    │
│  (consumes chunks)               │         │                                  │
└──────────────────────────────────┘         └──────────────────────────────────┘
```

Chat is a pure consumer. Embeddings, ANN, score-to-label calibration,
RRF (if hybrid is ever added back), and the chunk dict shape all live
in rag. Chat's only job is to know when to call which skill and how
to incorporate the chunks into the integrator prompt.

## 3. Skill contract — the API surface

Single source of truth between repos. Schema lives in mobius-rag and
is mirrored into the chat skill registry as a typed wrapper.

### 3.1 Primary: corpus search

```
POST /api/skills/corpus_search
Content-Type: application/json
Auth: X-Skill-Internal-Key: <shared secret>

Request:
{
  "query": str,                    # natural language question
  "k": int,                        # default 10, range 1..100
  "mode": "corpus" | "precision" | "recall",   # default "corpus"
  "filters": {
    "payer": str | null,
    "state": str | null,
    "program": str | null,
    "authority_level": str | null
  },
  "include_document_ids": [str] | null,
  "min_similarity": float | null   # caller floor; null = use mode default
}

Response 200:
{
  "chunks": [
    {
      "id": str,                   # was source_id; rename at boundary
      "text": str,
      "document_id": str,
      "document_name": str,
      "page_number": int | null,
      "paragraph_index": int | null,
      "source_type": "hierarchical" | "fact",
      "similarity": float,         # 0..1, 1 = identical. KILL "distance".
      "confidence_label": "high" | "medium" | "low" | "abstain",
      "retrieval_arms": [str]      # ["pgvector"], maybe ["pgvector","bm25"] later
    }
  ],
  "telemetry": {
    "mode": str,
    "k": int,
    "embed_ms": int,
    "ann_ms": int,
    "resolve_ms": int,
    "total_ms": int,
    "arms_used": [str],
    "arms_hits": { "pgvector": int, ... },
    "filter_hit_count": int        # before-rerank size
  }
}

Errors:
  400: malformed request (validation_error envelope)
  401: missing/invalid skill key
  500: backend failure (always wrapped in error_envelope so chat can
       classify into recoverable / non-recoverable for the user prompt)
```

**Why these specific fields?**
* `id` not `source_id`: drops the chat-side backfill hack.
* `similarity` not `distance`: kills the polarity lie we've been
  carrying since Chroma.
* `confidence_label` is rendered in rag (calibration moves there).
  Chat just shows it.
* `retrieval_arms` is a list, not a single string, so a future hybrid
  fusion in rag can advertise overlap without breaking the consumer.
* `telemetry` is a flat dict so chat can dump it into thinking_log
  without reshaping.

### 3.2 Secondary: thread-scoped (uploaded doc) search

```
POST /api/skills/thread_doc_search
Body: { document_id: str, query: str, k: int }
Response: same chunk shape as corpus_search, no telemetry detail
```

This replaces today's chat-side `instant_rag_search.lazy_rag_search`.
Same contract minus filters; document_id is the only filter that
matters.

### 3.3 Versioning

Path-versioned: `/api/skills/v1/corpus_search`. Bumps mean breaking
contract changes. Both repos pin to v1 until both are ready to move.

## 4. Phased migration

### Phase 0 — verify the patched path (already in flight)

* Wakeup at 20:55 confirms chunk-id-backfill restored sources>0.
* Run 9-question quality bench solo against the patched path. Save as
  baseline_2026-04-27_pre-extraction.json.
* **Gate:** must show ≥7/9 questions returning sources>0 with median
  turn time <60s before we touch anything else.

### Phase 1 — design freeze (this doc)

* rag agent reviews and edits this doc.
* Both agents agree on the schema in §3.
* Open questions in §6 are resolved.
* No code changes.

### Phase 2 — implement the skill in mobius-rag (rag-led)

* New module: `mobius-rag/app/skills/corpus_search.py`
  * Pulls together: embed query (existing) + asearch (existing) +
    source_id → text resolve (existing in main.py) + calibration (new
    — moved from chat).
  * Returns the v1 schema.
* New endpoint: `mobius-rag/app/main.py POST /api/skills/v1/corpus_search`
  * Thin handler; all logic in the skill module.
* Move from chat → rag (delete on chat side after Phase 3 lands):
  * `app/services/retrieval_calibration.py` → `mobius-rag/app/skills/_calibration.py`
  * Query-side embedding helpers from `app/services/embedding_provider.py`
    (rag already has its own; dedupe and pick one)
* Auth: shared secret in Secret Manager (`mobius-skill-internal-key`,
  already exists for chat→rag LLM proxy; reuse).
* Tests in rag: contract round-trip + score polarity + calibration
  thresholds.

### Phase 3 — register skill in mobius-chat (chat-led)

* New module: `mobius-chat/app/skills/builtin/corpus_search_remote.py`
  * Implements the chat skill interface.
  * HTTP POSTs to rag's endpoint with the v1 contract.
  * Maps response to chat's internal chunk dict (chat keeps its own
    dict shape if downstream code wants `match_score` etc., but the
    boundary is the v1 contract).
* Refactor `retriever_backend.retrieve_for_chat`:
  * Becomes a thin dispatcher into the skill.
  * BM25-fallback inline path stays for now (skill unreachable → fall
    back, log loud) but is marked deprecated.
* Refactor `react_loop` tool dispatch:
  * `search_corpus`, `precision_search`, `recall_search` all call the
    same skill with different `mode` params.
  * Delete the `if False:` retired Chroma branch I left in
    `react_loop.recall_search`.
* Delete entirely:
  * `app/services/published_rag_search.py` (dead — Chroma-only)
  * `app/services/retriever_hybrid._run_vector_arm` (today's no-op)
  * `app/services/retrieval_calibration.py` (moved to rag in Phase 2)
* Defer (separate cleanup):
  * `cache_writer.py` + `cached_answer.py` — still gated off; either
    migrate to a `cache_skill` in rag or kill outright. Not on the
    critical path for this extraction.
  * `instant_rag_search.py` — wait for the secondary skill (§3.2)
    before deleting.

### Phase 4 — verify + cut over

* Smoke 5/5 on chat after deploy.
* Replay the same 9-question bench against the skill-mediated path.
* **Gate:** must match or beat the Phase-0 baseline on (a) sources/turn
  median, (b) turn-time p95, (c) zero-source rate.
* If green: tag, commit, close.
* If red: roll chat back; the skill stays live in rag (idempotent)
  and we diff what regressed.

### Phase 5 — eventually retire `/api/query`

* Once chat is fully on the skill path for ≥1 week with no rollback,
  delete `/api/query` from mobius-rag.
* The skill at `/api/skills/v1/corpus_search` is the only public read
  path; ingestion endpoints stay as-is.

## 5. Ownership matrix

| Item                                       | mobius-rag | mobius-chat |
|--------------------------------------------|:----------:|:-----------:|
| Embedding pipeline (ingest)                |     ✓      |             |
| pgvector writes                            |     ✓      |             |
| pgvector reads / ANN                       |     ✓      |             |
| Query embedding                            |     ✓      |             |
| source_id → text resolve                   |     ✓      |             |
| Score → confidence_label calibration       |     ✓      |             |
| RRF / hybrid fusion (if reintroduced)      |     ✓      |             |
| Skill HTTP surface                         |     ✓      |             |
| Skill registry + dispatch                  |            |     ✓       |
| ReAct tool wiring (`search_corpus` etc.)   |            |     ✓       |
| Doc assembly for integrator prompt         |            |     ✓       |
| User-facing rendering (sources, citations) |            |     ✓       |
| BM25 inline fallback                       |  *(later)* | *(now)*     |

## 6. Open questions for the rag agent

1. **Calibration thresholds:** today chat applies a `confidence_min`
   floor (default 0.3 since 2026-04-19, configurable via
   `MOBIUS_REACT_CORPUS_CONFIDENCE_MIN`). Should the calibration
   move to rag with mode-keyed defaults (corpus=0.3, precision=0.5,
   recall=0.0)? Or stay caller-driven via `min_similarity`?

2. **BM25:** chat currently runs a BM25 arm via `mobius-retriever`
   against Postgres text. Two options:
   * (a) BM25 also moves to rag, exposed inside `corpus_search` as a
     hybrid fusion controlled by the rag side.
   * (b) BM25 stays in chat as a separate skill, fused on chat side.
   I lean (a) — same reasoning as moving everything else: one place
   that knows about retrieval. But BM25 needs the Postgres text index
   which rag doesn't currently maintain.

3. **Embedding provider dedupe:** rag has `embed_async` for ingest;
   chat has `get_query_embedding`. Same Vertex model, different code
   paths. Phase 2 should converge — confirm rag is willing to host
   the query-side embedder too.

4. **Internal auth:** reuse `mobius-skill-llm-internal-key` (already
   wired chat→rag) or mint a separate `mobius-skill-corpus-key` so
   we can rotate retrieval auth without touching LLM proxy auth?

5. **Telemetry contract:** what fields *must* be in `telemetry` for
   rag's own dashboards? Chat will dump everything it gets into
   `thinking_log`, but rag may want guarantees so its monitoring
   doesn't break on schema drift.

6. **Idempotency / rate-limiting:** any concern about chat hammering
   the skill during 4-instance fan-out? Today `/api/query` doesn't
   rate-limit. With the skill becoming the only read path it's worth
   thinking about a per-instance budget.

## 7. Migration risks

* **Cross-repo lockstep release.** v1 contract bumps require both
  repos to deploy in order. Mitigation: keep `/api/query` alive for
  Phase 2-4 so chat can roll back instantly to the patched path.
* **Calibration parity.** When calibration moves to rag, the exact
  similarity-→-label thresholds must reproduce today's behavior on
  the same chunk inputs. Mitigation: take a snapshot of 50 chat-side
  calibrated outputs in Phase 0 and assert rag produces identical
  labels in Phase 2 tests.
* **Latency regression.** The skill adds an HTTP hop per turn, but
  today's `/api/query` already adds it — moving to the skill should
  be net-zero. Phase 4 bench is the gate.
* **Chat-side BM25 fallback erosion.** Once the BM25 inline path is
  deprecated and BM25 moves to rag, chat has no graceful degradation
  if the skill is unreachable. Mitigation: keep inline BM25 alive
  through Phase 5 with a loud log when it triggers.

## 8. Out of scope for this extraction

* Web search / Google grounding skill (separate concern).
* Cache-assist (`chat_answer_cache` Chroma) — dispositioned by the
  cache cleanup task, not this one.
* Hybrid BM25+vector fusion strategy (decide after the basic
  extraction lands; either rag-internal or chat-side).
* Cross-tenant filtering (no tenant boundaries today; revisit when
  we onboard CMHC #2).
* Streaming / chunk-by-chunk retrieval responses — current request/
  response model is sufficient for ReAct, where one round = one full
  retrieval call.

## 9. Decision needed from the rag agent

Please respond with:

* ✅ / 🟡 / ❌ on the §3 schema (and any specific edits).
* Answers to §6 open questions.
* Estimated effort for Phase 2 in your repo (mine for Phase 3 is
  ~1 day including tests + bench replay).
* Preferred ordering — happy to do Phase 2 and Phase 3 in parallel
  with a v1-stable contract, or strictly sequential?

If aligned, the chat agent will lift Phase 3 immediately after rag
ships Phase 2 (or in parallel against a stub if you prefer).
