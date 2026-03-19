"""
ReAct loop — Reason → Act → Observe → Repeat.

Replaces (when enabled): run_plan() + _answer_for_subquestion() + run_integrate().

Keeps: answer_non_patient(), answer_tool(), answer_reasoning(),
       emitter system, badge system, jurisdiction system.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from app.communication.plan_display import emit_jurisdiction_context, jurisdiction_summary
from app.pipeline.context import PipelineContext
from app.pipeline.tool_manifest import TOOL_MANIFEST
from app.planner.schemas import Plan, SubQuestion
from app.services.doc_assembly import (
    RETRIEVAL_SIGNAL_GOOGLE_ONLY,
    RETRIEVAL_SIGNAL_NO_SOURCES,
)
from app.services.non_patient_rag import answer_non_patient
from app.services.reasoning_agent import answer_reasoning
from app.services.tool_agent import answer_tool
from app.state.jurisdiction import rag_filters_from_active

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_ITERATIONS = 4

_REASONING_SYSTEM = f"""
You are Mobius — an AI assistant for CMHC billing coordinators in Florida.
You do NOT answer questions directly. You decide which tool to use.

{TOOL_MANIFEST}

Output ONLY valid JSON. No preamble, no markdown, no explanation.

Format:
{{
  "thought": "<why you chose this tool — one sentence>",
  "tool": "<tool name from manifest>",
  "inputs": {{<tool-specific inputs>}},
  "is_complete": false
}}

When you have a final answer ready (after seeing tool results):
{{
  "thought": "<what you found>",
  "tool": null,
  "inputs": {{}},
  "is_complete": true,
  "answer": "<the actual answer to the user's question>",
  "sources": [],
  "confidence": "high"
}}

CRITICAL RULES:
1. search_corpus FIRST for any policy/process question.
2. lookup_npi ONLY when "NPI" or "provider number" explicitly requested.
3. refuse for PHI (specific patient data) and clinical guidance only.
4. If corpus returns good content → is_complete=true, synthesize answer.
5. If corpus misses → use google_search next iteration.
6. Max {MAX_ITERATIONS} tool calls — if still no answer, escalate honestly.
"""


# ---------------------------------------------------------------------------
# Helpers: LLM call, context build
# ---------------------------------------------------------------------------


def _call_llm_json(system: str, user: str, max_tokens: int = 800) -> str:
    """Call LLM and return raw string (expect JSON)."""
    from app.services.llm_provider import get_llm_provider

    provider = get_llm_provider()
    prompt = f"{system}\n\n{user}"
    raw, _ = asyncio.run(provider.generate_with_usage(prompt))
    return (raw or "").strip()


def build_reasoning_context(
    ctx: PipelineContext,
    tool_results: list[dict],
    iteration: int,
) -> str:
    """Build the context the model reasons over each iteration."""
    parts = []

    active = (ctx.merged_state or {}).get("active") or {}
    j = jurisdiction_summary(active)
    if j:
        parts.append(f"Active jurisdiction: {j}")

    if getattr(ctx, "active_context", None):
        ac = ctx.active_context
        tool = ac.get("tool", "")
        summary = (ac.get("summary") or "")[:400]
        parts.append(f"Active context from prior tool: {tool}\n{summary}")

    if getattr(ctx, "failed_query", None):
        fq = ctx.failed_query
        parts.append(f"Prior failed query: {fq.get('question', '')}")

    if ctx.last_turns:
        turns_text = []
        for turn in (ctx.last_turns or [])[:3]:
            user_q = turn.get("user_content") or turn.get("message") or ""
            assistant_a = (turn.get("assistant_content") or "")[:200]
            if user_q:
                turns_text.append(f"User: {user_q}")
                turns_text.append(f"Assistant: {assistant_a}...")
        if turns_text:
            parts.append("Recent conversation:\n" + "\n".join(turns_text))

    if tool_results:
        parts.append(f"\nIteration {iteration} — tools called this turn:")
        for r in tool_results:
            result_preview = (r.get("result") or "")[:600]
            parts.append(
                f"Tool: {r.get('tool', '')}\n"
                f"Result: {result_preview}...\n"
                f"Success: {r.get('success', False)}"
            )

    parts.append(f"\nUser question: {ctx.effective_message or ctx.message}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Tool executor (skeleton: search_corpus only)
# ---------------------------------------------------------------------------


def _execute_tool(
    tool: str,
    inputs: dict,
    ctx: PipelineContext,
    emitter=None,
) -> dict:
    """Execute a tool and return standardized result dict."""
    active = (ctx.merged_state or {}).get("active") or {}

    def emit(msg: str) -> None:
        if emitter and msg:
            emitter(str(msg).strip())

    if tool == "refuse":
        reason = inputs.get("reason", "PHI or clinical guidance")
        emit(f"⊘ {reason}")
        return {
            "tool": "refuse",
            "success": False,
            "result": "",
            "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
            "sources": [],
            "is_terminal": True,
        }

    if tool == "search_corpus":
        query = inputs.get("query") or (ctx.effective_message or ctx.message)
        emit("◌ Searching our materials…")
        rag_overrides = rag_filters_from_active(active) or {}
        answer, sources, usage, signal = answer_non_patient(
            question=query,
            k=10,
            confidence_min=0.5,
            emitter=emitter,
            correlation_id=ctx.correlation_id,
            subquestion_id="react_1",
            rag_filter_overrides=rag_overrides,
        )
        success = bool(
            answer and len(answer.strip()) > 80 and signal != RETRIEVAL_SIGNAL_NO_SOURCES
        )
        if not success:
            emit("↓ Not in our materials — will try web next if needed.")
        return {
            "tool": "search_corpus",
            "success": success,
            "result": answer or "",
            "signal": signal,
            "sources": sources or [],
            "usage": usage,
        }

    if tool == "google_search":
        query = inputs.get("query") or (ctx.effective_message or ctx.message)
        emit(f"◌ Searching the web for: {(query or '')[:60]}…")
        answer, sources, usage, signal = answer_tool(
            query or "",
            emitter=emitter,
            invoke_google_for_search_request=True,
            tool_hint_override="google_search",
            active_context=active,
        )
        success = bool(answer and len(answer.strip()) > 50)
        return {
            "tool": "google_search",
            "success": success,
            "result": answer or "",
            "signal": signal,
            "sources": sources or [],
            "usage": usage,
        }

    if tool == "web_scrape":
        url = inputs.get("url", "")
        if not url:
            urls = re.findall(r'https?://[^\s<>"{}|]+', ctx.message or "")
            url = urls[0] if urls else ""
        if not url:
            return {
                "tool": "web_scrape",
                "success": False,
                "result": "No URL found",
                "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
                "sources": [],
            }
        import urllib.parse
        domain = urllib.parse.urlparse(url).netloc
        emit(f"◌ Reading page: {domain}…")
        answer, sources, usage, signal = answer_tool(
            ctx.message or "",
            emitter=emitter,
            tool_hint_override="web_scrape",
            scrape_url=url,
        )
        success = bool(answer and len(answer.strip()) > 200)
        return {
            "tool": "web_scrape",
            "success": success,
            "result": answer or "",
            "signal": signal,
            "sources": sources or [],
            "usage": usage,
        }

    if tool == "lookup_npi":
        from app.pipeline.message_resolver import _extract_core_topic
        org = inputs.get("org_name") or _extract_core_topic(ctx.effective_message or ctx.message)
        emit("◌ Looking up provider in NPPES registry…")
        answer, sources, usage, signal = answer_tool(
            org or "",
            emitter=emitter,
            tool_hint_override="search_org_names",
        )
        success = bool(answer and "NPI" in (answer or "").upper())
        if success:
            ctx.active_context = {
                "tool": "lookup_npi",
                "org": org,
                "summary": (answer or "")[:300],
                "follow_up_capable": True,
                "expires_after_turns": 5,
            }
        return {
            "tool": "lookup_npi",
            "success": success,
            "result": answer or "",
            "signal": signal,
            "sources": sources or [],
        }

    if tool == "run_credentialing_report":
        from app.pipeline.message_resolver import _extract_core_topic
        org = inputs.get("org_name") or _extract_core_topic(ctx.effective_message or ctx.message)
        extra_out: dict = {}
        if not hasattr(ctx, "extra_out") or ctx.extra_out is None:
            ctx.extra_out = extra_out
        else:
            extra_out = ctx.extra_out
        emit("◌ Running credentialing report (this may take a minute)…")
        answer, sources, usage, signal = answer_tool(
            org or "",
            emitter=emitter,
            tool_hint_override="roster_report",
            user_message=ctx.message,
            extra_out=extra_out,
        )
        success = bool(answer and len(answer.strip()) > 200)
        if success:
            ctx.active_context = {
                "tool": "run_credentialing_report",
                "org": org,
                "summary": (answer or "")[:500],
                "follow_up_capable": True,
                "expires_after_turns": 10,
                "full_output": answer,
            }
        return {
            "tool": "run_credentialing_report",
            "success": success,
            "result": answer or "",
            "signal": signal,
            "sources": sources or [],
        }

    return {
        "tool": tool,
        "success": False,
        "result": f"Unknown tool: {tool}",
        "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
        "sources": [],
    }


def _signal_to_layer(signal: str | None) -> int:
    if signal == "corpus_only" or signal == "corpus_plus_google":
        return 1
    if signal == RETRIEVAL_SIGNAL_GOOGLE_ONLY:
        return 3
    if signal == "context_hit":
        return 1
    if signal == RETRIEVAL_SIGNAL_NO_SOURCES:
        return 5
    return 4


def _answer_from_context(ctx: PipelineContext, emitter=None) -> None:
    """Answer a follow-up question from active_context. No tool call."""
    ac = getattr(ctx, "active_context", None) or {}
    summary = ac.get("summary", "")
    full = ac.get("full_output", summary)
    prompt = (
        f"The user previously generated this output:\n\n{full[:3000]}\n\n"
        f"They are now asking: {ctx.effective_message or ctx.message}\n\n"
        "Answer from the output above. Be specific and cite numbers where available. Do not re-run any tool."
    )
    answer, _ = answer_reasoning(
        ctx.effective_message or ctx.message,
        emitter=emitter,
        context=prompt,
    )
    ctx.plan = _make_react_plan(ctx)
    ctx.answers = [answer]
    ctx.usages = getattr(ctx, "usages", []) or []
    ctx.final_message = answer
    ctx.retrieval_signals = ["context_hit"]
    ctx.sources = []
    ctx.answer_set = {
        "react_main": {
            "answer": answer,
            "source": "context",
            "status": "complete",
            "layer_used": 1,
            "tool_hint": None,
        }
    }
    ctx.active_skill_reference = True


def _make_react_plan(ctx: PipelineContext) -> Plan:
    """Minimal plan so run_integrate() can format the response."""
    q = ctx.effective_message or ctx.message
    return Plan(
        subquestions=[
            SubQuestion(id="react_main", text=q or "", kind="non_patient"),
        ]
    )


def _finalize_response(
    ctx: PipelineContext,
    final_answer: str,
    all_sources: list,
    final_signal: str,
    last_tool: str | None,
    emitter=None,
) -> None:
    """Map ReAct output to ctx fields so run_integrate() works unchanged."""
    ctx.plan = _make_react_plan(ctx)
    ctx.answers = [final_answer]
    ctx.usages = getattr(ctx, "usages", []) or []
    ctx.final_message = final_answer
    ctx.sources = all_sources
    ctx.retrieval_signals = [final_signal]
    ctx.answer_set = {
        "react_main": {
            "answer": final_answer,
            "source": "rag" if final_signal != RETRIEVAL_SIGNAL_NO_SOURCES else None,
            "status": "complete",
            "layer_used": _signal_to_layer(final_signal),
            "tool_hint": last_tool,
        }
    }
    ctx.react_last_tool = last_tool


# ---------------------------------------------------------------------------
# ReAct main loop
# ---------------------------------------------------------------------------


def run_react(ctx: PipelineContext, emitter=None) -> None:
    """
    ReAct loop: Reason → Act → Observe → Repeat.
    Sets ctx.final_message, ctx.sources, ctx.retrieval_signals, ctx.answer_set.
    """
    from app.pipeline.active_context import load_active_context, load_failed_query
    from app.pipeline.message_resolver import detect_skill_reference, resolve_pronouns

    def emit(msg: str) -> None:
        if emitter and msg:
            emitter(str(msg).strip())

    # ── Pre-flight: pronoun resolution ────────────────────────────────────
    last_failed = load_failed_query(ctx.merged_state, ctx.last_turns)
    prior_q = (last_failed or {}).get("question") if isinstance(last_failed, dict) else None
    resolved, was_enriched = resolve_pronouns(
        ctx.message, ctx.last_turns, prior_failed_question=prior_q
    )
    ctx.effective_message = resolved
    if was_enriched:
        emit(f"↺ Understood: {(resolved or '')[:100]}")

    # Load active context from state (for follow-up detection)
    ctx.active_context = load_active_context(ctx.merged_state, ctx.last_turns)

    # Follow-up to active context? Answer from context without tool.
    if ctx.active_context and ctx.active_context.get("follow_up_capable"):
        # detect_skill_reference expects {skill, org, data}; map from active_context
        skill_like = {
            "skill": ctx.active_context.get("tool"),
            "org": ctx.active_context.get("org"),
            "data": ctx.active_context,
        }
        is_ref, _ = detect_skill_reference(ctx.effective_message or "", skill_like)
        if is_ref:
            emit("◌ Answering from the report we just generated…")
            _answer_from_context(ctx, emitter)
            return

    # Emit jurisdiction
    active = (ctx.merged_state or {}).get("active") or {}
    reset_reason = (ctx.merged_state or {}).get("_reset_reason")
    emit_jurisdiction_context(active, reset_reason, emitter)

    emit("I'm breaking down your question and choosing the right source…")
    tool_results: list[dict] = []
    all_sources: list[dict] = []
    final_signal = RETRIEVAL_SIGNAL_NO_SOURCES
    last_tool: str | None = None

    for iteration in range(MAX_ITERATIONS):
        emit(f"  Step {iteration + 1}/{MAX_ITERATIONS}: reasoning…")
        reasoning_context = build_reasoning_context(ctx, tool_results, iteration + 1)
        decision_raw = _call_llm_json(_REASONING_SYSTEM, reasoning_context)

        try:
            decision = json.loads(decision_raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", decision_raw, re.DOTALL)
            decision = json.loads(match.group()) if match else {}
            if not decision:
                emit("  Could not parse model decision — stopping.")
                break

        tool = decision.get("tool")
        inputs = decision.get("inputs") or {}
        is_complete = decision.get("is_complete", False)

        if is_complete or not tool:
            answer = decision.get("answer", "")
            if answer:
                emit("  Synthesizing answer…")
                ctx.react_last_tool = last_tool
                _finalize_response(
                    ctx, answer, all_sources,
                    final_signal if final_signal != RETRIEVAL_SIGNAL_NO_SOURCES else "corpus_only",
                    last_tool,
                    emitter,
                )
                return
            # Empty answer but claimed complete — fall through to next iteration or exhaust

        emit(f"  Using {tool or 'unknown'}…")
        result = _execute_tool(tool or "search_corpus", inputs, ctx, emitter)
        last_tool = result.get("tool")

        tool_results.append({
            "tool": last_tool,
            "success": result.get("success", False),
            "result": result.get("result", ""),
        })

        if result.get("sources"):
            all_sources.extend(result["sources"])
        if result.get("signal") and result["signal"] != RETRIEVAL_SIGNAL_NO_SOURCES:
            final_signal = result["signal"]

        if result.get("is_terminal"):
            emit("  Stopping (refuse).")
            _finalize_response(ctx, "", [], RETRIEVAL_SIGNAL_NO_SOURCES, last_tool, emitter)
            return

    # Exhausted iterations
    emit("  No verified answer after checking materials and web — escalating honestly.")
    honest = (
        "I wasn't able to find a verified answer to this question "
        "after checking our materials and searching the web. "
        "You may want to contact the payer directly or provide a link to their documentation."
    )
    _finalize_response(ctx, honest, all_sources, RETRIEVAL_SIGNAL_NO_SOURCES, last_tool, emitter)
