"""Master objective list: per-thread persistent objective and sub-objectives for relentless pursuit.

See docs/RELENTLESS_CONTINUITY_PLAN.md for design.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

SubObjectiveStatus = Literal["pending", "answered", "partial", "failed", "blocked"]
ObjectiveStatus = Literal["active", "solved", "abandoned", "blocked", "incomplete"]

# Max turns before we stop asking and mark incomplete
MAX_ATTEMPTS_BEFORE_STOP = 4


@dataclass
class SubObjective:
    """One part of the master objective (maps to plan subquestion)."""
    id: str
    text: str
    status: SubObjectiveStatus = "pending"
    answer: str | None = None  # persisted answer text for summarization, upsert, and skip-retrieval


@dataclass
class MasterObjective:
    """Master objective for a thread. Pursued relentlessly until solved or abandoned."""
    id: str
    created_at: str
    updated_at: str
    status: ObjectiveStatus = "active"
    summary: str = ""
    sub_objectives: list[SubObjective] = field(default_factory=list)
    attempts: int = 0
    last_user_ask: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "summary": self.summary,
            "sub_objectives": [
                {"id": so.id, "text": so.text, "status": so.status, "answer": so.answer}
                for so in self.sub_objectives
            ],
            "attempts": self.attempts,
            "last_user_ask": self.last_user_ask,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> MasterObjective | None:
        if not d or not isinstance(d, dict):
            return None
        sub_raw = d.get("sub_objectives") or []
        sub_objectives = []
        for item in sub_raw:
            if isinstance(item, dict) and item.get("id") and item.get("text"):
                ans = item.get("answer")
                sub_objectives.append(SubObjective(
                    id=str(item["id"]),
                    text=str(item["text"]),
                    status=str(item.get("status", "pending")) or "pending",
                    answer=str(ans).strip() if ans and isinstance(ans, str) else None,
                ))
        return cls(
            id=str(d.get("id") or str(uuid.uuid4())),
            created_at=str(d.get("created_at") or _now_iso()),
            updated_at=str(d.get("updated_at") or _now_iso()),
            status=str(d.get("status") or "active"),
            summary=str(d.get("summary") or ""),
            sub_objectives=sub_objectives,
            attempts=int(d.get("attempts") or 0),
            last_user_ask=d.get("last_user_ask"),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_or_update_objective(
    plan: Any,
    thread_state: dict[str, Any],
    is_new_question: bool = True,
) -> MasterObjective:
    """Create or update master objective from plan. Returns updated objective."""
    existing_raw = (thread_state or {}).get("master_objective")
    existing = MasterObjective.from_dict(existing_raw) if existing_raw else None

    if not plan or not getattr(plan, "subquestions", None):
        if existing:
            return existing
        return _fresh_objective("", [])

    subquestions = plan.subquestions
    summary = _summary_from_subquestions(subquestions)
    sub_objectives = [
        SubObjective(id=sq.id, text=(sq.text or "").strip(), status="pending")
        for sq in subquestions
        if sq.id and (sq.text or "").strip()
    ]

    if is_new_question or not existing:
        return MasterObjective(
            id=str(uuid.uuid4()),
            created_at=_now_iso(),
            updated_at=_now_iso(),
            status="active",
            summary=summary,
            sub_objectives=sub_objectives,
            attempts=0,
            last_user_ask=None,
        )

    # Follow-up: merge plan with existing; preserve status of existing; keep existing not in plan (deterministic)
    existing_by_id = {so.id: so for so in existing.sub_objectives}
    plan_ids = {so.id for so in sub_objectives}
    merged = []
    for so in sub_objectives:
        if so.id in existing_by_id:
            merged.append(existing_by_id[so.id])  # keep status from previous turn
        else:
            merged.append(so)
    for so in existing.sub_objectives:
        if so.id not in plan_ids:
            merged.append(so)  # planner dropped it; preserve (deterministic upsert)

    return MasterObjective(
        id=existing.id,
        created_at=existing.created_at,
        updated_at=_now_iso(),
        status=existing.status if existing.status != "solved" else "active",
        summary=summary or existing.summary,
        sub_objectives=merged,
        attempts=existing.attempts,
        last_user_ask=existing.last_user_ask,
    )


def _summary_from_subquestions(subquestions: list) -> str:
    texts = []
    for sq in subquestions:
        t = (getattr(sq, "text", None) or "").strip()
        if t:
            texts.append(t)
    return "; ".join(texts[:5]) if texts else ""


def _fresh_objective(summary: str, sub_objectives: list[SubObjective]) -> MasterObjective:
    return MasterObjective(
        id=str(uuid.uuid4()),
        created_at=_now_iso(),
        updated_at=_now_iso(),
        status="active",
        summary=summary,
        sub_objectives=sub_objectives,
        attempts=0,
        last_user_ask=None,
    )
