# RAG population agent — integration contract with Mobius Chat (dev)

**Audience:** the agent / job that writes documents into the Mobius Chat retrieval stack.

**TL;DR:** dev does **not** use Vertex Vector Search or pgvector. It uses **ChromaDB (hosted on a GCE VM) for embeddings** and **Cloud SQL Postgres (`mobius_chat.published_rag_metadata`) for metadata**. Both are up, reachable, and populated with 1168 vectors as of 2026-04-24. The RAG agent doesn't need `VERTEX_INDEX_ID` — that path is disabled on dev.

---

## 1. The two stores

### 1a. ChromaDB (embeddings + HNSW index)

| Field | Value |
|---|---|
| Host | `34.170.243.161` |
| Port | `8000` |
| SSL | off (internal GCE VM) |
| Auth header | `X-Chroma-Token: <secret>` |
| Auth secret | Secret Manager: `chroma-auth-token` (project: `mobius-os-dev`) |
| Collection | `published_rag` |
| Vector dim | **1536** |
| Embedding model | Vertex `gemini-embedding-001` with `output_dimensionality=1536` |
| Distance | cosine |

**Fetch the token locally:**
```bash
export CHROMA_AUTH_TOKEN=$(gcloud secrets versions access latest \
  --secret=chroma-auth-token --project=mobius-os-dev)
```

**Verify it's up:**
```bash
curl -s http://34.170.243.161:8000/api/v2/heartbeat
# → {"nanosecond heartbeat": ...}
```

### 1b. Cloud SQL Postgres (per-chunk metadata)

| Field | Value |
|---|---|
| Instance | `mobius-os-dev:us-central1:mobius-platform-dev-db` |
| Database | `mobius_chat` |
| Table | `published_rag_metadata` |
| Password secret | Secret Manager: `db-password` (env: `CHAT_DB_PASSWORD`) |

**Chat reads Postgres via the Cloud SQL Unix socket:**
```
postgresql+psycopg2://postgres@/mobius_chat?host=/cloudsql/mobius-os-dev:us-central1:mobius-platform-dev-db
```
(password injected from Secret Manager at runtime — don't put it in the URL)

**For the RAG agent writing from outside Cloud Run:** use the Cloud SQL Auth Proxy or IP allowlisting + password auth. Set the env var as:
```bash
export CHAT_DATABASE_URL='postgresql+psycopg2://postgres:<password>@127.0.0.1:5432/mobius_chat'
```
(adjust host/port for whatever proxy mode you're running)

---

## 2. The linking contract — critical

**Every chunk has a UUID `id` that MUST be identical in both stores.** Chat does a vector search in Chroma → gets back IDs → looks those IDs up in Postgres to pull metadata.

```
Chroma id (string UUID)  ==  published_rag_metadata.id (uuid PK)
```

If the IDs drift, chat gets a hit from Chroma but can't find the metadata → the chunk silently disappears from results.

**Bootstrap a new chunk:**
```python
import uuid
chunk_id = str(uuid.uuid4())
# use `chunk_id` as BOTH the Chroma vector id AND the Postgres PK
```

---

## 3. `published_rag_metadata` schema (Postgres)

```sql
CREATE TABLE published_rag_metadata (
    id                        UUID PRIMARY KEY,        -- matches Chroma id
    document_id               UUID NOT NULL,           -- parent doc id
    source_type               TEXT NOT NULL,           -- 'chunk'|'hierarchical'|'policy'|'section'|'fact'
    source_id                 UUID NOT NULL,
    model                     TEXT,                    -- e.g. 'gemini-embedding-001'
    created_at                TIMESTAMPTZ NOT NULL,
    text                      TEXT,                    -- chunk content (for display)
    page_number               INT,
    paragraph_index           INT,
    section_path              TEXT,
    chapter_path              TEXT,
    summary                   TEXT,
    document_filename         TEXT,
    document_display_name     TEXT,
    document_authority_level  TEXT,                    -- 'official'|'guidance'|'informational'
    document_effective_date   TEXT,
    document_termination_date TEXT,
    document_payer            TEXT,                    -- 'Sunshine Health', 'Humana', ...
    document_state            TEXT,                    -- 'FL', 'GA', ...
    document_program          TEXT,                    -- 'Medicaid', 'Medicare', 'Commercial'
    document_status           TEXT,                    -- 'published'|'draft'|'archived'
    document_created_at       TIMESTAMPTZ,
    document_review_status    TEXT,
    document_reviewed_at      TIMESTAMPTZ,
    document_reviewed_by      TEXT,
    content_sha               TEXT NOT NULL,           -- dedup key on reingest
    updated_at                TIMESTAMPTZ NOT NULL,
    source_verification_status TEXT
);
```

**Filters the chat uses (set these well):**
- `document_payer` — filter by payer on per-turn queries
- `document_state` — geographic scope
- `document_program` — Medicaid / Medicare / Commercial
- `document_authority_level` — used by `source_type_allow` to favor canonical sources
- `source_type` — `hierarchical` / `policy` / `section` surface before `chunk` / `fact` in the retrieval blend

---

## 4. Chroma metadata payload

Chat's filter layer reads these keys from Chroma's per-vector metadata. **Keep them consistent with Postgres columns** — chat uses them for candidate pre-filtering before HNSW ranking:

| Chroma metadata key | Value |
|---|---|
| `document_id` | matches `published_rag_metadata.document_id` |
| `document_payer` | e.g. `"Sunshine Health"` |
| `document_state` | e.g. `"FL"` |
| `document_program` | e.g. `"Medicaid"` |
| `source_type` | `"chunk"` / `"hierarchical"` / `"policy"` / `"section"` / `"fact"` |
| `instant_rag` | **`"false"` or omit** for the approved/published corpus. `"true"` is reserved for user uploads (the `approved_only` filter drops these). |
| `document_program` | as above |
| `verification_tier` | optional; surfaces in lazy-corpus-search ranking |
| `agent_scope_tags` | optional; comma-separated tags |

**Important:** the chat code filters with `{"instant_rag": {"$ne": "true"}}` to scope to approved material. If the RAG agent writes `instant_rag="true"` on published docs, they'll never surface.

---

## 5. End-to-end write example (Python)

```python
import os
import uuid
from datetime import datetime, timezone

import chromadb
import psycopg2
from vertexai.language_models import TextEmbeddingModel
import vertexai

# ── Init ──
vertexai.init(project="mobius-os-dev", location="us-central1")
embed_model = TextEmbeddingModel.from_pretrained("gemini-embedding-001")

chroma = chromadb.HttpClient(
    host="34.170.243.161", port=8000, ssl=False,
    headers={"X-Chroma-Token": os.environ["CHROMA_AUTH_TOKEN"]},
)
collection = chroma.get_or_create_collection("published_rag")

pg = psycopg2.connect(os.environ["CHAT_DATABASE_URL"])

# ── Per chunk ──
def upsert_chunk(
    *, text: str, document_id: str, source_type: str,
    document_payer: str, document_state: str, document_program: str,
    page_number: int | None = None, paragraph_index: int | None = None,
) -> str:
    chunk_id = str(uuid.uuid4())

    # 1. Embed (1536 dim — MUST match collection)
    resp = embed_model.get_embeddings(
        [text],
        output_dimensionality=1536,
    )
    vector = resp[0].values

    # 2. Write to Chroma
    collection.upsert(
        ids=[chunk_id],
        embeddings=[vector],
        documents=[text],
        metadatas=[{
            "document_id":       document_id,
            "document_payer":    document_payer,
            "document_state":    document_state,
            "document_program":  document_program,
            "source_type":       source_type,
            "instant_rag":       "false",   # approved corpus
        }],
    )

    # 3. Write to Postgres (same id!)
    now = datetime.now(timezone.utc)
    content_sha = hashlib.sha256(text.encode()).hexdigest()
    with pg.cursor() as cur:
        cur.execute("""
            INSERT INTO published_rag_metadata (
                id, document_id, source_type, source_id, model,
                created_at, text, page_number, paragraph_index,
                document_payer, document_state, document_program,
                document_status, content_sha, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s
            )
            ON CONFLICT (id) DO UPDATE SET
                text = EXCLUDED.text,
                updated_at = EXCLUDED.updated_at
        """, (
            chunk_id, document_id, source_type, document_id, "gemini-embedding-001",
            now, text, page_number, paragraph_index,
            document_payer, document_state, document_program,
            "published", content_sha, now,
        ))
    pg.commit()
    return chunk_id
```

---

## 6. How chat queries (so you know what it expects)

1. Chat embeds the user question → 1536-dim vector via `get_query_embedding(text)`
2. Calls `collection.query(query_embeddings=[vector], n_results=k, where={...filters...})`
3. Filters use the Chroma metadata keys listed in §4
4. Chroma returns IDs + distances
5. Chat queries Postgres with `SELECT ... FROM published_rag_metadata WHERE id = ANY(%s)` to hydrate
6. Downstream `doc_assembly` step optionally reranks + labels each chunk (`process_confident` / `process_with_caution` / `abstain`)

**Known tuning issue (2026-04-24):** the rerank + confidence-label step can down-rank legitimate 0.17-distance Chroma hits to `abstain`, which then gets dropped by `MOBIUS_REACT_CORPUS_CONFIDENCE_MIN=0.3`. Chat team is instrumenting this; RAG agent doesn't need to do anything.

---

## 7. Verifying your writes

**After upserting, confirm round-trip:**

```python
# Chroma side
r = collection.query(
    query_embeddings=[vector],
    n_results=3,
    where={"document_payer": "Sunshine Health"},
)
print(r["ids"], r["distances"])

# Postgres side
with pg.cursor() as cur:
    cur.execute(
        "SELECT id, document_payer, source_type, length(text) "
        "FROM published_rag_metadata WHERE id = ANY(%s)",
        (r["ids"][0],),
    )
    print(cur.fetchall())
```

Both must return the same IDs. If Chroma has IDs that Postgres doesn't, chat will silently drop them.

---

## 8. Paths that are NOT used on dev

Ignore these — they're legacy or reserved for prod migration:

- `VERTEX_INDEX_ID`, `VERTEX_DEPLOYED_INDEX_ID` — Vertex Vector Search path is disabled (`CHAT_VECTOR_STORE=chroma` in `deploy/dev.env`).
- pgvector — no embeddings live in Postgres on any environment today.
- `mobius_rag` database — ingestion-side mart, but chat reads from `mobius_chat` (copy target).

---

## 9. Env cheat-sheet for the RAG agent

```bash
# Chroma
export CHROMA_HOST=34.170.243.161
export CHROMA_PORT=8000
export CHROMA_SSL=0
export CHROMA_COLLECTION=published_rag
export CHROMA_AUTH_TOKEN=$(gcloud secrets versions access latest \
  --secret=chroma-auth-token --project=mobius-os-dev)

# Postgres (via Cloud SQL Auth Proxy on localhost:5432)
export CHAT_DATABASE_URL='postgresql+psycopg2://postgres:<pw>@127.0.0.1:5432/mobius_chat'
# where <pw> = gcloud secrets versions access latest --secret=db-password --project=mobius-os-dev

# Vertex (for embeddings)
export GOOGLE_CLOUD_PROJECT=mobius-os-dev
export VERTEX_PROJECT_ID=mobius-os-dev
# ADC: gcloud auth application-default login   (for local dev)
```

Questions → chat team. Last verified end-to-end: 2026-04-24 (rev 00046).
