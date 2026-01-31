"""Store and retrieve final response by correlation_id."""
import threading

_store: dict[str, dict] = {}
_lock = threading.Lock()


def store_response(correlation_id: str, response: dict) -> None:
    """Store response for correlation_id. response has 'status', 'message', etc."""
    with _lock:
        _store[correlation_id] = response


def get_response(correlation_id: str) -> dict | None:
    """Return stored response for correlation_id, or None."""
    with _lock:
        return _store.get(correlation_id)
