# Mobius Chat

Chat question → queue → background worker reads → worker publishes response to queue → client gets response. Plug-and-play queue backend (memory or Redis) and modules.

## Queue flow

1. **Client** (chat webpage or API) writes a chat question → `publish_request(correlation_id, { message })`.
2. **Background worker** reads from the queue → `consume_requests(callback)` → processes (planner, stub respond) → `publish_response(correlation_id, response)`.
3. **Client** gets response by correlation_id → `get_response(correlation_id)` (polling).

Queue backend is pluggable: **memory** (single process) or **redis** (API and worker can run in separate processes).

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

Chat uses the same GCP/Vertex credentials as **Mobius RAG**. Copy `.env.example` to `.env` and set:

- `VERTEX_PROJECT_ID=mobiusos-new`
- `VERTEX_LOCATION=us-central1`, `VERTEX_MODEL=gemini-2.5-flash`
- `GOOGLE_APPLICATION_CREDENTIALS` = path to the service account JSON (e.g. `../Mobius RAG/mobiusos-new-090a058b63d9.json` if Mobius RAG is a sibling folder).

The app loads `.env` from the project root at startup (API and worker). See Mobius RAG’s `docs/CREDENTIALS.md` for details.

### Option B: Redis queue (API and worker separate)

Use the scripts `mragc` (chat interface) and `mragcw` (worker + Redis) from the repo root:

**Terminal 1 – Chat interface (API + frontend)**  
```bash
cd /path/to/Mobius-Chat
./mragc
```
Uses `QUEUE_TYPE=redis` and `REDIS_URL=redis://localhost:6379/0` by default; override with env if needed.

**Terminal 2 – Worker + Redis**  
```bash
cd /path/to/Mobius-Chat
./mragcw
```
Starts Redis if not already running (via `redis-server` or Docker), then starts the worker. Worker and Redis are coupled on this server.

**Run from anywhere:** Add the repo to PATH (`export PATH="/path/to/Mobius-Chat:$PATH"`) or symlink `mragc` and `mragcw` into a directory on PATH (e.g. `~/bin`). Then you can run `mragc` and `mragcw` from any directory.

Open http://localhost:8000/ and send a message; the worker (started by `mragcw`) will process it via the Redis list.

## Modules

See [docs/MODULES.md](docs/MODULES.md) for the list of modules and helpers. Summary:

- **config** – `app/config.py` (env)
- **queue** – `app/queue/base.py`, `memory.py` (publish/consume request, publish/get response)
- **planner** – `app/planner/schemas.py`, `parser.py` (parse message → plan with thinking emission)
- **storage** – `app/storage/plans.py`, `responses.py` (store plan + response by correlation_id)
- **worker** – `app/worker/run.py` (consume → plan → store → stub respond → publish)
- **responder** – `app/responder/final.py` (format_response)
- **API** – `app/main.py` (POST /chat, GET /chat/response/:id, GET /chat/plan/:id, GET /)
- **Frontend** – `frontend/index.html`, `frontend/static/app.js`
