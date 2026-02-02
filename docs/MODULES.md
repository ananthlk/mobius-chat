# Mobius Chat – Modules and Helpers (Initial Implementation)

## Flow

```
Chat webpage → POST /chat (enqueue) → Request queue
     ↑                                        ↓
     |                                  Worker picks up
     |                                        ↓
     |                                  Planner (thinking emitted)
     |                                        ↓
     |                                  Store plan
     |                                        ↓
     |                                  Stub sub-modules → Final responder
     |                                        ↓
     |                                  Write response to queue + DB
     |                                        ↓
     +—— GET /chat/response/:id (poll) ← Response queue / storage
```

## Modules and Helpers Created

### 1. Config
- **`app/config.py`** – Load settings from env: queue type, DB path (or in-memory), planner model hint. Helper: `get_config()`.

### 2. Queue
- **`app/queue/base.py`** – Abstract interface: `publish_request(payload)`, `consume_requests(callback)` (or async iterator), `publish_response(correlation_id, payload)`, optional `get_response(correlation_id)` if responses are stored by API.
- **`app/queue/memory.py`** – In-memory implementation for local dev: request queue, response queue; worker runs in same process or background thread.
- **`app/queue/__init__.py`** – Export `get_queue()` (returns implementation based on config).

### 3. Planner (Parser)
- **`app/planner/schemas.py`** – Pydantic models: `SubQuestion(id, text, kind: patient | non_patient)`, `Plan(subquestions, thinking_log: list[str])`.
- **`app/planner/parser.py`** – `parse(message: str, *, thinking_emitter: Callable[[str], None] | None) -> Plan`. Generates plan (subquestions + classification); calls `thinking_emitter(chunk)` for “thinking” updates. Implementation can be rule-based or LLM-based later.
- **`app/planner/__init__.py`** – Export `parse`, `Plan`, `SubQuestion`.

### 4. Storage (Plans and Responses)
- **`app/storage/plans.py`** – `store_plan(correlation_id, plan: Plan, thinking_log: list[str])`, `get_plan(correlation_id)`. Backed by in-memory dict or SQLite/file for minimal scaffolding.
- **`app/storage/responses.py`** – `store_response(correlation_id, response: dict)`, `get_response(correlation_id)`.
- **`app/storage/__init__.py`** – Export storage helpers. Optional: `app/storage/backend.py` for SQLite if we add it.

### 5. Worker
- **`app/worker/run.py`** – Loop: consume from request queue → parse request (correlation_id, message) → run planner with thinking_emitter (store thinking + optional forward to “thinking” stream) → store plan → run stub answer/combine → final responder → store response → publish to response queue. Helper: `run_worker()` (blocking or async).
- **`app/worker/__init__.py`** – Export `run_worker`.

### 6. Responder (Final)
- **`app/responder/final.py`** – `format_response(plan: Plan, stub_answers: list[str]) -> str`. Chat-style final message (e.g. “Here’s what I found: …” with plan summary + stub answers). For now stubs return placeholder text per subquestion.
- **`app/responder/__init__.py`** – Export `format_response`.

### 7. API (FastAPI)
- **`app/main.py`** – FastAPI app:
  - `POST /chat` – Body: `{ "message": "..." }`. Generate correlation_id, enqueue request, return `{ "correlation_id": "..." }`.
  - `GET /chat/response/{correlation_id}` – Return stored response (pending / completed / error). Frontend polls until completed.
  - Optional: `GET /chat/thinking/{correlation_id}` – Return stored thinking log (or SSE later).
  - `GET /health` – Health check.

### 8. Frontend (Chat Webpage)
- **`frontend/index.html`** – Simple chat UI: input, send button, area for messages; load `app.js`.
- **`frontend/static/app.js`** or inline – On send: POST /chat, get correlation_id, poll GET /chat/response/:id until done, append response to chat area; optionally show “thinking” or “planning…” while pending.

### 9. Root
- **`requirements.txt`** – fastapi, uvicorn, pydantic, python-dotenv. Optional: redis, google-cloud-pubsub for later.
- **`.env`** – QUEUE_TYPE=memory, Vertex/RAG vars (see [ENV.md](ENV.md)); gitignored.
- **`README.md`** – How to run worker, run API, open frontend; flow summary.

---

## File Tree (Initial)

```
Mobius-Chat/
  app/
    __init__.py
    config.py
    main.py
    queue/
      __init__.py
      base.py
      memory.py
    planner/
      __init__.py
      schemas.py
      parser.py
    storage/
      __init__.py
      plans.py
      responses.py
    worker/
      __init__.py
      run.py
    responder/
      __init__.py
      final.py
  frontend/
    index.html
    static/
      app.js
  docs/
    ARCHITECTURE.md
    MODULES.md
  requirements.txt
  .env
  README.md
```
