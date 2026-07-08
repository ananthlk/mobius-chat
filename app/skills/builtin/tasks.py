"""Builtin skills: ``list_tasks`` / ``create_task`` / ``resolve_task``.

CRUD bridge between chat and the standalone task-manager skill (the
FastAPI service at ``mobius-skills/task-manager``). Before this module,
chat dispatched these three tool names through a hand-rolled ``if tool
in (...)`` branch in ``app/pipeline/react_loop.py`` that lived outside
the SkillSpec registry — meaning the planner manifest, the per-user
tool policy, and the analytics view all had to be hand-maintained in
parallel. Migrating to the registry collapses those to one spec.

The actual structured response (rows for the ``task_list`` UI block)
flows via ``app/skills/task_envelope.py::TaskEnvelope``. See that
module for the dual-channel rationale (``pipeline_ctx`` for the legacy
UI block + ``SkillEnvelope.extra`` for non-pipeline consumers).

Stub behavior — when ``CHAT_SKILLS_TASK_MANAGER_URL`` is unset, points
at ``.invalid``, or contains ``not-yet-deployed`` — is preserved here
so the user-facing message is identical to the legacy branch in dev
environments where the task-manager isn't running.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

from app.skills.registry import SkillCall, SkillEnvelope, SkillSpec, register
from app.skills.task_envelope import TaskEnvelope, TaskRow

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_S = 10.0


# ── HTTP helpers (stdlib-only, mirrors app/sub_skills/task_management.py) ──


def _task_base() -> str:
    """Resolve task-manager base URL, lowercased + trimmed of trailing slash."""
    return (
        os.environ.get("CHAT_SKILLS_TASK_MANAGER_URL") or "http://localhost:8015"
    ).rstrip("/")


def _is_stub_url(url: str) -> bool:
    """True when the configured URL is a placeholder, not a real endpoint.

    Matches the legacy ``react_loop`` checks so dev environments that
    deliberately set the URL to a sentinel get the same friendly message.
    """
    if not url:
        return True
    return ".invalid" in url or "not-yet-deployed" in url


def _http_request(method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def _http_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET with stdlib urlencode — keeps the dependency surface tiny."""
    from urllib.parse import urlencode
    qs = urlencode({k: v for k, v in params.items() if v is not None and v != ""})
    url = f"{_task_base()}{path}"
    if qs:
        url = f"{url}?{qs}"
    return _http_request("GET", url)


def _stub_envelope(operation: str) -> SkillEnvelope:
    """Friendly fallback used when the task-manager URL points at a
    placeholder. Same wording the legacy branch returned so dev users
    don't see a regression."""
    messages = {
        "list": "The task manager is coming soon. Tasks will appear here once the service is live.",
        "create": (
            "Task noted! The task manager is coming soon — "
            "your manager will be notified through the usual channel in the meantime."
        ),
        "resolve": "The task manager is coming soon. Task resolution will be available once the service is live.",
    }
    return SkillEnvelope(
        text=messages.get(operation, "The task manager is coming soon."),
        signal="corpus_only",
    )


def _emit(call: SkillCall, msg: str) -> None:
    if call.emitter:
        try:
            call.emitter(msg)
        except Exception:  # pragma: no cover — emitter is best-effort
            pass


def _attach_to_ctx(call: SkillCall, envelope: TaskEnvelope) -> None:
    """Write the structured payload to ``pipeline_ctx.react_task_list_data``.

    ``app/stages/integrate.py`` reads this attribute and injects a
    ``task_list`` UI block — the same path the legacy inline branch
    used. Setting the attribute is a no-op when the dispatcher didn't
    pass a pipeline context (e.g. an MCP caller invoking the skill
    standalone)."""
    ctx = call.pipeline_ctx
    if ctx is None:
        return
    try:
        ctx.react_task_list_data = envelope.to_react_payload()
    except Exception as e:  # pragma: no cover — context is loose-typed
        logger.debug("attach react_task_list_data failed (non-fatal): %s", e)


# ── Handlers ──────────────────────────────────────────────────────────


def _run_list_tasks(call: SkillCall) -> SkillEnvelope:
    """Query the task-manager for tasks matching the planner's filters.

    Accepts the same filter aliases the legacy branch did (``org`` ↔
    ``org_name``) so planner prompts that used either keep working.
    """
    base = _task_base()
    if _is_stub_url(base):
        return _stub_envelope("list")

    _emit(call, "◌ Task manager: list_tasks…")
    inputs = call.inputs or {}

    # "tasks assigned to ME" — resolve the authenticated chat identity to
    # the canonical assignee_ref (user:<uuid>) via mobius-user. Unknown
    # identity → fall back to unscoped (never guess), with a note.
    me_note = ""
    assignee = inputs.get("assignee")
    if inputs.get("assigned_to_me") and not assignee:
        from app.services.user_identity import resolve_self
        subject = getattr(call.pipeline_ctx, "user_id", None) if call.pipeline_ctx else None
        me = resolve_self(subject)
        if me:
            assignee = me["assignee_ref"]
        else:
            me_note = (
                "\n\n_Couldn't resolve your user identity — showing all user tasks "
                "instead of just yours._"
            )

    params = {
        "org_name": inputs.get("org") or inputs.get("org_name"),
        "module": inputs.get("module"),
        "status": inputs.get("status"),
        "assignee": assignee,
        "npi": inputs.get("npi"),
        "run_id": inputs.get("run_id"),
        "severity": inputs.get("severity"),
        "type": inputs.get("type"),
        "workflow": inputs.get("workflow"),
        # Default to USER-audience tasks — system telemetry (chat turn
        # events, pipeline step signals) never pollutes a user's queue
        # unless they explicitly ask (audience="developer" or "all").
        "audience": (inputs.get("audience") or "user"),
        "kind": inputs.get("kind"),
        "limit": inputs.get("limit", 50),
    }
    if params["audience"] == "all":
        params["audience"] = None

    try:
        data = _http_get("/tasks", params)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
        logger.warning("list_tasks: task-manager unreachable: %s", e)
        return SkillEnvelope(
            text=f"Task manager error: {e}",
            signal="no_sources",
        )

    raw_tasks = data.get("tasks") or []
    rows = [TaskRow.from_api(t) for t in raw_tasks if isinstance(t, dict)]
    count = data.get("count", len(rows))

    if rows:
        lines = [f"**{count} task(s) found**\n"]
        for t in rows[:20]:
            sev = (t.severity or "").upper()
            prov = t.provider_name or t.npi or ""
            prov_str = f" — {prov}" if prov else ""
            lines.append(
                f"- [{sev}] {t.text} ({t.status}){prov_str} `{t.task_id[:8]}`"
            )
        summary = "\n".join(lines) + me_note
    else:
        summary = "No tasks found matching the given filters." + me_note

    # filters carries only the non-empty params the user actually asked
    # for — the UI uses this for the "Showing tasks for: …" header.
    visible_filters = {k: v for k, v in params.items() if v is not None and v != "" and k != "limit"}

    envelope = TaskEnvelope(
        operation="list",
        tasks=rows,
        filters=visible_filters,
        summary_text=summary,
    )
    _attach_to_ctx(call, envelope)
    return SkillEnvelope(
        text=summary,
        signal="corpus_only",
        extra=envelope.to_extra(),
    )


def _run_create_task(call: SkillCall) -> SkillEnvelope:
    """POST a new task to the task-manager.

    Planner inputs come as a flat dict; we map the canonical fields and
    pass through ``provider_name`` / ``npi`` / ``severity`` when present.
    """
    base = _task_base()
    if _is_stub_url(base):
        return _stub_envelope("create")

    _emit(call, "◌ Task manager: create_task…")
    inputs = call.inputs or {}
    body = {
        "org_name": inputs.get("org") or inputs.get("org_name") or "",
        "text": inputs.get("text") or inputs.get("description") or "",
        "source_module": inputs.get("module") or "manual",
        "severity": inputs.get("severity") or "low",
        "provider_name": inputs.get("provider_name"),
        "npi": inputs.get("npi"),
        "assignee": inputs.get("assignee"),
        # Reminders: kind="reminder" + deadline. "Remind me to check X
        # tomorrow" → the planner sets both; surfacing-by-due-date is the
        # contextual-surfacing service's job (v2 backlog item 2).
        "kind": inputs.get("kind"),
        "deadline": inputs.get("deadline"),
        "audience": "user",
    }
    body = {k: v for k, v in body.items() if v is not None}

    try:
        created = _http_request("POST", f"{base}/tasks", body=body)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
        logger.warning("create_task: task-manager unreachable: %s", e)
        return SkillEnvelope(
            text=f"Task manager error: {e}",
            signal="no_sources",
        )

    row = TaskRow.from_api(created)
    summary = (
        f"Task created: **{row.text or row.title or '(untitled)'}** "
        f"(ID: `{row.task_id[:8]}`, severity: {row.severity})"
    )

    envelope = TaskEnvelope(
        operation="create",
        tasks=[row],
        filters={},
        allow_create=False,  # we just created it; no double-fire
        summary_text=summary,
    )
    _attach_to_ctx(call, envelope)
    return SkillEnvelope(
        text=summary,
        signal="corpus_only",
        extra=envelope.to_extra(),
    )


def _run_resolve_task(call: SkillCall) -> SkillEnvelope:
    """Mark a task as resolved via POST /tasks/{id}/resolve.

    ``task_id`` is required — if missing, returns a friendly clarifier
    instead of a 422 from the task-manager.
    """
    base = _task_base()
    if _is_stub_url(base):
        return _stub_envelope("resolve")

    inputs = call.inputs or {}
    task_id = (inputs.get("task_id") or "").strip()
    if not task_id:
        return SkillEnvelope(
            text="`task_id` is required to resolve a task.",
            signal="no_sources",
        )

    _emit(call, f"◌ Task manager: resolve_task {task_id[:8]}…")
    body = {
        "resolved_by": inputs.get("resolved_by") or "chat",
        "note": inputs.get("note"),
    }

    try:
        resolved = _http_request("POST", f"{base}/tasks/{task_id}/resolve", body=body)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
        logger.warning("resolve_task: task-manager unreachable: %s", e)
        return SkillEnvelope(
            text=f"Task manager error: {e}",
            signal="no_sources",
        )

    row = TaskRow.from_api(resolved) if isinstance(resolved, dict) else None
    summary = f"Task `{task_id[:8]}` marked as resolved."

    envelope = _confirmation_envelope("resolve", row, summary)
    _attach_to_ctx(call, envelope)
    return SkillEnvelope(
        text=summary,
        signal="corpus_only",
        extra=envelope.to_extra(),
    )


def _confirmation_envelope(operation: str, row: TaskRow | None, summary: str) -> TaskEnvelope:
    """Single-row confirmation card with all action buttons disabled —
    a rendered confirmation must never double-fire an action."""
    return TaskEnvelope(
        operation=operation,
        tasks=[row] if row else [],
        filters={},
        allow_create=False,
        allow_resolve=False,
        allow_edit=False,
        allow_assign=False,
        allow_dismiss=False,
        summary_text=summary,
    )


def _run_patch_task(call: SkillCall) -> SkillEnvelope:
    """Edit task fields via PATCH /tasks/{id}.

    Accepts any subset of: title, text, body, severity, deadline/due_at,
    status, note (note is appended as a comment interaction by the
    task-manager rather than stored as a column).
    """
    base = _task_base()
    if _is_stub_url(base):
        return _stub_envelope("list")

    inputs = call.inputs or {}
    task_id = (inputs.get("task_id") or "").strip()
    if not task_id:
        return SkillEnvelope(text="`task_id` is required to edit a task.", signal="no_sources")

    updates = {k: inputs[k] for k in
               ("title", "text", "body", "severity", "deadline", "due_at", "status", "note")
               if inputs.get(k) not in (None, "")}
    if not updates:
        return SkillEnvelope(
            text="Nothing to update — provide at least one of: title, text, body, severity, deadline, status, note.",
            signal="no_sources",
        )

    _emit(call, f"◌ Task manager: patch_task {task_id[:8]}…")
    try:
        patched = _http_request("PATCH", f"{base}/tasks/{task_id}", body=updates)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
        logger.warning("patch_task: task-manager unreachable: %s", e)
        return SkillEnvelope(text=f"Task manager error: {e}", signal="no_sources")

    row = TaskRow.from_api(patched) if isinstance(patched, dict) else None
    changed = ", ".join(sorted(k for k in updates if k != "note"))
    summary = f"Task `{task_id[:8]}` updated ({changed})." if changed else f"Note added to task `{task_id[:8]}`."
    envelope = _confirmation_envelope("patch", row, summary)
    _attach_to_ctx(call, envelope)
    return SkillEnvelope(text=summary, signal="corpus_only", extra=envelope.to_extra())


def _run_assign_task(call: SkillCall) -> SkillEnvelope:
    """Assign / reassign a task. Thin wrapper over PATCH — sets both
    ``assigned_to`` and the legacy ``assignee`` mirror, and records an
    'assigned' interaction so the audit trail shows the handoff."""
    base = _task_base()
    if _is_stub_url(base):
        return _stub_envelope("list")

    inputs = call.inputs or {}
    task_id = (inputs.get("task_id") or "").strip()
    assignee = (inputs.get("assignee") or inputs.get("assigned_to") or "").strip()
    if not task_id or not assignee:
        return SkillEnvelope(
            text="Both `task_id` and `assignee` are required to assign a task.",
            signal="no_sources",
        )

    # Resolve the natural-language assignee to a canonical identity via
    # mobius-user ("Sam" → user:<uuid> + display name). Convention:
    # assigned_to carries the canonical ref (exact matching for "my
    # tasks"), assignee carries the display name (cards stay readable).
    # No candidate → both get the literal string (legacy behavior).
    from app.services.user_identity import resolve_assignee
    active = call.active_context or {}
    cand = resolve_assignee(assignee, org=active.get("org_name") or active.get("payer"))
    canonical = cand["assignee_ref"] if cand else assignee
    display = (cand.get("display_name") if cand else None) or assignee

    _emit(call, f"◌ Task manager: assign_task {task_id[:8]} → {display}…")
    try:
        patched = _http_request("PATCH", f"{base}/tasks/{task_id}",
                                body={"assigned_to": canonical, "assignee": display})
        _http_request("POST", f"{base}/tasks/{task_id}/interact",
                      body={"actor": "chat", "action": "assigned",
                            "note": f"assigned to {display}" + (f" ({canonical})" if canonical != display else "")})
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
        logger.warning("assign_task: task-manager unreachable: %s", e)
        return SkillEnvelope(text=f"Task manager error: {e}", signal="no_sources")

    row = TaskRow.from_api(patched) if isinstance(patched, dict) else None
    summary = f"Task `{task_id[:8]}` assigned to **{display}**."
    if not cand:
        summary += " _(no matching user profile — stored as plain text; enroll them in mobius-user for exact 'my tasks' matching)_"
    envelope = _confirmation_envelope("assign", row, summary)
    _attach_to_ctx(call, envelope)
    return SkillEnvelope(text=summary, signal="corpus_only", extra=envelope.to_extra())


def _run_dismiss_task(call: SkillCall) -> SkillEnvelope:
    """Dismiss a task (won't-do / not-relevant) via POST /tasks/{id}/dismiss."""
    base = _task_base()
    if _is_stub_url(base):
        return _stub_envelope("list")

    inputs = call.inputs or {}
    task_id = (inputs.get("task_id") or "").strip()
    if not task_id:
        return SkillEnvelope(text="`task_id` is required to dismiss a task.", signal="no_sources")

    _emit(call, f"◌ Task manager: dismiss_task {task_id[:8]}…")
    try:
        dismissed = _http_request("POST", f"{base}/tasks/{task_id}/dismiss",
                                  body={"dismissed_by": inputs.get("dismissed_by") or "chat"})
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
        logger.warning("dismiss_task: task-manager unreachable: %s", e)
        return SkillEnvelope(text=f"Task manager error: {e}", signal="no_sources")

    row = TaskRow.from_api(dismissed) if isinstance(dismissed, dict) else None
    summary = f"Task `{task_id[:8]}` dismissed."
    envelope = _confirmation_envelope("dismiss", row, summary)
    _attach_to_ctx(call, envelope)
    return SkillEnvelope(text=summary, signal="corpus_only", extra=envelope.to_extra())


# ── Registrations ────────────────────────────────────────────────────


register(
    SkillSpec(
        name="list_tasks",
        description=(
            "List tasks from the unified task manager (open follow-ups, blockers, reminders).\n"
            "Use when: user asks 'what tasks are open', 'show me follow-ups for <org>',\n"
            "  'what's pending on this credentialing run', 'tasks assigned to <person>',\n"
            "  'my reminders'.\n"
            "Shows USER tasks by default — system/developer telemetry is excluded unless\n"
            "  the user explicitly asks for system tasks (set audience='developer' or 'all').\n"
            "When the user says MY tasks / assigned to ME / my reminders, set\n"
            "  assigned_to_me=true (resolves their identity; do NOT guess an assignee name).\n"
            "Filters (all optional): assigned_to_me, org_name, module, status, assignee,\n"
            "  npi, run_id, severity, type, workflow, kind (work_item|reminder|signal),\n"
            "  audience, limit.\n"
            "Returns: Markdown summary + structured task_list UI block."
        ),
        inputs_schema={
            "type": "object",
            "properties": {
                "org_name": {"type": "string", "description": "Org filter; alias: org."},
                "module": {"type": "string", "description": "Source module (credentialing, roster, manual, …)."},
                "status": {"type": "string", "description": "open | resolved | dismissed | running."},
                "assignee": {"type": "string"},
                "npi": {"type": "string", "description": "10-digit NPI of the provider."},
                "run_id": {"type": "string"},
                "severity": {"type": "string", "description": "low | medium | high."},
                "type": {"type": "string", "description": "info | blocker | decision | …"},
                "workflow": {"type": "string"},
                "assigned_to_me": {"type": "boolean", "description": "True when the user asks for THEIR tasks/reminders ('my tasks', 'assigned to me')."},
                "audience": {"type": "string", "description": "user (default) | developer (system telemetry) | all."},
                "kind": {"type": "string", "description": "work_item | reminder | signal."},
                "limit": {"type": "integer", "description": "Default 50, max 1000."},
            },
        },
        handler=_run_list_tasks,
        requires_jurisdiction=False,
        follow_up_capable=True,
        category="tasks",
        display_name="List Tasks",
    )
)

register(
    SkillSpec(
        name="create_task",
        description=(
            "Create a new task or reminder in the unified task manager.\n"
            "Use when: user asks to 'create a task', 'log a follow-up', 'add an action item',\n"
            "  or 'remind me to <X> tomorrow/on <date>' (then set kind='reminder' + deadline).\n"
            "Required: org_name (alias: org) and text (alias: description).\n"
            "Optional: severity (critical|warning|info|low), module, provider_name, npi,\n"
            "  assignee, kind ('reminder' for time-anchored nudges), deadline (YYYY-MM-DD).\n"
            "Returns: confirmation + the created task as a single-row task_list block."
        ),
        inputs_schema={
            "type": "object",
            "properties": {
                "org_name": {"type": "string", "description": "Org the task belongs to; alias: org."},
                "text": {"type": "string", "description": "Task description; alias: description."},
                "module": {"type": "string", "description": "Source module; defaults to 'manual'."},
                "severity": {"type": "string", "description": "low | medium | high. Defaults to 'low'."},
                "provider_name": {"type": "string"},
                "npi": {"type": "string"},
                "assignee": {"type": "string"},
                "kind": {"type": "string", "description": "'reminder' for time-anchored nudges; default work_item."},
                "deadline": {"type": "string", "description": "YYYY-MM-DD; required for reminders."},
            },
            "required": ["org_name", "text"],
        },
        handler=_run_create_task,
        requires_jurisdiction=False,
        follow_up_capable=False,
        category="tasks",
        display_name="Create Task",
    )
)

register(
    SkillSpec(
        name="resolve_task",
        description=(
            "Mark a task as resolved in the unified task manager.\n"
            "Use when: user confirms a follow-up is done, says 'mark task X resolved',\n"
            "  or closes out an action item from a prior turn.\n"
            "Required: task_id (full UUID or the 8-char short form from a prior list).\n"
            "Optional: note (added as a resolution comment).\n"
            "Returns: confirmation + the resolved row."
        ),
        inputs_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Full UUID or 8-char short id."},
                "note": {"type": "string", "description": "Optional resolution note."},
                "resolved_by": {"type": "string", "description": "Defaults to 'chat'."},
            },
            "required": ["task_id"],
        },
        handler=_run_resolve_task,
        requires_jurisdiction=False,
        follow_up_capable=False,
        category="tasks",
        display_name="Resolve Task",
    )
)

register(
    SkillSpec(
        name="patch_task",
        description=(
            "Edit an existing task's fields in the unified task manager.\n"
            "Use when: user asks to 'change the severity', 'update the title', 'push the deadline',\n"
            "  'add a note to task X', or otherwise modify a task without resolving it.\n"
            "Required: task_id (full UUID or 8-char short form from a prior list).\n"
            "Optional: title, text, body, severity (critical|warning|info|low|none),\n"
            "  deadline (YYYY-MM-DD), status (open|in_progress), note (appended as comment).\n"
            "Returns: confirmation + the updated row."
        ),
        inputs_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Full UUID or 8-char short id."},
                "title": {"type": "string"},
                "text": {"type": "string"},
                "body": {"type": "string"},
                "severity": {"type": "string", "description": "critical | warning | info | low | none."},
                "deadline": {"type": "string", "description": "YYYY-MM-DD."},
                "status": {"type": "string", "description": "open | in_progress."},
                "note": {"type": "string", "description": "Appended to the task's comment trail."},
            },
            "required": ["task_id"],
        },
        handler=_run_patch_task,
        requires_jurisdiction=False,
        follow_up_capable=True,
        category="tasks",
        display_name="Edit Task",
    )
)

register(
    SkillSpec(
        name="assign_task",
        description=(
            "Assign or reassign a task to a person (or agent) in the unified task manager.\n"
            "Use when: user says 'assign task X to Sam', 'reassign this to the credentialing team',\n"
            "  'give that follow-up to me'.\n"
            "Required: task_id AND assignee (name, email, or agent:<name>).\n"
            "Returns: confirmation + the updated row; the handoff is recorded in the audit trail."
        ),
        inputs_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Full UUID or 8-char short id."},
                "assignee": {"type": "string", "description": "Person/team/agent to assign to."},
            },
            "required": ["task_id", "assignee"],
        },
        handler=_run_assign_task,
        requires_jurisdiction=False,
        follow_up_capable=True,
        category="tasks",
        display_name="Assign Task",
    )
)

register(
    SkillSpec(
        name="dismiss_task",
        description=(
            "Dismiss a task (won't-do / not relevant) in the unified task manager.\n"
            "Use when: user says 'dismiss that task', 'we don't need this one', 'not relevant, close it'.\n"
            "Different from resolve_task: resolve = done, dismiss = won't do.\n"
            "Required: task_id (full UUID or 8-char short form).\n"
            "Returns: confirmation."
        ),
        inputs_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Full UUID or 8-char short id."},
            },
            "required": ["task_id"],
        },
        handler=_run_dismiss_task,
        requires_jurisdiction=False,
        follow_up_capable=False,
        category="tasks",
        display_name="Dismiss Task",
    )
)
