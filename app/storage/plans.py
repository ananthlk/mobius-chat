"""Store and retrieve planner output by correlation_id."""
from __future__ import annotations

import threading
from typing import Any

_store: dict[str, dict] = {}
_lock = threading.Lock()


def store_plan(
    correlation_id: str,
    plan: Any,
    thinking_log: list[str] | None = None,
) -> None:
    """Store plan (and optional thinking_log) for correlation_id."""
    if isinstance(plan, dict):
        plan_dict = plan
    else:
        plan_dict = plan.model_dump() if hasattr(plan, "model_dump") else plan.dict()
    payload = {
        "plan": plan_dict,
        "thinking_log": thinking_log or getattr(plan, "thinking_log", []),
    }
    with _lock:
        _store[correlation_id] = payload


def get_plan(correlation_id: str) -> dict | None:
    """Return stored plan payload for correlation_id, or None."""
    with _lock:
        return _store.get(correlation_id)
