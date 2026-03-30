"""Stage: parse message into plan, compute refined_query, build blueprint."""
import logging
from collections.abc import Callable

from app.chat_config import get_chat_config, get_config_sha
from app.pipeline.context import PipelineContext
from app.planner import parse
from app.planner.blueprint import build_blueprint
from app.planner.credentialing_flow_intent import parse_credentialing_flow_intent
from app.planner.schemas import Plan, SubQuestion
from app.state.master_objective import MasterObjective
from app.state.refined_query import compute_refined_query, is_followup_continuation
from app.stages.agents.capabilities import capabilities_for_parser

logger = logging.getLogger(__name__)


def _minimal_plan(message: str) -> Plan:
    """Fallback plan when parse fails: single subquestion with raw message, kind=non_patient."""
    text = (message or "").strip() or "What can you help with?"
    return Plan(
        subquestions=[
            SubQuestion(id="sq1", text=text, kind="non_patient", question_intent="canonical", intent_score=0.5),
        ],
        credentialing_flow_intent=parse_credentialing_flow_intent(text),
    )


def _plan_from_master_objective(obj: MasterObjective) -> Plan:
    """Build Plan from master_objective sub_objectives (skip planner when slot_fill)."""
    subs = []
    for so in (obj.sub_objectives or []):
        if so.id and (so.text or "").strip():
            subs.append(SubQuestion(
                id=str(so.id),
                text=so.text.strip(),
                kind="non_patient",
                question_intent="factual",
                intent_score=0.6,
            ))
    joined = " ".join(so.text.strip() for so in (obj.sub_objectives or []) if (so.text or "").strip())
    return Plan(
        subquestions=subs,
        credentialing_flow_intent=parse_credentialing_flow_intent(joined),
    )


def run_plan(ctx: PipelineContext, emitter: Callable[[str], None] | None = None) -> None:
    """Parse effective_message into plan, compute refined_query, build blueprint."""
    # When slot_fill and we have master_objective, reuse its sub_objectives as plan (don't re-parse)
    if ctx.classification in ("slot_fill", "jurisdiction_change") and ctx.master_objective:
        obj = MasterObjective.from_dict(ctx.master_objective)
        if obj and obj.sub_objectives:
            plan = _plan_from_master_objective(obj)
            logger.info("Plan stage: slot_fill, reusing plan from master_objective (%d subquestions)", len(plan.subquestions))
            ctx.plan = plan
            plan_text = plan.subquestions[0].text if plan.subquestions else None
            last_turn = ctx.last_turns[0] if ctx.last_turns else {}
            ctx.refined_query = compute_refined_query(
                ctx.classification,
                ctx.message,
                (ctx.merged_state or {}).get("refined_query"),
                ctx.merged_state or {},
                plan_text,
                last_turn=last_turn,
            )
            rag_k = get_chat_config().rag.top_k
            from app.state.jurisdiction import get_jurisdiction_from_active
            last_refined = (ctx.merged_state or {}).get("refined_query")
            retrieval_ctx = {
                "refined_query": ctx.refined_query,
                "jurisdiction": get_jurisdiction_from_active((ctx.merged_state or {}).get("active")),
                "is_followup": is_followup_continuation(ctx.message, last_turn, last_refined),
                "user_message": ctx.effective_message or ctx.message,
            }
            if (ctx.merged_state or {}).get("active_skill"):
                retrieval_ctx["active_skill"] = (ctx.merged_state or {}).get("active_skill")
            act = (ctx.merged_state or {}).get("active") or {}
            if (act.get("report_run_id") or "").strip():
                retrieval_ctx["report_run_id"] = (act.get("report_run_id") or "").strip()
            if (act.get("last_report_org") or "").strip():
                retrieval_ctx["last_report_org"] = (act.get("last_report_org") or "").strip()
            ctx.blueprint = build_blueprint(plan, rag_default_k=rag_k, retrieval_ctx=retrieval_ctx)
            return

    parser_context = ctx.context_pack
    if parser_context:
        parser_context = f"{parser_context}\n\nAvailable paths and capabilities: {capabilities_for_parser()}"
    else:
        parser_context = f"Available paths and capabilities: {capabilities_for_parser()}"
    try:
        last_plan = ctx.master_objective if ctx.master_objective else None
        _sha = get_config_sha() or None
        plan = parse(
            ctx.effective_message,
            thinking_emitter=emitter,
            context=parser_context,
            last_master_plan=last_plan,
            correlation_id=ctx.correlation_id,
            thread_id=ctx.thread_id,
            config_sha=_sha,
            mode=getattr(ctx, "chat_mode", None),
        )
        if not plan or not plan.subquestions:
            logger.warning("Plan stage: parse returned empty plan, using minimal plan.")
            plan = _minimal_plan(ctx.effective_message or ctx.message)
    except Exception as e:
        logger.warning("Plan stage: parse failed, using minimal plan: %s", e, exc_info=True)
        plan = _minimal_plan(ctx.effective_message or ctx.message)
    ctx.plan = plan

    plan_text = plan.subquestions[0].text if plan.subquestions else None
    last_turn = ctx.last_turns[0] if ctx.last_turns else {}
    ctx.refined_query = compute_refined_query(
        ctx.classification,
        ctx.message,
        (ctx.merged_state or {}).get("refined_query"),
        ctx.merged_state or {},
        plan_text,
        last_turn=last_turn,
    )

    rag_k = get_chat_config().rag.top_k
    from app.state.jurisdiction import get_jurisdiction_from_active
    last_refined = (ctx.merged_state or {}).get("refined_query")
    retrieval_ctx = {
        "refined_query": ctx.refined_query,
        "jurisdiction": get_jurisdiction_from_active((ctx.merged_state or {}).get("active")),
        "is_followup": is_followup_continuation(ctx.message, last_turn, last_refined),
        "user_message": ctx.effective_message or ctx.message,
    }
    active_skill = (ctx.merged_state or {}).get("active_skill")
    if active_skill:
        retrieval_ctx["active_skill"] = active_skill
    active = (ctx.merged_state or {}).get("active") or {}
    if (active.get("report_run_id") or "").strip():
        retrieval_ctx["report_run_id"] = (active.get("report_run_id") or "").strip()
    if (active.get("last_report_org") or "").strip():
        retrieval_ctx["last_report_org"] = (active.get("last_report_org") or "").strip()
    ctx.blueprint = build_blueprint(plan, rag_default_k=rag_k, retrieval_ctx=retrieval_ctx)
