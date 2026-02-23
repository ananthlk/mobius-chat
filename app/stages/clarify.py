"""Stage: jurisdiction clarification, route clash, and query refinement. Sets resolvable? and messages."""
from collections.abc import Callable

from app.pipeline.context import PipelineContext
from app.planner.route_triggers import detect_route
from app.state.clarification import need_jurisdiction_clarification
from app.state.query_refinement import need_query_refinement


def _get_rag_url() -> str:
    """RAG DB URL for lexicon (JPD tagger)."""
    try:
        from app.chat_config import get_chat_config

        return (get_chat_config().rag.database_url or "").strip()
    except Exception:
        return ""


def run_clarify(ctx: PipelineContext, emitter: Callable[[str], None] | None = None) -> bool:
    """Check if we need clarification or refinement. Set ctx fields. Returns True if resolvable (no ask)."""
    plan = ctx.plan
    if not plan:
        return True  # No plan = nothing to clarify

    # Route clash: multiple conflicting deterministic triggers (web vs RAG)
    message = ctx.effective_message or ctx.message or ""
    _, route_confidence, route_choices = detect_route(message)
    if route_confidence < 1.0 and route_choices:
        ctx.needs_route_clarification = True
        ctx.clarification_message = (
            "I can either search the web or search our policy materials. Which would you like?"
        )
        ctx.route_clarification_choices = route_choices
        return False

    active = (
        ctx.merged_state.get("active")
        if ctx.merged_state
        else {}
    )
    rag_url = _get_rag_url()
    needs_clar, missing_slots, clarification_message = need_jurisdiction_clarification(
        plan.subquestions,
        active,
        question_text=ctx.message or "",
        rag_url=rag_url,
    )
    if needs_clar and clarification_message:
        ctx.needs_clarification = True
        ctx.clarification_message = clarification_message
        ctx.missing_slots = missing_slots or []
        return False

    # Skip refinement when slot_fill/jurisdiction_change: user is providing info, not asking a vague question
    if ctx.classification in ("slot_fill", "jurisdiction_change"):
        return True

    # Heuristic refinement only for legacy plans. Mobius planner decides decomposition; trust it.
    if not getattr(plan, "task_plan", None):
        user_msg = ctx.effective_message or ctx.message or ""
        should_refine, refinement_suggestions = need_query_refinement(plan, user_message=user_msg)
        if should_refine and refinement_suggestions:
            ctx.should_refine = True
            ctx.refinement_suggestions = refinement_suggestions or []
            return False

    return True  # Resolvable
