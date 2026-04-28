# mobius-answer-cache

**Status:** Carve-out bundle from `mobius-chat`. Not yet a deployed
service. Hand this directory to a new repo and follow the steps in
[Bringing this up as a real repo](#bringing-this-up-as-a-real-repo).

A standalone skill service that owns chat's answer cache:

* **Read path (skill `cache_lookup`)** — semantic retrieval of past
  answers by question similarity, with caller-supplied filters
  (max_age_days, payer/state/program, thumbs_down, etc.). Used to
  short-circuit a chat turn when the system has already answered
  the same question recently.
* **Write path (skill `cache_write`)** — writes a turn's final
  answer + sources + metadata after a successful turn so future
  lookups can find it.
* **History (admin endpoints)** — the cache rows are also a
  queryable history of every turn the system answered ("show me
  everything thread X has asked", "top repeated questions this
  week", "what's the cache hit rate per caller").

## Why this is being extracted

Today's chat code talks to ChromaDB directly via the `chromadb`
Python client (host `34.170.243.161:8000`, collection
`chat_answer_cache`). Three problems:

1. **Direct backend coupling.** Chat imports `chromadb`, knows
   about Chroma's `where`-clause syntax, auth headers, schema. Any
   backend or schema change is a chat code change.
2. **No queryable history.** Cache rows are a *de facto* history
   ("we answered this on date X for thread Y") but Chroma is
   wrong-shaped for that — no relational joins, no time-ordered
   scans, no per-thread aggregations.
3. **Brittle infra dependency.** The Chroma VM has been unstable
   (138-second TCP-reset hangs killed chat turns earlier this
   week). With cache disabled, chat keeps working; with it enabled
   on a wobbly Chroma, every turn pays the timeout.

## What's in this bundle

```
mobius-answer-cache/
├── README.md                              ← you are here
├── docs/
│   ├── SPEC.md                            ← API spec (skill contract)
│   └── INTEGRATION.md                     ← how mobius-chat calls this service
├── app/
│   ├── main.py                            ← FastAPI service skeleton
│   ├── config.py                          ← env settings
│   ├── embedding.py                       ← Vertex embed wrapper
│   ├── skills/
│   │   ├── cache_lookup.py                ← read-path handler (lifted from chat)
│   │   ├── cache_write.py                 ← write-path handler (lifted from chat)
│   │   └── _filters.py                    ← shared filter parsing
│   └── backends/
│       ├── base.py                        ← backend interface
│       ├── chroma.py                      ← Phase 0: existing Chroma client
│       └── pgvector.py                    ← Phase 1: pgvector backend (stub)
├── migrations/
│   └── 001_chat_answer_cache.sql          ← Phase 1 pgvector schema
├── scripts/
│   └── migrate_chroma_to_pgvector.py      ← Phase 2 one-time migration
├── Dockerfile                             ← Cloud Run image
├── requirements.txt                       ← Python deps
└── pyproject.toml                         ← package metadata
```

## Phased rollout

The migration is staged so chat keeps working at every step. None of
the phases require a chat-side code change beyond the initial
integration in Phase 0.

### Phase 0 — HTTP shim over existing Chroma (1-2 days)

Goal: cache lives behind an HTTP API, but the data + backend are
unchanged. Chat stops importing `chromadb` and starts calling
`POST /api/skills/v1/cache_lookup` and `POST /api/skills/v1/cache_write`
instead.

What ships:
1. This service, deployed to Cloud Run with `BACKEND=chroma` env.
2. Service connects to the same `34.170.243.161` Chroma VM with
   the same `chat_answer_cache` collection. Zero data migration.
3. Chat removes `app/skills/builtin/cached_answer.py` +
   `app/services/cache_writer.py`, replaces them with thin HTTP
   skill consumers (mirroring `app/skills/builtin/corpus_search.py`,
   already shipped).

When this lands, `CACHE_ASSIST_ENABLED=1` can be flipped on without
introducing new failure modes — chat is no longer importing the
fragile Chroma client at the request path; it's an HTTP call to a
service the cache-agent owns.

### Phase 1 — pgvector schema + dual-write (2-3 days)

Goal: new backend exists in parallel, every write goes to both
backends, lookups still read Chroma.

What ships:
1. `migrations/001_chat_answer_cache.sql` runs against
   `mobius_rag` (or a sibling `mobius_cache` DB — see open
   question §6.1 of `docs/SPEC.md`).
2. `BACKEND=dual` env: writes go to Chroma AND pgvector. Lookups
   read Chroma. Latency increase on writes is fine (writes are
   already async / fire-and-forget on the chat side).

When this lands, pgvector has fresh data flowing in; Chroma is
still authoritative. Failure of pgvector writes is logged but
non-fatal.

### Phase 2 — one-time migration + cutover (3-4 days)

Goal: pgvector becomes authoritative; Chroma read-only fallback.

What ships:
1. `scripts/migrate_chroma_to_pgvector.py` reads every Chroma row
   and bulk-inserts into pgvector. Idempotent (keyed on
   `correlation_id`), replayable until the diff is zero.
2. `BACKEND=pgvector_primary` env: lookups read pgvector first,
   fall back to Chroma if pgvector miss. Writes go to pgvector
   only.
3. Validation: run a 24h dual-read window in shadow mode (read
   both, return pgvector's result, log diffs) before flipping the
   default.

### Phase 3 — retire Chroma (1 day)

`BACKEND=pgvector_only`. Delete the Chroma VM + every reference to
`CHROMA_*` env vars in this service. The `app/backends/chroma.py`
module stays in tree as a reference for ~1 month, then gets
removed.

## Bringing this up as a real repo

```bash
# 1. Move the bundle out of mobius-chat
git -C /Users/ananth/Mobius/mobius-chat mv extraction-bundles/mobius-answer-cache /tmp/
mv /tmp/mobius-answer-cache /Users/ananth/Mobius/mobius-answer-cache

# 2. Initialize the new repo
cd /Users/ananth/Mobius/mobius-answer-cache
git init
git add .
git commit -m "Initial carve-out from mobius-chat (Phase 0 scope)"

# 3. Wire deploy: copy mobius-rag's deploy/ as a starting template
#    (same Cloud Run + Cloud SQL pattern, fewer moving parts)
cp -r ../mobius-rag/deploy ./deploy
# edit deploy/deploy_cloudrun_dev.sh — service name → mobius-answer-cache
# edit deploy/cloudbuild.yaml — image path

# 4. First deploy
bash deploy/deploy_cloudrun_dev.sh

# 5. After it's running:
#    - chat agent updates RAG_API_URL pattern to also expose
#      MOBIUS_CACHE_URL=https://mobius-answer-cache-... in dev.env
#    - chat agent adds cache_lookup + cache_write skills (mirror
#      the corpus_search skill — see docs/INTEGRATION.md)
#    - flip CACHE_ASSIST_ENABLED=1 on chat
```

## What chat will do once Phase 0 is live

1. Add `MOBIUS_CACHE_URL` env on chat (placeholder until this
   service deploys, then real URL).
2. Add `app/skills/builtin/cache_lookup.py` — HTTP client that
   POSTs to `/api/skills/v1/cache_lookup`. Mirrors the shape of
   `app/skills/builtin/corpus_search.py` already in chat.
3. Add `app/skills/builtin/cache_write.py` — same pattern for the
   write path. Called fire-and-forget after `_publish_completed`
   (replaces today's `app/services/cache_writer.py`).
4. Delete `app/skills/builtin/cached_answer.py` and
   `app/services/cache_writer.py` (the carved-out code).
5. Flip `CACHE_ASSIST_ENABLED=1`.

Total chat-side: ~1 day. Patterns are well-rehearsed —
`corpus_search` already shipped this exact shape.

## Where to find the originals (for git history)

The bundle's modules were copied from these chat files. Use these
paths if you need the full original git history during development:

| In bundle | Original |
|---|---|
| `app/skills/cache_lookup.py` | `mobius-chat/app/skills/builtin/cached_answer.py` |
| `app/skills/cache_write.py` | `mobius-chat/app/services/cache_writer.py` |
| (cache mode selector stays in chat, not extracted) | `mobius-chat/app/services/cache_mode.py` |

## Open questions (in `docs/SPEC.md` §6)

The new agent should resolve these before Phase 1:

* Same DB as `mobius_rag` or sibling `mobius_cache`?
* Embedding source: reuse rag's embed endpoint or run own Vertex client?
* Repo ownership: cache-agent vs rag-agent?
* Default `min_similarity` for lookups (lean: 0.90 — tighter than retrieval).
* Per-thread vs global lookups (continuity vs hit rate trade-off).
* `config_sha` policy: exact-match-only (today) or lenient mode.

These are noted in the spec doc; the plan doc on the chat side
proposed leans for each.

## Deploy + ops checklist (Phase 0)

* [ ] Cloud Run service: `mobius-answer-cache` (us-central1,
      mobius-os-dev project), min-instances=1
* [ ] VPC connector: `mobius-dev-vpc-connector` (so pgvector / DB
      access works for Phase 1 without re-deploy)
* [ ] Secrets: `chroma-auth-token` (existing), `db-password`
      (existing — for Phase 1 readiness)
* [ ] Env vars: `BACKEND=chroma`, `CHROMA_HOST=34.170.243.161`,
      `CHROMA_PORT=8000`, `CHROMA_AUTH_TOKEN=<secret>`,
      `CACHE_COLLECTION=chat_answer_cache`,
      `VERTEX_PROJECT_ID=mobius-os-dev`,
      `EMBEDDING_PROVIDER=vertex`,
      `EMBEDDING_MODEL=gemini-embedding-001`
* [ ] Health endpoint: `GET /health` returns 200 if Chroma is
      reachable
* [ ] Skill auth: none for dev (open within project); add
      `X-Skill-Token` header check before prod
