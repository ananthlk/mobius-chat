"""Live progress store: thinking_log and message_so_far streamed per correlation_id so clients can poll and see progress."""
import threading

_progress: dict[str, dict] = {}  # correlation_id -> {"thinking": list[str], "message": str}
_lock = threading.Lock()


def start_progress(correlation_id: str) -> None:
    """Mark correlation_id as in progress. Worker calls this at start of process_one."""
    with _lock:
        _progress[correlation_id] = {"thinking": [], "message": ""}


def append_thinking(correlation_id: str, chunk: str) -> None:
    """Append one thinking chunk. Worker's on_thinking should call this so clients see live updates."""
    with _lock:
        if correlation_id in _progress and chunk.strip():
            _progress[correlation_id]["thinking"].append(chunk.strip())


def append_message_chunk(correlation_id: str, chunk: str) -> None:
    """Append one chunk to the streaming final message. Integrator calls this so clients see the draft stream."""
    with _lock:
        if correlation_id in _progress:
            _progress[correlation_id]["message"] += chunk


def get_progress(correlation_id: str) -> tuple[bool, list[str], str]:
    """Return (in_progress, thinking_log_copy, message_so_far). Call from API when polling."""
    with _lock:
        if correlation_id not in _progress:
            return (False, [], "")
        p = _progress[correlation_id]
        return (True, list(p["thinking"]), p["message"])


def clear_progress(correlation_id: str) -> None:
    """Clear progress for correlation_id. Worker calls when done so next poll returns full response."""
    with _lock:
        _progress.pop(correlation_id, None)
