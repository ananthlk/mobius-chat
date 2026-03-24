"""Entry point for: python -m app.worker (standalone worker process)."""
import logging
import sys

from app.worker.run import run_worker

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [worker] %(levelname)s %(message)s",
    )
    try:
        run_worker()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        logging.exception("Chat worker exited with error: %s", e)
        sys.exit(1)
