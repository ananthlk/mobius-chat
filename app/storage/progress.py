"""Live progress store: thinking_log and message_so_far streamed per correlation_id so clients can poll and see progress.
Supports SSE: append_* also push events to a per-id queue so GET /chat/stream/:id can yield them in real time.
When queue_type=redis, worker runs in a separate process: we PUBLISH to Redis and persist to DB (chat_progress_events).
API stream polls DB for progress (like RAG chunking_events) so it works without Redis subscribe.

Ordering guarantee: every emit goes through a per-request serial queue (_db_queues) so DB inserts happen in strict
emit order regardless of OS thread scheduling. This prevents the race where a later emit gets a lower DB id
than an earlier emit (which caused "Formatting the response…" to appear before "✓ Step 1 done" in the UI).
"""
import json
import logging
import queue
import threading
import time
from typing import Any


def _event_ts() -> tuple[float, str]:
    """Return (unix_ts, readable) for event debugging (when written / received / sent)."""
    t = time.time()
    readable = time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t * 1000) % 1000:03d}"
    return (t, readable)

logger = logging.getLogger(__name__)
_progress: dict[str, dict] = {}  # correlation_id -> {"thinking": list[str], "message": str, "events": list[dict]}
_lock = threading.Lock()
_progress_redis_logged: set[str] = set()  # correlation_ids we've logged "[progress] publishing to Redis" for
_external_last_line: dict[str, str] = {}  # cid -> last line pushed via push_external_thinking (dedup guard)

# Serial DB insert queues — one per active request. Ensures DB insertion order == emit order.
# Each queue is drained by exactly one background thread. None sentinel signals shutdown.
_db_queues: dict[str, queue.Queue] = {}
_db_workers: dict[str, threading.Thread] = {}
_db_queues_lock = threading.Lock()


def _db_worker_loop(correlation_id: str, q: queue.Queue) -> None:
    """Drain the serial DB insert queue for one request. Runs in its own daemon thread."""
    while True:
        item = q.get()
        if item is None:  # sentinel — request complete
            q.task_done()
            break
        ev = item
        try:
            _persist_progress_event_to_db(correlation_id, ev)
        except Exception as e:
            logger.debug("[progress] serial DB insert failed cid=%s: %s", correlation_id[:8], e)
        q.task_done()


def _ensure_db_worker(correlation_id: str) -> queue.Queue:
    """Return (creating if needed) the serial insert queue for this request."""
    with _db_queues_lock:
        existing = _db_queues.get(correlation_id)
        if existing is not None:
            return existing
        q: queue.Queue = queue.Queue()
        _db_queues[correlation_id] = q
        t = threading.Thread(
            target=_db_worker_loop,
            args=(correlation_id, q),
            daemon=True,
            name=f"progress-db-{correlation_id[:8]}",
        )
        t.start()
        _db_workers[correlation_id] = t
        return q


def _enqueue_for_db(correlation_id: str, events: list[dict]) -> None:
    """Put events onto the serial DB insert queue (creates queue/worker on first call)."""
    if not events:
        return
    q = _ensure_db_worker(correlation_id)
    for ev in events:
        q.put(ev)


def _publish_progress_event_impl(correlation_id: str, ev: dict[str, Any]) -> None:
    """Actually connect to Redis and publish. Run in a thread so the worker never blocks."""
    try:
        from app.config import get_config
        cfg = get_config()
        if getattr(cfg, "queue_type", "memory") != "redis":
            return
        import redis
        r = redis.from_url(cfg.redis_url, decode_responses=True)
        channel = getattr(cfg, "redis_progress_channel_prefix", "mobius:chat:progress:") + correlation_id
        r.publish(channel, json.dumps(ev))
        data = ev.get("data") or {}
        ts_readable = data.get("ts_readable", "")
        with _lock:
            if correlation_id not in _progress_redis_logged:
                _progress_redis_logged.add(correlation_id)
                logger.info("[progress] publishing to Redis for correlation_id=%s channel=%s", correlation_id[:8], channel)
        logger.info("[progress] published event %s cid=%s written_at=%s", ev.get("event"), correlation_id[:8], ts_readable)
    except Exception as e:
        logger.warning("[progress] Redis publish failed (stream may not be live): %s", e)


def _persist_progress_event_to_db(correlation_id: str, ev: dict[str, Any]) -> None:
    """Persist event via PersistencePort (chat_progress_events). Run in thread."""
    try:
        from app.persistence import get_persistence

        get_persistence().append_progress_event(
            correlation_id,
            ev.get("event", ""),
            ev.get("data") or {},
        )
    except Exception as e:
        logger.debug("Progress DB persist failed (stream may poll Redis): %s", e)


def _publish_progress_event(correlation_id: str, ev: dict[str, Any]) -> None:
    """If queue is Redis, publish to Redis and persist to DB via serial queue. When trace enabled, always persist."""
    try:
        from app.config import get_config
        from app.trace_log import is_trace_enabled

        cfg = get_config()
        use_redis = getattr(cfg, "queue_type", "memory") == "redis"
        use_db_persist = use_redis or is_trace_enabled()

        if use_redis:
            t = threading.Thread(
                target=_publish_progress_event_impl,
                args=(correlation_id, ev),
                daemon=True,
                name="progress-publish",
            )
            t.start()
        if use_db_persist:
            # Serial queue guarantees DB insertion order == emit order
            _enqueue_for_db(correlation_id, [ev])
    except Exception:
        pass


def _publish_progress_events_ordered(correlation_id: str, events: list[dict[str, Any]]) -> None:
    """Publish multiple events preserving strict emit order.
    Redis: per-event publish (fire-and-forget; Redis delivery order is best-effort).
    DB: serial queue — all events for a request flow through one thread, in order."""
    if not events:
        return
    try:
        from app.config import get_config
        from app.trace_log import is_trace_enabled

        cfg = get_config()
        use_redis = getattr(cfg, "queue_type", "memory") == "redis"
        use_db_persist = use_redis or is_trace_enabled()

        if use_redis:
            for ev in events:
                t = threading.Thread(
                    target=_publish_progress_event_impl,
                    args=(correlation_id, ev),
                    daemon=True,
                    name="progress-publish",
                )
                t.start()
        if use_db_persist:
            # All events for this batch go onto the same serial queue, preserving order
            _enqueue_for_db(correlation_id, events)
    except Exception:
        pass


def start_progress(correlation_id: str) -> None:
    """Mark correlation_id as in progress. Worker calls this at start of process_one.
    Pre-creates the serial DB insert queue so the first emit never races with queue creation."""
    with _lock:
        _progress[correlation_id] = {"thinking": [], "message": "", "events": []}
    # Pre-create the serial DB insert worker; _ensure_db_worker is idempotent
    _ensure_db_worker(correlation_id)


def append_thinking(correlation_id: str, chunk: str) -> None:
    """Append one or more thinking chunks. Splits on newlines so SSE delivers line-by-line for live display.
    Publishes to Redis outside the lock so a slow Redis does not block the worker."""
    to_publish: list[dict[str, Any]] = []
    with _lock:
        if correlation_id not in _progress or not chunk.strip():
            return
        lines = [s.strip() for s in chunk.strip().split("\n") if s.strip()]
        for line in lines:
            ts, ts_readable = _event_ts()
            ev = {"event": "thinking", "data": {"line": line, "ts": ts, "ts_readable": ts_readable}}
            _progress[correlation_id]["thinking"].append(line)
            _progress[correlation_id]["events"].append(ev)
            to_publish.append(ev)
    if to_publish:
        _publish_progress_events_ordered(correlation_id, to_publish)


def append_message_chunk(correlation_id: str, chunk: str) -> None:
    """Append one chunk to the streaming final message. Integrator calls this so clients see the draft stream.
    Publishes to Redis outside the lock so a slow Redis does not block the worker."""
    ev_to_publish: dict[str, Any] | None = None
    with _lock:
        if correlation_id in _progress:
            ts, ts_readable = _event_ts()
            ev_to_publish = {"event": "message", "data": {"chunk": chunk, "ts": ts, "ts_readable": ts_readable}}
            _progress[correlation_id]["message"] += chunk
            _progress[correlation_id]["events"].append(ev_to_publish)
    if ev_to_publish is not None:
        _publish_progress_event(correlation_id, ev_to_publish)


def append_draft_answer(correlation_id: str, text: str, mode_hint: str | None = None) -> None:
    """Emit the raw ReAct answer before the integrator runs so the frontend can render it immediately.
    Fires a draft_ready SSE event; the completed event fills in remaining panels in-place.
    mode_hint (e.g. "RECITAL") lets the renderer create the right shell without waiting for completed."""
    ev: dict[str, Any] = {"event": "draft_ready", "data": {"text": text}}
    if mode_hint:
        ev["data"]["mode_hint"] = mode_hint
    with _lock:
        if correlation_id in _progress:
            _progress[correlation_id]["events"].append(ev)
    _publish_progress_event(correlation_id, ev)


def get_progress(correlation_id: str) -> tuple[bool, list[str], str]:
    """Return (in_progress, thinking_log_copy, message_so_far). Call from API when polling."""
    with _lock:
        if correlation_id not in _progress:
            return (False, [], "")
        p = _progress[correlation_id]
        return (True, list(p["thinking"]), p["message"])


def get_and_clear_events(correlation_id: str) -> list[dict[str, Any]]:
    """Return and clear pending SSE events for this correlation_id. Used by the stream endpoint."""
    with _lock:
        if correlation_id not in _progress:
            return []
        events = _progress[correlation_id]["events"]
        _progress[correlation_id]["events"] = []
        return list(events)


def clear_progress(correlation_id: str) -> None:
    """Clear progress for correlation_id. Worker calls when done so next poll returns full response.
    Sends sentinel to the serial DB insert queue so its worker thread exits cleanly."""
    with _lock:
        _progress.pop(correlation_id, None)
        _progress_redis_logged.discard(correlation_id)
    # Signal serial DB worker to stop after draining remaining inserts
    with _db_queues_lock:
        q = _db_queues.pop(correlation_id, None)
        _db_workers.pop(correlation_id, None)
    if q is not None:
        q.put(None)  # sentinel — worker exits after draining
def get_progress_from_db(correlation_id: str) -> tuple[list[str], str]:
    """Build thinking_log and message_so_far from DB events. Used when worker runs in separate process (Redis)."""
    events = get_progress_events_from_db(correlation_id, after_id=0)
    thinking: list[str] = []
    message_so_far = ""
    for _ev_id, ev in events:
        ev_data = ev.get("data") or {}
        if not isinstance(ev_data, dict):
            ev_data = {}
        if ev.get("event") == "thinking":
            line = ev_data.get("line") or ""
            if line:
                thinking.append(line)
        elif ev.get("event") == "message":
            message_so_far += ev_data.get("chunk") or ""
        elif ev.get("event") == "quality_audit":
            line = ev_data.get("line") or ""
            if line:
                thinking.append(line)
    return (thinking, message_so_far)


def push_external_thinking(correlation_id: str, line: str) -> None:
    """Push a thinking-log line from an external caller (e.g. RAG via /internal/progress).

    Cross-instance safe: always persists to DB + Redis-publishes via
    _publish_progress_event regardless of which Cloud Run instance handles the
    request. Appends to the local _progress dict only if this instance happens
    to own the turn (no-op otherwise — the DB-backed SSE poll picks it up).

    This is the right primitive for external HTTP push; use append_thinking
    for in-process emits only.
    """
    line = (line or "").strip()
    if not line:
        return
    # Suppress immediate consecutive duplicates from RAG re-emitting the same
    # opening labels on each tool-call. Instance-local check — catches the common
    # case where consecutive POSTs land on the same instance.
    with _lock:
        if _external_last_line.get(correlation_id) == line:
            return
        _external_last_line[correlation_id] = line
    ts, ts_readable = _event_ts()
    ev: dict[str, Any] = {"event": "thinking", "data": {"line": line, "ts": ts, "ts_readable": ts_readable}}
    with _lock:
        if correlation_id in _progress:
            _progress[correlation_id]["thinking"].append(line)
            _progress[correlation_id]["events"].append(ev)
    _publish_progress_event(correlation_id, ev)


def publish_quality_audit_event(correlation_id: str, audit: dict[str, Any], line: str) -> None:
    """Emit a standalone progress event for QC / eval audit (SSE + optional DB replay)."""
    ts, ts_readable = _event_ts()
    ev: dict[str, Any] = {
        "event": "quality_audit",
        "data": {**audit, "line": line, "ts": ts, "ts_readable": ts_readable},
    }
    with _lock:
        if correlation_id in _progress:
            _progress[correlation_id]["thinking"].append(line)
            _progress[correlation_id]["events"].append(ev)
    _publish_progress_event(correlation_id, ev)


def get_progress_events_from_db(correlation_id: str, after_id: int = 0) -> list[tuple[int, dict[str, Any]]]:
    """Poll chat_progress_events for this correlation_id. Returns [(id, {event, data}), ...] for API stream.
    Used when worker runs in separate process (Redis queue); worker persists events to DB.

    db-agent refactor: routes through ``app.db_client.db_query`` instead of
    direct psycopg2. Handles structured errors silently — polling must stay
    quiet (debug-level) since the caller is a tight stream loop.
    """
    from app.db_client import db_query

    result = db_query(
        """
        SELECT id, event_type, event_data
        FROM chat_progress_events
        WHERE correlation_id = :cid AND id > :after
        ORDER BY id ASC
        LIMIT 100
        """,
        "chat",
        params={"cid": correlation_id, "after": after_id},
    )
    err = result.get("error") if isinstance(result, dict) else None
    if err:
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        logger.debug("Failed to poll progress events from DB: %s", msg)
        return []

    def _norm_data(val: Any) -> dict:
        if isinstance(val, dict):
            return val
        if isinstance(val, str):
            try:
                return json.loads(val) if val else {}
            except Exception:
                return {}
        return {}

    cols = result.get("columns") or []
    rows = result.get("rows") or []
    out: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        d = dict(zip(cols, row))
        out.append((
            d["id"],
            {
                "event": d.get("event_type") or "",
                "data": _norm_data(d.get("event_data")),
            },
        ))
    return out
