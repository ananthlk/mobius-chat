# Published RAG Setup (Vertex AI + Postgres)

RAG for non-patient questions uses **Vertex AI Vector Search** (1536 dims) and **Postgres** `published_rag_metadata` (metadata only). The sync job (MOBIUS-DBT) reads the BigQuery mart and writes to your Postgres and Vertex. Chat only consumes after sync.

## 1. Create Postgres database and apply schema

On the Postgres server (e.g. Cloud SQL at your `POSTGRES_HOST`), create the database if it does not exist:

```bash
# Connect to the default postgres database, then create mobius_chat
psql -h <your-chat-db-host> -U postgres -d postgres -c "CREATE DATABASE mobius_chat;"
```

**Migrations run automatically:** When you start Mobius with `./mstart`, mobius-chat DB migrations run first (before any services). They apply all `.sql` files in `db/schema/` in order (001, 002, 003) using `CHAT_RAG_DATABASE_URL` from `mobius-chat/.env`. If that URL is not set, migrations are skipped and startup continues.

For first-time setup or if you are not using `mstart`, apply the schema manually. Published RAG metadata and sync audit:

```bash
psql -h <your-chat-db-host> -U <your-user> -d mobius_chat -f db/schema/002_published_rag_metadata.sql
```

With Cloud SQL Proxy:

```bash
# Start proxy first
cloud-sql-proxy <your-instance-connection-name> --port 5432

# Then apply
psql -h 127.0.0.1 -U <your-user> -d mobius_chat -f db/schema/002_published_rag_metadata.sql
```

Creates: `published_rag_metadata` (metadata only; id = link to Vertex), `sync_runs` (audit).

Chat feedback table (thumbs up/down + comment per turn):

```bash
psql -h <your-chat-db-host> -U <your-user> -d mobius_chat -f db/schema/003_chat_feedback.sql
```

Creates: `chat_feedback` (correlation_id, rating, comment, created_at).

## 2. Create Vertex AI Vector Search index

**Option A – Batch index (recommended for dev):**

MOBIUS-DBT has a script to create a batch index from the BigQuery mart:

```bash
cd /path/to/Mobius/mobius-dbt
export BQ_PROJECT=mobiusos-new BQ_DATASET=mobius_rag_dev
export GCS_BUCKET=your-bucket-name
export VERTEX_PROJECT=mobiusos-new VERTEX_REGION=us-central1
python scripts/create_vertex_batch_index.py
```

Then create endpoint + deploy in GCP Console. See MOBIUS-DBT `docs/RUN_PIPELINE_RAG_TO_CHAT_DEV.md`.

**Option B – Manual in GCP Console (Vertex AI → Vector Search):**

1. **Create Index**
   - Dimensions: **1536**
   - Distance: **Cosine**
   - Type: **Streaming** (real-time upserts) or **Batch** (periodic rebuilds)
   - **Metadata filtering:** Enabled (required for document_payer, document_state, document_program, document_authority_level)

2. **Create Endpoint** (Index Endpoints → Create)

3. **Deploy** the index to the endpoint. Note the **Deployed index ID** from the Console (e.g. `endpoint_mobius_chat_publi_1769989702095`); it may be auto-generated and differ from the display name.

4. Note **Index ID** and **Endpoint ID** (full resource name or short id).

**Hierarchical retrieval (canonical questions):** Chat asks the vector DB for neighbors with `source_type` in `[policy, section, chunk]` so we don’t filter in code. The sync job must expose **`source_type`** as a **filterable namespace** (restrict) when upserting to Vertex, with values like `policy`, `section`, `chunk`, `fact`. If the index has no `source_type` namespace, hierarchical queries return 0 and Chat falls back to fetch-then-sort in code (see worker logs: “Falling back to fetch-then-sort”).

## 3. Share connection details with MOBIUS-DBT

The sync job (MOBIUS-DBT) needs:

- **CHAT_DATABASE_URL** – Postgres for `published_rag_metadata` (e.g. `postgresql://user:pass@host:port/mobius_chat`)
- **VERTEX_PROJECT**, **VERTEX_REGION**, **VERTEX_INDEX_ID**, **VERTEX_INDEX_ENDPOINT_ID**

See MOBIUS-DBT `docs/SETUP_MOBIUS_CHAT_CONSUMER.md` and `docs/CONTRACT_MOBIUS_CHAT_PUBLISHED_RAG.md`.

## 4. Configure Mobius-Chat

In `.env` (see [ENV.md](ENV.md)):

- **VERTEX_INDEX_ENDPOINT_ID** – Vertex index endpoint (full name or short id)
- **VERTEX_DEPLOYED_INDEX_ID** – Deployed index id on that endpoint
- **CHAT_RAG_DATABASE_URL** – Same Postgres URL (published_rag_metadata)
- **VERTEX_PROJECT_ID** (or **VERTEX_PROJECT_ID**) – GCP project for Vertex
- **GOOGLE_APPLICATION_CREDENTIALS** – Service account JSON path

Optional filters: `CHAT_RAG_FILTER_PAYER`, `CHAT_RAG_FILTER_STATE`, `CHAT_RAG_FILTER_PROGRAM`, `CHAT_RAG_FILTER_AUTHORITY_LEVEL`.

## 5. Test after first sync

After MOBIUS-DBT runs the sync:

- Postgres: `SELECT COUNT(*) FROM published_rag_metadata;`
- Run a non-patient question in the chat; logs should show Vertex query and Postgres fetch.

## Troubleshooting

- **404 "Index '…' is not found"** when querying Vertex Vector Search  
  The **deployed index id** in `.env` (`VERTEX_DEPLOYED_INDEX_ID`) must match the **exact id** shown in Vertex AI Console for that endpoint. In GCP: Vertex AI → Vector Search → Index Endpoints → your endpoint → **Deployed indexes** table. Use the **ID** column (or the id you set when deploying); it can differ from the display name. If you deployed with a different id, set `VERTEX_DEPLOYED_INDEX_ID` to that value.

- **503 / "failed to connect to all addresses" or "Socket closed"** when querying Vertex Vector Search  
  The index **endpoint** may be **private** (VPC-only). Private endpoints are only reachable from the same VPC (e.g. GCE, Cloud Run). Run the worker from that VPC, or create/use a **public** index endpoint in Vertex AI so your laptop can reach it.

- **"database \"mobius_chat\" does not exist"**  
  Create the database on your Postgres server, then apply the schema. From repo root:  
  `psql -h <POSTGRES_HOST> -U postgres -d postgres -c "CREATE DATABASE mobius_chat;"`  
  then  
  `psql -h <POSTGRES_HOST> -U postgres -d mobius_chat -f db/schema/002_published_rag_metadata.sql`  
  (use your actual host and user; Cloud SQL may require authorized networks or Cloud SQL Proxy.)

- **"keys/your-service-account.json was not found"** or **"RAG: Vertex endpoint/deployed index or database URL not set"**  
  Start the worker from the **Mobius-Chat repo directory** so `.env` is loaded (e.g. `cd /path/to/Mobius/mobius-chat && mchatcw`). Ensure `.env` has `GOOGLE_APPLICATION_CREDENTIALS` and the RAG variables (e.g. `VERTEX_INDEX_ENDPOINT_ID`, `VERTEX_DEPLOYED_INDEX_ID`, `CHAT_RAG_DATABASE_URL`).
