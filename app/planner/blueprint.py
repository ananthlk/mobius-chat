"""Parser 2: Blueprint per subquestion (sensitivity, RAG strategy, agent) derived from Plan."""
from typing import Any, Literal

from app.planner.route_triggers import detect_route
from app.planner.schemas import Plan, SubQuestion
from app.state.query_refinement import reframe_for_retrieval
from app.trace_log import trace_entered

Sensitivity = Literal["low", "medium", "high"]
AgentType = Literal["RAG", "patient_stub", "tool", "reasoning"]


def _message_refers_to_org(message: str, org: str) -> bool:
    """True if the message refers to the same org as the active report (avoids re-running)."""
    msg_lower = (message or "").strip().lower()
    org_lower = (org or "").strip().lower()
    if not org_lower:
        return False
    if org_lower in msg_lower:
        return True
    words = org_lower.split()
    if len(words) >= 2 and (words[0] + " " + words[1]) in msg_lower:
        return True
    return False


def _sensitivity_for(sq: SubQuestion) -> Sensitivity:
    """Derive sensitivity from kind and intent. High = personal/rigorous; low = general policy."""
    if sq.kind == "patient":
        return "high"
    intent = sq.question_intent or ""
    if intent == "factual":
        return "medium"
    return "low"


def build_blueprint(
    plan: Plan,
    rag_default_k: int = 10,
    *,
    retrieval_ctx: dict[str, Any] | None = None,
) -> list[dict]:
    """Build Parse 2 Blueprint: one entry per subquestion with sensitivity, RAG config, agent."""
    trace_entered("planner.blueprint.build_blueprint", subquestions=len(plan.subquestions))
    rctx = retrieval_ctx or {}
    refined_query = rctx.get("refined_query")
    jurisdiction = rctx.get("jurisdiction")
    is_followup = rctx.get("is_followup", False)
    user_message = rctx.get("user_message") or ""
    if not user_message and plan.subquestions:
        user_message = plan.subquestions[0].text or ""

    # Pre-check: if a report was just generated, answer from it — do NOT re-run
    deterministic_agent: AgentType | None = None
    force_roster_tool_hint = False
    active_skill = rctx.get("active_skill")
    # When parser chose a tool (e.g. credentialing_qa), honor it — don't override to reasoning
    parser_tool_hint = (plan.subquestions[0].tool_hint if plan.subquestions else None) or ""
    if active_skill and (active_skill.get("skill") or "").strip().lower() == "roster_report":
        if parser_tool_hint and str(parser_tool_hint).lower() == "credentialing_qa":
            deterministic_agent = "tool"
        elif _message_refers_to_org(user_message, active_skill.get("org")):
            deterministic_agent = "reasoning"
        else:
            from app.pipeline.message_resolver import detect_skill_reference
            is_skill_ref, _ = detect_skill_reference(user_message, active_skill)
            if is_skill_ref:
                deterministic_agent = "reasoning"

    # Fallback: state has report_run_id/last_report_org but no active_skill — still route to tool
    if deterministic_agent is None:
        report_run_id = (rctx.get("report_run_id") or "").strip()
        last_report_org = (rctx.get("last_report_org") or "").strip()
        if report_run_id or last_report_org:
            msg_lower = user_message.lower()
            if (
                "pml" in msg_lower and "npi" in msg_lower
                or "section" in msg_lower
                or ("how many" in msg_lower and "pml" in msg_lower)
                or "readiness" in msg_lower
                or "revenue opportunity" in msg_lower
            ):
                deterministic_agent = "tool"
                force_roster_tool_hint = True

    # Deterministic route override: explicit triggers (search web, credentialing report, etc.)
    if deterministic_agent is None:
        route_agent, route_confidence, _ = detect_route(user_message)
        if route_confidence >= 1.0 and route_agent:
            deterministic_agent = route_agent

    # Credentialing follow-up vs build: use credentialing_qa (answer from report) unless user clearly asked to build
    msg_lower = (user_message or "").lower()
    followup_phrases = (
        "section a", "section b", "section c", "section d", "section e",
        "explain section", "i meant", "of the credentialing report", "of the report",
        "what does the report", "what is section", "can you explain section",
    )
    has_followup = any(p in msg_lower for p in followup_phrases)
    has_report_ctx = bool(rctx.get("active_skill") or rctx.get("report_run_id") or rctx.get("last_report_org"))
    build_phrases = (
        "create a credentialing report for", "create credentialing report for",
        "create a medicaid npi report for", "create medicaid npi report for",
        "run the medicaid npi report for", "run credentialing report for",
    )
    has_build = any(p in msg_lower for p in build_phrases)
    use_credentialing_qa = (
        (parser_tool_hint and str(parser_tool_hint).lower() == "credentialing_qa")
        or (has_followup and (has_report_ctx or not has_build))
        or (
            (deterministic_agent == "tool" or (plan.subquestions and getattr(plan.subquestions[0], "tool_hint", None) == "roster_report"))
            and has_followup
            and not (has_build and len((user_message or "").strip()) < 80)
        )
    )
    if has_followup and (has_report_ctx or not has_build):
        deterministic_agent = deterministic_agent or "tool"

    out: list[dict] = []
    for i, sq in enumerate(plan.subquestions):
        # Apply deterministic override to first subquestion when single-intent
        if deterministic_agent and i == 0 and sq.kind != "patient":
            agent = deterministic_agent
        else:
            primary = getattr(sq, "capabilities_primary", None) or ""
            primary = (primary or "").strip().lower()
            if sq.kind == "patient":
                agent = "patient_stub"
            elif primary in ("reasoning",):
                agent = "reasoning"
            elif primary in ("web", "tools") or sq.kind == "tool":
                agent = "tool"
            elif sq.kind == "tool":
                agent = "tool"
            elif sq.kind == "non_patient":
                agent = "RAG"
            else:
                agent = "patient_stub"
        sensitivity = _sensitivity_for(sq)
        rag_k = rag_default_k if agent == "RAG" else 0
        retrieval_config = "standard"
        reframed = reframe_for_retrieval(
            sq.text,
            intent=sq.question_intent,
            question_intent=sq.question_intent,
            last_refined_query=refined_query,
            jurisdiction=jurisdiction,
            is_followup=is_followup,
        )
        on_rag_fail = list(getattr(sq, "on_rag_fail", None) or [])
        # Add web fallback for eligibility/criteria lookups when corpus may lack current rules
        if agent == "RAG":
            text_lower = (sq.text or "").lower()
            if any(kw in text_lower for kw in ("qualify", "eligibility", "eligible", "income threshold", "criteria")):
                if "search_google" not in on_rag_fail and "web" not in str(on_rag_fail).lower():
                    on_rag_fail = list(on_rag_fail) + ["search_google"]
        requires_jurisdiction = getattr(sq, "requires_jurisdiction", None)
        tool_hint = getattr(sq, "tool_hint", None)
        if force_roster_tool_hint and i == 0 and agent == "tool":
            tool_hint = "roster_report"
        if use_credentialing_qa and i == 0 and agent == "tool":
            tool_hint = "credentialing_qa"
        skip_layer_4 = bool(getattr(sq, "skip_layer_4", False))
        question_intent = getattr(sq, "question_intent", None)
        out.append({
            "sq_id": sq.id,
            "agent": agent,
            "sensitivity": sensitivity,
            "rag_k": rag_k,
            "retrieval_config": retrieval_config,
            "kind": sq.kind,
            "intent": sq.question_intent or "—",
            "text": sq.text,
            "reframed_text": reframed if reframed != sq.text else sq.text,
            "on_rag_fail": on_rag_fail if isinstance(on_rag_fail, list) else [],
            "requires_jurisdiction": requires_jurisdiction,
            "tool_hint": tool_hint,
            "skip_layer_4": skip_layer_4,
            "question_intent": question_intent,
        })
    return out
