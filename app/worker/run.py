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
    # system_context (2026-04-22): pre-loaded ground-truth context from a
    # caller that has structured data already (story layer, skill cards).
    # Triggers ReAct Round 0 short-circuit when answerable from the
    # context alone. None when the caller sent only a message.
    system_context = payload.get("system_context")
    if system_context is not None and not isinstance(system_context, str):
        system_context = None
    # Normalize empty/whitespace-only to None so downstream `if system_context`
    # checks are correct.
    if isinstance(system_context, str) and not system_context.strip():
        system_context = None
    # cache_assist (2026-04-23): per-turn override. True/False only;
    # ignore malformed values (callers shouldn't send them, but don't
    # let a bad flag break the turn).
    cache_assist = payload.get("cache_assist")
    if cache_assist is not None and not isinstance(cache_assist, bool):
        cache_assist = None
    # model_profile (2026-04-27): per-turn override that travels with
    # the chat request payload (set by the UI dropdown / API caller via
    # ChatRequest.model_profile). Worker enters profile_override(...)
    # around run_pipeline so resolution is correct for this turn only;
    # other concurrent turns on this worker are unaffected. Unknown
    # profile names get a warning + ignored — bad payload from a stale
    # frontend shouldn't kill the turn.
    model_profile = payload.get("model_profile")
    if model_profile is not None and not isinstance(model_profile, str):
        model_profile = None
    if isinstance(model_profile, str):
        model_profile = model_profile.strip().lower() or None

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
        from app.services.model_profile import profile_override
        with profile_override(model_profile):
            run_pipeline(
                correlation_id,
                message,
                thread_id,
                t0_start=time.perf_counter(),
                use_react_override=use_react,
                chat_mode=chat_mode,
                user_id=user_id,
                system_context=system_context,
                cache_assist=cache_assist,
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
        # Path 2: daemon-thread timeout
        #
        # First attempt used ThreadPoolExecutor with ``with`` + on-timeout
        # ``shutdown(wait=False)``. That looked right but the ``with``
        # block's ``__exit__`` called shutdown(wait=True) again, which
        # re-joined the zombie thread — effectively blocking the whole
        # queue consumer from picking up the next turn. Observed
        # downstream: one timed-out turn stalled 5 subsequent queued
        # turns for ~15 min until we caught it in smoke.
        #
        # Fix: raw daemon ``threading.Thread`` + ``Event``. A daemon
        # thread doesn't block process exit, and we never join the
        # zombie — we just return control the moment the deadline
        # trips. The thread continues to run in the background,
        # bounded by the nested tool-call timeouts (LLM 60s, httpx
        # 30s, db 15s) so it finishes on its own within a few minutes.
        done = threading.Event()
        exc_holder: list[BaseException] = []

        def _target() -> None:
            try:
                _run_pipeline()
            except BaseException as e:  # noqa: BLE001  — forwarded below
                exc_holder.append(e)
            finally:
                done.set()

        t = threading.Thread(
            target=_target,
            name=f"turn-{correlation_id[:8]}" if correlation_id else "turn",
            daemon=True,
        )
        t.start()
        finished_in_time = done.wait(timeout=deadline_s)
        if not finished_in_time:
            logger.warning(
                "turn_deadline_exceeded correlation_id=%s deadline_s=%d "
                "(daemon-thread path; worker thread may still be running)",
                correlation_id, deadline_s,
            )
            _publish_deadline_failure()
            # Don't join — we return to the queue consumer immediately.
            # The zombie thread keeps running; it's a daemon so it
            # won't hold the process open, and the next turn starts
            # right away.
            return
        # Pipeline finished within deadline — propagate any exception
        # so the queue consumer logs it normally (matches legacy path
        # 1 behavior).
        if exc_holder:
            raise exc_holder[0]


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
    # MCP tool registration (mirrors app.main on_startup hook).
    # Worker is a separate process — it must register MCP skills so that
    # get_tool_manifest() in the planner includes auto-discovered tools.
    # Best-effort: if the MCP server is down, we continue with builtins only.
    try:
        from app.skills.mcp_adapter import register_mcp_skills
        register_mcp_skills()
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
