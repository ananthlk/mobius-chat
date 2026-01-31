"""Entry point for: python -m app.worker (standalone worker process)."""
import logging

from app.worker.run import run_worker

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [worker] %(levelname)s %(message)s",
    )
    run_worker()
