"""ReAct prompt + reasoning-context helpers (Phase 1i pass 1).

Extracted from react_loop.py to isolate the text-generation surface
from the tool-dispatch surface. Everything in this module is either:

  - A mode-label / max-rounds constant the planner reads to decide how
    much leeway to give the reasoner.
  - A prompt template builder (``_react_reasoning_system``).
  - The per-round reasoning context builder
    (``build_reasoning_context``) that feeds the planner.
  - One LLM call wrapper ``_call_llm_json`` used by run_react to emit
    and collect the decision JSON.

The Phase 1i split (2026-04-18) moves these out of a 2,459-LOC monolith
so future prompt edits don't require opening the dispatcher. Dispatcher
stays put for now (Phase 1i pass 2) because _execute_tool's internal
cross-references are too dense to split safely in one pass.
"""

from __future__ import annotations

import asyncio
import logging

from app.pipeline.context import PipelineContext
# Import the module (not the symbol) so each _react_reasoning_system()
# call reads the current manifest. Importing ``TOOL_MANIFEST`` directly
# would snapshot it at prompts-module import time and miss MCP tools
# registered later during FastAPI startup. See
# ``app.pipeline.tool_manifest.get_tool_manifest`` for the contract.
from app.pipeline import tool_manifest as _tool_manifest_module
from app.communication.plan_display import jurisdiction_summary

logger = logging.getLogger(__name__)


# ── Mode constants ────────────────────────────────────────────────────────

REACT_MAX_ROUNDS_COPILOT = 3
REACT_MAX_ROUNDS_AGENTIC = 10  # 2026-04-24: bumped 6→10 for complex multi-hop
                               # questions. Paired with MOBIUS_TURN_DEADLINE_S=240
                               # in deploy/dev.env (was 180) so long agentic turns
                               # don't deadline-out mid-reasoning.
REACT_MAX_ROUNDS_QUICK   = 2   # mini-container: fail-fast, brief answer

# Answers longer than this in quick mode signal that the user should
# follow up in full chat.
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
    """Quick: 2 rounds (mini container). Copilot: 3. Agentic: 10."""
    label = react_chat_mode_label(chat_mode)
    if label == "agentic":
        return REACT_MAX_ROUNDS_AGENTIC
    if label == "quick":
        return REACT_MAX_ROUNDS_QUICK
    return REACT_MAX_ROUNDS_COPILOT


def guidance_mode_threshold(max_it: int) -> int:
    """First ROUND (1-indexed) at which guidance mode activates.

    The 80/20 split: rounds 1..guidance_threshold-1 are "hunt for the
    authoritative answer"; rounds guidance_threshold..max_it are
    "synthesize next-best guidance from what we've already found."

    Ceiling, so quick (2) → 2, copilot (3) → 3, agentic (6) → 5. All
    three give the planner at least one dedicated guidance round; on
    the longer modes it also has a round to revise if the critic
    rejects the guidance.
    """
    if max_it <= 2:
        return max_it  # quick mode: last round is guidance round
    return max(2, -(-max_it * 4 // 5))  # ceil(0.8 * max_it), never below 2


def is_guidance_round(iteration: int, max_it: int) -> bool:
    """True when the 0-indexed iteration falls in the guidance band.

    The loop's ``rn`` is 1-indexed, but we key on the 0-indexed
    iteration because that's what ``build_reasoning_context`` and
    ``_react_round_headline`` both get.
    """
    return (iteration + 1) >= guidance_mode_threshold(max_it)


def _react_round_headline(iteration: int, max_it: int) -> str:
    """User-facing headline for this round index (0-based), depends on total rounds.

    Guidance rounds get a distinct label that precedes the per-iteration
    defaults — so the user sees that the planner has shifted from
    searching to synthesis, regardless of where in the mode-specific
    numbering that happens.
    """
    # Round 0 is always Scoping — even in quick mode (which has only 2
    # rounds total). The first round is where the planner makes its
    # initial tool choice; guidance mode never overrides round 0.
    if iteration == 0:
        return "Scoping — interpret the question and choose the first tool or answer"

    # Guidance-mode label takes precedence over per-iteration defaults.
    # Without this ordering, quick mode's round 2 would render
    # "Grounding" even though the planner has shifted to guidance mode.
    if is_guidance_round(iteration, max_it):
        if iteration >= max_it - 1:
            return "Guidance — synthesize best next-step advice from what's been gathered"
        return "Guidance — shifting from search to synthesis"

    if iteration == 1:
        return "Grounding — use evidence from prior tool results"
    if iteration >= max_it - 1:
        return "Finalize — answer or escalate honestly"
    if iteration == 2:
        return "Refinement — close gaps or gather missing details"
    if iteration == 3:
        return "Extended — alternate tools or queries if needed"
    return "Extended — narrow or verify before answering"


def _react_guidance_instruction(iteration: int, max_it: int) -> str:
    """Return the guidance-mode instruction to inject into the reasoning
    context, or an empty string if this round isn't a guidance round.

    Why this exists. On information-gathering questions with no
    definitive corpus answer, the planner historically burns all
    rounds searching and lets the ReAct loop fall out via rounds-
    exhaustion — producing a generic "I couldn't confirm" message that
    ignores all the evidence it did collect. That's a bad UX: the user
    asked a question, the system found relevant context, and the
    response is "sorry, nothing." Users are better served by: "Here's
    what I found; based on that, your best next step is X. The
    specific Y was not in the sources I could access."

    The 80/20 split the operator wants:

      - Rounds 1 .. ceil(0.8 * max_it) - 1 : hunt for the authoritative
        answer (normal ReAct).
      - Rounds ceil(0.8 * max_it) .. max_it: shift to synthesis-from-
        evidence. Draft a hedged answer that extracts concrete
        next-step guidance from what's already been gathered.

    The critic remains the safety net. In guidance mode the planner
    is explicitly encouraged to synthesize from partial evidence,
    which is fertile ground for hallucination. The critic audits the
    resulting draft against the retrieved sources and rejects
    anything that isn't grounded — forcing a revise round if one is
    available.

    What this does NOT do: permit fabrication. The instruction
    explicitly warns that "you should contact X at <number>" is only
    safe if <number> came from a source. Unsupported phone numbers,
    invented rule citations, and unsubstantiated modal assertions
    ("X is required") are still hallucinations and the critic will
    still flag them.
    """
    if not is_guidance_round(iteration, max_it):
        return ""

    rounds_remaining = max_it - iteration  # includes this round

    return (
        "## GUIDANCE MODE ACTIVATED\n"
        f"You are now on round {iteration + 1} of {max_it}. "
        f"{rounds_remaining} round(s) remain.\n"
        "\n"
        "Shift strategy: **stop hunting for the perfect authoritative "
        "source**. The sources you have already retrieved are what you "
        "have to work with. Your job now is to produce the most useful "
        "possible answer for the user, given that evidence.\n"
        "\n"
        "Preferred action this round:\n"
        "  Set ``is_complete: true`` with an answer that:\n"
        "    1. States plainly what was found in the sources (with "
        "citations).\n"
        "    2. Acknowledges what was NOT found (\"the specific X was "
        "not available in the sources I could access\").\n"
        "    3. Gives concrete next-step guidance based on what WAS "
        "found (\"based on <source>, you should try X\" or \"the "
        "<provider portal> is the authoritative source — check it for "
        "the specific Y\").\n"
        "\n"
        "HARD RULES (a grounding critic will audit your answer):\n"
        "  - Do NOT invent facts. If no source contains a specific "
        "phone number, do NOT state one — say \"contact provider "
        "services\" without making up a number.\n"
        "  - Do NOT assert definitive requirements (\"X is required\", "
        "\"Y must be done\") unless a retrieved source establishes "
        "them. Hedge if uncertain: \"the typical requirement is...\" or "
        "\"this usually involves...\".\n"
        "  - Do NOT extrapolate from training-data knowledge. Only use "
        "what the retrieved sources show.\n"
        "\n"
        "A useful hedged answer grounded in partial evidence is MUCH "
        "better than \"I couldn't confirm\". The user asked a question; "
        "if you have partial evidence, coach them on what to do with "
        "it. The critic will flag anything ungrounded and you can "
        "revise on the next round if one remains."
    )


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
{_tool_manifest_module.get_tool_manifest()}

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


# ── LLM call wrapper ──────────────────────────────────────────────────────


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


# ── Reasoning-context builder ─────────────────────────────────────────────


def build_reasoning_context(
    ctx: PipelineContext,
    tool_results: list[dict],
    iteration: int,
    max_iterations: int | None = None,
) -> str:
    """Build the context the model reasons over each iteration.

    ``max_iterations`` is optional so legacy tests that call this with
    three positional args keep working. When supplied, it enables the
    guidance-mode instruction on the appropriate rounds (see
    :func:`_react_guidance_instruction`). Legacy callers that pass
    None silently skip the guidance pathway — identical to
    pre-guidance-mode behavior.
    """
    parts = []

    # Guidance mode gets prepended so it's the first thing the planner
    # reads each round during the 80/20 synthesis phase. The rest of
    # the context (jurisdiction, uploads, turns, tool results) follows
    # unchanged. An empty string from the helper means "not a guidance
    # round" and no change is made.
    #
    # Note: ``iteration`` here is actually 1-indexed (the caller passes
    # ``rn`` which is round number 1..max_it). The guidance helpers
    # internally use 0-indexed so convert at this boundary — the
    # _react_round_headline caller uses 0-indexed directly, so the
    # offset only applies here.
    if max_iterations is not None:
        guidance = _react_guidance_instruction(iteration - 1, max_iterations)
        if guidance:
            parts.append(guidance)

    active = (ctx.merged_state or {}).get("active") or {}
    j = jurisdiction_summary(active)
    if j:
        parts.append(f"Active jurisdiction: {j}")

    # Phase B.1 — surface thread-scoped uploads so the planner knows to
    # prefer search_uploaded_document when the user's question is self-
    # referential ("this document", "the PDF I uploaded", "my file").
    #
    # Without this block, the planner is blind to active.uploaded_files[]
    # and defaults to search_corpus, which silently misses because instant-
    # RAG chunks don't have the tag metadata corpus-wide search filters on.
    #
    # 2026-04-17: a user uploaded a provider manual, asked "what is in
    # this document", and got "I was unable to find information about the
    # document" because the planner never knew it was there.
    _uploads = [
        u for u in (active.get("uploaded_files") or [])
        if isinstance(u, dict)
        and (u.get("purpose") == "instant_rag")
        and u.get("document_id")
    ]
    if _uploads:
        upload_lines = ["Documents attached to this thread (searchable via search_uploaded_document):"]
        for u in _uploads[:10]:  # cap — a thread with >10 uploads is rare and the first 10 are enough context
            fname = str(u.get("filename") or "upload")
            uid = str(u.get("upload_id") or "")
            chunks = u.get("row_count") or u.get("chunks_count") or 0
            chunks_s = f", {chunks} chunks" if chunks else ""
            upload_lines.append(f"  - {fname} (upload_id={uid}{chunks_s})")
        upload_lines.append(
            "When the user's question refers to an attached document ('this document', "
            "'the PDF', 'my upload', 'what does it say'), call search_uploaded_document "
            "BEFORE search_corpus. search_corpus does not find these user uploads."
        )
        parts.append("\n".join(upload_lines))

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
