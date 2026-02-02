# RAG DB

## Published RAG (production) – Vertex AI + Postgres

**Source:** MOBIUS-DBT BigQuery mart (synced on schedule).  
**Contract:** MOBIUS-DBT repo `docs/CONTRACT_MOBIUS_CHAT_PUBLISHED_RAG.md`.

- **published_rag_metadata** – Metadata only (no embeddings in Postgres). One row per published chunk/fact. Columns: id (PK, link to Vertex), document_id, source_type, source_id, text, page_number, all document_* fields, content_sha, updated_at, etc.

**source_type and retrieval:** Chat uses intent (canonical vs factual) to blend retrieval. For **hierarchical** retrieval we ask the **vector DB** (Vertex) for neighbors with `source_type` in `[policy, section, chunk]` via a Namespace filter—so the index must expose a filterable namespace named `source_type` and the sync must write it when upserting. If the index has no `source_type` namespace, the hierarchical query returns 0 and we fall back to fetch-then-sort in code (and you’ll see “Falling back to fetch-then-sort” in logs). Postgres `published_rag_metadata.source_type` is still used for display and for the in-code fallback sort. To get true hierarchical retrieval at query time, the MOBIUS-DBT / sync pipeline must (1) populate `source_type` in Postgres and (2) add `source_type` as a restrict namespace when writing vectors to Vertex.
- **sync_runs** – Audit table for sync runs (run_id, started_at, finished_at, row counts, status).
- **Embeddings:** In **Vertex AI Vector Search** (1536 dims). Search: embed query (1536) → query Vertex with filters → get ids → fetch metadata from `published_rag_metadata` by id.

### Apply schema

**Standard:** Migrations run automatically at the start of `./mstart`. They apply all `.sql` files in `db/schema/` in order using `CHAT_RAG_DATABASE_URL` from `mobius-chat/.env`. No manual step needed when using mstart.

**Manual (first-time or without mstart):**

```bash
psql -h <host> -U <user> -d mobius_chat -f db/schema/002_published_rag_metadata.sql
```

See [docs/PUBLISHED_RAG_SETUP.md](../docs/PUBLISHED_RAG_SETUP.md) for full setup (Vertex index, endpoint, handoff to MOBIUS-DBT).

## Legacy: Local cloned RAG (deprecated)

**Schema:** `001_rag_schema.sql` (documents, chunks, chunk_embeddings with pgvector 768 dims).

This path is **deprecated**. Chat uses only Vertex AI (1536) + `published_rag_metadata`. The scripts `app/db/copy_from_rag.py` and `app/db/seed.py` target the old schema and are kept for reference or one-off migration only.

## Run Postgres locally (for published_rag_metadata)

```bash
docker compose up -d
```

Set in `.env`:

```bash
CHAT_RAG_DATABASE_URL=postgresql://mobius:mobius@localhost:5433/mobius_chat
```

Apply schema 002 (see above). Data is populated by the MOBIUS-DBT sync job, not by this repo.
