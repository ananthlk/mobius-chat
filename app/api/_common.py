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
