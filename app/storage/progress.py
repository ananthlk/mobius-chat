"""Live progress store: thinking_log and message_so_far streamed per correlation_id so clients can poll and see progress.
Supports SSE: append_* also push events to a per-id queue so GET /chat/stream/:id can yield them in real time.
When queue_type=redis, worker runs in a separate process so we also PUBLISH each event to Redis for the API to stream."""
import json
import logging
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


def _publish_progress_event(correlation_id: str, ev: dict[str, Any]) -> None:
    """If queue is Redis, publish event in a background thread so the worker never blocks on Redis."""
    try:
        from app.config import get_config
        cfg = get_config()
        if getattr(cfg, "queue_type", "memory") != "redis":
            return
    except Exception:
        return
    t = threading.Thread(
        target=_publish_progress_event_impl,
        args=(correlation_id, ev),
        daemon=True,
        name="progress-publish",
    )
    t.start()


def start_progress(correlation_id: str) -> None:
    """Mark correlation_id as in progress. Worker calls this at start of process_one."""
    with _lock:
        _progress[correlation_id] = {"thinking": [], "message": "", "events": []}


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
    for ev in to_publish:
        _publish_progress_event(correlation_id, ev)


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
    """Clear progress for correlation_id. Worker calls when done so next poll returns full response."""
    with _lock:
        _progress.pop(correlation_id, None)
        _progress_redis_logged.discard(correlation_id)
