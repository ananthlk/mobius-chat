"""
ReAct loop — Reason → Act → Observe → Repeat.

Replaces (when enabled): run_plan() + _answer_for_subquestion() + run_integrate().

Keeps: answer_non_patient(), answer_tool(), answer_reasoning(),
       emitter system, badge system, jurisdiction system.

Emission map (thinking chunks sent to UI via emitter=on_thinking):
  Pre-loop:
    [if pronoun enriched] "↺ Understood: <resolved message>"
    [if follow-up to active context] "◌ Answering from the report we just generated…"
    [jurisdiction] emit_jurisdiction_context: "✓ Confirmed: …" | "? Payer not identified…" | etc.
    "I'm breaking down your question and choosing the right source…"
    "  (Up to 4 reasoning rounds: each round I decide to use a tool or to give a final answer.)"
  Per iteration (round 1..4):
    "  Round N/4 — <headline: scoping | grounding | refinement | finalize>"
    "  Reasoning round N/4…"
    [LLM thought] "  → Round N: <thought>"
    [if is_complete with answer] "  Synthesizing answer…" → then exit to integrate
    [else] "  Using <tool>…"
    [if credentialing] "  (The report runs its own steps below — …)"
    [tool-specific] "◌ Searching our materials…" | "◌ Searching the web for: …" | "◌ Reading page: …" | etc.
    [search_corpus fail] "↓ Not in our materials — will try web next if needed."
    [if refuse] "  Stopping (refuse)."
  Exhausted:
    "  No verified answer after checking materials and web — escalating honestly."
  Rule 8: When "Recent conversation" is present and user asks for something the prior answer
  did NOT provide → model must NOT set is_complete=true in round 1; must call a tool first.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

import httpx

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
from app.skills.document_upload import DOCUMENT_UPLOAD_SKILL_MARKDOWN, format_thread_uploads_markdown

def _credentialing_copilot_turn_markdown(run: dict[str, Any], org_name: str) -> str:
    """User-facing summary for a co-pilot turn (chat + panel show draft details)."""
    phase = run.get("phase") or ""
    pending = run.get("pending_step_id") or ""
    rid = run.get("run_id") or ""
    lines = [
        "### Credentialing co-pilot",
        f"**Organization:** {org_name or '—'}",
        f"**Run ID:** `{rid}`",
        "",
    ]
    if phase == "complete":
        lines.append("**Status:** All steps complete.")
        fr = run.get("final_report_text")
        if isinstance(fr, str) and fr.strip():
            lines.append("")
            lines.append(fr.strip()[:4000])
        return "\n".join(lines)
    if phase == "awaiting_validation":
        lines.append(f"**Pending step:** `{pending}` — review the **validation panel** below (or JSON in the UI).")
        lines.append("")
        lines.append("Submit edits, then click **Continue**, or ask me to proceed with the values shown.")
        return "\n".join(lines)
    lines.append(f"**Status:** {phase}")
    return "\n".join(lines)


def _roster_uploads_from_active(active: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for u in active.get("uploaded_files") or []:
        if isinstance(u, dict) and (u.get("purpose") or "").strip() == "roster_reconciliation":
            out.append(u)
    return out


def _format_billing_npi_options_markdown(org_name: str) -> str:
    """NPPES rows with practice address + taxonomy for user-friendly billing NPI choice."""
    base = (os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL") or "").rstrip("/").split("/report")[0]
    name = (org_name or "").strip()
    if not base or not name:
        return ""
    try:
        with httpx.Client(timeout=45.0) as c:
            r = c.post(
                f"{base}/search/org-names",
                json={
                    "name": name,
                    "state": "FL",
                    "limit": 12,
                    "include_practice_address": True,
                    "entity_type_filter": "2",
                    "include_pml": True,
                },
            )
            if r.status_code != 200:
                return ""
            results = (r.json() or {}).get("results") or []
    except Exception:
        return ""
    if not results:
        return ""
    lines = [
        "These **organization NPIs** match that name (NPPES + PML where available). "
        "Large organizations often have **more than one** billing entity — pick the one that matches the claims slice you care about.",
        "",
        "| NPI | Organization | Practice address | Taxonomy | Source | Match |",
        "|-----|--------------|------------------|----------|--------|-------|",
    ]
    for row in results[:10]:
        npi = str(row.get("npi") or "").strip().zfill(10)
        oname = str(row.get("name") or "").replace("|", "/")
        addr = str(row.get("practice_address") or "—").replace("|", "/")
        tax = str(row.get("taxonomy_code") or "—")
        src = str(row.get("source") or "—")
        mt = str(row.get("match_type") or "—")
        lines.append(f"| {npi} | {oname} | {addr} | {tax} | {src} | {mt} |")
    lines.append("")
    lines.append(
        "After you upload a roster, we **auto-pick the best-matching billing NPI** from this list and run reconciliation against **one NPI at a time** "
        "(you can run again with another NPI if you have multiple billing entities)."
    )
    lines.append(
        'To **override** the auto pick before or after upload, reply with **"Use billing NPI 1234567890"** (any row above).'
    )
    return "\n".join(lines)
from app.state.jurisdiction import rag_filters_from_active

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_ITERATIONS = 4

# User-facing headline per ReAct round (complements generic "Reasoning round N/4").
_REACT_ROUND_HEADLINES: tuple[str, ...] = (
    "Scoping — interpret the question and choose the first tool or answer",
    "Grounding — use evidence from prior tool results",
    "Refinement — close gaps or gather missing details",
    "Finalize — answer or escalate honestly",
)

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
2. NPI + PML (e.g. "Is NPI X set up for PML?"): try ask_credentialing_npi FIRST. If it fails (no report), try healthcare_npi_lookup for NPPES info.
3. NPI number only (no PML): use healthcare_npi_lookup for NPPES lookup.
4. lookup_npi ONLY when user asks for NPI of an organization BY NAME ("NPI for David Lawrence Center").
5. refuse for PHI (specific patient data) and clinical guidance only.
6. If corpus returns good content → is_complete=true, synthesize answer.
7. If corpus misses → use google_search next iteration.
8. Max {MAX_ITERATIONS} tool calls — if still no answer, escalate honestly.
9. If a tool result shows success (e.g. "Report stored", "Step 11 done", "report generated", "You can ask any question about it") → set is_complete=true and answer MUST confirm that the report or output was generated successfully. Do NOT say "I cannot generate" when the tool already succeeded.
10. When "Recent conversation" is present: treat the prior assistant reply as the current answer. If the user is asking for something that answer did NOT provide (e.g. a link, URL, specific page, more detail, a number), the answer is INSUFFICIENT — do NOT set is_complete=true. Call a tool (e.g. google_search or web_scrape for links/URLs, search_corpus for policy detail) and only set is_complete=true after you have tool results to fulfill the request.
"""


# ---------------------------------------------------------------------------
# Helpers: LLM call, context build
# ---------------------------------------------------------------------------


def _get_config_sha() -> str:
    """Current prompts+LLM config version for analytics."""
    from app.prompts_llm_config import load_prompts_llm_config
    _, sha = load_prompts_llm_config()
    return sha or ""


def _call_llm_json(
    system: str,
    user: str,
    max_tokens: int = 800,
    ctx: PipelineContext | None = None,
    stage: str = "planner",
) -> str:
    """Call LLM and return raw string (expect JSON). When ctx is provided, uses llm_manager and appends usage to ctx.usages."""
    prompt = f"{system}\n\n{user}"
    if ctx is not None:
        from app.services.llm_manager import generate as llm_generate
        raw, usage = asyncio.run(
            llm_generate(
                prompt,
                stage=stage,
                max_tokens=max_tokens,
                config_sha=_get_config_sha(),
                correlation_id=getattr(ctx, "correlation_id", None),
                thread_id=getattr(ctx, "thread_id", None),
                parser=False,
            )
        )
        if not getattr(ctx, "usages", None):
            ctx.usages = []
        ctx.usages.append(usage)
        return (raw or "").strip()
    from app.services.llm_manager import generate_sync

    raw, _ = generate_sync(prompt, stage="planner", max_tokens=max_tokens, parser=False)
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
            raw = r.get("result") or ""
            # For long results (e.g. credentialing), show head + tail so completion messages are visible
            max_len = 600
            if len(raw) > max_len:
                head_len, tail_len = 320, 400
                result_preview = (
                    raw[:head_len] + "\n... [truncated] ...\n" + raw[-tail_len:]
                )
            else:
                result_preview = raw
            parts.append(
                f"Tool: {r.get('tool', '')}\n"
                f"Result: {result_preview}\n"
                f"Success: {r.get('success', False)}"
            )

    parts.append(f"\nUser question: {ctx.effective_message or ctx.message}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Tool executor (skeleton: search_corpus only)
# ---------------------------------------------------------------------------

# When tools use generate_sync / provider.generate_with_usage, stage may be missing — map for LLM performance UI.
_TOOL_STAGE_FOR_USAGE: dict[str, str] = {
    "search_corpus": "rag",
    "google_search": "web_search",
    "web_scrape": "web_scrape",
    "lookup_npi": "npi_lookup",
    "run_credentialing_report": "roster_report",
    "validate_credentialing_step": "roster_report",
    "run_roster_reconciliation_report": "roster_reconciliation",
    "ask_credentialing_npi": "credentialing_qa",
    "healthcare_npi_lookup": "healthcare_query",
    "document_upload_skill": "document_upload",
    "list_thread_document_uploads": "document_upload",
}


def _append_tool_llm_usage(ctx: PipelineContext, tool: str, result: dict) -> None:
    """Append tool-time LLM usage (RAG, web synthesis, etc.) to ctx.usages for integrate usage_breakdown."""
    u = result.get("usage")
    if not isinstance(u, dict) or not u:
        return
    u = dict(u)
    if not str(u.get("stage") or "").strip():
        key = (tool or "").strip().lower()
        u["stage"] = _TOOL_STAGE_FOR_USAGE.get(key, f"tool_{key}" if key else "tool")
    if not getattr(ctx, "usages", None):
        ctx.usages = []
    ctx.usages.append(u)


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

    if tool == "document_upload_skill":
        emit("◌ Document upload skill…")
        return {
            "tool": "document_upload_skill",
            "success": True,
            "result": DOCUMENT_UPLOAD_SKILL_MARKDOWN,
            "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
            "sources": [],
        }

    if tool == "list_thread_document_uploads":
        tid = (inputs.get("thread_id") or ctx.thread_id or "").strip()
        emit("◌ Listing documents attached to this chat…")
        if not tid:
            return {
                "tool": "list_thread_document_uploads",
                "success": False,
                "result": format_thread_uploads_markdown(""),
                "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
                "sources": [],
            }
        return {
            "tool": "list_thread_document_uploads",
            "success": True,
            "result": format_thread_uploads_markdown(tid),
            "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
            "sources": [],
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
            thread_id=ctx.thread_id,
            phi_detected=False,
            config_sha=_get_config_sha() or None,
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
            "usage": usage,
        }

    if tool == "run_credentialing_report":
        from app.pipeline.message_resolver import _extract_core_topic
        from app.services.credentialing_run_service import create_credentialing_run

        org = inputs.get("org_name") or _extract_core_topic(ctx.effective_message or ctx.message)
        extra_out = {}
        if not hasattr(ctx, "extra_out") or ctx.extra_out is None:
            ctx.extra_out = extra_out
        else:
            extra_out = ctx.extra_out

        mode = (inputs.get("mode") or "autopilot").strip().lower()
        if mode not in ("autopilot", "copilot"):
            mode = "autopilot"

        if mode == "copilot":
            emit("◌ Starting credentialing co-pilot (step-by-step validation)…")
            try:
                run = create_credentialing_run(
                    org or "",
                    "copilot",
                    thread_id=(ctx.thread_id or "").strip() or None,
                    emitter=emit,
                )
            except ValueError as e:
                return {
                    "tool": "run_credentialing_report",
                    "success": False,
                    "result": str(e),
                    "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
                    "sources": [],
                }
            if run.get("phase") == "error":
                return {
                    "tool": "run_credentialing_report",
                    "success": False,
                    "result": run.get("error") or "Co-pilot run failed",
                    "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
                    "sources": [],
                }
            extra_out["credentialing_copilot"] = {
                "run_id": run["run_id"],
                "pending_step_id": run.get("pending_step_id"),
                "phase": run.get("phase"),
                "draft_output": run.get("draft_output"),
                "mode": "copilot",
                "org_name": org,
            }
            answer = _credentialing_copilot_turn_markdown(run, org or "")
            ctx.active_context = {
                "tool": "credentialing_copilot",
                "org": org,
                "summary": answer[:500],
                "follow_up_capable": True,
                "expires_after_turns": 30,
                "credentialing_copilot": True,
                "full_output": answer,
                "credentialing_run_id": run["run_id"],
                "pending_step_id": run.get("pending_step_id"),
            }
            return {
                "tool": "run_credentialing_report",
                "success": True,
                "result": answer,
                "signal": RETRIEVAL_SIGNAL_GOOGLE_ONLY,
                "sources": [],
            }

        emit("◌ Running credentialing report (this may take a minute)…")
        extra_out["credentialing_copilot_clear"] = True
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
            "usage": usage,
        }

    if tool == "validate_credentialing_step":
        from app.services.credentialing_run_service import validate_and_advance_credentialing_run

        active = (ctx.merged_state or {}).get("active") or {}
        run_id = (inputs.get("run_id") or active.get("credentialing_run_id") or "").strip()
        step_id = (inputs.get("step_id") or active.get("credentialing_pending_step_id") or "").strip()
        raw_vo = inputs.get("validated_output")
        if isinstance(raw_vo, str) and raw_vo.strip():
            try:
                validated_output = json.loads(raw_vo)
            except json.JSONDecodeError:
                validated_output = {}
        elif isinstance(raw_vo, dict):
            validated_output = raw_vo
        else:
            validated_output = {}

        if not run_id:
            return {
                "tool": "validate_credentialing_step",
                "success": False,
                "result": "No credentialing run in context. Start with run_credentialing_report(mode='copilot').",
                "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
                "sources": [],
            }
        if not step_id:
            return {
                "tool": "validate_credentialing_step",
                "success": False,
                "result": "step_id is required (or set from thread state as credentialing_pending_step_id).",
                "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
                "sources": [],
            }
        emit(f"◌ Validating step {step_id} and advancing…")
        extra_out = {}
        if not hasattr(ctx, "extra_out") or ctx.extra_out is None:
            ctx.extra_out = extra_out
        else:
            extra_out = ctx.extra_out
        try:
            run = validate_and_advance_credentialing_run(run_id, step_id, validated_output, emitter=emit)
        except KeyError:
            return {
                "tool": "validate_credentialing_step",
                "success": False,
                "result": "Credentialing run not found (expired or wrong run_id).",
                "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
                "sources": [],
            }
        except ValueError as e:
            return {
                "tool": "validate_credentialing_step",
                "success": False,
                "result": str(e),
                "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
                "sources": [],
            }
        if run.get("phase") == "error":
            return {
                "tool": "validate_credentialing_step",
                "success": False,
                "result": run.get("error") or "Step failed",
                "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
                "sources": [],
            }
        extra_out["credentialing_copilot"] = {
            "run_id": run["run_id"],
            "pending_step_id": run.get("pending_step_id"),
            "phase": run.get("phase"),
            "draft_output": run.get("draft_output"),
            "mode": "copilot",
            "org_name": run.get("org_name"),
            "final_report_text": run.get("final_report_text"),
        }
        answer = _credentialing_copilot_turn_markdown(run, run.get("org_name") or "")
        ctx.active_context = {
            "tool": "credentialing_copilot",
            "org": run.get("org_name"),
            "summary": answer[:500],
            "follow_up_capable": True,
            "expires_after_turns": 30,
            "credentialing_copilot": True,
            "full_output": answer,
            "credentialing_run_id": run["run_id"],
            "pending_step_id": run.get("pending_step_id"),
        }
        return {
            "tool": "validate_credentialing_step",
            "success": True,
            "result": answer,
            "signal": RETRIEVAL_SIGNAL_GOOGLE_ONLY,
            "sources": [],
        }

    if tool == "run_roster_reconciliation_report":
        org_name = inputs.get("org_name") or ""
        upload_id = inputs.get("upload_id") or ""
        org_id = inputs.get("org_id") or ""
        active = (ctx.merged_state or {}).get("active") or {}
        roster_files = _roster_uploads_from_active(active)
        # Fallback to thread state (from roster upload via POST /chat/roster-upload)
        if not upload_id or not org_id:
            upload_id = upload_id or (active.get("reconciliation_upload_id") or "").strip()
            org_id = org_id or (active.get("reconciliation_org_id") or "").strip()
            org_name = org_name or (active.get("reconciliation_org_name") or "").strip() or org_name
        # If still missing, use the most recent roster reconciliation upload on this thread (newest first in list)
        if (not upload_id or not org_id) and len(roster_files) >= 1:
            latest = roster_files[0]
            upload_id = upload_id or (latest.get("upload_id") or "").strip()
            org_id = org_id or (latest.get("org_id") or "").strip()
            org_name = org_name or (latest.get("org_name") or "").strip() or org_name
        extra_out = {}
        if not hasattr(ctx, "extra_out") or ctx.extra_out is None:
            ctx.extra_out = extra_out
        else:
            extra_out = ctx.extra_out
        if not org_name or not upload_id or not org_id:
            parts: list[str] = []
            if roster_files:
                parts.append(
                    "**Roster uploads on this chat** (newest first). If you just uploaded, ensure this message "
                    "uses the **same chat** (thread) — starting **New chat** clears the link.\n\n"
                    "| # | File | Organization | Billing NPI | Rows |\n"
                    "|---|------|--------------|-------------|------|"
                )
                for i, u in enumerate(roster_files[:8], 1):
                    fn = str(u.get("filename") or "—").replace("|", "/")
                    on = str(u.get("org_name") or "—").replace("|", "/")
                    oid = str(u.get("org_id") or "—").replace("|", "/")
                    rc = u.get("row_count", "—")
                    parts.append(f"| {i} | {fn} | {on} | {oid} | {rc} |")
                parts.append("")
                if len(roster_files) > 1:
                    parts.append(
                        "We default to the **most recent** file. To use another, say **Run reconciliation using &lt;filename&gt;** "
                        "or upload again.\n"
                    )
            else:
                parts.append(
                    "No roster file is linked to **this chat** yet. Open **⋯** (next to Send) → **Upload file**, "
                    "choose **Roster for reconciliation**, wait for **Upload complete** in the banner, then send your request "
                    "(or enable **Send request after upload** in the upload dialog).\n\n"
                )
            if org_name:
                tbl = _format_billing_npi_options_markdown(org_name)
                if tbl:
                    parts.append(tbl)
            elif not roster_files:
                parts.append("Also tell me the **organization name** (e.g. David Lawrence Center) so we can list matching billing NPIs.")
            result = "\n".join(parts).strip() or (
                "Roster reconciliation needs your org name and a roster file on this chat. "
                "Use ⋯ → Upload file, wait for confirmation, then ask to run the reconciliation report."
            )
            return {
                "tool": "run_roster_reconciliation_report",
                "success": False,
                "result": result,
                "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
                "sources": [],
            }
        if len(roster_files) > 1:
            emit("◌ Several roster files on this chat — using the most recent upload for this run.")
        emit("◌ Running roster reconciliation report…")
        answer, sources, usage, signal = answer_tool(
            org_name,
            emitter=emitter,
            tool_hint_override="roster_reconciliation",
            user_message=ctx.effective_message or ctx.message,
            extra_out=extra_out,
            reconciliation_upload_id=upload_id,
            reconciliation_org_id=org_id,
        )
        success = bool(answer and len(answer.strip()) > 100)
        if success:
            ctx.active_context = {
                "tool": "run_roster_reconciliation_report",
                "org": org_name,
                "summary": (answer or "")[:500],
                "follow_up_capable": True,
                "expires_after_turns": 5,
            }
        return {
            "tool": "run_roster_reconciliation_report",
            "success": success,
            "result": answer or "",
            "signal": signal,
            "sources": sources or [],
            "usage": usage,
        }

    if tool == "ask_credentialing_npi":
        # NPI + PML from credentialing report. Requires report_run_id in context.
        question = inputs.get("question") or (ctx.effective_message or ctx.message)
        emit("◌ Checking NPI in credentialing report (PML status)…")
        answer, sources, usage, signal = answer_tool(
            question or "",
            emitter=emitter,
            tool_hint_override="credentialing_qa",
            user_message=ctx.message,
            active_context=active,
        )
        # Success if we got a substantive answer (not "no report" or CREDENTIALING_QA_NO_REPORT)
        no_report = (
            not answer
            or "don't have a report" in (answer or "").lower()
            or "no report" in (answer or "").lower()
            or "run a credentialing report" in (answer or "").lower()
        )
        success = bool(answer and len(answer.strip()) > 50 and not no_report)
        fallback_hint = " Try healthcare_npi_lookup for NPPES info (name, taxonomy, address)."
        if not success:
            emit("↓ No credentialing report in context — try healthcare_npi_lookup for NPPES info.")
            result_text = (answer or "No credentialing report in context.") + fallback_hint
        else:
            result_text = answer or ""
        return {
            "tool": "ask_credentialing_npi",
            "success": success,
            "result": result_text,
            "signal": signal,
            "sources": sources or [],
            "usage": usage,
        }

    if tool == "healthcare_npi_lookup":
        # NPPES lookup by NPI number (no PML). Fallback when ask_credentialing_npi fails.
        question = inputs.get("question") or (ctx.effective_message or ctx.message)
        emit("◌ Looking up NPI in NPPES registry…")
        answer, sources, usage, signal = answer_tool(
            question or "",
            emitter=emitter,
            tool_hint_override="healthcare_query",
            user_message=ctx.message,
            active_context=active,
        )
        success = bool(answer and len(answer.strip()) > 50 and "Error:" not in (answer or ""))
        return {
            "tool": "healthcare_npi_lookup",
            "success": success,
            "result": answer or "",
            "signal": signal,
            "sources": sources or [],
            "usage": usage,
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


def _sync_extra_out_to_context(ctx: PipelineContext, emitter=None) -> None:
    """Copy extra_out (from credentialing or other tools) onto ctx so integrate can include report PDF/md and payload has report_run_id."""
    extra = getattr(ctx, "extra_out", None)
    if not extra or not isinstance(extra, dict):
        return
    if extra.get("report_run_id"):
        ctx.report_run_id = extra["report_run_id"]
    if extra.get("last_report_org"):
        ctx.last_report_org = extra["last_report_org"]
    pdf_b64 = extra.get("roster_report_pdf_base64")
    if pdf_b64 and isinstance(pdf_b64, str) and len(pdf_b64) > 0:
        ctx.roster_report_pdf_base64 = pdf_b64
    md = extra.get("roster_report_final_md")
    if md and isinstance(md, str) and len(md.strip()) > 0:
        ctx.roster_report_final_md = md
    if extra.get("roster_step_outputs"):
        ctx.roster_step_outputs = extra["roster_step_outputs"]
    cred = extra.get("credentialing_copilot")
    if isinstance(cred, dict) and cred.get("run_id"):
        ctx.credentialing_copilot = cred
    elif extra.get("credentialing_copilot_clear"):
        ctx.credentialing_copilot = None
    # Persist report_run_id / last_report_org / credentialing co-pilot pointers
    if ctx.thread_id and (ctx.thread_id or "").strip():
        try:
            from app.storage.threads import get_state, save_state_full
            from app.state.model import ThreadState
            raw = get_state(ctx.thread_id) or {}
            ts = ThreadState.from_dict(raw)
            delta: dict[str, Any] = {}
            if extra.get("report_run_id"):
                delta["report_run_id"] = extra["report_run_id"]
            if extra.get("last_report_org"):
                delta["last_report_org"] = extra["last_report_org"]
            if extra.get("credentialing_copilot_clear"):
                delta["credentialing_run_id"] = None
                delta["credentialing_pending_step_id"] = None
                delta["credentialing_run_mode"] = None
            if isinstance(cred, dict) and cred.get("run_id"):
                delta["credentialing_run_id"] = cred["run_id"]
                delta["credentialing_run_mode"] = cred.get("mode", "copilot")
                delta["credentialing_pending_step_id"] = cred.get("pending_step_id")
            if delta:
                ts.apply_delta({"active": delta})
                save_state_full(ctx.thread_id, ts.to_dict())
        except Exception:
            pass


def _finalize_response(
    ctx: PipelineContext,
    final_answer: str,
    all_sources: list,
    final_signal: str,
    last_tool: str | None,
    emitter=None,
) -> None:
    """Map ReAct output to ctx fields so run_integrate() works unchanged."""
    _sync_extra_out_to_context(ctx, emitter)
    ctx.plan = _make_react_plan(ctx)
    ctx.answers = [final_answer]
    ctx.usages = getattr(ctx, "usages", []) or []
    ctx.final_message = final_answer
    ctx.sources = all_sources if all_sources is not None else []
    ctx.retrieval_signals = [final_signal] if final_signal else [RETRIEVAL_SIGNAL_NO_SOURCES]
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
    if (
        ctx.active_context
        and ctx.active_context.get("follow_up_capable")
        and not ctx.active_context.get("credentialing_copilot")
    ):
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
    emit("  (Up to 4 reasoning rounds: each round I decide to use a tool or to give a final answer.)")
    tool_results: list[dict] = []
    all_sources: list[dict] = []
    final_signal = RETRIEVAL_SIGNAL_NO_SOURCES
    last_tool: str | None = None

    for iteration in range(MAX_ITERATIONS):
        rn = iteration + 1
        headline = _REACT_ROUND_HEADLINES[iteration]
        emit(f"  Round {rn}/{MAX_ITERATIONS} — {headline}")
        emit(f"  Reasoning round {rn}/{MAX_ITERATIONS}…")
        reasoning_context = build_reasoning_context(ctx, tool_results, rn)
        decision_raw = _call_llm_json(
            _REASONING_SYSTEM,
            reasoning_context,
            ctx=ctx,
            stage=f"react_{rn}",
        )

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
        thought = (decision.get("thought") or "").strip()

        if thought:
            emit(f"  → Round {rn}: {thought}")

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
        if (tool or "").strip().lower() == "run_credentialing_report":
            emit("  (The report runs its own steps below — org, locations, providers, PML, opportunity, etc.)")
        result = _execute_tool(tool or "search_corpus", inputs, ctx, emitter)
        last_tool = result.get("tool")
        _append_tool_llm_usage(ctx, str(last_tool or tool or ""), result)

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
