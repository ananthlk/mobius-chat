"""Stage: jurisdiction clarification and query refinement. Sets resolvable? and messages."""
from collections.abc import Callable

from app.pipeline.context import PipelineContext
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

    should_refine, refinement_suggestions = need_query_refinement(plan)
    if should_refine and refinement_suggestions:
        ctx.should_refine = True
        ctx.refinement_suggestions = refinement_suggestions or []
        return False

    return True  # Resolvable
