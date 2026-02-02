# Mobius Chat

Chat question → queue → background worker reads → worker publishes response to queue → client gets response. Plug-and-play queue backend (memory or Redis) and modules.

## Queue flow

1. **Client** (chat webpage or API) writes a chat question → `publish_request(correlation_id, { message })`.
2. **Background worker** reads from the queue → `consume_requests(callback)` → processes (planner, stub respond) → `publish_response(correlation_id, response)`.
3. **Client** gets response by correlation_id → `get_response(correlation_id)` (polling).

Queue backend is pluggable: **memory** (single process) or **redis** (API and worker can run in separate processes).

## Prerequisites (for RAG simulation)

- **Docker Desktop** – [Install for Mac](https://docs.docker.com/desktop/install/mac-install/) (or `brew install --cask docker`). Start Docker Desktop before running the simulation.
- **Python 3 + venv** – `python3 -m venv .venv && source .venv/bin/activate` then `python3 -m pip install -r requirements.txt` (use `python3 -m pip` if `pip` is not in PATH).

## Run

### Option A: In-memory queue (single process, dev)

API and worker run in one process (worker in a background thread).

```bash
cd /path/to/Mobius-Chat
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open http://localhost:8000/ — send a message; response appears after the in-process worker runs.

### Credentials (Vertex AI)

Chat uses GCP/Vertex for LLM and RAG. Create a **`.env`** file in the **repo root** (see [docs/ENV.md](docs/ENV.md)) and set:

- **`GOOGLE_APPLICATION_CREDENTIALS`** – **Must point to an existing file.** Use the full path to your GCP service account JSON (e.g. `keys/mobiusos-new-xxxx.json` or `../Mobius RAG/mobiusos-new-090a058b63d9.json`). If this file does not exist, you will see: `LLM failed: File keys/your-service-account.json was not found.`
- `VERTEX_PROJECT_ID`, `VERTEX_LOCATION`, `VERTEX_MODEL` – for Vertex AI (LLM).
- For RAG: `VERTEX_INDEX_ENDPOINT_ID`, `VERTEX_DEPLOYED_INDEX_ID`, `CHAT_RAG_DATABASE_URL` (see [docs/PUBLISHED_RAG_SETUP.md](docs/PUBLISHED_RAG_SETUP.md)). If these are missing, you will see: `RAG: Vertex endpoint/deployed index or database URL not set. Using no context.`

The API and worker both load `.env` from the project root. When using Redis, run `mchatc` and `mchatcw` from the repo root so the worker sees the same `.env`.

### Option B: Redis queue (API and worker separate)

Use the scripts `mchatc` (chat interface) and `mchatcw` (worker + Redis) from the repo root:

**Terminal 1 – Chat interface (API + frontend)**  
```bash
cd /path/to/Mobius-Chat
./mchatc
```
Uses `QUEUE_TYPE=redis` and `REDIS_URL=redis://localhost:6379/0` by default; override with env if needed.

**Terminal 2 – Worker + Redis**  
```bash
cd /path/to/Mobius-Chat
./mchatcw
```
Starts Redis if not already running (via `redis-server` or Docker), then starts the worker. Worker and Redis are coupled on this server.

**Run from anywhere:** Add the repo to PATH (`export PATH="/path/to/Mobius-Chat:$PATH"`) or symlink `mchatc` and `mchatcw` into a directory on PATH (e.g. `~/bin`). Then you can run `mchatc` and `mchatcw` from any directory.

Open http://localhost:8000/ and send a message; the worker (started by `mchatcw`) will process it via the Redis list.

### RAG for non-patient questions (Vertex AI + Postgres)

RAG uses **Vertex AI Vector Search** (1536 dims) and **Postgres** `published_rag_metadata` (metadata only). The sync job (MOBIUS-DBT) writes the BigQuery mart to your Postgres and Vertex; Chat only reads. When docs are ready to be published they move here. Use the local DB first; fallback to Mobius RAG API if you don’t set a DB URL.

**Get RAG working**

1. **Apply Postgres schema** (see [docs/PUBLISHED_RAG_SETUP.md](docs/PUBLISHED_RAG_SETUP.md)):
   ```bash
   psql -h <host> -U <user> -d mobius_chat -f db/schema/002_published_rag_metadata.sql
   ```

2. **Create Vertex index** (1536, Cosine, Streaming, metadata filtering) and deploy to an endpoint. Note endpoint id and deployed index id.

3. **Configure `.env`** (see [docs/ENV.md](docs/ENV.md)):
   ```bash
   VERTEX_INDEX_ENDPOINT_ID=projects/your-project/locations/us-central1/indexEndpoints/your-endpoint-id
   VERTEX_DEPLOYED_INDEX_ID=endpoint_mobius_chat_publi_1769989702095
   CHAT_RAG_DATABASE_URL=postgresql://user:pass@host:port/mobius_chat
   VERTEX_PROJECT_ID=your-project
   GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
   ```

4. **Share** Postgres URL and Vertex index/endpoint ids with MOBIUS-DBT so the sync job can write to your stores.

After the first sync, ask a **non-patient** question; the worker will embed (1536), query Vertex, fetch metadata from Postgres, and answer with context + sources. See [db/README.md](db/README.md) and [docs/PUBLISHED_RAG_SETUP.md](docs/PUBLISHED_RAG_SETUP.md).


## Modules

See [docs/MODULES.md](docs/MODULES.md) for the list of modules and helpers. Summary:

- **config** – `app/config.py` (env)
- **queue** – `app/queue/base.py`, `memory.py` (publish/consume request, publish/get response)
- **planner** – `app/planner/schemas.py`, `parser.py` (parse message → plan with thinking emission)
- **storage** – `app/storage/plans.py`, `responses.py` (store plan + response by correlation_id)
- **worker** – `app/worker/run.py` (consume → plan → store → stub respond → publish)
- **responder** – `app/responder/final.py` (format_response)
- **API** – `app/main.py` (POST /chat, GET /chat/response/:id, GET /chat/plan/:id, GET /)
- **Frontend** – TypeScript in `frontend/src/app.ts`, built to `frontend/static/app.js`. To rebuild: `cd frontend && npm install && npm run build`
