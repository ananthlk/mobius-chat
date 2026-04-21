"""Worker: consume from request queue → run_pipeline → publish."""
import logging
import os
import signal
import threading
import time

from dotenv import load_dotenv

load_dotenv()
# After .env: force ReAct (mstart also exports MOBIUS_USE_REACT=1 for API+worker).
os.environ["MOBIUS_USE_REACT"] = "1"

from app.queue import get_queue

logger = logging.getLogger(__name__)


# Per-request deadline (2026-04-20 hardening). Without this, a runaway
# turn (pathological LLM output, infinite tool loop, network hang) can
# pin a worker indefinitely and the user sees no response. 90s matches
# P99 of real turns on this stack — headroom for 3 ReAct rounds with
# an LLM rate-limit retry, while still failing loudly for genuinely
# stuck turns.
_DEFAULT_TURN_DEADLINE_S = 90


def _turn_deadline_seconds() -> int:
    raw = (os.environ.get("MOBIUS_TURN_DEADLINE_S") or "").strip()
    if not raw:
        return _DEFAULT_TURN_DEADLINE_S
    try:
        n = int(raw)
        return max(10, min(900, n))  # clamp [10s, 15min]
    except ValueError:
        return _DEFAULT_TURN_DEADLINE_S


class _TurnDeadlineExceeded(Exception):
    """Raised when a turn exceeds its allotted wall-clock budget."""


def _deadline_handler(signum, frame):  # noqa: ARG001 — signal signature
    raise _TurnDeadlineExceeded("turn exceeded deadline")


def process_one(correlation_id: str, payload: dict) -> None:
    """Process one request via pipeline: state_load → classify → plan → clarify → resolve → integrate → publish.

    Wrapped in a wall-clock deadline (2026-04-20). On trip, publishes a
    ``turn_failed`` response via the queue so the user gets a graceful
    message instead of the client polling forever, and logs a loud
    warning so ops sees stuck turns surface immediately.
    """
    from app.pipeline.orchestrator import run_pipeline

    message = payload.get("message", "").strip()
    thread_id = (payload.get("thread_id") or "").strip() or None
    use_react = payload.get("use_react")
    if use_react is not None and not isinstance(use_react, bool):
        use_react = None
    chat_mode = payload.get("chat_mode")
    if chat_mode is not None and not isinstance(chat_mode, str):
        chat_mode = None
    # Phase 2d completion (2026-04-19): user_id comes from POST /chat's
    # require_user dependency, forwarded through the queue so the
    # worker can stamp it on chat_turns for audit attribution. None
    # when auth is disabled in dev or the JWT couldn't be decoded.
    user_id = payload.get("user_id")
    if user_id is not None and not isinstance(user_id, str):
        user_id = None

    deadline_s = _turn_deadline_seconds()
    is_main_thread = threading.current_thread() is threading.main_thread()

    # Two deadline enforcement paths, chosen by execution context:
    #
    # 1. Main thread → signal.alarm (cheap, interrupts Python anywhere).
    #    The standalone ``python -m app.worker`` process hits this path.
    #
    # 2. Background thread (the Cloud Run monolith path where the API's
    #    startup hook spawns start_worker_background) → ThreadPool
    #    timeout. The turn runs in a worker-pool thread; if it exceeds
    #    deadline_s, .result() raises FutureTimeoutError and we publish
    #    a graceful failure. The pool thread keeps running (Python
    #    can't kill a thread mid-syscall), but the outer process_one
    #    returns immediately — so the queue consumer can pick up the
    #    next job without being blocked by a stuck turn.
    #
    # The zombie-thread caveat of path 2 is acceptable: each tool call
    # in run_pipeline has its own timeout (LLM providers ~60s, httpx
    # ~30s, db-client ~15s), so a "hung" turn bounded-deadlines its
    # way out within a few minutes even without forced cancellation.
    # The main user-visible symptom (infinite polling) is fixed.

    def _publish_deadline_failure() -> None:
        try:
            from app.queue import get_queue
            get_queue().publish_response(
                correlation_id,
                {
                    "status": "failed",
                    "message": (
                        "This is taking longer than expected. Please try again — "
                        "if it keeps happening, rephrase your question or try a narrower scope."
                    ),
                    "error": "turn_deadline_exceeded",
                    "deadline_s": deadline_s,
                },
            )
        except Exception as _pub_err:
            logger.exception("Failed to publish deadline-exceeded response: %s", _pub_err)

    def _run_pipeline() -> None:
        run_pipeline(
            correlation_id,
            message,
            thread_id,
            t0_start=time.perf_counter(),
            use_react_override=use_react,
            chat_mode=chat_mode,
            user_id=user_id,
        )

    if is_main_thread and hasattr(signal, "SIGALRM"):
        # Path 1: signal.alarm
        prev_handler = signal.signal(signal.SIGALRM, _deadline_handler)
        signal.alarm(deadline_s)
        try:
            _run_pipeline()
        except _TurnDeadlineExceeded:
            logger.warning(
                "turn_deadline_exceeded correlation_id=%s deadline_s=%d",
                correlation_id, deadline_s,
            )
            _publish_deadline_failure()
        finally:
            signal.alarm(0)
            if prev_handler is not None:
                signal.signal(signal.SIGALRM, prev_handler)
    else:
        # Path 2: thread-pool timeout
        from concurrent.futures import ThreadPoolExecutor
        from concurrent.futures import TimeoutError as FutureTimeoutError
        # One-off executor — cheap to construct, auto-cleans when the
        # enclosing call returns. A persistent pool would save a few ms
        # of thread-start but wouldn't help if a zombie monopolizes it.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="turn") as ex:
            fut = ex.submit(_run_pipeline)
            try:
                fut.result(timeout=deadline_s)
            except FutureTimeoutError:
                logger.warning(
                    "turn_deadline_exceeded correlation_id=%s deadline_s=%d "
                    "(thread-pool path; worker thread may still be running)",
                    correlation_id, deadline_s,
                )
                _publish_deadline_failure()
                # Return control without waiting for the runaway thread.
                # ThreadPoolExecutor's __exit__ would block on it,
                # so we disable wait. The zombie finishes on its own
                # (bounded by the nested tool-call timeouts) and the
                # pool then joins it at GC.
                ex.shutdown(wait=False)


# ── Graceful shutdown ────────────────────────────────────────────────
#
# ``_shutdown_event`` lets the API's on_event("shutdown") hook signal
# the worker to stop pulling new jobs. The queue's ``consume_requests``
# loop polls this between iterations (when the queue impl supports a
# stop predicate). In-flight turns continue — bounded by the 90s
# per-request deadline above, so Cloud Run's container-stop grace
# period (10s default, configurable to 120s via
# ``--max-instances --timeout``) is comfortably enough to drain one
# turn before SIGKILL.
#
# Queue impls that don't accept a stop_fn still honor shutdown via the
# ``KeyboardInterrupt``-like fast path each impl already handles.

_shutdown_event = threading.Event()


def request_shutdown() -> None:
    """Signal the consumer loop to stop accepting new jobs.

    Called by the FastAPI shutdown hook on SIGTERM. Safe to call
    multiple times — the event only latches.
    """
    _shutdown_event.set()


def is_shutting_down() -> bool:
    """Predicate the queue consumer polls between jobs."""
    return _shutdown_event.is_set()


def run_worker() -> None:
    """Blocking: consume requests and process each."""
    try:
        from app.services.model_registry import auto_enable_from_env
        auto_enable_from_env()
    except Exception:
        pass
    q = get_queue()
    # Queue impls that accept a stop predicate (the newer shape) drain
    # cleanly on SIGTERM. Older impls fall back to the original signature
    # — they'll stop on process exit, which is still correct, just less
    # graceful.
    try:
        q.consume_requests(process_one, stop_fn=is_shutting_down)  # type: ignore[call-arg]
    except TypeError:
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
