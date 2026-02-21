"""Stage: classify slot_fill vs new_question, compute effective_message."""
from collections.abc import Callable

from app.pipeline.context import PipelineContext
from app.state.jurisdiction import get_jurisdiction_from_active
from app.state.refined_query import build_refined_query, classify_message


def run_classify(ctx: PipelineContext, emitter: Callable[[str], None] | None = None) -> None:
    """Classify message, set effective_message for planning."""
    open_slots = (ctx.merged_state or {}).get("open_slots") or []
    last_refined = (ctx.merged_state or {}).get("refined_query") or None
    last_turn = ctx.last_turns[0] if ctx.last_turns else {}

    ctx.classification = classify_message(ctx.message, last_turn, open_slots, last_refined)

    if ctx.classification == "slot_fill" and last_refined and ctx.merged_state:
        ctx.effective_message = build_refined_query(
            last_refined,
            get_jurisdiction_from_active((ctx.merged_state or {}).get("active")),
        )
    else:
        ctx.effective_message = ctx.message
