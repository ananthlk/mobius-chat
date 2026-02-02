"""
Trace log: when CHAT_DEBUG_TRACE or DEBUG_TRACE is on, log every key module/function entry.
Use to pinpoint call flow (which modules and functions run for a request).

Set in .env:
  CHAT_DEBUG_TRACE=1
  # or
  DEBUG_TRACE=1

Accepted values for "on": 1, true, yes (case-insensitive).
"""
import logging
import os
from functools import wraps
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

TRACE_ENV_KEYS = ("CHAT_DEBUG_TRACE", "DEBUG_TRACE")
_TRACE_ENABLED: bool | None = None


def is_trace_enabled() -> bool:
    """True if trace mode is on (CHAT_DEBUG_TRACE or DEBUG_TRACE = 1, true, yes, on; or any non-empty except 0/false/no)."""
    global _TRACE_ENABLED
    if _TRACE_ENABLED is not None:
        return _TRACE_ENABLED
    for key in TRACE_ENV_KEYS:
        v = (os.environ.get(key) or "").strip().lower()
        if v in ("1", "true", "yes", "on"):
            _TRACE_ENABLED = True
            return True
        if v and v not in ("0", "false", "no"):
            _TRACE_ENABLED = True
            return True
    _TRACE_ENABLED = False
    return False


def trace_log(component: str, message: str = "", **kwargs: Any) -> None:
    """
    Log a trace line when trace mode is on.
    component: e.g. "worker.run.process_one", "chat_config.get_chat_config"
    message: optional extra (e.g. "entered", "exited")
    kwargs: optional key=value to append (e.g. kind="non_patient")
    """
    if not is_trace_enabled():
        return
    parts = [f"[trace] {component}"]
    if message:
        parts.append(message)
    if kwargs:
        parts.append(" ".join(f"{k}={v!r}" for k, v in kwargs.items()))
    logger.info(" ".join(parts))


def trace_entered(component: str, **kwargs: Any) -> None:
    """Convenience: trace_log(component, "entered", **kwargs)."""
    trace_log(component, "entered", **kwargs)


def trace_exited(component: str, **kwargs: Any) -> None:
    """Convenience: trace_log(component, "exited", **kwargs)."""
    trace_log(component, "exited", **kwargs)


F = TypeVar("F", bound=Callable[..., Any])


def trace_calls(component: str | None = None) -> Callable[[F], F]:
    """
    Decorator: log "component entered" / "component exited" when trace is on.
    If component is None, use "{module}.{qualname}".
    """

    def decorator(f: F) -> F:
        name = component or f"{f.__module__}.{f.__qualname__}"

        @wraps(f)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            trace_entered(name)
            try:
                out = f(*args, **kwargs)
                trace_exited(name)
                return out
            except Exception as e:
                trace_log(name, "raised", error=type(e).__name__, message=str(e)[:80])
                raise

        return wrapped  # type: ignore[return-value]

    return decorator
