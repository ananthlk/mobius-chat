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
| `GOOGLE_APPLICATION_CREDENTIALS` | **Required.** Absolute path to GCP service account JSON (e.g. `/Users/ananth/Mobius/mobius-rag/mobiusos-new-090a058b63d9.json`) |

## Vertex Vector Search + Postgres (RAG)

| Variable | Description |
|---------|-------------|
| `VERTEX_INDEX_ENDPOINT_ID` | Vertex index endpoint (full resource name) |
| `VERTEX_DEPLOYED_INDEX_ID` | Deployed index id from Console (e.g. `endpoint_mobius_chat_publi_1769989702095`). Use the **ID** under Deployed indexes, not the display name. |
| `CHAT_RAG_DATABASE_URL` | Postgres URL for `published_rag_metadata` (e.g. `postgresql://postgres:PASSWORD@HOST:5432/mobius_chat`) |
| `VERTEX_PROJECT`, `VERTEX_REGION`, `VERTEX_INDEX_ID`, `GCS_BUCKET`, `BQ_PROJECT`, `BQ_DATASET` | Used by sync job (MOBIUS-DBT); optional for Chat |
| `CHAT_RAG_FILTER_PAYER`, `CHAT_RAG_FILTER_STATE`, `CHAT_RAG_FILTER_PROGRAM`, `CHAT_RAG_FILTER_AUTHORITY_LEVEL` | Optional RAG filter defaults. If set, **only** documents matching these values are returned (e.g. `CHAT_RAG_FILTER_PAYER=Sunshine Health`). Leave unset to search all payers. |

## Local dev RAG (Mobius RAG backend + pgvector)

If you want a **local dev** setup (no Vertex Vector Search), you can run `mobius-rag` locally and have Chat query it.

- **Run Mobius RAG backend on a non-conflicting port**: Chat typically runs on `8000`, so run RAG on `8001`:

```bash
cd "/Users/ananth/Mobius/mobius-rag" && python -m uvicorn app.main:app --reload --port 8001
```

- **Set env vars**:
  - `RAG_APP_API_BASE=http://localhost:8001`
  - Leave `VERTEX_INDEX_ENDPOINT_ID` and `VERTEX_DEPLOYED_INDEX_ID` **unset** (so Chat uses local retrieval).

Notes:
- Chat still uses `CHAT_RAG_DATABASE_URL` for **chat persistence** (threads/turns/feedback). Point it at a local `mobius_chat` DB if you want persistence in dev.
- `mobius-rag` uses **pgvector** via the `chunk_embeddings` table; make sure embeddings have been generated (see `mobius-rag/INSTALL_AND_TEST.md` → embedding worker).

## User auth

**Option A – Proxy to Mobius-OS (plug-and-play, same users as extension):**

| Variable | Description |
|----------|-------------|
| `MOBIUS_OS_AUTH_URL` | Mobius-OS backend URL (e.g. `http://localhost:5001`). When set, `/api/v1/auth/*` is proxied to Mobius-OS. Same users and tokens as the extension. |
| `JWT_SECRET` | Must match Mobius-OS for token validation (optional; enables user_id extraction for chat payloads). |

**Option B – Standalone mobius-user (separate DB):**

| Variable | Description |
|----------|-------------|
| `USER_DATABASE_URL` | Postgres URL for mobius_user DB. When set (and `MOBIUS_OS_AUTH_URL` not set), auth routes use mobius-user. |
| `JWT_SECRET` | Secret for JWT signing. |

Install mobius-user for Option B: `pip install -e ../mobius-user`. Create DB and run migrations: see mobius-user/README.md.

## Document mini reader (inline + open in new tab)

| Variable | Description |
|----------|-------------|
| `RAG_APP_API_BASE` | RAG backend URL for full-page inline reader (e.g. `http://localhost:8000`). When set, `GET /api/v1/documents/{document_id}/pages` proxies to RAG. If unset, mini reader shows snippet only and "Open in new tab" still works when `RAG_APP_BASE` is set. |
| `RAG_APP_BASE` (frontend) | RAG app base URL for "Open in new tab" (e.g. `http://localhost:5173` for RAG Vite dev). Set in `frontend/index.html` as `window.RAG_APP_BASE = '...'` or at build time. Deep link: `?tab=read&documentId=<id>&pageNumber=<n>`. |

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
