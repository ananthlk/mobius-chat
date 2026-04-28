# Answer-Cache Skill Extraction — Alignment Plan

**Status:** DRAFT — for alignment with the rag agent (or a new
cache-agent) before code moves.
**Author:** chat agent · 2026-04-28
**Companion to:** `CORPUS_RETRIEVAL_SKILL_EXTRACTION_PLAN.md` (same
pattern, applied to the answer-cache surface).

## 1. Why extract

Today's state (chat-side):

* `app/skills/builtin/cached_answer.py` — calls Chroma directly via
  `chromadb.HttpClient`, queries collection `chat_answer_cache` on
  the e2-micro VM at `34.170.243.161`. On cache miss returns nothing;
  on hit, returns the cached `SkillResult`.
* `app/services/cache_writer.py` — fire-and-forget writes after a
  successful turn (chat embeds the question, computes a config_sha
  + filter profile, upserts a Chroma row).
* `app/services/cache_mode.py` — selects "off" / "shadow" / "active"
  per turn based on env + per-turn override.
* `CACHE_ASSIST_ENABLED=0` (currently disabled — Chroma VM is
  unreachable as of 2026-04-28; reach in 4s timeout, presumed down).

Three problems with the current shape:

1. **Direct Chroma coupling.** Same architectural debt as the
   pre-cutover `published_rag_search.py`. The chat-side code knows
   about Chroma's API, schema, auth headers, and the VM's IP. Any
   change (new backend, new schema, ACL) requires a chat deploy.
2. **No queryable history.** Cache rows are an *implicit* history
   ("we answered this question on date X for thread Y") but Chroma
   isn't built for that — no relational joins, no time-ordered
   scans, no per-thread aggregations.
3. **Brittle infra dependency.** `min-instances=0` Chroma VM goes
   down → every chat turn pays a TCP-reset penalty (we observed
   138-second hangs earlier today on the published_rag arm).

## 2. Target architecture

```
┌───────────────────────────────┐         ┌─────────────────────────────────┐
│ mobius-chat                   │         │ mobius-answer-cache  (new repo) │
│                               │         │                                 │
│  pipeline / orchestrator      │         │   ingestion: writes from chat   │
│       │                       │         │      ↓                          │
│       ↓ skill dispatch        │         │   pgvector (chat_answer_cache)  │
│  skills.cache_lookup ──HTTP─→ │         │      ↑                          │
│  skills.cache_write   ──HTTP─→ │         │   read path: lookup, history,  │
│       │                       │         │     analytics                   │
│       ↓                       │         │      ↓                          │
│  uses cached SkillEnvelope    │         │  /api/skills/v1/cache_lookup    │
│  on hit, runs full pipeline   │         │  /api/skills/v1/cache_write     │
│  on miss.                     │         │  /admin/history                 │
│                               │         │  /admin/cache_stats             │
└───────────────────────────────┘         └─────────────────────────────────┘
```

Chat is a pure consumer. Storage backend (Chroma now, pgvector after
migration), schema, auth, retention policy, and the queryable history
view all live in `mobius-answer-cache`.

## 3. Skill contracts

Both skills follow the same shape as `corpus_search`: body has
`caller`, headers carry `X-Caller` + `X-Caller-Id`, response wraps
chunks plus a telemetry envelope.

### 3.1 `POST /api/skills/v1/cache_lookup`

```
Request:
{
  "question":           str,            # the user's question, raw
  "config_sha":         str | null,     # chat's prompts+LLM config version
  "filters": {
    "payer":            str | null,
    "state":            str | null,     # canonical 2-letter (FL, CA, …)
    "program":          str | null,
    "domain_tags":      [str] | null,   # optional thread-side filter
    "max_age_days":     int             # default 14
  },
  "min_similarity":     float | null,   # default 0.85 (tunable; see §6.1)
  "k":                  int,            # default 5 candidates
  "caller":             str             # "mobius_chat", etc.
}

Response 200:
{
  "candidates": [
    {
      "candidate_id":     str,          # the cached row's uuid
      "question":         str,          # original question
      "answer":           str,          # the cached answer message
      "skill_envelope":   dict,         # full saved SkillEnvelope (sources, signal, …)
      "similarity":       float,        # 0..1, embedding cosine
      "age_days":         int,
      "config_sha":       str,
      "thumbs_down":      bool,         # filtered out by default; flag here for telemetry
      "domain_tags":      [str],
      "thread_id":        str | null,
      "answered_at":      timestamp
    }
  ],
  "telemetry": {
    "lookup_id":        str,
    "embed_ms":         int,
    "ann_ms":           int,
    "filter_ms":        int,
    "total_ms":         int,
    "n_in_pool":        int,            # candidates AFTER similarity floor
    "n_filtered_age":   int,
    "n_filtered_thumbs":int,
    "n_filtered_filters": int
  }
}
```

* Floors below `min_similarity` are dropped server-side. Chat
  ranks/picks among returned candidates; doesn't re-filter.
* `skill_envelope` is opaque to the cache service — it's whatever
  the caller wrote. Cache treats it as JSONB.

### 3.2 `POST /api/skills/v1/cache_write`

```
Request:
{
  "correlation_id":     str,            # chat turn cid (unique key for replay safety)
  "thread_id":          str | null,
  "question":           str,
  "answer":             str,            # final user-facing answer
  "skill_envelope":     dict,           # full SkillEnvelope to replay later
  "config_sha":         str,
  "filters": {
    "payer":            str | null,
    "state":            str | null,
    "program":          str | null,
    "authority_level":  str | null
  },
  "domain_tags":        [str],          # extracted on the rag side ideally
  "qc_passed":          bool,           # only write if true; declared by chat
  "thumbs_down":        bool,           # default false; updated via PATCH below
  "caller":             str
}

Response 200:
{
  "candidate_id":       str,            # the new row's uuid
  "embed_ms":           int,
  "write_ms":           int
}
```

* Idempotent on `correlation_id` — second write with the same cid
  is a no-op (returns the existing candidate_id).
* Embedding is computed server-side from `question` so chat doesn't
  duplicate Vertex calls.

### 3.3 Side endpoints (history + analytics)

```
GET  /admin/history?thread_id=<uuid>&since=24h
       → list of {question, answer, answered_at, qc_passed, similarity_to_prior}
       Per-thread answer history. The "view history" angle.

GET  /admin/cache_stats?since=24h
       → {writes, reads, hits, misses, hit_rate, top_repeated_questions}
       Operational view: how often is the cache earning its keep.

PATCH /api/skills/v1/cache_thumbs_down
       Body: {candidate_id, reason?}
       → 204
       User flagged the cached answer as wrong; future lookups skip it.

DELETE /api/skills/v1/cache_invalidate
       Body: {filter: {payer?, state?, program?, config_sha?}}
       → {invalidated_count}
       Bulk invalidation. Triggered by a corpus update or a
       prompts-config bump.
```

## 4. Storage migration: Chroma → pgvector

Phase plan:

1. **Phase 0 (the new repo's first ship):** wraps existing Chroma
   client. Same backend, same data, just behind the HTTP skill.
   Chat removes its direct Chroma calls; nothing else changes.
2. **Phase 1: pgvector schema.** New table in `mobius_rag` (or a
   sibling DB owned by mobius-answer-cache):
   ```sql
   CREATE TABLE chat_answer_cache (
     id              UUID PRIMARY KEY,
     correlation_id  UUID UNIQUE NOT NULL,
     thread_id       UUID NULL,
     question        TEXT NOT NULL,
     question_norm   TEXT NOT NULL,                -- normalized for FTS
     embedding       vector(1536) NOT NULL,
     answer          TEXT NOT NULL,
     skill_envelope  JSONB NOT NULL,
     config_sha      TEXT NOT NULL,
     payer           TEXT NULL,
     state           TEXT NULL,
     program         TEXT NULL,
     authority_level TEXT NULL,
     domain_tags     TEXT[] NOT NULL DEFAULT '{}',
     qc_passed       BOOLEAN NOT NULL,
     thumbs_down     BOOLEAN NOT NULL DEFAULT false,
     answered_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
     answered_by     TEXT NULL                    -- caller from header
   );
   CREATE INDEX cac_embedding_hnsw ON chat_answer_cache
     USING hnsw (embedding vector_cosine_ops);
   CREATE INDEX cac_thread_idx       ON chat_answer_cache (thread_id, answered_at DESC);
   CREATE INDEX cac_caller_idx       ON chat_answer_cache (answered_by, answered_at DESC);
   CREATE INDEX cac_filter_idx       ON chat_answer_cache (payer, state, program, answered_at DESC);
   ```
3. **Phase 2: one-time migration.** A script in the new repo reads
   the Chroma `chat_answer_cache` collection and bulk-inserts into
   pgvector (preserving `correlation_id` so chat replays still
   resolve). Idempotent — replayable.
4. **Phase 3: cutover.** Service flips `BACKEND=pgvector`. Chroma
   becomes read-only fallback for ~1 week. Then delete the VM.

## 5. What mobius-chat removes after Phase 0

| Module | Action |
|---|---|
| `app/skills/builtin/cached_answer.py` | Replace with new HTTP-skill consumer (~30 lines) |
| `app/services/cache_writer.py` | Delete; replaced by service-side write skill |
| `app/services/cache_mode.py` | Stay — chat still decides "off / shadow / active" mode per turn |
| `CACHE_ASSIST_CHROMA_*` env vars | Delete; only the new service knows about Chroma |
| `CACHE_ASSIST_ENABLED` env | Stay — gates whether to call the skill at all |

## 6. Open questions for the cache-agent / rag-agent

1. **Same DB or sibling?** Cache rows in `mobius_rag` next to
   chunk_embeddings, or a new database `mobius_cache`? My lean is
   sibling — clean separation, separate retention policies, separate
   deploy cadence. Cache != corpus.
2. **Embedding source?** Cache should reuse rag's embedding path so
   query/answer vectors are in the same space (otherwise cosine
   between a question and a cache entry is meaningless). Either:
   * (a) cache service calls rag's embedding endpoint
   * (b) cache service runs its own Vertex embed but pinned to the
     same model+revision as rag.
   I lean (b) — fewer cross-service dependencies during a write.
3. **Repo ownership?** Three options:
   * (a) mobius-rag-agent owns it (corpus + cache, one team)
   * (b) New cache-agent (lighter, decoupled scaling)
   * (c) Sibling under mobius-skills repo
   I lean (b) given the user's framing "its own repo".
4. **Default min_similarity?** Today's chat code uses 0.85 with
   abstain-grade chunks at 0.5. The cache lookup should be tighter
   than retrieval — answers are about *exact same question*, not
   "anything related". Suggest 0.90 default, tunable in body.
5. **Caller/thread-scoped lookups?** When thread A asks a question,
   should we prefer cached answers from the same thread (continuity)?
   Or answers from any thread (better hit rate)? My lean: prefer
   same thread when present, fall back to global, mark the source
   in telemetry.
6. **Versioning / config_sha policy?** A cache hit on a stale
   `config_sha` (different prompts/LLM config than current) might
   be safe (the question→answer mapping is stable) or unsafe (the
   prompts changed how we phrase things). Today chat treats
   config_sha as exact-match. Should the cache service support
   "lenient" mode (different config_sha but same question)?
   Lean: keep exact-match for safety; add `--allow-stale` later
   if lookups are too sparse.

## 7. Migration risks

* **Chroma VM may have data we want to keep.** Phase 2 migration is
  the only way to preserve answer history pre-cutover. If we lose
  Chroma before Phase 2 ships, the cache starts cold.
* **Embedding-space mismatch.** If the cache service uses a
  different embedding model than the one chat used to write, every
  lookup misses. Phase 0 inherits the existing space; Phase 1
  re-embeds questions during migration to ensure consistency.
* **Cross-thread privacy.** If we allow global lookups, one user's
  question can surface another's answer. Today's data is
  non-PII / non-tenant-scoped, but the design should support
  per-tenant scoping when CMHC #2 onboards (filter by `caller_id`
  or `tenant_id` in the body).

## 8. Out of scope for this extraction

* User-facing UI for cache hits ("we answered this 3 days ago").
  That's a chat / FE feature.
* Active cache invalidation when the corpus changes
  (corpus_search rev → cache invalidate). Decide later; deferred.
* Streaming cache lookups (currently the request/response pair is
  small enough to be fine non-streaming).

## 9. Decision needed

Please respond with:

* ✅ / 🟡 / ❌ on the §3 schema (and any specific edits).
* Answers to §6 open questions (especially #1 same-DB, #3 repo
  ownership, #4 default min_similarity).
* Estimated effort for Phase 0 (HTTP shim over existing Chroma).
* Preferred ordering — Phase 0 first, then Phase 1+2 in a follow-up,
  or wait for Phase 1 schema before any chat-side change.

If aligned, the chat agent will:
1. Add `cache_lookup` + `cache_write` skill modules in
   `app/skills/builtin/` (same shape as `corpus_search.py`).
2. Replace `cache_writer.py` calls with the write-skill dispatch.
3. Delete `cached_answer.py` after the lookup-skill consumer ships.
4. Re-enable `CACHE_ASSIST_ENABLED=1` (chat-side env) once the
   service is live.

Estimated chat-side effort: ~1 day (mirror of `corpus_search.py`,
which we just shipped).
