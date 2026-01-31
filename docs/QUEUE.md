# Queue – How It Works

## Idea

- **Chat question** is written to a **request queue**.
- A **background worker** reads from that queue and **publishes the response** to a response channel (keyed by `correlation_id`).
- Any client (webpage, API, another service) can submit questions and get responses by polling (or subscribing) by `correlation_id`.

This keeps the pipeline plug-and-play: swap queue backend (memory, Redis, Pub/Sub) or swap internal modules (planner, responder, persistence) without changing the flow.

## Flow

```
Client (e.g. chat UI)                Queue (request)              Worker
       |                                    |                         |
       |  publish_request(cid, {message})   |                         |
       |----------------------------------->|                         |
       |                                    |  consume_requests()      |
       |                                    |<------------------------|
       |                                    |                         | process_one()
       |                                    |                         | (planner, stub, responder)
       |                                    |  publish_response(cid, response)
       |                                    |<------------------------|
       |  get_response(cid)                 |                         |
       |----------------------------------->|                         |
       |  { status, message, plan, ... }    |                         |
       |<-----------------------------------|                         |
```

## Implementations

| Backend | Use case | How |
|---------|----------|-----|
| **memory** | Single process, dev | `queue.Queue` for requests; dict for responses. API starts worker in a background thread. |
| **redis** | API and worker separate | Redis list for requests (LPUSH/BRPOP); Redis key per `correlation_id` for response (SET/GET with TTL). Run worker with `python -m app.worker`. |

Set `QUEUE_TYPE=memory` or `QUEUE_TYPE=redis` (and `REDIS_URL` for Redis).

## Payloads

- **Request** (what goes into the queue): `{ "message": "...", "session_id"?: "..." }`. Worker receives `(correlation_id, payload)`.
- **Response** (what worker publishes): `{ "status": "completed"|"failed", "message": "...", "plan"?: {...}, "thinking_log"?: [...] }`. Client polls `get_response(correlation_id)`.

See `app/queue/schemas.py` for helpers (`make_request_payload`, `make_response_payload`).

## Running the worker

- **Memory**: Worker is started automatically by the API (background thread). One process.
- **Redis**: Run the worker yourself in a separate process: `QUEUE_TYPE=redis python -m app.worker`. Worker blocks on `consume_requests` and processes each request.

## Next: persistence

Internal persistence (plans, subanswers, responses in a DB) can be added **inside the worker** and/or in the API without changing the queue contract. The queue only carries request and response payloads; persistence is a separate layer that the worker (and optionally the API) uses when reading/writing.
