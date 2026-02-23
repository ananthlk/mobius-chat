"""Continuity stage: detect when to ask user for help (user-as-leverage), when user ends pursuit, and when max attempts reached.

See docs/RELENTLESS_CONTINUITY_PLAN.md.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from app.communication.user_leverage import format_user_ask
from app.state.continuity_checks import user_wants_to_end_pursuit
from app.state.master_objective import MasterObjective, MAX_ATTEMPTS_BEFORE_STOP

if TYPE_CHECKING:
    from app.pipeline.context import PipelineContext

StuckReason = Literal["no_evidence", "missing_code", "conflicting_info", "partial_answer", "tool_failed"]

# End states for clear user messaging
OBJECTIVE_STATUS_RESOLVED = "resolved"
OBJECTIVE_STATUS_NEED_INFO = "need_info"
OBJECTIVE_STATUS_UNABLE = "unable"
OBJECTIVE_STATUS_USER_ENDED = "user_ended"
OBJECTIVE_STATUS_INCOMPLETE = "incomplete"


def should_ask_user_for_help(ctx: "PipelineContext") -> tuple[bool, str | None]:
    """If we have partial/failed sub-objectives and under max attempts, return (True, message). Else (False, None)."""
    # Max attempts reached: stop asking, mark incomplete
    objective_raw = (ctx.merged_state or {}).get("master_objective")
    obj = MasterObjective.from_dict(objective_raw) if objective_raw else None
    if obj and obj.attempts >= MAX_ATTEMPTS_BEFORE_STOP:
        failed = [so for so in obj.sub_objectives if so.status in ("failed", "partial")]
        if failed:
            obj.status = "incomplete"
            ctx.master_objective = obj.to_dict()
            ctx.merged_state = {**(ctx.merged_state or {}), "master_objective": ctx.master_objective}
        return False, None

    if user_wants_to_end_pursuit(ctx.message or ""):
        objective_raw = (ctx.merged_state or {}).get("master_objective")
        if objective_raw:
            obj = MasterObjective.from_dict(objective_raw)
            if obj and obj.status == "active":
                obj.status = "abandoned"
                ctx.master_objective = obj.to_dict()
                ctx.merged_state = {**(ctx.merged_state or {}), "master_objective": ctx.master_objective}
        return False, None

    objective_raw = (ctx.merged_state or {}).get("master_objective")
    objective = MasterObjective.from_dict(objective_raw) if objective_raw else None
    if not objective or objective.status == "solved":
        return False, None

    failed = [so for so in objective.sub_objectives if so.status in ("failed", "partial")]
    if not failed:
        return False, None

    answered = [so for so in objective.sub_objectives if so.status == "answered"]
    answered_texts = [so.text for so in answered]
    first_failed = failed[0]

    # Heuristic: choose stuck reason from sub-objective text
    text_lower = first_failed.text.lower()
    if "code" in text_lower or "icd" in text_lower or "cpt" in text_lower:
        reason: StuckReason = "missing_code"
    elif len(failed) < len(objective.sub_objectives):
        reason = "partial_answer"
    else:
        reason = "no_evidence"

    msg = format_user_ask(reason, first_failed.text, answered_texts if answered else None)
    return True, msg


def get_objective_end_state(ctx: "PipelineContext") -> tuple[str, str | None]:
    """Return (objective_status, closure_message). Used for clear user feedback.

    Status: resolved | need_info | unable | user_ended | incomplete
    closure_message: optional short line for incomplete/unable (e.g. try again from recents)
    """
    obj_raw = (ctx.merged_state or {}).get("master_objective")
    obj = MasterObjective.from_dict(obj_raw) if obj_raw else None
    if not obj:
        return "resolved", None

    if obj.status == "solved":
        return OBJECTIVE_STATUS_RESOLVED, "We've resolved your question."
    if obj.status == "abandoned":
        return OBJECTIVE_STATUS_USER_ENDED, None  # already said "Understood"
    if obj.status == "incomplete":
        failed = [so for so in obj.sub_objectives if so.status in ("failed", "partial")]
        if failed:
            return OBJECTIVE_STATUS_INCOMPLETE, (
                "We weren't able to fully resolve this after several tries. "
                "You can pick this up from your recent queries to try again."
            )
        return OBJECTIVE_STATUS_UNABLE, None

    failed = [so for so in obj.sub_objectives if so.status in ("failed", "partial")]
    answered = [so for so in obj.sub_objectives if so.status == "answered"]
    if failed and answered:
        return OBJECTIVE_STATUS_NEED_INFO, None
    if failed:
        return OBJECTIVE_STATUS_NEED_INFO, None
    return "resolved", None
