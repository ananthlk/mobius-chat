# mobius-answer-cache · API spec

The full alignment doc lives in
`mobius-chat/docs/ANSWER_CACHE_SKILL_EXTRACTION_PLAN.md`. This file
mirrors the skill-contract sections (§3–§6) so the new agent has
the spec next to the code.

## §3 Skill contracts

### 3.1 `POST /api/skills/v1/cache_lookup`

**Request:**

```json
{
  "question":           "what is the timely filing limit for sunshine health?",
  "config_sha":         "359523480b1f",
  "filters": {
    "payer":            "Sunshine Health",
    "state":            "FL",
    "program":          "Medicaid",
    "authority_level":  "contract_source_of_truth",
    "domain_tags":      ["d:claims_processing.timely_filing"],
    "max_age_days":     14,
    "require_critic_approved": false,
    "require_no_thumbs_down":  true
  },
  "min_similarity":     0.85,
  "k":                  5,
  "caller":             "mobius_chat"
}
```

Headers: `Content-Type: application/json`,
`X-Caller: mobius_chat`, `X-Caller-Id: <uuid>` (chat turn cid).

**Response 200:**

```json
{
  "candidates": [
    {
      "candidate_id":   "8fba1cb5-…",
      "question":       "what is timely filing for sunshine health?",
      "answer":         "Sunshine Health requires participating providers …",
      "skill_envelope": {"text": "...", "sources": [...], "signal": "...", "extra": {}},
      "similarity":     0.94,
      "age_days":       2.7,
      "config_sha":     "359523480b1f",
      "thumbs_down":    false,
      "domain_tags":    ["d:claims_processing.timely_filing"],
      "thread_id":      "9a5917be-…",
      "answered_at":    "2026-04-25T15:32:11Z"
    }
  ],
  "telemetry": {
    "lookup_id":   "uuid",
    "backend":     "chroma",
    "embed_ms":    210,
    "ann_ms":      14,
    "total_ms":    240,
    "n_in_pool":   3,
    "min_similarity": 0.85,
    "k":           5,
    "caller":      "mobius_chat",
    "caller_id":   "<chat-turn-cid>"
  }
}
```

### 3.2 `POST /api/skills/v1/cache_write`

**Request:**

```json
{
  "correlation_id":  "<chat-turn-cid>",
  "thread_id":       "<chat-thread-id>",
  "question":        "what is the timely filing limit for sunshine health?",
  "answer":          "Sunshine Health requires participating providers …",
  "skill_envelope":  {"text": "...", "sources": [...], "signal": "...", "extra": {}},
  "config_sha":      "359523480b1f",
  "filters": {
    "payer":           "Sunshine Health",
    "state":           "FL",
    "program":         "Medicaid",
    "authority_level": "contract_source_of_truth"
  },
  "domain_tags":     ["d:claims_processing.timely_filing"],
  "qc_passed":       true,
  "thumbs_down":     false,
  "caller":          "mobius_chat"
}
```

Headers: same as lookup.

**Response 200:**

```json
{
  "candidate_id":   "8fba1cb5-…",
  "embed_ms":       210,
  "write_ms":       45
}
```

Idempotent on `correlation_id` — second write with the same cid
returns the existing `candidate_id` without inserting.

### 3.3 Mutations

```
PATCH /api/skills/v1/cache_thumbs_down
  Body: {"candidate_id": "…", "reason": "stale"}
  → 204
  Future lookups will skip this row (require_no_thumbs_down=true is the default).

DELETE /api/skills/v1/cache_invalidate
  Body: {"filter": {"config_sha": "old_sha"}}
  → 200 {"invalidated_count": 47}
  Bulk soft-delete on pgvector backend (sets invalidated_at).
  Bulk hard-delete on Chroma backend (collection delete).
```

### 3.4 History & analytics

```
GET /admin/history?thread_id=<uuid>&since=24h&limit=100
  → list of cache rows in the thread, time-ordered.
  Use case: "show me everything thread X has asked".

GET /admin/history?caller=mobius_chat&since=7d&limit=200
  → list of cache rows from a specific caller in the window.
  Use case: cross-thread audit ("what did chat answer this week").

GET /admin/cache_stats?since=24h
  → {"backend": "chroma", "row_count": 547, "thumbs_down_count": 3,
     "callers": {"mobius_chat": 540, "mobius_chat_bench": 7}}
  Use case: hit-rate monitoring once we add hit/miss columns.
```

`since` accepts: `15m`, `24h`, `7d`, `4w`. Default `24h`.

## §6 Open questions for the new agent

These need decisions before Phase 1 (pgvector schema). Each has a
suggested lean from the chat-side plan doc; treat them as starting
points, not commitments.

### 6.1 — Same DB or sibling?

* (a) Cache rows in `mobius_rag` next to chunk_embeddings.
* (b) New database `mobius_cache` on the same Cloud SQL instance.

Lean: (b). Cache != corpus; separate retention, separate deploy,
separate ACL story when prod auth lands.

### 6.2 — Embedding source

* (a) Cache service calls rag's embedding endpoint.
* (b) Cache service runs its own Vertex embed pinned to the same
  model+revision as rag.

Lean: (b). Fewer cross-service dependencies during a write.
Provided the embedding model is pinned the same way (env var
`EMBEDDING_MODEL=gemini-embedding-001`), the vector spaces align.

### 6.3 — Repo ownership

* (a) rag-agent owns it (corpus + cache, one team).
* (b) New cache-agent (decoupled scaling, single-purpose).
* (c) Sibling under `mobius-skills`.

Lean: (b) per the user's framing "its own repo".

### 6.4 — Default `min_similarity`

Today's chat-side cache uses 0.82. Cache lookups should be
**tighter** than retrieval — answers are about *exact same*
question, not "anything related".

Lean: 0.90 default, tunable via body. Phase 0 reproduces 0.82 for
parity; bump to 0.90 in Phase 1.

### 6.5 — Per-thread vs global lookups

When thread A asks a question, prefer cached answers from the same
thread (continuity)? Or any thread (better hit rate)?

Lean: prefer same thread when present, fall back to global. Mark
the source in telemetry (`thread_match=true|false`) so chat can
treat them differently in rendering.

### 6.6 — `config_sha` policy

A cache hit on a stale `config_sha` (different prompts/LLM than
current) might be safe (question→answer mapping is stable) or
unsafe (prompts changed how we phrase).

Lean: keep exact-match for safety. Add `--allow-stale` parameter
later if hit rate is too sparse.

## §7 Migration risks

* Chroma VM may have data we want to keep — Phase 2 migration
  preserves history before cutover.
* Embedding-space mismatch — pin `EMBEDDING_MODEL` everywhere,
  re-embed during migration if model changes.
* Cross-thread privacy — design supports per-tenant scoping in the
  body, but today's data is non-PII / non-tenant-scoped.

## §8 Out of scope

* User-facing UI for cache hits (chat / FE responsibility).
* Active corpus-change → cache-invalidate triggering. Decide later.
* Streaming lookups (request/response is small enough).
