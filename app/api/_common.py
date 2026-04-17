"""Shared helpers for the ``app.api`` routers (Phase 1e).

Consolidates tiny cross-router utilities that were duplicated during the
Phase 1a–1d extraction. Each helper here is either:

- used by two or more routers (genuine shared surface), or
- used by one router and main.py during the migration
  (staying here prevents backsliding to inline duplicates).

Rule: before duplicating a 3+ line helper into a new router, add it here.
If a helper is only one router's concern, keep it local to that router.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException


def task_manager_base_url() -> str:
    """Base URL of the task-manager skill server.

    Was duplicated as ``_task_manager_base`` in main.py, app.api.credentialing,
    and app.api.roster during Phase 1a–1d. Single source of truth going forward.

    Returns empty string if the env var is unset — callers MUST check and
    raise 503 if they require the skill server. This signature matches the
    pre-consolidation behavior exactly.
    """
    return (
        os.environ.get("CHAT_SKILLS_TASK_MANAGER_URL") or "http://localhost:8015"
    ).rstrip("/")


def task_proxy(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    timeout: float = 15.0,
):
    """Generic proxy helper for task-manager skill calls.

    Lifted out of main.py in Phase 1f.1. Used by both the tasks router and
    the /chat/runs aggregator, so it belongs in shared surface. Raises
    HTTPException on failure with fidelity:

    - 503 when the skill URL is unset (prevents silent localhost fallback)
    - 404 mapped through from the skill
    - 422 mapped through from the skill (body forwarded)
    - 502 for any other transport / upstream error
    """
    import httpx

    base = task_manager_base_url()
    if not base:
        raise HTTPException(
            status_code=503,
            detail="Task manager skill not configured (CHAT_SKILLS_TASK_MANAGER_URL)",
        )
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.request(method, f"{base}{path}", params=params, json=json_body)
            if r.status_code == 404:
                raise HTTPException(status_code=404, detail="Task not found")
            if r.status_code == 422:
                raise HTTPException(status_code=422, detail=r.json())
            r.raise_for_status()
            return r
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Task manager error: {e}")
