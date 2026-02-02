# Environment variables (.env)

The app loads `.env` from the **repo root**. Create a `.env` file there with the variables below. `.env` is gitignored; do not commit secrets.

## Queue

| Variable | Default | Description |
|---------|--------|-------------|
| `QUEUE_TYPE` | `memory` | `memory` (single process) or `redis` (API + worker separate) |
| `REDIS_URL` | `redis://localhost:6379/0` | When `QUEUE_TYPE=redis` |
| `STORAGE_BACKEND` | `memory` | Storage for plans/responses |

## Vertex AI (LLM + embeddings)

| Variable | Description |
|---------|-------------|
| `VERTEX_PROJECT_ID` | GCP project (e.g. `mobiusos-new`) |
| `VERTEX_LOCATION` | Region (e.g. `us-central1`) |
| `VERTEX_MODEL` | Model (e.g. `gemini-2.5-flash`) |
| `GOOGLE_APPLICATION_CREDENTIALS` | **Required.** Absolute path to GCP service account JSON (e.g. `/Users/ananth/Mobius RAG/mobiusos-new-090a058b63d9.json`) |

## Vertex Vector Search + Postgres (RAG)

| Variable | Description |
|---------|-------------|
| `VERTEX_INDEX_ENDPOINT_ID` | Vertex index endpoint (full resource name) |
| `VERTEX_DEPLOYED_INDEX_ID` | Deployed index id from Console (e.g. `endpoint_mobius_chat_publi_1769989702095`). Use the **ID** under Deployed indexes, not the display name. |
| `CHAT_RAG_DATABASE_URL` | Postgres URL for `published_rag_metadata` (e.g. `postgresql://postgres:PASSWORD@HOST:5432/mobius_chat`) |
| `VERTEX_PROJECT`, `VERTEX_REGION`, `VERTEX_INDEX_ID`, `GCS_BUCKET`, `BQ_PROJECT`, `BQ_DATASET` | Used by sync job (MOBIUS-DBT); optional for Chat |
| `CHAT_RAG_FILTER_PAYER`, `CHAT_RAG_FILTER_STATE`, `CHAT_RAG_FILTER_PROGRAM`, `CHAT_RAG_FILTER_AUTHORITY_LEVEL` | Optional RAG filter defaults. If set, **only** documents matching these values are returned (e.g. `CHAT_RAG_FILTER_PAYER=Sunshine Health`). Leave unset to search all payers. |

## Optional overrides

| Variable | Description |
|---------|-------------|
| `CHAT_LLM_PROVIDER` | `vertex` or `ollama` |
| `CHAT_RAG_TOP_K` | RAG top-k (default 10) |
| `API_BASE_URL` | Frontend API base URL (e.g. `http://localhost:8000`) |

## RAG returns 0 chunks

1. **Postgres** – Confirm metadata exists: `SELECT COUNT(*) FROM published_rag_metadata;` (run against `CHAT_RAG_DATABASE_URL`). If 0, the sync job (MOBIUS-DBT) may not have run or may write to a different DB.
2. **Vertex index** – In GCP Console → Vertex AI → Vector Search → your index, check that datapoints exist. If the index is empty, sync has not populated it.
3. **Filters** – If `CHAT_RAG_FILTER_PAYER`, `CHAT_RAG_FILTER_STATE`, etc. are set, only documents with **exactly** those values are returned. For “Sunshine Health” content, set `CHAT_RAG_FILTER_PAYER=Sunshine Health` (or whatever value is in the index); leave filter vars **unset** to search across all payers.
4. **Worker logs** – Restart `mchatcw` and watch logs for: `Vertex find_neighbors returned N id(s)` and `Postgres published_rag_metadata returned M row(s)`. That shows whether the drop is at Vertex (0 ids) or Postgres (ids not found).
