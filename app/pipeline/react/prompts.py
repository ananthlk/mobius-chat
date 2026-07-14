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
REACT_MAX_ROUNDS_TASK    = 3   # task mode: same cap as copilot; skips integrator

# Answers longer than this in quick mode signal that the user should
# follow up in full chat.
QUICK_MODE_TRUNCATED_CHARS = 500


def react_chat_mode_label(chat_mode: str | None) -> str:
    """Normalized ReAct mode for prompts and UI: copilot (default), agentic, quick, or task."""
    m = (chat_mode or "").strip().lower()
    if m == "agentic":
        return "agentic"
    if m == "quick":
        return "quick"
    if m == "task":
        return "task"
    return "copilot"


def react_max_iterations_for_mode(chat_mode: str | None) -> int:
    """Quick: 2 rounds (mini container). Copilot/Task: 3. Agentic: 10."""
    label = react_chat_mode_label(chat_mode)
    if label == "agentic":
        return REACT_MAX_ROUNDS_AGENTIC
    if label == "quick":
        return REACT_MAX_ROUNDS_QUICK
    if label == "task":
        return REACT_MAX_ROUNDS_TASK
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

    # Iteration labels are positional UI markers only — they tell the
    # user "where in the budget we are." Operational guidance about
    # WHEN to switch tools, WHEN to escalate, etc. lives in the tool
    # descriptions (_SEARCH_CORPUS_BLOCK, _RECALL_SEARCH_BLOCK,
    # _PRECISION_SEARCH_BLOCK) so the LLM reads it on every tool-choice
    # decision regardless of mode (copilot 3-round vs deep 5+round).
    # Putting operational content here was a 2026-05-01 footgun: the
    # iter==2 "switch tool" branch never fired in copilot mode because
    # `iteration >= max_it - 1` matched first at iter=2 when max_it=3.
    if iteration == 1:
        return "Grounding — use evidence from prior tool results"
    if iteration >= max_it - 1:
        return "Finalize — answer with what you have or escalate honestly. Do not start a new search direction."
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
        "  - ATTRIBUTION: when a tool result contains a specific fact "
        "(number, date, code, limit), read the SOURCE PAYER carefully "
        "before using it. A 180-day limit found in a Sunshine Health "
        "document is NOT the same as one found in an Aetna or Molina "
        "document — only cite it for the payer it actually belongs to. "
        "If the source payer matches the user's question payer, state "
        "the fact directly; if it does NOT match, do not apply it.\n"
        "\n"
        "A useful hedged answer grounded in partial evidence is MUCH "
        "better than \"I couldn't confirm\". The user asked a question; "
        "if you have partial evidence, coach them on what to do with "
        "it. The critic will flag anything ungrounded and you can "
        "revise on the next round if one remains.\n"
        "\n"
        "OUTPUT FORMAT — THIS IS MANDATORY:\n"
        "Return a JSON object with is_complete=true. Put the full answer "
        "text inside the \"answer\" field. Do NOT write prose outside the "
        "JSON. Your entire response must start with `{` and end with `}`.\n"
        "Example:\n"
        "{\n"
        "  \"thought\": \"Synthesising from retrieved sources.\",\n"
        "  \"tool\": null,\n"
        "  \"inputs\": {},\n"
        "  \"is_complete\": true,\n"
        "  \"answer\": \"<your complete answer here>\",\n"
        "  \"confidence\": \"medium\"\n"
        "}"
    )


def _react_reasoning_system(
    max_iterations: int,
    chat_mode: str,
    user_profile: dict | None = None,
    allowed_tools: list[str] | None = None,
) -> str:
    """Build reasoning system prompt; chat_mode is 'copilot', 'agentic', 'quick', or 'task'.

    ``user_profile`` is the mobius-user profile dict (see
    Mobius-user/CONSUMER_RECIPE_PROFILE.md). When present, its
    ``rendered_prompt`` is appended to the system prompt so the
    planner / ReAct reasoner picks tools and frames intermediate
    thinking in the user's preferred voice + autonomy style. Default
    None for the un-onboarded case + the worker-prewarm caller in
    main.py (which doesn't have a real ctx).

    ``allowed_tools`` is ``ctx.allowed_tools`` resolved by the orchestrator:
        None  — no filter (all tools visible).
        []    — no tools available; use context-only system prompt.
        [..] — filtered manifest rendered from this list.
    """
    mode = (chat_mode or "copilot").strip().lower()

    # No-tools path: either task mode OR ctx.allowed_tools == [].
    # Unify here so the prompt is identical regardless of why tools are absent.
    _no_tools = (mode == "task") or (allowed_tools is not None and len(allowed_tools) == 0)
    if _no_tools:
        return (
            "You are a precise assistant operating in task mode.\n\n"
            "Rules — follow ALL of them without exception:\n"
            "1. You MUST NOT call any tools. Tool calls are disabled in this mode.\n"
            "2. You MUST set is_complete=true and provide your best answer on this "
            "single response — do NOT return is_complete=false or an empty answer.\n"
            "3. Use the SYSTEM CONTEXT block as your primary source. If the context "
            "is partial, give the best answer you can from what is provided; do not "
            "refuse or ask for more information.\n"
            "4. The 'answer' field must be non-empty — fill in whatever is "
            "inferable or relevant from the context.\n\n"
            "Output ONLY valid JSON (no preamble, no explanation outside the JSON):\n"
            "{\n"
            '  "thought": "<one sentence summarising the context>",\n'
            '  "tool": null,\n'
            '  "inputs": {},\n'
            '  "is_complete": true,\n'
            '  "answer": "<bold bottom line + 2–4 bullets. No prose paragraphs.>",\n'
            '  "sources": [],\n'
            '  "confidence": "high"\n'
            "}"
        )

    if mode not in ("agentic", "quick"):
        mode = "copilot"
    if mode == "quick":
        mode_block = f"""
CHAT MODE: **quick** (mini-container, max {max_iterations} rounds — fail fast)

Quality bar for this mode:
- Use **at most 1 tool call** unless the first call returns nothing useful. Start with **search_corpus**.
- Follow FORMAT RULES for the answer field: bold bottom line + 2–3 bullets max. No paragraphs.
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
    _base_prompt_text = f"""
You are Mobius — an AI assistant for CMHC billing coordinators in Florida.
You do NOT answer questions directly. You decide which tool to use.
{mode_block}
{_tool_manifest_module.get_tool_manifest(allowed=allowed_tools)}

Your response each round MUST be a single JSON object — nothing before `{{`, nothing after `}}`.
Two valid shapes:

Tool call (need more evidence):
{{
  "thought": "<why you chose this tool — one sentence>",
  "tool": "<tool name from manifest>",
  "inputs": {{<tool-specific inputs>}},
  "is_complete": false
}}

Final answer (have enough evidence to answer now):
{{
  "thought": "<what you found>",
  "tool": null,
  "inputs": {{}},
  "is_complete": true,
  "answer": "<structured answer — see FORMAT RULES below>",
  "sources": [],
  "confidence": "high"
}}

FORMAT RULES for the "answer" field (always follow):
• Start with ONE bold sentence that gives the direct bottom line: **The answer here.**
• Follow with 2–4 short bullet points (each 10–25 words) with key supporting details.
• If a concrete next action is supported by evidence (deadline, email, phone, portal URL), add: → Next step: [action]
• Use **bold** for entity names, deadlines, codes, and contact info so they scan quickly.
• Do NOT write paragraphs. Do NOT repeat the question. Do NOT hedge vaguely.
• If the answer is genuinely unknown after searching, say so in ONE sentence and give the best next step (e.g. which phone number to call).

NEVER write prose outside the JSON. If you have the answer, put it formatted per the rules above in the "answer" field.
Prose (even correct prose) breaks the pipeline — use JSON every time.

CRITICAL RULES:
1. **rag FIRST** for any policy/process/overview question. rag is the ONLY retrieval tool — it handles corpus, payor registry facts (EDI, phone, portal, timely filing), and web sources internally. Do NOT call separate search tools.
1b. After rag returns results, synthesize what you have. If the first result is weak, call rag ONCE more with a BETTER QUERY (sharper noun, different phrasing). Do NOT call rag a third time with a paraphrased version of the same query — rephrasing is paraphrase-invariant. Surface the gap honestly instead.
1c. **Overview / assembled answers are valid** — do NOT abstain because you can't confirm a COMPLETE list. If rag returned real content about X (e.g. networks, services, programs), present what was found and note it may not be exhaustive. "Here is what I found: [assembled content]. There may be additional services not covered in the available documents." is the correct response. Demoting assembled content to "I wasn't able to find a verified answer" is wrong.
2. NPI + PML (e.g. "Is NPI X set up for PML?"): try ask_credentialing_npi FIRST. If it fails (no report), try healthcare_npi_lookup for NPPES info.
3. ICD-10, diagnosis/procedure codes, CPT, HCPCS, Medicare/Medicaid coverage (NCD/LCD), "what does code … mean": use healthcare_query as the FIRST tool — NOT rag.
3b. Mobius product identity — "why the name Mobius", "what does Mobius mean", "who is Mobius", "what is Mobius", "what can Mobius do", "how does Mobius work", "tell me about Mobius", "what does the name mean", or any what/why/who/how question where the subject is Mobius itself: use **product_help_search** as the FIRST tool — NOT rag. Mobius is our own product; the authoritative answer is in the product knowledge base, not the internet.
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
7. If rag returns good content → is_complete=true, synthesize answer. Assembled/partial content IS a good answer — see rule 1c.
8b. **web_scrape**: pass **scrape_mode** in inputs — **quick** (one page, default), **medium** (≤3 depth, 6 pages), **detailed** (≤5 depth, 50 pages, ≤10 doc downloads). Use **quick** unless the question needs a broader crawl or many linked documents.
9. Max {max_iterations} reasoning rounds — if still no answer, escalate honestly with what was found.
9b. **Credentialing / NPPES tools** often include a **Summary** in the tool trace plus long **Result** markdown. If Success is true and the Summary answers the user, set **is_complete=true** immediately — do **not** call the same tool again in a new round.
10. If a tool result shows success (e.g. "Report stored", "Step 11 done", "report generated", "You can ask any question about it") → set is_complete=true and answer MUST confirm that the report or output was generated successfully. Do NOT say "I cannot generate" when the tool already succeeded.
11. When "Recent conversation" is present: treat the prior assistant reply as the current answer. If the user is asking for something that answer did NOT provide (e.g. a link, URL, specific page, more detail, a number), the answer is INSUFFICIENT — do NOT set is_complete=true. Call rag or web_scrape and only set is_complete=true after you have tool results to fulfill the request.
"""
    # 2026-05-06 — splice mobius-user profile (rendered_prompt) so the
    # planner / ReAct picks tools and frames intermediate thinking in
    # the user's preferred voice + autonomy style. No-op when profile
    # is None (un-onboarded).
    from app.pipeline.personalization import splice_user_profile
    return splice_user_profile(_base_prompt_text, user_profile)


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
    from app.services.llm_provider import VertexBlockedError

    if (stage or "").startswith("react_"):
        # Reasoning rounds may return longer thoughts + final answer JSON; Flash sometimes truncated at 800.
        max_tokens = max(max_tokens, 1400)
    prompt = f"{system}\n\n{user}"

    def _run(p: str) -> tuple[str, object | None]:
        if ctx is not None:
            from app.services.llm_manager import generate as llm_generate
            raw, usage = asyncio.run(
                llm_generate(
                    p,
                    stage=stage,
                    max_tokens=max_tokens,
                    config_sha=_get_config_sha(),
                    correlation_id=getattr(ctx, "correlation_id", None),
                    thread_id=getattr(ctx, "thread_id", None),
                    parser=False,
                    mode=getattr(ctx, "chat_mode", None),
                )
            )
            return (raw or "").strip(), usage
        from app.services.llm_manager import generate_sync
        raw, usage = generate_sync(prompt, stage="planner", max_tokens=max_tokens, parser=False, mode=None)
        return (raw or "").strip(), usage

    try:
        raw, usage = _run(prompt)
    except VertexBlockedError:
        # Vertex safety filter blocked the response (empty candidate). This
        # commonly happens when tool results carry dense financial tables.
        # Retry once with a condensed prompt: keep the system prompt intact
        # but truncate the user section to 1 500 chars so the model can
        # produce an answer without tripping the filter.
        logger.warning(
            "[react] vertex blocked on stage=%s — retrying with condensed prompt (cid=%s)",
            stage,
            getattr(ctx, "correlation_id", "?")[:8] if ctx else "?",
        )
        condensed_user = user[:1500] + ("\n\n[Context condensed to avoid processing limits. Answer from what is available above.]" if len(user) > 1500 else "")
        condensed_prompt = f"{system}\n\n{condensed_user}"
        raw, usage = _run(condensed_prompt)

    if ctx is not None and usage is not None:
        if not getattr(ctx, "usages", None):
            ctx.usages = []
        ctx.usages.append(usage)
    return raw


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
    # No-tools path: task mode OR ctx.allowed_tools == [].
    # Skip all tool guidance (strategy arms, upload hints, jurisdiction, etc.)
    # — they actively instruct the LLM to call tools, overriding the no-tools
    # system prompt. Instead give only the system_context and the question.
    _allowed_tools = getattr(ctx, "allowed_tools", None)
    _is_no_tools = (
        react_chat_mode_label(getattr(ctx, "chat_mode", None)) == "task"
        or (_allowed_tools is not None and len(_allowed_tools) == 0)
    )
    if _is_no_tools:
        sys_ctx = (getattr(ctx, "system_context", None) or "").strip()
        question = (getattr(ctx, "effective_message", None) or ctx.message or "").strip()
        parts = []
        if sys_ctx:
            parts.append(f"SYSTEM CONTEXT (use this as your only source):\n{sys_ctx}")
        if tool_results:
            # Include prior tool output on subsequent rounds (shouldn't normally
            # happen in no-tools mode, but be safe rather than drop evidence).
            for tr in tool_results:
                res_text = (tr.get("result") or "").strip()
                if res_text:
                    parts.append(f"Context:\n{res_text}")
        parts.append(f"User question: {question}")
        parts.append(
            "---\n"
            "RESPOND IN JSON: { \"thought\": \"...\", \"tool\": null, \"inputs\": {}, "
            "\"is_complete\": true, \"answer\": \"your full answer here\", \"confidence\": \"high\" }\n"
            "Do not write prose. The \"answer\" field must contain your complete response."
        )
        return "\n\n".join(parts)

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
            chunks_s = f", {chunks} chunks indexed" if chunks else " (indexing…)"
            upload_lines.append(f"  - {fname} (upload_id={uid}{chunks_s})")
        upload_lines.append(
            "When the user's question refers to an attached document ('this document', "
            "'the PDF', 'my upload', 'what does it say'), call search_uploaded_document "
            "BEFORE search_corpus. search_corpus does not find these user uploads.\n"
            "QUERY GUIDANCE for uploaded documents:\n"
            "  • Summarise / overview / 'what is in this': use the filename or apparent topic as the "
            "query (e.g. 'provider billing timely filing policy overview'), NOT 'summarize this document' "
            "— the query is a semantic search, not a command.\n"
            "  • Specific question: use the exact terms from the question.\n"
            "  • If search returns 0 chunks and the result says 'still being indexed': "
            "tell the user to wait 10–20 seconds and try again. Do NOT retry immediately with the same query.\n"
            "  • If search returns 0 chunks but the document IS indexed (result says 'N chunks indexed'): "
            "retry with a different, broader query before escalating."
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
        # Phase 13.6 + 2026-04-28 follow-up-latency fix.
        #
        # The original Phase-13.6 logic always inlined the most-recent
        # assistant answer at 3000 chars (~750 tokens) so the planner
        # could reshape it for transform queries ("convert this to an
        # appeal letter", "make it shorter", "rewrite for X"). That
        # 3000-char dump went into EVERY ReAct round of EVERY follow-up,
        # plus into critic and consolidator — accounting for ~3-5k of
        # the prompt-size growth between turn 1 (23k) and turn 2 (26k)
        # observed in latency traces, and ~7x of the LLM elapsed time.
        #
        # The integrator already produces a compact rolling summary
        # (``ctx.previous_thread_summary``, ~600 chars) which is exactly
        # what we want for substantive follow-ups — we just weren't
        # using it. So the new policy is:
        #
        #   - Transform-intent follow-up:  keep the 3000-char raw dump
        #                                  (transform_previous_answer
        #                                   needs the full text).
        #   - Substantive follow-up:       use previous_thread_summary
        #                                  + a short head of the prior
        #                                  answer for pronoun grounding.
        #
        # Detection is keyword-based — same flavor as the existing
        # planner instruction. Keep deliberately permissive: a false
        # positive costs ~750 tokens; a false negative breaks transform.
        msg_lower = (
            getattr(ctx, "effective_message", None) or ctx.message or ""
        ).lower()
        _TRANSFORM_TRIGGERS = (
            # transformation verbs
            "rewrite", "rephrase", "reword", "shorten", "lengthen",
            "expand", "summarize", "condense", "tighten", "polish",
            "convert to", "convert it", "convert this", "turn it into",
            "turn this into", "make it ", "make this ",
            # artifact requests built off prior substance
            "appeal letter", "denial letter", "memo", "email", "draft",
            "letter for", "letter to",
            # pronouns referring to prior content as material
            "the above", "the previous", "the prior",
        )
        is_transform = any(t in msg_lower for t in _TRANSFORM_TRIGGERS)

        if is_transform:
            MOST_RECENT_PREVIEW = 3000
            OLDER_PREVIEW = 200
            preamble = (
                "Recent conversation (the FIRST 'Assistant:' below is the "
                "MOST RECENT answer — treat it as available source material. "
                "The user's message looks like a transformation/continuation "
                "('rewrite', 'shorten', 'convert to an appeal letter', "
                "'the above'). Call `transform_previous_answer` — do NOT "
                "re-run search_corpus or other retrieval tools.):\n"
            )
        else:
            # Compact form for substantive follow-ups. A short head of the
            # most-recent answer is still helpful for pronoun resolution
            # ("what does that mean for...", "for that payer...") but the
            # full body is not — that's what previous_thread_summary is for.
            MOST_RECENT_PREVIEW = 400
            OLDER_PREVIEW = 120
            preamble = (
                "Recent conversation (compact preview — for full prior "
                "substance see the rolling thread summary above; for raw "
                "transformation source the user must signal a transform "
                "intent):\n"
            )

        turns_text = []
        ordered = list(ctx.last_turns or [])[:3]
        for idx, turn in enumerate(ordered):
            user_q = turn.get("user_content") or turn.get("message") or ""
            assistant_full = turn.get("assistant_content") or ""
            preview_budget = MOST_RECENT_PREVIEW if idx == 0 else OLDER_PREVIEW
            assistant_a = assistant_full[:preview_budget]
            ellipsis = "..." if len(assistant_full) > preview_budget else ""
            if user_q:
                turns_text.append(f"User: {user_q}")
                turns_text.append(f"Assistant: {assistant_a}{ellipsis}")
        if turns_text:
            parts.append(preamble + "\n".join(turns_text))

    # Inject the integrator-produced rolling summary on follow-up turns.
    # This is the cheap, condensed form of conversation history — does the
    # job for substantive follow-ups without paying the full-prior-answer
    # tax. Capped to 600 chars (matches the integrator's own truncate).
    _prev_summary = (getattr(ctx, "previous_thread_summary", None) or "").strip()
    if _prev_summary:
        parts.append(
            "Rolling thread summary (from prior turns — use this as the "
            "primary continuity signal; do NOT re-summarize):\n"
            + _prev_summary[:600]
        )

    # ── 5-arm strategy bandit state ──────────────────────────────────────
    # Expose which retrieval/answer strategies have been tried this turn
    # so the planner can pick from only the remaining arms.
    #   a) precision   — BM25 exact-match (corpus)
    #   b) recall      — vector semantic (corpus)
    #   c) hybrid      — BM25 ⊕ vector RRF (corpus)
    #   d) google      — external web search
    #   e) llm_direct  — answer from model knowledge (implicit: set is_complete=true)
    _ALL_ARMS = ["precision", "recall", "hybrid", "google", "llm_direct"]
    _arms_tried_ctx: set[str] = getattr(ctx, "_strategy_arms_tried", set())
    if _arms_tried_ctx:
        _remaining = [a for a in _ALL_ARMS if a not in _arms_tried_ctx]
        _tried_str = ", ".join(_arms_tried_ctx) if _arms_tried_ctx else "none"
        _remaining_str = ", ".join(_remaining) if _remaining else "NONE — set is_complete=true"
        parts.append(
            f"Strategy arms tried this turn: {_tried_str}\n"
            f"Strategy arms still available: {_remaining_str}\n"
            "Do NOT repeat an arm that has already been tried. "
            "If no corpus arms (precision/recall/hybrid) remain, call google_search. "
            "If google is also tried, answer from model knowledge (llm_direct = is_complete=true with caveats)."
        )

    # ── feedback cadence signal (docs/feedback-agent-spec.md §4B/§6) ─────────
    # Injected only when a periodic ask is *eligible* (the ceiling is computed
    # in code). The planner decides whether the moment is right and, on its
    # final step, may set offer_feedback — it must NOT let this delay or replace
    # answering the user's actual question.
    _fb = getattr(ctx, "feedback_signal", None)
    if isinstance(_fb, dict):
        _kind = _fb.get("kind") or "generic"
        if _kind == "nps":
            _how = ('ask IN YOUR REPLY, in one sentence, how likely they are to recommend Mobius '
                    'to a colleague on a 0–10 scale (and set offer_feedback {"kind":"nps"}). '
                    'When the user answers with a number, call product_feedback with kind=survey, '
                    'survey_type=nps, score=<their number>')
        elif _kind == "csat":
            _how = ('ask IN YOUR REPLY a quick "how did that go? (1–5)" (and set offer_feedback '
                    '{"kind":"csat"}). When the user answers with a number, call product_feedback '
                    'with kind=survey, survey_type=csat, score=<their number>')
        elif _kind == "targeted_miss":
            _how = 'the last answer may have missed — you may ask what they expected via offer_feedback {"kind":"targeted_miss"}'
        else:
            _how = 'you may invite open feedback via offer_feedback {"kind":"generic"}'
        parts.append(
            f"FEEDBACK SIGNAL: a feedback ask is due ({_fb.get('reason', 'cadence')}). "
            f"AFTER you have fully answered the user's request, if the moment is right "
            f"(they are not mid-task or frustrated), {_how} on your final is_complete step. "
            f"Skip it silently if the moment is wrong. Never let this change your answer."
        )

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

    # ── JSON enforcement footer ───────────────────────────────────────────
    # Placed LAST so it is the final instruction the model reads before
    # generating.  Gemini Flash in particular tends to slip into prose at
    # mid-hunt rounds (3-5) when accumulated tool results fill the context
    # and it "decides" to answer directly.  Without a closing reminder the
    # JSON constraint from the system prompt gets overridden by that
    # context-heavy prose impulse.
    #
    # Two valid formats (repeat from system prompt here for proximity):
    #   • Tool call:    { "thought": "...", "tool": "...", "inputs": {...},   "is_complete": false }
    #   • Final answer: { "thought": "...", "tool": null,  "inputs": {},      "is_complete": true,
    #                     "answer": "...", "confidence": "high"|"medium"|"low" }
    #
    # If you have enough evidence to answer the user's question RIGHT NOW,
    # use the final-answer format with is_complete=true — do NOT write prose.
    # Prose responses (even well-reasoned ones) cannot be parsed.
    parts.append(
        "---\n"
        "RESPOND IN JSON — your entire response must be a single JSON object "
        "starting with `{` and ending with `}`. No text before `{`, no text after `}`.\n"
        "  • Need another tool → set is_complete=false with the tool name and inputs.\n"
        "  • Have the answer  → set is_complete=true, tool=null, and put the full "
        "answer in the \"answer\" field.\n"
        "Prose responses cannot be parsed and will be discarded."
    )

    return "\n\n".join(parts)
