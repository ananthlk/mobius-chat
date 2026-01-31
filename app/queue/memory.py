"""In-memory queue for local dev. Single process: API enqueues, worker consumes."""
import logging
import queue
import threading
from typing import Any, Callable

from app.queue.base import QueueAdapter

logger = logging.getLogger(__name__)

# Shared in-memory queues (module-level for same-process API + worker)
_request_queue: queue.Queue = queue.Queue()
_response_store: dict[str, dict[str, Any]] = {}
_response_store_lock = threading.Lock()


class MemoryQueue(QueueAdapter):
    """In-memory request queue; responses stored in dict for polling."""

    def publish_request(self, correlation_id: str, payload: dict[str, Any]) -> None:
        _request_queue.put({"correlation_id": correlation_id, **payload})

    def consume_requests(self, callback: Callable[[str, dict], None]) -> None:
        while True:
            try:
                item = _request_queue.get(timeout=1.0)
                cid = item.pop("correlation_id", "")
                callback(cid, item)  # payload = { "message": "...", ... }
            except queue.Empty:
                continue
            except Exception as e:
                logger.exception("Request consumer error: %s", e)

    def publish_response(self, correlation_id: str, payload: dict[str, Any]) -> None:
        with _response_store_lock:
            _response_store[correlation_id] = payload

    def get_response(self, correlation_id: str) -> dict[str, Any] | None:
        with _response_store_lock:
            return _response_store.get(correlation_id)
