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
    "  (Up to N reasoning rounds — N is 3 in copilot, 6 in agentic.)"
  Per iteration (round 1..N):
    "  Round N/M — <headline varies by round and mode>"
    "  Reasoning round N/M…"
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
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

import httpx

from app.communication.plan_display import emit_jurisdiction_context, jurisdiction_summary
from app.communication.tool_output_envelope import compose_mobius_tool_envelope
from app.pipeline.context import PipelineContext
from app.pipeline.tool_manifest import TOOL_MANIFEST
from app.planner.schemas import Plan, SubQuestion
from app.services.doc_assembly import (
    RETRIEVAL_SIGNAL_GOOGLE_ONLY,
    RETRIEVAL_SIGNAL_NO_SOURCES,
    RETRIEVAL_SIGNAL_ROSTER_COMPLETE,
)
from app.services.non_patient_rag import answer_non_patient
from app.services.reasoning_agent import answer_reasoning
from app.services.tool_agent import (
    REACT_TOOL_SUMMARY_KEY,
    answer_tool,
    _react_summary_from_long_markdown,
)
from app.skills.document_upload import DOCUMENT_UPLOAD_SKILL_MARKDOWN, format_thread_uploads_markdown

# After these tools succeed, ReAct finalizes with summary + full markdown (avoids wasted rounds on huge payloads).
_CREDENTIALING_DUAL_FINALIZE_TOOLS = frozenset({
    "find_org_locations",
    "find_associated_providers_at_locations",
})


def _attach_credentialing_result_summary(
    out: dict[str, Any],
    result_text: str,
    *,
    summary_heading: str,
    long_threshold: int = 800,
) -> dict[str, Any]:
    """Add result_summary when prose is long (NPPES/credentialing/healthcare tools)."""
    txt = (result_text or "").strip()
    if len(txt) > long_threshold:
        summ = _react_summary_from_long_markdown(txt, heading=summary_heading)
        if summ:
            out = dict(out)
            out["result_summary"] = summ
    return out


from app.pipeline.credentialing_envelope import (
    envelope_routes_to_reconciliation,
    roster_uploads_from_active as _roster_uploads_from_active,
)

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


def _envelope_routes_to_reconciliation(ctx: PipelineContext) -> bool:
    """Delegate to shared envelope routing (credentialing vs reconciliation)."""
    return envelope_routes_to_reconciliation(
        ctx.merged_state or {},
        getattr(ctx, "credentialing_options", None) or {},
        ctx.message or "",
    )


def _format_billing_npi_options_markdown(org_name: str, *, skill_search_mode: str = "copilot") -> str:
    """NPPES rows with practice address + taxonomy for user-friendly billing NPI choice."""
    base = (os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL") or "").rstrip("/").split("/report")[0]
    name = (org_name or "").strip()
    if not base or not name:
        return ""
    sm = skill_search_mode if skill_search_mode in ("copilot", "agentic") else "copilot"
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
                    "search_mode": sm,
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
# ReAct decision JSON (reasoning LLM returns a single JSON object)
# ---------------------------------------------------------------------------


def _strip_markdown_json_fence(s: str) -> str:
    """Remove ```json ... ``` wrapper if present."""
    t = s.strip()
    if not t.startswith("```"):
        return t
    lines = t.split("\n")
    if len(lines) >= 2 and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_balanced_json_object(text: str) -> str | None:
    """
    First top-level `{ ... }` with brace depth outside of JSON strings.
    Avoids greedy `\\{.*\\}` which breaks when values contain `}` (e.g. markdown).
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    i = start
    while i < len(text):
        c = text[i]
        if escape:
            escape = False
            i += 1
            continue
        if in_string:
            if c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    return None


def _parse_react_decision_dict_obj(text: str) -> dict | None:
    """Try stdlib json.loads then json_repair (LLMs often emit trailing commas, etc.)."""
    t = (text or "").strip()
    if not t:
        return None
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    try:
        import json_repair

        obj = json_repair.loads(t)
        if isinstance(obj, dict):
            return obj
    except Exception as e:
        logger.debug("ReAct decision json_repair failed: %s", e)
    return None


def _parse_react_decision_json(decision_raw: str) -> dict | None:
    """
    Parse reasoning-round JSON. Returns None if parsing fails (caller may stop the loop).
    """
    raw = (decision_raw or "").strip()
    if not raw:
        return None
    stripped = _strip_markdown_json_fence(raw)
    for candidate in (stripped, raw):
        obj = _parse_react_decision_dict_obj(candidate)
        if obj is not None:
            return obj
        extracted = _extract_balanced_json_object(candidate)
        if extracted:
            obj = _parse_react_decision_dict_obj(extracted)
            if obj is not None:
                return obj
            logger.warning(
                "ReAct decision JSON failed after balanced extract (first 240 chars): %s",
                extracted[:240],
            )
    return None


_ORG_NPI_NAME_LOOKUP_HINT = re.compile(
    r"(?:^|\b)(?:find|look|lookup|list|search|get|show)\s+(?:the\s+)?npis?\s+for\s+",
    re.I,
)


def _react_fallback_org_npi_lookup_decision(ctx: PipelineContext) -> dict | None:
    """If the reasoning model returns unusable text, still route clear 'NPIs for Org' asks to lookup_npi."""
    m = (ctx.effective_message or ctx.message or "").strip()
    if not m:
        return None
    mm = _ORG_NPI_NAME_LOOKUP_HINT.search(m)
    if not mm:
        return None
    if re.search(r"\b\d{10}\b", m):
        return None
    tail = m[mm.end() :].strip().rstrip("?.!")
    tail = re.split(r"\s+and\s+i\s+can\b", tail, maxsplit=1, flags=re.I)[0].strip()
    tail = re.split(r"\s+so\s+(?:that|i)\s+can\b", tail, maxsplit=1, flags=re.I)[0].strip()
    if len(tail) < 2:
        return None
    if len(tail) > 100:
        tail = tail[:100].strip()
    return {
        "thought": "Fallback: user asked for organization NPI(s) by name.",
        "tool": "lookup_npi",
        "inputs": {"org_name": tail},
        "is_complete": False,
    }


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REACT_MAX_ROUNDS_COPILOT = 3
REACT_MAX_ROUNDS_AGENTIC = 6
REACT_MAX_ROUNDS_QUICK   = 2   # mini-container: fail-fast, brief answer

# Answers longer than this in quick mode signal that the user should follow up in full chat
QUICK_MODE_TRUNCATED_CHARS = 500


def react_chat_mode_label(chat_mode: str | None) -> str:
    """Normalized ReAct mode for prompts and UI: copilot (default), agentic, or quick."""
    m = (chat_mode or "").strip().lower()
    if m == "agentic":
        return "agentic"
    if m == "quick":
        return "quick"
    return "copilot"


def react_max_iterations_for_mode(chat_mode: str | None) -> int:
    """Quick: 2 rounds (mini container). Copilot: 3. Agentic: 6."""
    label = react_chat_mode_label(chat_mode)
    if label == "agentic":
        return REACT_MAX_ROUNDS_AGENTIC
    if label == "quick":
        return REACT_MAX_ROUNDS_QUICK
    return REACT_MAX_ROUNDS_COPILOT


def _react_round_headline(iteration: int, max_it: int) -> str:
    """User-facing headline for this round index (0-based), depends on total rounds."""
    if iteration == 0:
        return "Scoping — interpret the question and choose the first tool or answer"
    if iteration == 1:
        return "Grounding — use evidence from prior tool results"
    if iteration >= max_it - 1:
        return "Finalize — answer or escalate honestly"
    if iteration == 2:
        return "Refinement — close gaps or gather missing details"
    if iteration == 3:
        return "Extended — alternate tools or queries if needed"
    return "Extended — narrow or verify before answering"


def _react_reasoning_system(max_iterations: int, chat_mode: str) -> str:
    """Build reasoning system prompt; chat_mode is 'copilot', 'agentic', or 'quick'."""
    mode = (chat_mode or "copilot").strip().lower()
    if mode not in ("agentic", "quick"):
        mode = "copilot"
    if mode == "quick":
        mode_block = f"""
CHAT MODE: **quick** (mini-container, max {max_iterations} rounds — fail fast)

Quality bar for this mode:
- Answer in **2–4 sentences maximum**. Be direct and specific.
- Use **at most 1 tool call**. If you can answer from context without a tool, do so immediately.
- No bullet lists longer than 3 items. No section headers. No lengthy explanations.
- If the full answer genuinely requires more detail, give the **key finding** in 1–2 sentences and end with "More detail available in full chat."
- Prefer speed and directness over completeness. The user can open the full chat for deeper exploration.
- Set **is_complete=true** as soon as you have a reasonable answer — do not run extra rounds for polish.
"""
    elif mode == "copilot":
        mode_block = f"""
CHAT MODE: **copilot** (fewer reasoning rounds: {max_iterations})

Quality bar for this mode:
- The user can follow up quickly. A **reasonable, practical** answer grounded in tool results is enough — do not chase perfection.
- When the evidence clearly supports the gist of the answer, you may set **is_complete=true** with confidence **medium** or **high** as appropriate; **low** only if you must hedge and say what is uncertain.
- Prefer finishing in fewer rounds when the question is answered well enough for a coordinator to act or ask a targeted follow-up.
"""
    else:
        mode_block = f"""
CHAT MODE: **agentic** (more reasoning rounds: {max_iterations})

Quality bar for this mode:
- Aim for **higher precision and confidence** than in copilot. Use the extra rounds to **verify**, narrow queries, or combine tools until the answer is **specific and well-supported**.
- Before **is_complete=true**, resolve avoidable ambiguity (e.g. another targeted tool call) when the user asked for definitive facts, numbers, policy detail, or roster/registry accuracy.
- Use **confidence: "high"** only when tool evidence backs it; otherwise **medium** with explicit limits, or **low** with clear caveats — avoid vague reassurance.
"""
    return f"""
You are Mobius — an AI assistant for CMHC billing coordinators in Florida.
You do NOT answer questions directly. You decide which tool to use.
{mode_block}
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
3. ICD-10, diagnosis/procedure codes, CPT, HCPCS, Medicare/Medicaid coverage (NCD/LCD), "what does code … mean": use healthcare_query as the FIRST tool — NOT search_corpus first, NOT healthcare_npi_lookup.
4. NPI number only (no PML, no code/coverage question): use healthcare_npi_lookup or healthcare_query for NPPES registry facts.
5. **lookup_npi** when the user wants **NPI(s) for an organization by name**: e.g. "NPI for Acme", "find the NPIs for Aspire Health",
    "list billing NPIs for …", "look up NPI for org …". Use **inputs.org_name** from the message (organization name only when possible).
5b. Practice **locations** / **sites** / **service addresses** for billing org(s): use **find_org_locations**.
    Supply **org_npis** (array) and/or **org_npi** and/or **org_name**. If the user says "these NPIs" after lookup_npi,
    pass **org_npis** from the message (10-digit numbers) or omit and let the tool parse digits from the thread context.
5c. **Who practices / who is at this site / providers at each location** for a **billing org** (operational roster): use **find_associated_providers_at_locations**.
    Same inputs as find_org_locations. This is **Step 4** (claims + registry address signals ± roster upload) — **not** a clinical schedule.
    If the user only wants addresses without providers, use **find_org_locations** instead.
6. refuse for PHI (specific patient data) and clinical guidance only.
7. If corpus returns good content → is_complete=true, synthesize answer.
8. If corpus misses → use google_search next iteration.
8b. **web_scrape**: pass **scrape_mode** in inputs — **quick** (one page, default), **medium** (≤3 depth, 6 pages), **detailed** (≤5 depth, 50 pages, ≤10 doc downloads). Use **quick** unless the question needs a broader crawl or many linked documents.
9. Max {max_iterations} reasoning rounds — if still no answer, escalate honestly.
9b. **Credentialing / NPPES tools** often include a **Summary** in the tool trace plus long **Result** markdown. If Success is true and the Summary answers the user, set **is_complete=true** immediately — do **not** call the same tool again in a new round.
10. If a tool result shows success (e.g. "Report stored", "Step 11 done", "report generated", "You can ask any question about it") → set is_complete=true and answer MUST confirm that the report or output was generated successfully. Do NOT say "I cannot generate" when the tool already succeeded.
11. When "Recent conversation" is present: treat the prior assistant reply as the current answer. If the user is asking for something that answer did NOT provide (e.g. a link, URL, specific page, more detail, a number), the answer is INSUFFICIENT — do NOT set is_complete=true. Call a tool (e.g. google_search or web_scrape for links/URLs, search_corpus for policy detail) and only set is_complete=true after you have tool results to fulfill the request.
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
    if (stage or "").startswith("react_"):
        # Reasoning rounds may return longer thoughts + final answer JSON; Flash sometimes truncated at 800.
        max_tokens = max(max_tokens, 1400)
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
                mode=getattr(ctx, "chat_mode", None),
            )
        )
        if not getattr(ctx, "usages", None):
            ctx.usages = []
        ctx.usages.append(usage)
        return (raw or "").strip()
    from app.services.llm_manager import generate_sync

    raw, _ = generate_sync(prompt, stage="planner", max_tokens=max_tokens, parser=False, mode=None)
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
        parts.append(
            "When **Summary** is present, treat it as the canonical short grounding; "
            "**Result** may be long markdown for the user — do not re-run the same tool if Summary already answers the ask."
        )
        for r in tool_results:
            raw = r.get("result") or ""
            summ = (r.get("result_summary") or "").strip()
            if summ:
                result_preview = (
                    f"[Summary for reasoning]\n{summ}\n\n"
                    f"[Full markdown length: {len(raw)} chars — included in Result; do not assume truncation means failure.]"
                )
            else:
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
    "healthcare_query": "healthcare_query",
    "healthcare_npi_lookup": "healthcare_query",
    "document_upload_skill": "document_upload",
    "list_thread_document_uploads": "document_upload",
    "find_org_locations": "find_org_locations",
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
            mode=getattr(ctx, "chat_mode", None),
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
            skill_search_mode=ctx.chat_mode,
            pipeline_ctx=ctx,
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
        # Phase 0.8 + 0.16a: hard wall-clock cap on the scrape.
        #
        # 0.8 introduced the timeout but used ``with ThreadPoolExecutor(...) as _pool``.
        # That pattern has a subtle bug: ``__exit__`` waits for the worker to
        # finish even after ``future.result(timeout=...)`` raises TimeoutError,
        # which means a scrape that exceeded the cap by N seconds STILL held
        # the tool handler for N extra seconds (one production turn overran
        # the 30s cap by 8s for this reason).
        #
        # 0.16a fix: construct the pool manually and call
        # ``shutdown(wait=False, cancel_futures=True)`` on timeout. The worker
        # thread may keep running in the background (Python has no clean way
        # to kill a thread), but our tool handler returns immediately — the
        # ReAct loop can move on, and the worker's side effects (an LLM call
        # that's already in-flight) complete or error silently.
        import concurrent.futures as _cf
        _SCRAPE_TIMEOUT_S = int(os.environ.get("MOBIUS_WEB_SCRAPE_TIMEOUT_S", "30"))

        def _run_scrape():
            return answer_tool(
                ctx.message or "",
                emitter=emitter,
                tool_hint_override="web_scrape",
                scrape_url=url,
                skill_search_mode=ctx.chat_mode,
                pipeline_ctx=ctx,
                tool_inputs=inputs,
            )

        _pool = _cf.ThreadPoolExecutor(max_workers=1)
        _future = _pool.submit(_run_scrape)
        try:
            answer, sources, usage, signal = _future.result(timeout=_SCRAPE_TIMEOUT_S)
            _pool.shutdown(wait=True)  # normal completion → clean up synchronously
        except _cf.TimeoutError:
            # Do NOT wait on the pool — let the worker keep running in the
            # background while we return immediately.
            _pool.shutdown(wait=False, cancel_futures=True)
            emit(f"  ⊘ web_scrape timed out after {_SCRAPE_TIMEOUT_S}s — moving on.")
            from app.communication.error_emit import classify_exception
            env = classify_exception(
                TimeoutError(f"web_scrape exceeded {_SCRAPE_TIMEOUT_S}s"),
                tool="web_scrape",
            )
            return {
                "tool": "web_scrape",
                "success": False,
                "result": env.user_facing_message,
                "error": env.model_dump(),
                "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
                "sources": [],
            }
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
        emit("◌ NPI lookup by organization name…")
        answer, sources, usage, signal = answer_tool(
            org or "",
            emitter=emitter,
            tool_hint_override="search_org_names",
            skill_search_mode=ctx.chat_mode,
            pipeline_ctx=ctx,
        )
        aup = (answer or "").upper()
        success = bool(
            answer
            and len((answer or "").strip()) > 15
            and (
                "NPI" in aup
                or "NPPES" in aup
                or "CANDIDATE" in aup
                or "BILLING" in aup
                or "REGISTRY" in aup
            )
        )
        if success:
            ctx.active_context = {
                "tool": "lookup_npi",
                "org": org,
                "summary": (answer or "")[:300],
                "full_output": answer or "",
                "follow_up_capable": True,
                "expires_after_turns": 5,
            }
        out = {
            "tool": "lookup_npi",
            "success": success,
            "result": answer or "",
            "signal": signal,
            "sources": sources or [],
            "usage": usage,
        }
        if success and answer:
            out = _attach_credentialing_result_summary(
                out, answer, summary_heading="**Organization / billing NPI lookup (NPPES + PML):**"
            )
        return out

    if tool == "find_org_locations":
        from app.pipeline.message_resolver import _extract_core_topic

        merged: dict = dict(inputs or {})
        if (
            not merged.get("org_name")
            and not merged.get("org_npi")
            and not merged.get("org_npis")
        ):
            topic = _extract_core_topic(ctx.effective_message or ctx.message)
            if topic:
                merged["org_name"] = topic
        emit("◌ Practice locations (credentialing Step 2)…")
        extra_out: dict = {}
        if not hasattr(ctx, "extra_out") or ctx.extra_out is None:
            ctx.extra_out = extra_out
        else:
            extra_out = ctx.extra_out
        answer, sources, usage, signal = answer_tool(
            ctx.effective_message or ctx.message or "",
            emitter=emitter,
            tool_hint_override="find_org_locations",
            user_message=ctx.effective_message or ctx.message,
            active_context=getattr(ctx, "active_context", None) or {},
            skill_search_mode=ctx.chat_mode,
            pipeline_ctx=ctx,
            tool_inputs=merged,
            extra_out=extra_out,
        )
        a = (answer or "").strip()
        success = bool(a) and len(a) > 25
        if "CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL" in a or "Practice location lookup failed" in a:
            success = False
        if success:
            ac0 = getattr(ctx, "active_context", None)
            # After a real Step 2 payload we must replace lookup_npi disambiguation context; otherwise
            # follow-ups like "find the locations" hit _answer_from_context with stale NPI-pick markdown.
            looks_like_find_locations_output = "# practice locations" in (answer or "").lower()
            keep_lookup_npi_disambiguation_only = (
                isinstance(ac0, dict)
                and ac0.get("tool") == "lookup_npi"
                and ac0.get("follow_up_capable")
                and not looks_like_find_locations_output
            )
            if not keep_lookup_npi_disambiguation_only:
                ctx.active_context = {
                    "tool": "find_org_locations",
                    "org": str(merged.get("org_name") or ""),
                    "summary": (answer or "")[:500],
                    "follow_up_capable": True,
                    "expires_after_turns": 8,
                    "full_output": answer,
                }
                # Prior tool(s) in this turn may have attached NPI/org chips; drop them once we have sites.
                ctx.pending_workflow_selection = []
        rsum = ""
        if isinstance(extra_out, dict):
            rsum = (extra_out.pop(REACT_TOOL_SUMMARY_KEY, None) or "").strip()
        out = {
            "tool": "find_org_locations",
            "success": success,
            "result": answer or "",
            "signal": signal,
            "sources": sources or [],
            "usage": usage,
        }
        if rsum:
            out["result_summary"] = rsum
        return out

    if tool == "find_associated_providers_at_locations":
        from app.pipeline.message_resolver import _extract_core_topic

        merged = dict(inputs or {})
        if (
            not merged.get("org_name")
            and not merged.get("org_npi")
            and not merged.get("org_npis")
        ):
            topic = _extract_core_topic(ctx.effective_message or ctx.message)
            if topic:
                merged["org_name"] = topic
        emit("◌ Providers per practice site (credentialing Step 4)…")
        extra_out_a: dict = {}
        if not hasattr(ctx, "extra_out") or ctx.extra_out is None:
            ctx.extra_out = extra_out_a
        else:
            extra_out_a = ctx.extra_out
        answer, sources, usage, signal = answer_tool(
            ctx.effective_message or ctx.message or "",
            emitter=emitter,
            tool_hint_override="find_associated_providers_at_locations",
            user_message=ctx.effective_message or ctx.message,
            active_context=getattr(ctx, "active_context", None) or {},
            skill_search_mode=ctx.chat_mode,
            pipeline_ctx=ctx,
            tool_inputs=merged,
            extra_out=extra_out_a,
        )
        a = (answer or "").strip()
        success = bool(a) and len(a) > 25
        if (
            "CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL" in a
            or "Practice location lookup failed" in a
            or "Find-associated-providers failed" in a
        ):
            success = False
        if success:
            ctx.active_context = {
                "tool": "find_associated_providers_at_locations",
                "org": str(merged.get("org_name") or ""),
                "summary": (answer or "")[:500],
                "follow_up_capable": True,
                "expires_after_turns": 8,
                "full_output": answer,
            }
        rsum = (extra_out_a.pop(REACT_TOOL_SUMMARY_KEY, None) or "").strip()
        out = {
            "tool": "find_associated_providers_at_locations",
            "success": success,
            "result": answer or "",
            "signal": signal,
            "sources": sources or [],
            "usage": usage,
        }
        if rsum:
            out["result_summary"] = rsum
        return out

    if tool == "run_credentialing_report":
        from app.pipeline.message_resolver import _extract_core_topic
        from app.services.credentialing_run_service import create_credentialing_run

        co = getattr(ctx, "credentialing_options", None) or {}
        org = (
            (co.get("org_name") or "").strip()
            or (inputs.get("org_name") or "").strip()
            or _extract_core_topic(ctx.effective_message or ctx.message)
        )
        extra_out = {}
        if not hasattr(ctx, "extra_out") or ctx.extra_out is None:
            ctx.extra_out = extra_out
        else:
            extra_out = ctx.extra_out

        mode = (co.get("mode") or inputs.get("mode") or "autopilot").strip().lower()
        if mode not in ("autopilot", "copilot"):
            mode = "autopilot"
        cred_opts_for_tool = dict(co) if co else None

        if _envelope_routes_to_reconciliation(ctx):
            emit("◌ Roster on this chat — running roster reconciliation (upload vs external data)…")
            return _execute_tool(
                "run_roster_reconciliation_report",
                {"org_name": org or "", "upload_id": "", "org_id": ""},
                ctx,
                emitter,
            )

        if mode == "copilot":
            emit("◌ Starting credentialing co-pilot (step-by-step validation)…")
            try:
                run = create_credentialing_run(
                    org or "",
                    "copilot",
                    thread_id=(ctx.thread_id or "").strip() or None,
                    emitter=emit,
                    credentialing_options=cred_opts_for_tool,
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
                "gate_events": run.get("gate_events"),
                "last_gate_event": run.get("last_gate_event"),
                "credentialing_prerequisites": run.get("credentialing_prerequisites"),
                "workflow_follow_ups_by_step": run.get("workflow_follow_ups_by_step"),
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
            out_c = {
                "tool": "run_credentialing_report",
                "success": True,
                "result": answer,
                "signal": RETRIEVAL_SIGNAL_GOOGLE_ONLY,
                "sources": [],
            }
            return _attach_credentialing_result_summary(
                out_c, answer, summary_heading="**Credentialing co-pilot:**"
            )

        emit("◌ Running credentialing report (this may take a minute)…")
        extra_out["credentialing_copilot_clear"] = True
        answer, sources, usage, signal = answer_tool(
            org or "",
            emitter=emitter,
            tool_hint_override="roster_report",
            user_message=ctx.message,
            extra_out=extra_out,
            thread_id=(ctx.thread_id or "").strip() or None,
            credentialing_options=cred_opts_for_tool,
            skill_search_mode=ctx.chat_mode,
            pipeline_ctx=ctx,
        )
        # Prefer retrieval signal over length — cached/short reports can be <200 chars of prose.
        success = bool(
            answer
            and answer.strip()
            and (
                signal == RETRIEVAL_SIGNAL_ROSTER_COMPLETE
                or len(answer.strip()) > 200
            )
        )
        if success:
            ctx.active_context = {
                "tool": "run_credentialing_report",
                "org": org,
                "summary": (answer or "")[:500],
                "follow_up_capable": True,
                "expires_after_turns": 10,
                "full_output": answer,
            }
        out_r = {
            "tool": "run_credentialing_report",
            "success": success,
            "result": answer or "",
            "signal": signal,
            "sources": sources or [],
            "usage": usage,
        }
        if success and answer:
            out_r = _attach_credentialing_result_summary(
                out_r, answer, summary_heading="**Credentialing report:**"
            )
        return out_r

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
            "credentialing_assertion_sync": run.get("credentialing_assertion_sync"),
            "gate_events": run.get("gate_events"),
            "last_gate_event": run.get("last_gate_event"),
            "credentialing_prerequisites": run.get("credentialing_prerequisites"),
            "workflow_follow_ups_by_step": run.get("workflow_follow_ups_by_step"),
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
        out_v = {
            "tool": "validate_credentialing_step",
            "success": True,
            "result": answer,
            "signal": RETRIEVAL_SIGNAL_GOOGLE_ONLY,
            "sources": [],
        }
        return _attach_credentialing_result_summary(
            out_v, answer, summary_heading="**Credentialing co-pilot (step advanced):**"
        )

    if tool == "run_roster_reconciliation_report":
        org_name = inputs.get("org_name") or ""
        explicit_upload_id = (inputs.get("upload_id") or "").strip()
        upload_id = explicit_upload_id
        org_id = (inputs.get("org_id") or "").strip()
        active = (ctx.merged_state or {}).get("active") or {}
        roster_files = _roster_uploads_from_active(active)
        # Fallback to thread state (from roster upload via POST /chat/roster-upload)
        if not upload_id or not org_id:
            upload_id = (upload_id or (active.get("reconciliation_upload_id") or "").strip()).strip()
            org_id = (org_id or (active.get("reconciliation_org_id") or "").strip()).strip()
            org_name = (org_name or (active.get("reconciliation_org_name") or "").strip() or org_name).strip()
        # If still missing, use the most recent roster reconciliation upload on this thread (newest first in list)
        if (not upload_id or not org_id) and len(roster_files) >= 1:
            latest = roster_files[0]
            upload_id = (upload_id or (latest.get("upload_id") or "").strip()).strip()
            org_id = (org_id or (latest.get("org_id") or "").strip()).strip()
            org_name = (org_name or (latest.get("org_name") or "").strip() or org_name).strip()
        # Source of truth: latest resolved roster in provider skill DB for this billing NPI (not chat memory),
        # unless the model passed an explicit upload_id.
        if org_id:
            from app.services.roster_source_of_truth import resolve_reconciliation_upload_id_for_org

            tid = resolve_reconciliation_upload_id_for_org(
                org_id, explicit_upload_id=explicit_upload_id or None
            )
            if tid:
                upload_id = tid
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
                if org_id:
                    parts.append(
                        "No **resolved** roster was found in the provider database for this billing NPI "
                        "(latest processed upload for the org), and nothing is linked on this chat. "
                        "Upload and process a roster, confirm the NPI, or check that the roster service URL is configured.\n\n"
                    )
                else:
                    parts.append(
                        "No roster file is linked to **this chat** yet. Open **⋯** (next to Send) → **Upload file**, "
                        "choose **Roster for reconciliation**, wait for **Upload complete** in the banner, then send your request "
                        "(or enable **Send request after upload** in the upload dialog).\n\n"
                    )
            if org_name:
                tbl = _format_billing_npi_options_markdown(
                    org_name,
                    skill_search_mode=getattr(ctx, "chat_mode", None) or "copilot",
                )
                if tbl:
                    parts.append(tbl)
            elif not roster_files:
                parts.append("Also tell me the **organization name** (e.g. David Lawrence Center) so we can list matching billing NPIs.")
            result = "\n".join(parts).strip() or (
                "Roster reconciliation needs an organization name, a billing NPI (org_id), and a resolved roster "
                "in the provider database (or a chat-linked upload). Use ⋯ → Upload file if you need to refresh data."
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
            skill_search_mode=ctx.chat_mode,
            pipeline_ctx=ctx,
        )
        success = bool(
            answer
            and answer.strip()
            and (
                signal == RETRIEVAL_SIGNAL_ROSTER_COMPLETE
                or len(answer.strip()) > 100
            )
        )
        if success:
            ctx.active_context = {
                "tool": "run_roster_reconciliation_report",
                "org": org_name,
                "summary": (answer or "")[:500],
                "follow_up_capable": True,
                "expires_after_turns": 5,
            }
        out_rec = {
            "tool": "run_roster_reconciliation_report",
            "success": success,
            "result": answer or "",
            "signal": signal,
            "sources": sources or [],
            "usage": usage,
        }
        if success and answer:
            out_rec = _attach_credentialing_result_summary(
                out_rec, answer, summary_heading="**Roster reconciliation report:**"
            )
        return out_rec

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
            skill_search_mode=ctx.chat_mode,
            pipeline_ctx=ctx,
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
        out_q = {
            "tool": "ask_credentialing_npi",
            "success": success,
            "result": result_text,
            "signal": signal,
            "sources": sources or [],
            "usage": usage,
        }
        if success and result_text:
            out_q = _attach_credentialing_result_summary(
                out_q, result_text, summary_heading="**Credentialing report Q&A (NPI / PML):**"
            )
        return out_q

    if tool == "healthcare_query":
        # ICD-10, CMS coverage, NPI-by-number — same MCP backend as legacy healthcare_npi_lookup.
        question = inputs.get("question") or (ctx.effective_message or ctx.message)
        emit("◌ Healthcare database (ICD-10, coverage, NPI)…")
        answer, sources, usage, signal = answer_tool(
            question or "",
            emitter=emitter,
            tool_hint_override="healthcare_query",
            user_message=ctx.message,
            active_context=active,
            skill_search_mode=ctx.chat_mode,
            pipeline_ctx=ctx,
        )
        success = bool(answer and len(answer.strip()) > 50 and "Error:" not in (answer or ""))
        out_h = {
            "tool": "healthcare_query",
            "success": success,
            "result": answer or "",
            "signal": signal,
            "sources": sources or [],
            "usage": usage,
        }
        if success and answer:
            out_h = _attach_credentialing_result_summary(
                out_h, answer, summary_heading="**Healthcare lookup (codes / NPPES / coverage):**"
            )
        return out_h

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
            skill_search_mode=ctx.chat_mode,
            pipeline_ctx=ctx,
        )
        success = bool(answer and len(answer.strip()) > 50 and "Error:" not in (answer or ""))
        out_n = {
            "tool": "healthcare_npi_lookup",
            "success": success,
            "result": answer or "",
            "signal": signal,
            "sources": sources or [],
            "usage": usage,
        }
        if success and answer:
            out_n = _attach_credentialing_result_summary(
                out_n, answer, summary_heading="**NPPES / registry (by NPI number):**"
            )
        return out_n

    # ── Task manager tools ────────────────────────────────────────────────────
    if tool in ("list_tasks", "create_task", "resolve_task"):
        import os as _os
        import httpx as _httpx

        _task_base = (
            _os.environ.get("CHAT_SKILLS_TASK_MANAGER_URL") or "http://localhost:8015"
        ).rstrip("/")

        emit(f"◌ Task manager: {tool}…")

        try:
            if not _task_base:
                raise ValueError("CHAT_SKILLS_TASK_MANAGER_URL not configured")

            with _httpx.Client(timeout=10.0) as _c:
                if tool == "list_tasks":
                    _params = {k: v for k, v in {
                        "org_name": inputs.get("org") or inputs.get("org_name"),
                        "module": inputs.get("module"),
                        "status": inputs.get("status"),
                        "assignee": inputs.get("assignee"),
                        "npi": inputs.get("npi"),
                        "run_id": inputs.get("run_id"),
                        "limit": inputs.get("limit", 50),
                    }.items() if v is not None}
                    _r = _c.get(f"{_task_base}/tasks", params=_params)
                    _r.raise_for_status()
                    _data = _r.json()
                    tasks = _data.get("tasks") or []
                    count = _data.get("count", len(tasks))
                    if tasks:
                        lines = [f"**{count} task(s) found**\n"]
                        for t in tasks[:20]:
                            sev = (t.get("severity") or "").upper()
                            st = t.get("status", "open")
                            prov = t.get("provider_name") or t.get("npi") or ""
                            prov_str = f" — {prov}" if prov else ""
                            lines.append(f"- [{sev}] {t.get('text', '')} ({st}){prov_str} `{t.get('task_id','')[:8]}`")
                        result_text = "\n".join(lines)
                    else:
                        result_text = "No tasks found matching the given filters."
                    # Attach raw tasks to context for envelope rendering
                    ctx.react_task_list_data = {"tasks": tasks, "filters": _params}
                    return {
                        "tool": "list_tasks",
                        "success": True,
                        "result": result_text,
                        "signal": "corpus_only",
                        "sources": [],
                    }

                elif tool == "create_task":
                    _body = {
                        "org_name": inputs.get("org") or inputs.get("org_name") or "",
                        "text": inputs.get("text") or inputs.get("description") or "",
                        "source_module": inputs.get("module") or "manual",
                        "severity": inputs.get("severity") or "low",
                        "provider_name": inputs.get("provider_name"),
                        "npi": inputs.get("npi"),
                    }
                    _r = _c.post(f"{_task_base}/tasks", json=_body)
                    _r.raise_for_status()
                    created = _r.json()
                    ctx.react_task_list_data = {"tasks": [created], "filters": {}, "allow_create": False}
                    return {
                        "tool": "create_task",
                        "success": True,
                        "result": f"Task created: **{created.get('text','')}** (ID: `{str(created.get('task_id',''))[:8]}`, severity: {created.get('severity','low')})",
                        "signal": "corpus_only",
                        "sources": [],
                    }

                elif tool == "resolve_task":
                    _tid = inputs.get("task_id") or ""
                    if not _tid:
                        return {"tool": "resolve_task", "success": False, "result": "task_id is required", "signal": RETRIEVAL_SIGNAL_NO_SOURCES, "sources": []}
                    _body = {"resolved_by": "chat", "note": inputs.get("note")}
                    _r = _c.post(f"{_task_base}/tasks/{_tid}/resolve", json=_body)
                    _r.raise_for_status()
                    return {
                        "tool": "resolve_task",
                        "success": True,
                        "result": f"Task `{_tid[:8]}` marked as resolved.",
                        "signal": "corpus_only",
                        "sources": [],
                    }

        except Exception as _te:
            return {
                "tool": tool,
                "success": False,
                "result": f"Task manager error: {_te}",
                "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
                "sources": [],
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
    _att_kind = (extra.get("roster_report_attachments_kind") or "").strip().lower()
    if _att_kind in ("reconciliation", "credentialing"):
        ctx.roster_report_attachments_kind = _att_kind
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


def _dedupe_sources(sources: list) -> list:
    """Phase 0.8 / 0.11: collapse near-duplicate source entries before rendering
    and renumber surviving ``index`` fields so the UI shows consecutive citations.

    Before Phase 0.11 the dedup worked correctly, but the surviving sources
    kept their pre-dedup ``index`` values (set upstream in non_patient_rag.py
    when iterating chunks). So when dedup collapsed 1,073 raw chunks down to
    139 unique (doc, page) pairs, the UI still rendered ``[1] [2] [3] [5] [7]
    [10] …`` with confusing gaps. This pass renumbers the survivors so the
    rendered list starts at ``[1]`` and increments by 1.

    Fallback dedup key order (first one that exists wins):
        1. (document_id, page_number)  — RAG / corpus citations
        2. (url, page_number)          — web scrape results
        3. (title, page_number)        — fallback for loose formats
        4. str(source)                 — last resort for opaque items
    """
    if not sources:
        return []
    seen: set = set()
    out: list = []
    for s in sources:
        if isinstance(s, dict):
            doc_id = s.get("document_id") or s.get("doc_id")
            url = s.get("url") or s.get("href")
            title = s.get("title") or s.get("label")
            page = s.get("page_number") or s.get("page")
            if doc_id is not None:
                key = ("doc", str(doc_id), page)
            elif url is not None:
                key = ("url", str(url), page)
            elif title is not None:
                key = ("title", str(title), page)
            else:
                # Opaque dict — fall back to full-content hash via repr.
                key = ("repr", repr(sorted(s.items())))
        else:
            key = ("repr", str(s))
        if key in seen:
            continue
        seen.add(key)
        out.append(s)

    # Phase 0.11: renumber the ``index`` field so the FE shows [1][2][3]… with
    # no gaps. Non-dict entries and dicts without an existing index are left
    # untouched (they never render a bracket number anyway).
    for i, s in enumerate(out, start=1):
        if isinstance(s, dict) and "index" in s:
            s["index"] = i
    return out


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
    # Phase 0.8: dedupe sources by (document_id, page_number) so the citation
    # list doesn't explode when multiple rounds cite the same document.
    ctx.sources = _dedupe_sources(all_sources) if all_sources else []
    ctx.retrieval_signals = [final_signal] if final_signal else [RETRIEVAL_SIGNAL_NO_SOURCES]
    # Quick mode: flag long answers so the mini container shows "Full answer →" link
    if react_chat_mode_label(getattr(ctx, "chat_mode", None)) == "quick":
        ctx.quick_truncated = len(final_answer) > QUICK_MODE_TRUNCATED_CHARS
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


# Phase 0.13: cap on auto-retry sleep so a stale retry_after_seconds from a
# provider can't stall the whole turn. 30s is tight enough to preserve UX and
# wide enough to cover typical rate-limit windows.
_MAX_AUTO_RETRY_SLEEP_S = 30


def _execute_tool_with_retry(
    tool: str,
    inputs: dict,
    ctx: PipelineContext,
    round_num: int,
    emit_fn,
    tool_emitter,
) -> dict:
    """Run ``_execute_tool`` with a single auto-retry on recoverable errors.

    Phase 0.13: closes the loop on the ErrorEnvelope contract from Phase 0.6a.
    ``is_recoverable`` is set on rate_limit / timeout / provider_error /
    scrape_failed. When we get one of these we sleep ``retry_after_seconds``
    (capped) and re-run the same call once. If the retry also fails, the
    failed result is returned as-is — the retry guard will record it and
    subsequent rounds will pick a different tool per Phase 0.7.

    Args:
        emit_fn: adds the reasoning-round "  " prefix; used for retry-status
            lines that belong to the ReAct loop, not the tool.
        tool_emitter: unprefixed emitter passed through to ``_execute_tool``
            so the tool's own emits look the same as before this phase.

    Rules:
    - Max 1 retry per call (no spirals).
    - Sleep bounded by ``_MAX_AUTO_RETRY_SLEEP_S``.
    - Non-recoverable codes (refusal, auth_error, context_too_long,
      validation_error, internal_error) return immediately.
    - Raised exceptions are classified via ``tool_result_from_exception``.
    """
    from app.communication.error_emit import tool_result_from_exception

    def _run_once() -> dict:
        try:
            return _execute_tool(tool, inputs, ctx, tool_emitter)
        except Exception as exc:
            r = tool_result_from_exception(exc, tool=tool, round=round_num)
            emit_fn(f"  ⊘ {r['result']}")
            return r

    result = _run_once()

    err = result.get("error") if isinstance(result, dict) else None
    if not (isinstance(err, dict) and err.get("schema_name") == "error_envelope"):
        return result

    # Only these error_codes auto-retry. Mirrors ErrorEnvelope.is_recoverable.
    if err.get("error_code") not in {
        "rate_limit",
        "timeout",
        "provider_error",
        "scrape_failed",
    }:
        return result

    retry_after = err.get("retry_after_seconds")
    try:
        wait_s = int(retry_after) if retry_after is not None else 3
    except (TypeError, ValueError):
        wait_s = 3
    wait_s = max(1, min(_MAX_AUTO_RETRY_SLEEP_S, wait_s))

    emit_fn(
        f"  ↻ {tool} hit {err.get('error_code')} — retrying in {wait_s}s…"
    )
    import time as _time
    _time.sleep(wait_s)
    retry_result = _run_once()
    # Whether or not the retry succeeded, attach a marker so telemetry can
    # distinguish auto-retried turns from clean first-try turns.
    if isinstance(retry_result, dict):
        retry_result["auto_retried"] = True
    return retry_result


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

    mode_label = react_chat_mode_label(getattr(ctx, "chat_mode", None))
    max_it = react_max_iterations_for_mode(getattr(ctx, "chat_mode", None))
    emit("I'm breaking down your question and choosing the right source…")
    emit(
        f"  (Up to {max_it} reasoning rounds — {mode_label}: "
        f"{'more tool passes when needed' if mode_label == 'agentic' else 'faster path; you can steer on the next message'}.)"
    )
    tool_results: list[dict] = []
    all_sources: list[dict] = []
    final_signal = RETRIEVAL_SIGNAL_NO_SOURCES
    last_tool: str | None = None
    reasoning_system = _react_reasoning_system(max_it, mode_label)

    # Phase 0.7: smart-retry guard — tracks failed attempts so we don't repeat
    # the same (tool, inputs) when no new evidence has come in, and enables
    # fail-fast when every round errors.
    from app.pipeline.react_retry_guard import ReactRetryGuard
    retry_guard = ReactRetryGuard()

    for iteration in range(max_it):
        rn = iteration + 1
        headline = _react_round_headline(iteration, max_it)
        emit(f"  Round {rn}/{max_it} — {headline}")
        emit(f"  Reasoning round {rn}/{max_it}…")
        reasoning_context = build_reasoning_context(ctx, tool_results, rn)
        # Inject already-failed attempts into the prompt so the LLM sees
        # them and picks differently.
        hint = retry_guard.failure_hint_for_prompt()
        if hint:
            reasoning_context = f"{reasoning_context}\n\n{hint}"
        decision_raw = _call_llm_json(
            reasoning_system,
            reasoning_context,
            ctx=ctx,
            stage=f"react_{rn}",
        )

        decision = _parse_react_decision_json(decision_raw)
        if decision is None:
            preview = (decision_raw or "")[:320].replace("\n", " ")
            logger.warning("ReAct parse failure (stage=%s): %s", f"react_{rn}", preview)
            emit("  Could not parse model decision — stopping.")
            # Do not throw away a good tool result (common with Gemini after a large Step 2 payload).
            if tool_results:
                last_tr = tool_results[-1]
                last_res = (last_tr.get("result") or "").strip()
                last_sum = (last_tr.get("result_summary") or "").strip()
                usable = last_res if len(last_res) >= 40 else last_sum
                if usable and (len(usable) >= 40 or (last_sum and last_tr.get("success"))):
                    emit("  Using the last tool output as the answer.")
                    lt_sig = final_signal
                    if last_tr.get("success"):
                        body = last_res
                        if last_sum and last_res and len(last_res) > len(last_sum) + 80:
                            body = compose_mobius_tool_envelope(last_sum, last_res)
                        _finalize_response(ctx, body, all_sources, lt_sig, last_tr.get("tool") or last_tool, emitter)
                    else:
                        # Short failures (e.g. "No URL") still beat a generic escalate.
                        _finalize_response(
                            ctx,
                            last_res or last_sum,
                            all_sources,
                            RETRIEVAL_SIGNAL_NO_SOURCES,
                            last_tr.get("tool") or last_tool,
                            emitter,
                        )
                    return
            if iteration == 0:
                fb = _react_fallback_org_npi_lookup_decision(ctx)
                if fb:
                    emit("  Recovered: routing to lookup_npi for organization name.")
                    decision = fb
            if decision is None:
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

        # Phase 0.7: block repeat call if (tool, inputs) already failed and
        # no new evidence has come in since.
        blocked_by = retry_guard.should_block(
            tool=tool or "search_corpus",
            inputs=inputs,
            current_results_count=len(tool_results),
        )
        if blocked_by is not None:
            emit(
                f"  ⊘ Already tried {blocked_by.tool} with these inputs "
                f"(round {blocked_by.round}, {blocked_by.error_code or 'failed'}) "
                f"— picking a different path."
            )
            # Record a synthetic result so the LLM sees we acknowledged the skip
            # and won't re-pick the same thing next round.
            tool_results.append({
                "tool": tool or "search_corpus",
                "success": False,
                "result": "(skipped — previously failed with no new evidence since)",
            })
            continue

        emit(f"  Using {tool or 'unknown'}…")
        if (tool or "").strip().lower() == "run_credentialing_report":
            emit("  (The report runs its own steps below — org, locations, providers, PML, opportunity, etc.)")
        if (tool or "").strip().lower() == "find_org_locations":
            emit("  (Calls credentialing POST /find-locations — NPPES, PML, DOGE; agentic may add web.)")
        if (tool or "").strip().lower() == "find_associated_providers_at_locations":
            emit("  (POST /find-locations then /find-associated-providers — operational roster per site.)")
        results_before = len(tool_results)
        # Phase 0.7 + 0.13: convert raised exceptions into a typed failed-tool
        # result AND auto-retry recoverable errors once, honoring the
        # retry_after_seconds hint on the classifier envelope. One retry per
        # call keeps the blast radius small; if it still fails, the retry
        # guard + fail-fast machinery take over.
        result = _execute_tool_with_retry(
            tool or "search_corpus", inputs, ctx, rn, emit, emitter
        )
        last_tool = result.get("tool")
        _append_tool_llm_usage(ctx, str(last_tool or tool or ""), result)
        retry_guard.record_result(
            tool=last_tool or tool or "search_corpus",
            inputs=inputs,
            result=result,
            round=rn,
            results_count_before=results_before,
        )

        tr_entry: dict[str, Any] = {
            "tool": last_tool,
            "success": result.get("success", False),
            "result": result.get("result", ""),
        }
        rsum_t = (result.get("result_summary") or "").strip()
        if rsum_t:
            tr_entry["result_summary"] = rsum_t
        tool_results.append(tr_entry)

        # Phase 0.8: do NOT emit sources from failed tool runs. When an LLM
        # step inside a retrieval tool fails (e.g. corpus search's LLM call
        # hits a rate limit AFTER the retriever already pulled hundreds of
        # chunks), the raw chunks were being attached to all_sources, landing
        # up to 1_000+ near-duplicate citations in the final answer card.
        if result.get("sources") and not (
            result.get("success") is False or result.get("error") is not None
        ):
            all_sources.extend(result["sources"])
        if result.get("signal") and result["signal"] != RETRIEVAL_SIGNAL_NO_SOURCES:
            final_signal = result["signal"]

        # Full roster report returned — finish without waiting for another reasoning round to
        # emit is_complete (otherwise we exhaust iterations and show a generic "no verified answer").
        _term_sig = result.get("signal")
        _term_text = (result.get("result") or "").strip()
        if (
            _term_sig == RETRIEVAL_SIGNAL_ROSTER_COMPLETE
            and _term_text
            and (last_tool or "")
            in ("run_roster_reconciliation_report", "run_credentialing_report")
        ):
            emit("  Synthesizing answer from report…")
            _finalize_response(ctx, _term_text, all_sources, _term_sig, last_tool, emitter)
            return

        if result.get("is_terminal"):
            emit("  Stopping (refuse).")
            _finalize_response(ctx, "", [], RETRIEVAL_SIGNAL_NO_SOURCES, last_tool, emitter)
            return

        # Credentialing / NPPES tools: summary + full markdown — finish immediately so ReAct does not burn rounds.
        if (
            last_tool in _CREDENTIALING_DUAL_FINALIZE_TOOLS
            and result.get("success")
            and (result.get("result_summary") or "").strip()
            and (result.get("result") or "").strip()
        ):
            rs = (result.get("result_summary") or "").strip()
            rm = (result.get("result") or "").strip()
            combined = compose_mobius_tool_envelope(rs, rm)
            emit("  Finishing: credentialing tool returned summary + full markdown.")
            _finalize_response(ctx, combined, all_sources, final_signal, last_tool, emitter)
            return

    # Exhausted iterations
    if tool_results:
        last_tr = tool_results[-1]
        if last_tr.get("success") and (last_tr.get("result_summary") or "").strip() and (last_tr.get("result") or "").strip():
            rs = (last_tr.get("result_summary") or "").strip()
            rm = (last_tr.get("result") or "").strip()
            emit("  Using last credentialing tool summary + full markdown after max rounds.")
            _finalize_response(
                ctx,
                compose_mobius_tool_envelope(rs, rm),
                all_sources,
                final_signal,
                last_tr.get("tool") or last_tool,
                emitter,
            )
            return
    # Phase 0.7: if every round failed and nothing succeeded, emit a clean
    # typed refusal instead of the generic "no verified answer" string —
    # avoids pretending we looked everywhere when the pipeline was broken.
    if retry_guard.all_rounds_failed(rounds_completed=max_it):
        emit("  ⊘ All reasoning rounds errored — stopping before burning more tokens.")
        # Use the most-common error code from the failed attempts for the message.
        codes = [fa.error_code for fa in retry_guard.failed_attempts if fa.error_code]
        dominant = max(set(codes), key=codes.count) if codes else "internal_error"
        user_msg_by_code = {
            "rate_limit":      "The models are temporarily busy. Please try again in a minute.",
            "token_budget":    "Your question needs a larger-context model that's not currently available.",
            "context_too_long":"This conversation is too long for the available models — start a new chat.",
            "auth_error":      "A service is mis-configured. The team has been notified.",
            "scrape_failed":   "I couldn't reach the external sources I needed for this answer.",
            "timeout":         "Requests kept timing out. Please try again in a moment.",
            "provider_error":  "The model services had trouble — please try again shortly.",
        }
        refusal = user_msg_by_code.get(
            dominant,
            "Every attempt to answer this hit an error. Please try again or rephrase.",
        )
        _finalize_response(ctx, refusal, all_sources, RETRIEVAL_SIGNAL_NO_SOURCES, last_tool, emitter)
        return

    emit("  No verified answer after checking materials and web — escalating honestly.")
    honest = (
        "I wasn't able to find a verified answer to this question "
        "after checking our materials and searching the web. "
        "You may want to contact the payer directly or provide a link to their documentation."
    )
    _finalize_response(ctx, honest, all_sources, RETRIEVAL_SIGNAL_NO_SOURCES, last_tool, emitter)
