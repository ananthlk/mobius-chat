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

from fastapi import APIRouter, Body, Depends
from fastapi.responses import PlainTextResponse

from app.api._common import task_proxy
from app.api.front_door import require_user

router = APIRouter()


# Phase 1h: write-path endpoints go through ``require_user``. Behavior is
# mode-driven (see front_door.auth_mode):
#   - CHAT_AUTH_MODE=off       (dev default) — dependency returns None, all
#                              routes execute as before.
#   - CHAT_AUTH_MODE=optional  — decodes JWT when present but doesn't 401.
#   - CHAT_AUTH_MODE=required  (hosted default) — 401 if no valid JWT.
# Read endpoints (list/export/get) intentionally aren't guarded yet; first
# tighten writes, then widen to reads in a follow-up once we see what the
# frontend actually needs to send.


@router.get("/chat/tasks")
def chat_tasks_list(
    org_name: str | None = None,
    module: str | None = None,
    status: str | None = None,
    assignee: str | None = None,
    npi: str | None = None,
    run_id: str | None = None,
    severity: str | None = None,
    audience: str | None = "user",
    kind: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """Proxy: list tasks from task-manager skill.

    Defaults to audience=user — this is the USER-facing surface (Tasks
    modal, task_list blocks); system telemetry stays out unless the
    caller passes audience=developer or audience=all. Run-scoped reads
    (run_id set) drop the audience filter so pipeline views still see
    their step/signal cards.
    """
    if run_id or audience == "all":
        audience = None
    params = {k: v for k, v in {
        "org_name": org_name, "module": module, "status": status,
        "assignee": assignee, "npi": npi, "run_id": run_id,
        "severity": severity, "audience": audience, "kind": kind,
        "limit": limit, "offset": offset,
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

    # 2026-04-18 disconnect: removed the credentialing_run_service
    # lookup that injected run_status ("running"/"awaiting_validation"/
    # "complete"/"error"/"paused"). The frontend polling loop that
    # consumed that field also got removed in commit 1. When
    # credentialing rebuilds as a proper skill integration, this
    # cross-skill join belongs in task-manager (or is replaced by a
    # polling endpoint on the credentialing skill itself), not here.
    return result


@router.get("/chat/whoami")
def chat_whoami(user_id: str | None = Depends(require_user)) -> dict[str, Any]:
    """Frontend identity echo: authenticated chat user → canonical
    assignee identity via mobius-user (server-side client; the internal
    key never reaches the browser). Powers per-user reminder-nudge
    scoping + the assignment banner. Unknown identity → {ok: false} —
    callers fall back to unscoped behavior."""
    from app.services.user_identity import resolve_self
    me = resolve_self(user_id)
    if not me:
        return {"ok": False}
    return {"ok": True, "user": {
        "user_id": me.get("user_id"),
        "display_name": me.get("display_name"),
        "assignee_ref": me.get("assignee_ref"),
        "greeting": me.get("greeting"),
        "org_memberships": me.get("org_memberships") or [],
    }}


@router.get("/chat/coworkers")
def chat_coworkers(
    q: str | None = None,
    limit: int = 20,
    user_id: str | None = Depends(require_user),
) -> dict[str, Any]:
    """Org-scoped coworker directory for @-mention autocomplete.

    Derives org_slug from the caller's own identity — the browser never
    passes an org, so callers cannot enumerate other orgs' rosters.
    Excludes the caller from the result list."""
    from app.services.user_identity import resolve_self, directory_search
    me = resolve_self(user_id)
    if not me:
        return {"ok": False, "coworkers": []}
    memberships = me.get("org_memberships") or []
    org_slug = memberships[0].get("org_slug") if memberships else None
    if not org_slug:
        return {"ok": True, "coworkers": []}
    members = directory_search(
        org_slug=org_slug,
        q=q or None,
        limit=min(limit, 30),
        exclude_user_id=me.get("user_id"),
    )
    return {"ok": True, "coworkers": [
        {k: m.get(k) for k in ("user_id", "display_name", "email", "assignee_ref", "is_agent", "roles") if m.get(k) is not None}
        for m in members
    ]}


@router.post("/chat/tasks")
def chat_tasks_create(
    body: dict = Body(...),
    user_id: str | None = Depends(require_user),
) -> dict[str, Any]:
    """Proxy: create a manual task.

    Reminders auto-assign to their creator when no assignee is given —
    "remind me to X" must land in the creator's own queue or per-user
    nudge scoping would hide it."""
    if (body.get("kind") == "reminder"
            and not body.get("assigned_to") and not body.get("assignee")):
        from app.services.user_identity import resolve_self
        me = resolve_self(user_id)
        if me:
            body["assigned_to"] = me["assignee_ref"]
            body["assignee"] = me.get("display_name") or me["assignee_ref"]
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
def chat_tasks_bulk_import(
    body: dict = Body(...),
    _user_id: str | None = Depends(require_user),
) -> dict[str, Any]:
    """Proxy: bulk upsert tasks (used by orchestrator and skills)."""
    return task_proxy("POST", "/tasks/bulk-import", json_body=body).json()


@router.get("/chat/tasks/{task_id}")
def chat_tasks_get(task_id: str) -> dict[str, Any]:
    """Proxy: fetch a single task."""
    return task_proxy("GET", f"/tasks/{task_id}").json()


@router.patch("/chat/tasks/{task_id}")
def chat_tasks_patch(
    task_id: str,
    body: dict = Body(...),
    _user_id: str | None = Depends(require_user),
) -> dict[str, Any]:
    """Proxy: update task fields (status, assignee, deadline, notes, etc.)."""
    return task_proxy("PATCH", f"/tasks/{task_id}", json_body=body).json()


@router.post("/chat/tasks/{task_id}/resolve")
def chat_tasks_resolve(
    task_id: str,
    body: dict = Body(default={}),
    _user_id: str | None = Depends(require_user),
) -> dict[str, Any]:
    """Proxy: mark a task resolved."""
    return task_proxy("POST", f"/tasks/{task_id}/resolve", json_body=body).json()


@router.post("/chat/tasks/{task_id}/dismiss")
def chat_tasks_dismiss(
    task_id: str,
    body: dict = Body(default={}),
    _user_id: str | None = Depends(require_user),
) -> dict[str, Any]:
    """Proxy: dismiss a task."""
    return task_proxy("POST", f"/tasks/{task_id}/dismiss", json_body=body).json()
