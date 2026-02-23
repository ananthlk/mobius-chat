"""Evaluate resolution outcome for master objective: did we solve each sub-objective?

See docs/RELENTLESS_CONTINUITY_PLAN.md.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.state.master_objective import MasterObjective, SubObjective, SubObjectiveStatus

if TYPE_CHECKING:
    from app.planner.schemas import Plan


NO_ANSWER_PHRASES = (
    "context does not contain",
    "does not contain the answer",
    "does not contain information",
    "not available in the provided",
    "not in the provided",
    "not specified in the provided",
    "not specified",
    "does not specify",
    "missing information",
    "could not find",
    "no relevant",
    "no information",
)


def evaluate_sub_objective_status(
    answer: str,
    retrieval_signal: str | None = None,
) -> SubObjectiveStatus:
    """Map RAG/tool answer and retrieval signal to sub-objective status."""
    if not answer or not str(answer).strip():
        return "failed"
    text = str(answer).strip().lower()
    sig = (retrieval_signal or "").lower()

    for phrase in NO_ANSWER_PHRASES:
        if phrase in text:
            return "failed"

    if "corpus" in sig and ("low" in sig or "no" in sig):
        return "partial"
    if "planner_pre_resolved" in sig:
        return "answered"
    if "google" in sig or "external" in sig:
        return "answered"
    return "answered"


def update_objective_from_answers(
    objective: MasterObjective | None,
    plan: "Plan | None",
    answers: list[str],
    retrieval_signals: list[str],
) -> MasterObjective | None:
    """Update master objective sub_objectives status from resolve stage answers."""
    if not objective or not plan or not getattr(plan, "subquestions", None):
        return objective

    sqs = plan.subquestions
    status_by_id: dict[str, SubObjectiveStatus] = {}
    for i, sq in enumerate(sqs):
        ans = answers[i] if i < len(answers) else ""
        sig = retrieval_signals[i] if i < len(retrieval_signals) else None
        status_by_id[sq.id] = evaluate_sub_objective_status(ans, sig)

    updated_subs = []
    for so in objective.sub_objectives:
        new_status = status_by_id.get(so.id, so.status)
        ans = so.answer  # preserve existing by default
        if new_status == "answered":
            for i, sq in enumerate(sqs):
                if sq.id == so.id and i < len(answers) and (answers[i] or "").strip():
                    ans = (answers[i] or "").strip()
                    break
        updated_subs.append(SubObjective(id=so.id, text=so.text, status=new_status, answer=ans))

    all_answered = all(s.status == "answered" for s in updated_subs)
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    return MasterObjective(
        id=objective.id,
        created_at=objective.created_at,
        updated_at=now,
        status="solved" if all_answered else "active",
        summary=objective.summary,
        sub_objectives=updated_subs,
        attempts=objective.attempts + 1,
        last_user_ask=objective.last_user_ask,
    )


def update_objective_from_integrator(
    objective: MasterObjective | None,
    integrator_output: dict | str | None,
) -> MasterObjective | None:
    """When integrator used user_provided_context or structured resolutions, promote sub_objectives to answered.
    Prefer closed_task_ids from resolutions; fallback to resolved_subquestions."""
    if not objective:
        return objective
    if not integrator_output:
        return objective
    if isinstance(integrator_output, str):
        try:
            import json
            integrator_output = json.loads(integrator_output)
        except (json.JSONDecodeError, TypeError):
            return objective
    if not isinstance(integrator_output, dict):
        return objective
    # Prefer closed_task_ids from structured resolutions
    resolved = integrator_output.get("closed_task_ids")
    if not resolved or not isinstance(resolved, list):
        resolved = integrator_output.get("resolved_subquestions")
    if not resolved or not isinstance(resolved, list):
        return objective
    resolved_ids = {str(x).strip() for x in resolved if x}
    if not resolved_ids:
        return objective
    # Build sq_id -> resolution text from resolutions list
    res_list = integrator_output.get("resolutions") or []
    answer_by_id = {}
    for r in res_list:
        if isinstance(r, dict) and r.get("sq_id") and r.get("resolution"):
            answer_by_id[str(r["sq_id"]).strip()] = str(r["resolution"]).strip()

    updated_subs = []
    for so in objective.sub_objectives:
        if so.id in resolved_ids and so.status in ("failed", "partial"):
            ans = answer_by_id.get(so.id) or so.answer
            updated_subs.append(SubObjective(id=so.id, text=so.text, status="answered", answer=ans))
        else:
            updated_subs.append(so)

    all_answered = all(s.status == "answered" for s in updated_subs)
    # Don't mark solved if direct_answer still hedges (e.g. "cannot be confirmed")
    direct = (integrator_output.get("direct_answer") or "").lower() if isinstance(integrator_output, dict) else ""
    hedges = any(p in direct for p in NO_ANSWER_PHRASES)
    if all_answered and hedges:
        all_answered = False  # Keep active so we may ask user again
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    return MasterObjective(
        id=objective.id,
        created_at=objective.created_at,
        updated_at=now,
        status="solved" if all_answered else objective.status,
        summary=objective.summary,
        sub_objectives=updated_subs,
        attempts=objective.attempts,
        last_user_ask=objective.last_user_ask,
    )
