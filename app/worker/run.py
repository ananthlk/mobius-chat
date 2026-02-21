"""Worker: consume from request queue → run_pipeline → publish."""
import logging
import threading
import time

from dotenv import load_dotenv
load_dotenv()

from app.queue import get_queue

logger = logging.getLogger(__name__)


def process_one(correlation_id: str, payload: dict) -> None:
    """Process one request via pipeline: state_load → classify → plan → clarify → resolve → integrate → publish."""
    from app.pipeline.orchestrator import run_pipeline

    message = payload.get("message", "").strip()
    thread_id = (payload.get("thread_id") or "").strip() or None
    run_pipeline(correlation_id, message, thread_id, t0_start=time.perf_counter())


def run_worker() -> None:
    """Blocking: consume requests and process each."""
    q = get_queue()
    q.consume_requests(process_one)


def start_worker_background() -> threading.Thread:
    """Start the worker in a background thread. Use for in-memory queue (single process)."""
    t = threading.Thread(target=run_worker, daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    """Run worker standalone: python -m app.worker. Use with Redis queue so API and worker are separate."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [worker] %(levelname)s %(message)s")
    run_worker()
