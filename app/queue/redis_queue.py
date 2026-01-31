"""Redis queue: chat requests go to a Redis list; responses to a key per correlation_id.

Request flow: API (or any client) LPUSHes to a Redis list; worker BRPOPs. So the chat
question is written to a Redis list (FIFO). Response flow: worker SETs a key per
correlation_id with TTL; API GETs by correlation_id.
"""
import json
import logging
from typing import Any, Callable

from app.config import get_config
from app.queue.base import QueueAdapter

logger = logging.getLogger(__name__)


class RedisQueue(QueueAdapter):
    """Request queue = Redis list (LPUSH/BRPOP). Response = Redis key per correlation_id (SET/GET) with TTL."""

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        request_key: str | None = None,
        response_key_prefix: str | None = None,
        response_ttl_seconds: int | None = None,
    ) -> None:
        cfg = get_config()
        self._redis_url = redis_url or cfg.redis_url
        self._request_key = request_key or cfg.redis_request_key
        self._response_prefix = response_key_prefix or cfg.redis_response_key_prefix
        self._response_ttl = response_ttl_seconds if response_ttl_seconds is not None else cfg.redis_response_ttl_seconds
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import redis
            except ImportError as e:
                raise ImportError("Redis queue requires: pip install redis") from e
            self._client = redis.from_url(self._redis_url, decode_responses=True)
        return self._client

    def publish_request(self, correlation_id: str, payload: dict[str, Any]) -> None:
        """Write chat request to Redis list (LPUSH). Worker consumes with BRPOP."""
        item = {"correlation_id": correlation_id, **payload}
        r = self._get_client()
        r.lpush(self._request_key, json.dumps(item))  # Redis list: left push
        logger.debug("Published request %s to list %s", correlation_id, self._request_key)

    def consume_requests(self, callback: Callable[[str, dict], None]) -> None:
        """Read from Redis list (BRPOP). Blocks until a request is available."""
        r = self._get_client()
        while True:
            try:
                # Redis list: BRPOP = block until item available (FIFO with LPUSH)
                result = r.brpop(self._request_key, timeout=5)
                if result is None:
                    continue
                _, raw = result
                item = json.loads(raw)
                cid = item.pop("correlation_id", "")
                callback(cid, item)
            except Exception as e:
                logger.exception("Request consumer error: %s", e)

    def publish_response(self, correlation_id: str, payload: dict[str, Any]) -> None:
        key = self._response_prefix + correlation_id
        r = self._get_client()
        r.set(key, json.dumps(payload), ex=self._response_ttl)
        logger.debug("Published response %s", correlation_id)

    def get_response(self, correlation_id: str) -> dict[str, Any] | None:
        key = self._response_prefix + correlation_id
        r = self._get_client()
        raw = r.get(key)
        if raw is None:
            return None
        return json.loads(raw)
