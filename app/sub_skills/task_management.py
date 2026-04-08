"""Thin HTTP client for the task-manager skill.

Used by domain modules (credentialing, roster, etc.) to push tasks into
the unified mobius_task store without creating circular imports.

v2 — adds emit_signal() so domain code stays clean:

    from app.sub_skills.task_management import emit_signal

    emit_signal("step_start", step_id="nppes_alignment", org=org_name, run_id=run_id)
    # ... do work ...
    emit_signal("step_done",  step_id="nppes_alignment", org=org_name, run_id=run_id,
                data={"matched": 45, "issues": 3})

    emit_signal("blocker", step_id="nppes_alignment", org=org_name, run_id=run_id,
                issue_code="deactivated_npi", provider_npi="1234567890",
                provider_name="Dr. Smith")

The task manager enriches minimal signals into full TaskCards (type, title,
body, actions, roles, detail_payload, interactions) before committing them.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_TASK_MANAGER_DEFAULT = "http://localhost:8015"


def _task_base() -> str:
    return (
        os.environ.get("CHAT_SKILLS_TASK_MANAGER_URL") or _TASK_MANAGER_DEFAULT
    ).rstrip("/")


def _http_post(url: str, body: dict) -> dict[str, Any]:
    """Stdlib-only HTTP POST (no httpx dependency)."""
    import json
    import urllib.request
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _http_patch(url: str, body: dict) -> dict[str, Any]:
    """Stdlib-only HTTP PATCH."""
    import json
    import urllib.request
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="PATCH")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return _http_post(f"{_task_base()}{path}", payload)
    except Exception as exc:
        logger.debug("task_management._post %s failed (non-fatal): %s", path, exc)
        return None


def _patch(path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return _http_patch(f"{_task_base()}{path}", payload)
    except Exception as exc:
        logger.debug("task_management._patch %s failed (non-fatal): %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Signal emission (v2 — preferred interface)
# ---------------------------------------------------------------------------

def emit_signal(
    signal: str,
    *,
    step_id: str = "",
    org: str = "",
    run_id: str | None = None,
    workflow: str = "credentialing",
    data: dict[str, Any] | None = None,
    provider_npi: str | None = None,
    provider_name: str | None = None,
    issue_code: str | None = None,
    source_module: str = "credentialing",
    created_by: str = "system",
    note: str | None = None,
    # optional overrides — only set when you need to force specific card text
    title: str | None = None,
    body: str | None = None,
    detail_payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    Emit a domain signal to the task manager.

    The task manager enriches it into a full TaskCard (type, title, body,
    actions, roles, detail_payload) based on the contract it holds for
    each step/signal combination.

    Returns the committed TaskCard dict, or None if the call fails
    (non-fatal — domain code should not depend on the return value).
    """
    payload: dict[str, Any] = {
        "signal":        signal,
        "step_id":       step_id,
        "org":           org,
        "run_id":        run_id,
        "workflow":      workflow,
        "data":          data or {},
        "source_module": source_module,
        "created_by":    created_by,
    }
    if provider_npi:
        payload["provider_npi"] = provider_npi
    if provider_name:
        payload["provider_name"] = provider_name
    if issue_code:
        payload["issue_code"] = issue_code
    if note:
        payload["note"] = note
    if title:
        payload["title"] = title
    if body:
        payload["body"] = body
    if detail_payload:
        payload.setdefault("data", {})["detail_payload"] = detail_payload

    return _post("/tasks/signal", payload)


# ---------------------------------------------------------------------------
# Running card body patch (called by _emit() wrappers in orchestrators)
# ---------------------------------------------------------------------------

def patch_running_card_body(step_id: str, body_text: str, run_id: str) -> bool:
    """
    Update the `body` field of the running info card for this step/run.
    Non-fatal — silently returns False on any failure.
    """
    if not run_id or not step_id:
        return False
    result = _post("/tasks/patch-running", {
        "run_id":  run_id,
        "step_id": step_id,
        "body":    body_text,
    })
    return bool((result or {}).get("patched"))


# ---------------------------------------------------------------------------
# Legacy bulk import (v1 compat — kept for roster_truth_pg.py)
# ---------------------------------------------------------------------------

def bulk_import_tasks(
    tasks: list[dict[str, Any]],
    *,
    org_name: str | None = None,
    source_module: str = "roster_open",
) -> int:
    """POST tasks to task-manager bulk-import. Returns count imported."""
    if not tasks:
        return 0
    enriched = []
    for t in tasks:
        enriched.append({
            **t,
            "org_name": org_name or t.get("org_name") or "",
            "source_module": t.get("source_module") or source_module,
        })
    result = _post("/tasks/bulk-import", {"tasks": enriched})
    return int((result or {}).get("imported", 0))


# ---------------------------------------------------------------------------
# Interaction / lifecycle helpers
# ---------------------------------------------------------------------------

def interact(task_id: str, actor: str, action: str, note: str | None = None) -> bool:
    """Append an interaction record to a task."""
    result = _post(f"/tasks/{task_id}/interact", {
        "actor": actor,
        "action": action,
        "note": note,
    })
    return bool(result)


def resolve_task_remote(task_id: str, resolved_by: str = "system", note: str | None = None) -> bool:
    result = _post(f"/tasks/{task_id}/resolve", {"resolved_by": resolved_by, "note": note})
    return bool(result)


def dismiss_task_remote(task_id: str, dismissed_by: str = "system") -> bool:
    result = _post(f"/tasks/{task_id}/dismiss", {"dismissed_by": dismissed_by})
    return bool(result)


def patch_task_remote(task_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    return _patch(f"/tasks/{task_id}", updates)
