"""Tasks proxy endpoints (Phase 1f.1).

Thin proxy to the task-manager skill at ``CHAT_SKILLS_TASK_MANAGER_URL``.
None of these endpoints contains business logic — they forward to the
skill and return its response verbatim (or, for the list endpoint,
annotate it with a single cross-skill run_status lookup).

Routes:
    GET   /chat/tasks                          list tasks (filterable)
    POST  /chat/tasks                          create a manual task
    GET   /chat/tasks/export                   CSV export
    POST  /chat/tasks/bulk-import              bulk upsert from orchestrator
    GET   /chat/tasks/{task_id}                fetch one
    PATCH /chat/tasks/{task_id}                update fields
    POST  /chat/tasks/{task_id}/resolve        mark resolved
    POST  /chat/tasks/{task_id}/dismiss        dismiss

Extracted from ``app/main.py`` as Phase 1f.1 of the main-split refactor.
The proxy helper moved to ``app.api._common.task_proxy`` because the
/chat/runs aggregator also needs it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import PlainTextResponse

from app.api._common import task_proxy

router = APIRouter()


@router.get("/chat/tasks")
def chat_tasks_list(
    org_name: str | None = None,
    module: str | None = None,
    status: str | None = None,
    assignee: str | None = None,
    npi: str | None = None,
    run_id: str | None = None,
    severity: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """Proxy: list tasks from task-manager skill. Injects run_status when run_id provided."""
    params = {k: v for k, v in {
        "org_name": org_name, "module": module, "status": status,
        "assignee": assignee, "npi": npi, "run_id": run_id,
        "severity": severity, "limit": limit, "offset": offset,
    }.items() if v is not None}
    result = task_proxy("GET", "/tasks", params=params).json()

    # Cross-run + status=open: blockers first, then decisions, then others
    # — all ordered by created_at ascending. Matches pre-extraction main.py
    # behavior exactly (Phase 1f.1 is a move, not a rewrite).
    if not run_id and status == "open":
        _TYPE_PRIORITY = {"blocker": 0, "decision": 1}
        result["tasks"] = sorted(
            result.get("tasks", []),
            key=lambda t: (
                _TYPE_PRIORITY.get(t.get("type", ""), 2),
                t.get("created_at", ""),
            ),
        )

    # Inject run_status so the frontend knows when to stop polling. Cheap
    # cross-skill join — kept here (not in task-manager) because the phase
    # state lives in chat's credentialing_runs_pg, not in task-manager.
    if run_id:
        try:
            from app.services.credentialing_run_service import get_credentialing_run
            rec = get_credentialing_run(run_id)
            rec_data = rec or {}
            phase = rec_data.get("phase", "")
            pending_step = rec_data.get("pending_step_id") or ""
            if phase == "running":
                run_status = "running"
            elif phase == "awaiting_validation":
                run_status = "awaiting_validation"
                result["pending_step_id"] = pending_step
            elif phase == "complete":
                run_status = "complete"
            elif phase == "error":
                run_status = "error"
            else:
                run_status = "paused"
        except Exception:
            # Intentionally swallow — this field is a frontend polling
            # optimization, not semantic. Missing == "unknown" is safe.
            run_status = "unknown"
        result["run_status"] = run_status

    return result


@router.post("/chat/tasks")
def chat_tasks_create(body: dict = Body(...)) -> dict[str, Any]:
    """Proxy: create a manual task."""
    return task_proxy("POST", "/tasks", json_body=body).json()


@router.get("/chat/tasks/export")
def chat_tasks_export(
    org_name: str | None = None,
    module: str | None = None,
    status: str | None = None,
) -> PlainTextResponse:
    """Proxy: export tasks as CSV.

    Note: path order matters in FastAPI — this must register BEFORE
    /chat/tasks/{task_id} so "export" isn't captured as a task_id. Since
    the router collects routes in definition order, this stays above
    the {task_id} handlers just like in the pre-extraction main.py.
    """
    params = {k: v for k, v in {
        "org_name": org_name, "module": module, "status": status,
    }.items() if v is not None}
    r = task_proxy("GET", "/tasks/export", params=params)
    return PlainTextResponse(
        content=r.text,
        media_type="text/csv",
        headers={
            "Content-Disposition": r.headers.get(
                "Content-Disposition", 'attachment; filename="tasks.csv"'
            ),
        },
    )


@router.post("/chat/tasks/bulk-import")
def chat_tasks_bulk_import(body: dict = Body(...)) -> dict[str, Any]:
    """Proxy: bulk upsert tasks (used by orchestrator and skills)."""
    return task_proxy("POST", "/tasks/bulk-import", json_body=body).json()


@router.get("/chat/tasks/{task_id}")
def chat_tasks_get(task_id: str) -> dict[str, Any]:
    """Proxy: fetch a single task."""
    return task_proxy("GET", f"/tasks/{task_id}").json()


@router.patch("/chat/tasks/{task_id}")
def chat_tasks_patch(task_id: str, body: dict = Body(...)) -> dict[str, Any]:
    """Proxy: update task fields (status, assignee, deadline, notes, etc.)."""
    return task_proxy("PATCH", f"/tasks/{task_id}", json_body=body).json()


@router.post("/chat/tasks/{task_id}/resolve")
def chat_tasks_resolve(task_id: str, body: dict = Body(default={})) -> dict[str, Any]:
    """Proxy: mark a task resolved."""
    return task_proxy("POST", f"/tasks/{task_id}/resolve", json_body=body).json()


@router.post("/chat/tasks/{task_id}/dismiss")
def chat_tasks_dismiss(task_id: str, body: dict = Body(default={})) -> dict[str, Any]:
    """Proxy: dismiss a task."""
    return task_proxy("POST", f"/tasks/{task_id}/dismiss", json_body=body).json()
