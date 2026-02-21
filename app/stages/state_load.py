"""Stage: load state, apply delta from message, build context pack."""
import logging
from collections.abc import Callable

from app.pipeline.context import PipelineContext
from app.state.context_pack import build_context_pack
from app.state.context_router import route_context
from app.state.model import ThreadState
from app.state.state_extractor import extract_state_delta
from app.storage.threads import get_last_turn_messages, get_state, save_state_full
from app.storage.turns import get_last_turn_sources

logger = logging.getLogger(__name__)


def run_state_load(
    ctx: PipelineContext,
    emitter: Callable[[str], None] | None = None,
    parse1_output=None,
    answer_card=None,
) -> None:
    """Load thread state, extract delta from message, apply_delta, save, build context pack."""
    if not ctx.thread_id or not (ctx.thread_id or "").strip():
        ctx.merged_state = {}
        ctx.last_turns = []
        ctx.context_pack = ""
        return

    raw = get_state(ctx.thread_id)
    thread_state = ThreadState.from_dict(raw)

    delta, reset_reason = extract_state_delta(
        ctx.message, thread_state.to_dict(),
        parse1_output=parse1_output, answer_card=answer_card
    )
    if delta:
        thread_state.apply_delta(delta)
        save_state_full(ctx.thread_id, thread_state.to_dict())

    merged = thread_state.to_dict()
    ctx.merged_state = merged
    ctx.last_turns = get_last_turn_messages(ctx.thread_id)
    ctx.last_turn_sources = get_last_turn_sources(ctx.thread_id)
    route = route_context(ctx.message, merged, ctx.last_turns, reset_reason=reset_reason)
    ctx.context_pack = build_context_pack(
        route, merged, ctx.last_turns, merged.get("open_slots") or [],
        last_turn_sources=ctx.last_turn_sources,
    )
