"""Typed envelope for task-manager skill responses.

The task CRUD builtins (``list_tasks``, ``create_task``, ``resolve_task``
in ``app/skills/builtin/tasks.py``) all return one of these on success.

``SkillEnvelope`` itself is a generic shape — text + sources + signal —
that the planner LLM consumes. The frontend's ``task_list`` UI block
needs a structured row payload, not markdown. Rather than encode that
payload as untyped dicts inside ``SkillEnvelope.extra``, we define
``TaskEnvelope`` here so the shape is documented in one place and every
task tool produces the same fields.

The envelope reaches the frontend via two routes:

  1. The skill handler writes ``envelope.to_react_payload()`` to
     ``pipeline_ctx.react_task_list_data``. ``app/stages/integrate.py``
     reads that attribute and injects a ``task_list`` UI block into the
     final assistant_envelope. This is the path the legacy inline
     branch in ``react_loop.py`` used; we keep it to avoid a frontend
     change in v1.

  2. The same dict is stashed in ``SkillEnvelope.extra["task_payload"]``
     so non-pipeline consumers (MCP server, eval harness) can read the
     structured shape without poking at thread state.

Promoting ``TaskEnvelope`` to a first-class field on ``SkillEnvelope``
becomes worthwhile once a second skill family needs the same dual-
channel pattern. Until then it lives here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TaskRow:
    """One task as it appears in the UI / in the task-manager API.

    Fields mirror the task-manager's TaskCard shape. ``extra`` carries
    any column the frontend doesn't render today but we don't want to
    drop on the way through (interactions, detail_payload, actions, …).
    """

    task_id: str
    org_name: str = ""
    text: str = ""
    title: str = ""
    body: str = ""
    severity: str = "low"
    status: str = "open"
    type: str = "info"
    source_module: str = ""
    provider_name: str | None = None
    npi: str | None = None
    assignee: str | None = None
    run_id: str | None = None
    workflow: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, row: dict[str, Any]) -> "TaskRow":
        """Build a TaskRow from a task-manager API response dict.

        Unknown keys land in ``extra``. Missing keys default to empty
        strings rather than ``None`` so the frontend doesn't have to
        null-guard every cell.
        """
        known = {
            "task_id", "org_name", "text", "title", "body",
            "severity", "status", "type", "source_module",
            "provider_name", "npi", "assignee", "run_id", "workflow",
            "created_at", "updated_at",
        }
        extra = {k: v for k, v in row.items() if k not in known}
        return cls(
            task_id=str(row.get("task_id") or ""),
            org_name=str(row.get("org_name") or ""),
            text=str(row.get("text") or ""),
            title=str(row.get("title") or ""),
            body=str(row.get("body") or ""),
            severity=str(row.get("severity") or "low"),
            status=str(row.get("status") or "open"),
            type=str(row.get("type") or "info"),
            source_module=str(row.get("source_module") or ""),
            provider_name=row.get("provider_name"),
            npi=row.get("npi"),
            assignee=row.get("assignee"),
            run_id=row.get("run_id"),
            workflow=row.get("workflow"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            extra=extra,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "task_id": self.task_id,
            "org_name": self.org_name,
            "text": self.text,
            "title": self.title,
            "body": self.body,
            "severity": self.severity,
            "status": self.status,
            "type": self.type,
            "source_module": self.source_module,
        }
        for k in ("provider_name", "npi", "assignee", "run_id", "workflow", "created_at", "updated_at"):
            v = getattr(self, k)
            if v is not None:
                out[k] = v
        if self.extra:
            for k, v in self.extra.items():
                if k not in out:
                    out[k] = v
        return out


@dataclass(frozen=True)
class TaskEnvelope:
    """Structured response from a task-manager skill call.

    ``operation`` names which CRUD action ran — useful for analytics and
    for the frontend when one envelope can carry either a list-result
    or a single-row result.

    ``filters`` is what the caller asked for (echoed back so the UI can
    show ``Showing tasks for: org=Foo, status=open``). Empty for
    create/resolve.

    ``allow_create`` / ``allow_resolve`` gate the task_list block's
    inline action buttons. Create-flow disables ``allow_create`` so the
    user can't double-fire from a confirmation card.
    """

    operation: str  # "list" | "create" | "resolve" | "get" | "patch" | "dismiss"
    tasks: list[TaskRow] = field(default_factory=list)
    filters: dict[str, Any] = field(default_factory=dict)
    allow_create: bool = True
    allow_resolve: bool = True
    summary_text: str = ""

    def to_react_payload(self) -> dict[str, Any]:
        """Shape that ``integrate.py`` reads off ``ctx.react_task_list_data``.

        The legacy inline branch in ``react_loop.py`` wrote a dict of
        this exact shape; preserving it means no frontend change.
        """
        return {
            "tasks": [t.to_dict() for t in self.tasks],
            "filters": dict(self.filters),
            "allow_create": self.allow_create,
            "allow_resolve": self.allow_resolve,
        }

    def to_extra(self) -> dict[str, Any]:
        """Shape stashed in ``SkillEnvelope.extra`` for non-pipeline
        consumers (MCP server, eval harness, future task-detail view).
        Carries the operation tag and summary text so an out-of-band
        reader has the same view the UI gets."""
        return {
            "task_payload": {
                "operation": self.operation,
                **self.to_react_payload(),
            },
        }
