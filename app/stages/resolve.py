"""Stage: route subquestions to agents, collect answers — with fallback cascade."""
import re
import urllib.parse
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from app.communication.plan_display import (
    emit_fallback,
    emit_jurisdiction_context,
    emit_layer_attempt,
    format_execution_plan,
    format_step_done,
    retrieval_signal_to_fallback_note,
)
from app.services.doc_assembly import (
    RETRIEVAL_SIGNAL_NO_SOURCES,
    RETRIEVAL_SIGNAL_ROSTER_COMPLETE,
)
from app.state.model import ThreadState
from app.state.objective_eval import evaluate_sub_objective_status
from app.storage.threads import get_state, save_state_full
from app.services.non_patient_rag import answer_non_patient
from app.services.reasoning_agent import answer_reasoning
from app.services.tool_agent import answer_tool
from app.services.retrieval_calibration import get_retrieval_blend, intent_to_score
from app.services.usage import LLMUsageDict

if TYPE_CHECKING:
    from app.pipeline.context import PipelineContext


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

URL_PATTERN = re.compile(r'https?://[^\s<>"{}|\\^`\[\]]+', re.IGNORECASE)


def extract_urls(text: str) -> list[str]:
    """Extract all URLs from a text string."""
    return URL_PATTERN.findall(text or "")


# ---------------------------------------------------------------------------
# Roster helpers
# ---------------------------------------------------------------------------

def _is_roster_request(text: str) -> bool:
    """True if message matches roster/credentialing triggers (avoids generic 'Checking capabilities')."""
    t = (text or "").strip().lower()
    roster_triggers = (
        "provider roster",
        "credentialing report",
        "roster report",
        "medicaid roster",
        "roster for",
        "medicaid npi report",
        "create a medicaid npi report",
        "create medicaid npi report",
        "create a credentialing report",
        "create credentialing report",
        "i want to create a medicaid npi report",
        "i want to create a credentialing report",
    )
    return any(tr in t for tr in roster_triggers)


# ---------------------------------------------------------------------------
# Result validation
# ---------------------------------------------------------------------------

_MIN_RAG_LEN = 80
_MIN_SCRAPE_LEN = 150
_MIN_SEARCH_LEN = 50

# Questions matching these patterns require a source citation.
# If no sources are present when reasoning-layer answers, Layer 4 is skipped → ask_user.
_REQUIRES_SOURCE = re.compile(
    r'provider.{0,10}enroll|prior.{0,5}auth|timely.{0,5}fil|credentialing'
    r'|network.{0,10}participation|become.{0,10}provider|join.{0,10}network'
    r'|coverage.{0,10}determination|denial.{0,10}reason|appeal.{0,10}process'
    r'|medicaid.{0,10}require|payer.{0,10}policy',
    re.IGNORECASE,
)


def validate_tool_result(
    agent: str,
    tool_hint: str | None,
    answer: str,
    sources: list,
    retrieval_signal: str,
    question: str,
) -> tuple[bool, str | None]:
    """Returns (is_valid, failure_reason). is_valid=False triggers fallback to next layer."""
    # RAG returned nothing meaningful
    if agent == "RAG":
        if retrieval_signal == RETRIEVAL_SIGNAL_NO_SOURCES:
            return False, "rag_no_sources"
        if not (answer or "").strip() or len((answer or "").strip()) < _MIN_RAG_LEN:
            return False, "rag_empty_answer"

    # Scrape returned no content
    if agent == "tool" and tool_hint == "web_scrape":
        if not (answer or "").strip() or len((answer or "").strip()) < _MIN_SCRAPE_LEN:
            return False, "scrape_empty"

    # Google search returned nothing
    if agent == "tool" and tool_hint == "google_search":
        if not (answer or "").strip() or len((answer or "").strip()) < _MIN_SEARCH_LEN:
            return False, "search_empty"

    # Payer-operational claim with no sources: reasoning is not acceptable
    if _REQUIRES_SOURCE.search(question) and not sources:
        if agent == "reasoning":
            return False, "payer_operational_no_source"

    return True, None


def _ask_user_text(question: str) -> str:
    """Structured ask-user response for Layer 5 escalation."""
    return (
        "I couldn't find verified information for this question in our materials or on the web. "
        "This appears to be a payer-specific operational detail. "
        "You may want to: (1) provide a link to the payer's documentation, "
        "(2) contact the payer directly, or (3) check the payer's provider portal."
    )


# ---------------------------------------------------------------------------
# Core subquestion answerer — fallback cascade
# ---------------------------------------------------------------------------

def _answer_for_subquestion(
    correlation_id: str,
    sq_id: str,
    agent: str,
    kind: str,
    text: str,
    retrieval_params: dict[str, Any] | None = None,
    emitter: Callable[[str], None] | None = None,
    rag_filter_overrides: dict[str, str] | None = None,
    include_document_ids: list[str] | None = None,
    on_rag_fail: list[str] | None = None,
    user_message: str | None = None,
    extra_out: dict | None = None,
    tool_hint: str | None = None,
    skip_layer_4: bool = False,
    question_intent: str | None = None,
    active_context: dict | None = None,
    active_skill_context: str | None = None,
) -> tuple[str, LLMUsageDict | None, list[dict], str, int]:
    """Answer one subquestion with fallback cascade.
    Returns (answer, usage, sources, retrieval_signal, layer_used).
    layer_used: 0=hard stop, 1=RAG, 2=system tool, 3=web/scrape, 4=reasoning, 5=ask_user.
    """

    def emit(msg: str) -> None:
        if emitter and msg and str(msg).strip():
            emitter(str(msg).strip())

    # ── LAYER 0: Hard stops ─────────────────────────────────
    if agent == "patient_stub":
        emit("⊘ This question involves patient-specific information.")
        emit("  I can answer general policy questions but not questions about specific patients.")
        return ("I don't have access to your personal records yet.", None, [], RETRIEVAL_SIGNAL_NO_SOURCES, 0)

    # ── URL pre-processing ───────────────────────────────────
    # Promote to web_scrape if a URL is present and no explicit scrape tool was requested
    detected_urls = extract_urls(text) or extract_urls(user_message or "")
    if detected_urls and agent == "tool" and tool_hint not in ("web_scrape", "roster_report",
                                                                 "npi_lookup", "search_org_names",
                                                                 "healthcare_query"):
        if tool_hint in (None, "google_search"):
            tool_hint = "web_scrape"

    # ── LAYER 1: RAG ─────────────────────────────────────────
    if agent == "RAG":
        emit_layer_attempt(agent, None, None, emitter)
        params = retrieval_params or get_retrieval_blend(0.5)
        on_fail = (on_rag_fail or []) if isinstance(on_rag_fail, list) else []
        answer_text, sources, usage, signal = answer_non_patient(
            question=text,
            k=params.get("top_k"),
            confidence_min=params.get("confidence_min"),
            n_hierarchical=params.get("n_hierarchical"),
            n_factual=params.get("n_factual"),
            emitter=emitter,
            correlation_id=correlation_id,
            subquestion_id=sq_id,
            rag_filter_overrides=rag_filter_overrides,
            include_document_ids=include_document_ids,
            on_rag_fail=on_fail,
        )
        is_valid, _ = validate_tool_result("RAG", None, answer_text, sources, signal, text)
        if is_valid:
            return (answer_text, usage, sources or [], signal, 1)
        # RAG failed — fall through to Layer 3 (web)
        emit_fallback("RAG", "google_search", emitter)
        agent = "tool"
        tool_hint = "google_search"
        # Fall through to tool block

    # ── LAYER 2 / 3: Tool and Web ───────────────────────────
    if agent == "tool":
        # Layer 2 = system tools; Layer 3 = generic web
        layer_num = 2 if tool_hint in (
            "npi_lookup", "search_org_names", "healthcare_query",
            "roster_report", "search_org_by_address",
        ) else 3

        scrape_url = detected_urls[0] if (tool_hint == "web_scrape" and detected_urls) else None

        if not _is_roster_request(user_message or text):
            emit_layer_attempt(agent, tool_hint, scrape_url, emitter)

        answer, sources, usage, signal = answer_tool(
            text,
            emitter=emitter,
            invoke_google_for_search_request=True,
            user_message=user_message,
            extra_out=extra_out,
            tool_hint_override=tool_hint,
            scrape_url=scrape_url,
            question_intent=question_intent,
            active_context=active_context,
        )
        is_valid, _ = validate_tool_result(agent, tool_hint, answer, sources, signal, text)
        if is_valid:
            return (answer, usage, sources or [], signal, layer_num)

        # Tool failed — fall to Layer 4 (unless skip_layer_4)
        if skip_layer_4:
            emit("⚠ I don't have verified information for this specific question.")
            emit("  This is a payer-specific operational detail I can't confirm without a source.")
            return (_ask_user_text(text), None, [], RETRIEVAL_SIGNAL_NO_SOURCES, 5)

        emit_fallback(agent, "reasoning", emitter)
        agent = "reasoning"
        # Fall through

    # ── LAYER 4: Reasoning ──────────────────────────────────
    if agent == "reasoning":
        # Safety net: skip LLM call entirely for payer-operational questions.
        # Catches cases where the planner missed skip_layer_4=true.
        # Avoids a wasted LLM round-trip that validate_tool_result would reject anyway.
        if _REQUIRES_SOURCE.search(text):
            emit("⚠ I don't have verified information for this specific question.")
            emit("  This is a payer-specific operational detail I can't confirm without a source.")
            return (_ask_user_text(text), None, [], RETRIEVAL_SIGNAL_NO_SOURCES, 5)

        emit_layer_attempt("reasoning", None, None, emitter)
        answer, usage = answer_reasoning(
            text,
            emitter=emitter,
            context=active_skill_context if (active_skill_context or "").strip() else None,
        )
        is_valid, _ = validate_tool_result("reasoning", None, answer, [], RETRIEVAL_SIGNAL_NO_SOURCES, text)
        if is_valid:
            emit("⚠ This answer is from general knowledge. Verify against payer documentation before acting.")
            return (answer, usage, [], RETRIEVAL_SIGNAL_NO_SOURCES, 4)
        # Layer 4 failed payer-operational check
        emit("⚠ I don't have verified information for this specific question.")
        emit("  This is a payer-specific operational detail I can't confirm without a source.")
        return (_ask_user_text(text), None, [], RETRIEVAL_SIGNAL_NO_SOURCES, 5)

    # Should not reach here
    return ("Unable to answer this question.", None, [], RETRIEVAL_SIGNAL_NO_SOURCES, 5)


# ---------------------------------------------------------------------------
# Main stage
# ---------------------------------------------------------------------------

def run_resolve(
    ctx: "PipelineContext",
    emitter: Callable[[str], None] | None = None,
) -> None:
    """Answer each subquestion, populate ctx.answers, ctx.sources, ctx.usages, ctx.retrieval_signals."""
    plan = ctx.plan
    if not plan:
        return

    # ── ORDERED HEADER EMITS — must fire first, unconditionally, before any retrieval ──
    # Placing these here (not in the orchestrator) guarantees they enter the serial DB
    # insert queue before any retrieval events, regardless of classification or timing.
    active = (ctx.merged_state or {}).get("active") or {}
    reset_reason = active.get("_reset_reason") or (ctx.merged_state or {}).get("_reset_reason")
    emit_jurisdiction_context(active, reset_reason, emitter)

    if ctx.blueprint:
        for line in format_execution_plan(ctx.plan, ctx.blueprint, user_message=ctx.effective_message or ctx.message):
            if emitter:
                emitter(line)

    from app.state.jurisdiction import rag_filters_from_active

    rag_filter_overrides = rag_filters_from_active((ctx.merged_state or {}).get("active")) or {}
    include_document_ids = [s["document_id"] for s in (ctx.last_turn_sources or []) if s.get("document_id")]
    blueprint = ctx.blueprint
    # When user is asking about last skill output, pass context so reasoning answers from it
    active_skill_context = (
        (ctx.context_pack or "").strip()
        if getattr(ctx, "active_skill_reference", False)
        else None
    )
    if not (active_skill_context or "").strip():
        active_skill_context = None
    plan_usage = getattr(plan, "llm_usage", None)
    usages: list[dict] = [plan_usage] if plan_usage else []
    answers: list[str] = []
    answer_set: dict[str, dict[str, Any]] = dict(getattr(ctx, "answer_set", None) or {})
    all_sources: list[dict] = []
    retrieval_signals: list[str] = []

    for i, sq in enumerate(plan.subquestions):
        bp = blueprint[i] if i < len(blueprint) else {}
        pre_answer = getattr(sq, "pre_answer", None)
        if pre_answer and str(pre_answer).strip():
            ans = str(pre_answer).strip()
            answers.append(ans)
            retrieval_signals.append("planner_pre_resolved")
            answer_set[sq.id] = {"answer": ans, "source": "planner", "status": "answered", "layer_used": 1}
            if emitter:
                total = len(plan.subquestions)
                emitter(format_step_done(i + 1, total, success=True, used_fallback=False))
            continue

        # Pre-filled from user_context or master_objective (prior-turn); skip RAG
        existing = answer_set.get(sq.id, {})
        if existing.get("answer") and existing.get("source") in ("user_context", "master_objective"):
            ans = str(existing["answer"]).strip()
            answers.append(ans)
            retrieval_signals.append(existing.get("source") or "user_context")
            if emitter:
                total = len(plan.subquestions)
                emitter(format_step_done(i + 1, total, success=True, used_fallback=False))
            continue

        retrieval_params = None
        agent = bp.get("agent") or ("RAG" if sq.kind == "non_patient" else "patient_stub")
        if agent == "RAG":
            # Use question_intent string (procedural/factual/canonical) to determine blend.
            # Do NOT use intent_score — that is the planner's routing confidence and is always
            # high (≈1.0) for well-routed RAG questions. Using it would set n_hierarchical=0,
            # eliminating paragraph chunks and leaving only BM25 sentence fragments.
            # Pass sq.text so intent_to_score can rescue process questions misclassified as factual.
            score = intent_to_score(getattr(sq, "question_intent", None), question_text=sq.text)
            retrieval_params = get_retrieval_blend(score)

        question_text = bp.get("reframed_text") or bp.get("text") or sq.text
        on_rag_fail = bp.get("on_rag_fail") if isinstance(bp.get("on_rag_fail"), list) else None
        tool_hint = bp.get("tool_hint")
        skip_layer_4 = bool(bp.get("skip_layer_4", False))
        question_intent = bp.get("question_intent") or getattr(sq, "question_intent", None)
        extra_out: dict = {} if agent == "tool" else {}

        # Dedupe: if we already ran the credentialing report this turn (same plan, two subquestions), reuse first answer
        reuse_roster_idx: int | None = None
        if agent == "tool" and tool_hint == "roster_report" and getattr(ctx, "report_run_id", None):
            for j in range(i):
                if (blueprint[j] or {}).get("tool_hint") == "roster_report" and j < len(retrieval_signals) and retrieval_signals[j] == RETRIEVAL_SIGNAL_ROSTER_COMPLETE:
                    reuse_roster_idx = j
                    break
        if reuse_roster_idx is not None:
            ans = answers[reuse_roster_idx]
            retrieval_signal = retrieval_signals[reuse_roster_idx]
            sources = []  # already in all_sources from first run
            usage = None
            layer_used = 2
            if agent == "tool":
                extra_out["report_run_id"] = ctx.report_run_id
                if getattr(ctx, "last_report_org", None):
                    extra_out["last_report_org"] = ctx.last_report_org
        else:
            ans, usage, sources, retrieval_signal, layer_used = _answer_for_subquestion(
                ctx.correlation_id,
                sq.id,
                agent,
                sq.kind,
                question_text,
                retrieval_params=retrieval_params,
                emitter=emitter,
                rag_filter_overrides=rag_filter_overrides or None,
                include_document_ids=include_document_ids or None,
                on_rag_fail=on_rag_fail,
                user_message=(ctx.effective_message or ctx.message) if agent in ("tool", "RAG") else None,
                extra_out=extra_out if agent == "tool" else None,
                tool_hint=tool_hint,
                skip_layer_4=skip_layer_4,
                question_intent=question_intent,
                active_context=active,
                active_skill_context=active_skill_context,
            )
        if extra_out and extra_out.get("roster_step_outputs"):
            ctx.roster_step_outputs = extra_out["roster_step_outputs"]
        if extra_out:
            if extra_out.get("report_run_id"):
                ctx.report_run_id = extra_out["report_run_id"]
            if extra_out.get("last_report_org"):
                ctx.last_report_org = extra_out["last_report_org"]
            # Persist so next message can "ask about this report" or pull up by org when report_run_id is missing
            if ctx.thread_id and (ctx.thread_id or "").strip() and (extra_out.get("report_run_id") or extra_out.get("last_report_org")):
                try:
                    raw = get_state(ctx.thread_id)
                    ts = ThreadState.from_dict(raw)
                    delta = {}
                    if extra_out.get("report_run_id"):
                        delta["report_run_id"] = extra_out["report_run_id"]
                    if extra_out.get("last_report_org"):
                        delta["last_report_org"] = extra_out["last_report_org"]
                    if delta:
                        ts.apply_delta({"active": delta})
                        save_state_full(ctx.thread_id, ts.to_dict())
                        if emitter:
                            emitter("Report stored. You can ask any question about it.")
                except Exception as e:
                    if __debug__:
                        import logging
                        logging.getLogger(__name__).debug("Could not persist report_run_id/last_report_org: %s", e)
            pdf_b64 = extra_out.get("roster_report_pdf_base64")
            if pdf_b64 and isinstance(pdf_b64, str) and len(pdf_b64) > 0:
                ctx.roster_report_pdf_base64 = pdf_b64
            md = extra_out.get("roster_report_final_md")
            if md and isinstance(md, str) and len(md.strip()) > 0:
                ctx.roster_report_final_md = md

        answers.append(ans)
        retrieval_signals.append(retrieval_signal)
        obj_status = evaluate_sub_objective_status(ans, retrieval_signal)
        source = "rag" if agent == "RAG" else ("tool" if agent == "tool" else str(agent).lower())
        answer_set[sq.id] = {
            "answer": ans,
            "source": source,
            "status": obj_status,
            "layer_used": layer_used,
        }

        # Emit step progress so user can follow along
        if emitter:
            total = len(plan.subquestions)
            if retrieval_signal == RETRIEVAL_SIGNAL_ROSTER_COMPLETE and total == 1:
                emitter("✓ All steps complete")
            else:
                fallback_note = retrieval_signal_to_fallback_note(retrieval_signal)
                if layer_used == 3 and not fallback_note:
                    fallback_note = "used web search — nothing relevant in our materials"
                status = format_step_done(i + 1, total, success=True, used_fallback=fallback_note)
                emitter(status)
        if usage:
            usages.append(usage)
        for s in sources or []:
            all_sources.append({**s, "index": len(all_sources) + 1})

    ctx.answers = answers
    ctx.answer_set = answer_set
    ctx.sources = all_sources
    ctx.usages = usages
    ctx.retrieval_signals = retrieval_signals

    # Active skill context: store for follow-up questions (PML, section C, revenue, NPI list)
    tool_hints = [
        (ctx.blueprint[i].get("tool_hint") or "")
        for i in range(len(ctx.blueprint or []))
    ]
    if "roster_report" in tool_hints:
        from app.pipeline.message_resolver import extract_roster_skill_data

        skill_data = extract_roster_skill_data(ctx)
        # Use resolved org name so "Tell me more about the report for X" matches active_skill.org
        report_org = getattr(ctx, "last_report_org", None) or ""
        if not (report_org and report_org.strip()):
            msg = (ctx.effective_message or ctx.message or "").strip()
            for prefix in (
                "credentialing report for ", "report for ", "medicaid npi report for ",
                "create a credentialing report for ", "create credentialing report for ",
            ):
                if prefix in msg.lower():
                    report_org = msg[msg.lower().find(prefix) + len(prefix) :].strip().rstrip("?.,;!")
                    break
            if not report_org and " for " in msg.lower() and "report" in msg.lower():
                idx = msg.lower().rfind(" for ")
                if idx >= 0:
                    report_org = msg[idx + 5 :].strip().rstrip("?.,;!")
        ctx.active_skill = {
            "skill": "roster_report",
            "org": (report_org or ctx.effective_message or ctx.message or "").strip() or "the organization",
            "data": skill_data,
            "turn": ctx.correlation_id,
        }
    elif "search_org_names" in tool_hints or "healthcare_query" in tool_hints:
        ctx.active_skill = {
            "skill": "npi_lookup",
            "org": ctx.effective_message or ctx.message,
            "data": {
                "results": [
                    {
                        "name": s.get("document_name"),
                        "npi": s.get("npi"),
                        "match_type": s.get("confidence_label"),
                    }
                    for s in (ctx.sources or [])
                ]
            },
            "turn": ctx.correlation_id,
        }
    else:
        ctx.active_skill = None

    # Conversational continuity: store failed query for next turn's message resolver
    all_layer5 = (
        all(
            (ctx.answer_set.get(sq.id) or {}).get("layer_used") == 5
            for sq in plan.subquestions
        )
        if plan and plan.subquestions
        else False
    )
    honest_miss_signals = ("no_sources", "ask_user")
    all_missed = any(
        sig in (r or "")
        for r in (ctx.retrieval_signals or [])
        for sig in honest_miss_signals
    )
    if all_layer5 or all_missed:
        ctx.failed_query = {
            "question": ctx.effective_message or ctx.message,
            "payer": (ctx.merged_state or {}).get("active", {}).get("payer"),
            "retrieval_signals": list(ctx.retrieval_signals or []),
            "layer_used": 5,
        }
