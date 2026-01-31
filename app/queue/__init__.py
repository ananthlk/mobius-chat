from app.config import get_config
from app.queue.base import QueueAdapter
from app.queue.memory import MemoryQueue
from app.queue.redis_queue import RedisQueue

_queue: QueueAdapter | None = None


def get_queue() -> QueueAdapter:
    global _queue
    if _queue is None:
        cfg = get_config()
        if cfg.queue_type == "memory":
            _queue = MemoryQueue()
        elif cfg.queue_type == "redis":
            _queue = RedisQueue()
        else:
            raise ValueError(f"Unsupported QUEUE_TYPE: {cfg.queue_type}. Use memory or redis.")
    return _queue
