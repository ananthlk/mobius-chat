# Persistence Plan – Data Architecture and Helper Modules

## Current state

- **Plans**: in-memory dict in `app/storage/plans.py` → lost on restart.
- **Responses**: in-memory dict in `app/storage/responses.py` → lost on restart.
- **Subquestion answers**: not stored as separate rows; only inside the final response payload. No per-step or per-submessage persistence.
- **Request**: not persisted; only enqueued with `correlation_id`.

Nothing survives process restart or is queryable for audit/debug.

---

## Goal

Persist every step of the pipeline so we can:

- Survive restarts and inspect past conversations.
- Audit: who asked what, what plan was generated, what each subquestion got as an answer, what the final response was.
- Debug: see plan, thinking_log, subanswers, and final message per `correlation_id`.
- Later: support critique attempts and retries (draft N, critique result N).

---

## Data architecture

### Entity relationship (high level)

```
ChatRequest (1) ──┬── (1) ChatPlan
                  ├── (0..n) ChatSubquestionAnswer
                  └── (0..1) ChatResponse
```

- One **request** per user message (`correlation_id`).
- One **plan** per request (subquestions + thinking_log).
- **N subquestion answers** per request (one row per subquestion: patient warning or RAG/stub answer).
- One **response** per request when completed (final message + optional full payload).

### Tables (SQLite-first; same schema works for PostgreSQL)

| Table | Purpose |
|-------|--------|
| **chat_requests** | Incoming request: message, session_id, status, timestamps. |
| **chat_plans** | Planner output: plan JSON (subquestions), thinking_log JSON. |
| **chat_subquestion_answers** | Per-subquestion answer (stub / RAG / patient_warning) for audit and display. |
| **chat_responses** | Final response: status, message, full payload JSON, completed_at. |

### Schema (SQL)

```sql
-- chat_requests: one row per user message
CREATE TABLE chat_requests (
    correlation_id TEXT PRIMARY KEY,
    message TEXT NOT NULL,
    session_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | planning | answering | completed | failed
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- chat_plans: planner output (1:1 with request)
CREATE TABLE chat_plans (
    correlation_id TEXT PRIMARY KEY REFERENCES chat_requests(correlation_id),
    plan_json TEXT NOT NULL,       -- JSON: { "subquestions": [...], "thinking_log": [...] }
    thinking_log_json TEXT,       -- JSON array of strings (can duplicate from plan_json for query ease)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- chat_subquestion_answers: one row per subquestion answer
CREATE TABLE chat_subquestion_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id TEXT NOT NULL REFERENCES chat_requests(correlation_id),
    subquestion_id TEXT NOT NULL,  -- e.g. sq1, sq2
    subquestion_text TEXT NOT NULL,
    kind TEXT NOT NULL,            -- patient | non_patient
    answer_text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'stub',  -- stub | rag | patient_warning
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_subanswer_correlation ON chat_subquestion_answers(correlation_id);

-- chat_responses: final response (1:1 with request)
CREATE TABLE chat_responses (
    correlation_id TEXT PRIMARY KEY REFERENCES chat_requests(correlation_id),
    status TEXT NOT NULL,         -- completed | failed
    message TEXT,
    response_json TEXT,            -- full payload for API compatibility
    completed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

For PostgreSQL later: same names and logic; use `TIMESTAMPTZ`, `SERIAL`/identity for `id`, and `JSONB` if desired.

---

## Helper modules to create

Single place for all persistence so the rest of the app stays easy to follow.

### 1. `app/persistence/` package

| Module | Role |
|--------|------|
| **config** | DB URL (e.g. `sqlite:///./data/chat.db` or env `DATABASE_URL`). |
| **models** | Table names, column names, and optionally SQLAlchemy model classes (or keep schema in migrations only). |
| **connection** | Create engine (SQLAlchemy or `sqlite3`), init DB (create tables if not exist), get session/connection. |
| **request_repo** | Create request, get by correlation_id, update status/error. |
| **plan_repo** | Save plan (plan_json + thinking_log), get plan by correlation_id. |
| **subanswer_repo** | Save list of subquestion answers, get by correlation_id. |
| **response_repo** | Save final response, get by correlation_id. |
| **conversation_repo** (optional) | High-level: `get_conversation(correlation_id)` → request + plan + subanswers + response; `save_plan_and_answers(...)` for worker. |

Repositories are the only layer that touch the DB; worker and API call repos only.

### 2. Flow: who writes what

| Step | Writer | Tables touched |
|------|--------|----------------|
| Request received (API) | `request_repo.create_request(correlation_id, message, session_id?)` | chat_requests |
| Planner finishes (worker) | `plan_repo.save_plan(correlation_id, plan, thinking_log)` | chat_plans |
| Worker can optionally | `request_repo.update_status(correlation_id, 'planning')` then `'answering'` | chat_requests |
| Per-subquestion answers (worker) | `subanswer_repo.save_answers(correlation_id, list of {sq_id, text, kind, answer, source})` | chat_subquestion_answers |
| Final response (worker) | `response_repo.save_response(correlation_id, status, message, response_json)` | chat_responses |
| | `request_repo.update_status(correlation_id, 'completed')` | chat_requests |

### 3. Reading back (API / debug)

- **GET /chat/response/{id}**: `response_repo.get(correlation_id)`; if missing, `request_repo.get(correlation_id)` for status.
- **GET /chat/plan/{id}**: `plan_repo.get(correlation_id)`.
- **GET /chat/conversation/{id}** (optional): `conversation_repo.get_conversation(correlation_id)` → request + plan + subanswers + response in one structure.

---

## File tree (persistence)

```
app/
  persistence/
    __init__.py          # Export get_session, init_db, repos
    config.py            # DATABASE_URL (sqlite path or env)
    connection.py        # engine, init_db(), get_session()
    schema.sql           # Optional: raw SQL for create tables (or in connection.init_db)
    request_repo.py      # create, get, update_status
    plan_repo.py         # save_plan, get_plan
    subanswer_repo.py    # save_answers, get_answers
    response_repo.py     # save_response, get_response
    conversation_repo.py # (optional) get_conversation, save_plan_and_answers
  storage/               # Keep for now: can delegate to persistence layer or remove later
    plans.py             # → calls plan_repo
    responses.py         # → calls response_repo
```

**Backward compatibility**: Keep `app/storage/plans.py` and `app/storage/responses.py` with the same `store_plan`, `get_plan`, `store_response`, `get_response` signatures, but implement them by calling the persistence layer. Then worker and API need no changes; only the storage implementations switch from in-memory to DB.

---

## Implementation order

1. **Persistence config + connection** – `persistence/config.py`, `persistence/connection.py`, create tables on startup (SQLite file under `data/` or project root).
2. **Repositories** – `request_repo`, `plan_repo`, `subanswer_repo`, `response_repo` with create/get/save methods.
3. **Wire storage to persistence** – In `storage/plans.py` and `storage/responses.py`, call repos instead of in-memory dicts; create request on first store if needed.
4. **Worker** – After planner: save plan via storage (already does). Add: save each subquestion answer via `subanswer_repo.save_answers`. Optionally update request status at each step.
5. **Optional** – `conversation_repo.get_conversation(id)` and `GET /chat/conversation/{id}` for a single “full conversation” response.

---

## Summary

| What | Where it persists |
|------|-------------------|
| User message, status | **chat_requests** |
| Plan (subquestions), thinking_log | **chat_plans** |
| Each subquestion’s answer (stub/RAG/warning) | **chat_subquestion_answers** |
| Final response (message, payload) | **chat_responses** |

Helper modules: **persistence/** with config, connection, request_repo, plan_repo, subanswer_repo, response_repo; optional conversation_repo. Storage layer keeps same API and delegates to persistence so the rest of the app stays easy to follow.
