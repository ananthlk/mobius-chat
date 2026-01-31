"""Queue abstraction: chat question in, response out by correlation_id.

Flow:
  1. Client writes a chat question → publish_request(correlation_id, { message, ... })
  2. Background worker reads from queue → consume_requests(callback)
  3. Worker processes and writes response → publish_response(correlation_id, { status, message, ... })
  4. Client gets response by correlation_id → get_response(correlation_id)

Implementations: MemoryQueue (single process), RedisQueue (API and worker can be separate).
"""
from abc import ABC, abstractmethod
from typing import Any, Callable


class QueueAdapter(ABC):
    """Abstract queue: request in, response out by correlation_id. Plug-and-play backend."""

    @abstractmethod
    def publish_request(self, correlation_id: str, payload: dict[str, Any]) -> None:
        """Enqueue a chat request. payload: { message, session_id? }. Worker will consume."""
        pass

    @abstractmethod
    def consume_requests(self, callback: Callable[[str, dict], None]) -> None:
        """Blocking: consume requests, call callback(correlation_id, payload). Run in worker process."""
        pass

    @abstractmethod
    def publish_response(self, correlation_id: str, payload: dict[str, Any]) -> None:
        """Publish response for correlation_id. Client polls get_response(correlation_id)."""
        pass

    def get_response(self, correlation_id: str) -> dict[str, Any] | None:
        """Get response by correlation_id (for polling). Returns None if not ready."""
        return None
