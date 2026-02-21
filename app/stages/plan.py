"""Stage: parse message into plan, compute refined_query, build blueprint."""
import logging
from collections.abc import Callable

from app.chat_config import get_chat_config
from app.pipeline.context import PipelineContext
from app.planner import parse
from app.planner.blueprint import build_blueprint
from app.planner.schemas import Plan, SubQuestion
from app.state.refined_query import compute_refined_query
from app.stages.agents.capabilities import capabilities_for_parser

logger = logging.getLogger(__name__)


def _minimal_plan(message: str) -> Plan:
    """Fallback plan when parse fails: single subquestion with raw message, kind=non_patient."""
    text = (message or "").strip() or "What can you help with?"
    return Plan(subquestions=[
        SubQuestion(id="sq1", text=text, kind="non_patient", question_intent="canonical", intent_score=0.5),
    ])


def run_plan(ctx: PipelineContext, emitter: Callable[[str], None] | None = None) -> None:
    """Parse effective_message into plan, compute refined_query, build blueprint."""
    parser_context = ctx.context_pack
    if parser_context:
        parser_context = f"{parser_context}\n\nAvailable paths and capabilities: {capabilities_for_parser()}"
    else:
        parser_context = f"Available paths and capabilities: {capabilities_for_parser()}"
    try:
        plan = parse(ctx.effective_message, thinking_emitter=emitter, context=parser_context)
        if not plan or not plan.subquestions:
            logger.warning("Plan stage: parse returned empty plan, using minimal plan.")
            plan = _minimal_plan(ctx.effective_message or ctx.message)
    except Exception as e:
        logger.warning("Plan stage: parse failed, using minimal plan: %s", e, exc_info=True)
        plan = _minimal_plan(ctx.effective_message or ctx.message)
    ctx.plan = plan

    plan_text = plan.subquestions[0].text if plan.subquestions else None
    ctx.refined_query = compute_refined_query(
        ctx.classification,
        ctx.message,
        (ctx.merged_state or {}).get("refined_query"),
        ctx.merged_state or {},
        plan_text,
    )

    rag_k = get_chat_config().rag.top_k
    ctx.blueprint = build_blueprint(plan, rag_default_k=rag_k)
