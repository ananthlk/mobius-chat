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
import re
from dataclasses import dataclass

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


def react_agent_role(iteration: int, max_it: int) -> str:
    """Deterministic, round-position-only phase label: 'explore' | 'synthesize' | 'draft'.

    Phase A (2026-07-29): drives ONLY composition selection for the v2 block path
    (react_explore/react_synthesize/react_draft — identical content, addressing/
    attribution infrastructure only; see docs/REACT_PHASE_A_IMPLEMENTATION_PLAN.md
    §2). Does NOT drive temperature or role-flavored content in this phase — that's
    Phase B (docs/REACT_AGENT_ROLE_DERIVATION_DRAFT.md §2, signed).

    ``iteration`` is 0-indexed (matches ``is_guidance_round``'s convention). "Draft"
    uses "last possible round in the budget" as the deterministic prospective proxy
    for the spec's "completing round" — which round actually completes is only
    known AFTER the model responds, too late to pick a composition/temperature for
    that round (see the derivation doc §1.1 for the full reasoning).
    """
    if iteration == 0:
        return "explore"
    if iteration >= max_it - 1:
        return "draft"
    return "synthesize"


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


# ── System-prompt block bodies (Phase A decomposition — docs/
#    REACT_PHASE_A_IMPLEMENTATION_PLAN.md §3). Jinja source (matches the
#    BlockAssembler's own `{{ var }}` convention, autoescape=False,
#    keep_trailing_newline=True) so ONE literal string renders identically
#    via the legacy join below AND via app/services/react_block_seed.py's
#    DB-seeded blocks — the two paths can't drift apart because they share
#    the same source text and the same renderer. ──────────────────────────

_REACT_JINJA_ENV = None  # lazy singleton — see _react_jinja_env()


def _react_jinja_env():
    global _REACT_JINJA_ENV
    if _REACT_JINJA_ENV is None:
        import jinja2

        _REACT_JINJA_ENV = jinja2.Environment(autoescape=False, keep_trailing_newline=True)
    return _REACT_JINJA_ENV


REACT_NO_TOOLS_PROMPT = (
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

REACT_MODE_BLOCK_QUICK = """
CHAT MODE: **quick** (mini-container, max {{ max_iterations }} rounds — fail fast)

Quality bar for this mode:
- Use **at most 1 tool call** unless the first call returns nothing useful. Start with **search_corpus**.
- Follow FORMAT RULES for the answer field: bold bottom line + 2–3 bullets max. No paragraphs.
- Set **is_complete=true** as soon as you have a reasonable answer — do not run extra rounds for polish.
"""

REACT_MODE_BLOCK_COPILOT = """
CHAT MODE: **copilot** (fewer reasoning rounds: {{ max_iterations }})

Quality bar for this mode:
- The user can follow up quickly. A **reasonable, practical** answer grounded in tool results is enough — do not chase perfection.
- When the evidence clearly supports the gist of the answer, you may set **is_complete=true** with confidence **medium** or **high** as appropriate; **low** only if you must hedge and say what is uncertain.
- Prefer finishing in fewer rounds when the question is answered well enough for a coordinator to act or ask a targeted follow-up.
- **USER PREFERENCES (appended at end) govern length.** If they specify brevity, pager-length, or concise output: HARD CAP ~15 words total. One verdict sentence, drop all elaboration, no bullets, no next-step. A 10-word answer is a complete answer for brevity-preferring users. Do NOT expand to explain or qualify.
"""

REACT_MODE_BLOCK_AGENTIC = """
CHAT MODE: **agentic** (more reasoning rounds: {{ max_iterations }})

Quality bar for this mode:
- Aim for **higher precision and confidence** than in copilot. Use the extra rounds to **verify**, narrow queries, or combine tools until the answer is **specific and well-supported**.
- Before **is_complete=true**, resolve avoidable ambiguity (e.g. another targeted tool call) when the user asked for definitive facts, numbers, policy detail, or roster/registry accuracy.
- Use **confidence: "high"** only when tool evidence backs it; otherwise **medium** with explicit limits, or **low** with clear caveats — avoid vague reassurance.
"""

_REACT_MODE_BLOCK_TEMPLATES = {
    "quick": REACT_MODE_BLOCK_QUICK,
    "copilot": REACT_MODE_BLOCK_COPILOT,
    "agentic": REACT_MODE_BLOCK_AGENTIC,
}

REACT_IDENTITY_TEXT = (
    "You are Mobius — an AI assistant for CMHC billing coordinators in Florida.\n"
    "You do NOT answer questions directly. You decide which tool to use."
)

REACT_RESPONSE_SHAPE_TEXT = """Your response each round MUST be a single JSON object — nothing before `{`, nothing after `}`.
Two valid shapes:

Tool call (need more evidence) — include "evidence_review" whenever this is NOT your first
round (i.e. earlier tool results are present in context above):
{
  "thought": "<why you chose this tool — one sentence>",
  "evidence_review": {
    "keep": [<chunk numbers from the LAST tool result's [N] headers that actually matter to this question>],
    "running_answer": "<the best answer you can build from kept evidence so far — even if partial. **Bold** the key fact.>",
    "gaps_closed": [<specific gaps THIS round's tool result resolved — empty array if none>],
    "gaps_open": [<specific gaps still unresolved — empty array if none>]
  },
  "tool": "<tool name from manifest>",
  "inputs": {<tool-specific inputs>},
  "is_complete": false
}

Final answer (have enough evidence to answer now) — include "evidence_review" whenever this is
NOT your first round, same as the tool-call shape. This is the MOST important round to include it
on: it's the record of which chunks actually grounded the answer you're about to give.
{
  "thought": "<what you found>",
  "evidence_review": {
    "keep": [<chunk numbers from the LAST tool result's [N] headers that actually grounded this answer>],
    "running_answer": "<same as your final answer's substance — this is what confirms you recomputed confidence from the kept evidence, not vibes. **Bold** the key fact.>",
    "gaps_closed": [<what THIS round's evidence resolved — empty array if none>],
    "gaps_open": [<anything still uncertain even though you're answering — empty array if none>]
  },
  "tool": null,
  "inputs": {},
  "is_complete": true,
  "answer": "<structured answer — see FORMAT RULES below>",
  "sources": [],
  "confidence": "high"
}"""

REACT_FORMAT_RULES_TEXT = """FORMAT RULES for the "answer" field (structural defaults — USER PREFERENCES appended at the end of this prompt take FINAL AUTHORITY, including over length):
• Start with ONE bold sentence that gives the direct bottom line: **The answer here.**
• Follow with 2–4 short bullet points (each 10–25 words) with key supporting details.
• If a concrete next action is supported by evidence (deadline, email, phone, portal URL), add: → Next step: [action]
• Use **bold** for entity names, deadlines, codes, and contact info so they scan quickly.
• Do NOT write paragraphs. Do NOT repeat the question. Do NOT hedge vaguely.
• If the answer is genuinely unknown after searching, say so in ONE sentence and give the best next step (e.g. which phone number to call).
• USER PREFERENCES (appended below, if any) specify voice, tone, structure, AND LENGTH. When they conflict
  with these defaults — e.g. "terse/pager" means verdict-only fragments without bullets, "friendly"
  means a warm human opener before the bold line, "concise/brief" means the total answer must be short
  regardless of completeness defaults — the USER PREFERENCES WIN and OVERRIDE these bullet guidelines.
  LENGTH CONSTRAINTS FROM USER PREFERENCES TAKE PRECEDENCE OVER THOROUGHNESS DEFAULTS.

NEVER write prose outside the JSON. If you have the answer, put it formatted per the rules above in the "answer" field.
Prose (even correct prose) breaks the pipeline — use JSON every time."""

REACT_CRITICAL_RULES_TEXT = """CRITICAL RULES:
1. **rag FIRST** for any policy/process/overview question. rag is the ONLY retrieval tool — it handles corpus, payor registry facts (EDI, phone, portal, timely filing), and web sources internally. Do NOT call separate search tools.
1b. **Retry protocol — relax, then reframe. Never blind-retry.** rag already runs its own internal strategy escalation (BM25/vector/web/fact-store) INSIDE one call — calling it again with the same or cosmetically-reworded query changes nothing server-side and gains zero new information. Every rag tool result carries a "RAG signal:" line (status, citable_required, chunks), and the [Evidence Ledger] block above shows gap_status for this exact pattern — read both before deciding your next move:
    - **First call weak/empty, citable_required=True**: your next rag call, same conceptual question, will automatically run with citable_required relaxed — you don't set this yourself, just call rag again with the same query. This re-opens non-citable sources (web/general) to learn the correct terminology or which section actually covers it. Do not treat this relaxed call as your answer — it's for learning.
    - **Result was the RELAXED call**: your next rag call MUST use a query that is MATERIALLY DIFFERENT — built from what the relaxed call actually taught you (the real term, code, or policy section), not a reworded version of the original phrasing. This is your one reframe.
    - **Result was the REFRAMED call and still empty**: this is a genuine gap, not a phrasing problem. STOP calling rag on this question — go to SHAPE 2 below.
    - **citable_required was never True and the result is weak**: you get ONE reframe with a materially different query (same materiality bar as above), then stop.
    - **Materiality gate for any reframe**: before re-asking, ask — does this query change the actual matched terms, or is it a cosmetic reword that will hit the same BM25/vector results? If cosmetic, don't re-fire.
    - **observer_final_reason overrides materiality when present**: when the [Evidence Ledger] shows an observer_final_reason indicating a structural/capacity limit (e.g. "...filled_to_capacity"), that's RAG's own agent telling you the search genuinely maxed out its candidates — a materially different query won't change that. Skip the reframe and go to SHAPE 2, even if gap_status alone still reads "progressing."
    - **Hard limit, enforced**: {{ rag_call_ceiling }} rag calls per question, no more. The pipeline itself refuses a call past this and returns a terminal signal — don't rely on remembering this, but don't try it either.
1c. **Show your reasoning, not just your move.** Starting round 2, every "thought" must read as three explicit beats, in order:
    (1) LEARNED — what the last tool result actually told you: cite the real signal (status, chunk count, whether it was citability-gated) — never "still gathering information" or other content-free filler.
    (2) RESTRATEGIZE — what concretely changes about your next move because of that (relax, reframe with new terms, switch tool, or stop) — not "try again" alone.
    (3) NEXT — which tool you're calling now and why it's the right one given (1) and (2).
    If your thought could be pasted unedited onto a different round of a different question, you have not actually reasoned from the result — rewrite it.
1c-2. **LEARNED must actually fill the answer, not just narrate.** The "evidence_review" object (required alongside "thought" from round 2 on, in BOTH the tool-call shape and the final-answer shape, see REACT_RESPONSE_SHAPE) is where LEARNED becomes concrete instead of prose:
    - **keep**: read every numbered chunk in the last tool result — all of it, nothing is truncated — and list which chunk numbers actually bear on this question. Don't skim the first chunk and stop; the answer is as likely to be chunk 6 of 12 as chunk 1.
    - **running_answer**: recompute this from scratch using ONLY the kept chunks — this is your real confidence check. Write it as you'd want it displayed: lead with the key fact, use **bold** for the specific number/name/deadline that answers the question, keep it to 1-3 sentences — no raw JSON, no unformatted wall of text. If running_answer already states the answer clearly, stop hunting: set is_complete=true this round. Continuing to call rag after running_answer already has the fact is wasted rounds and a false "not found" waiting to happen.
    - **Citation discipline — state only what the chunk text literally says.** Cite a chunk ONLY for claims that chunk's own text actually contains, verbatim or near-verbatim — never attribute a specific number, name, program, rule, or eligibility detail to a citation unless that chunk states it. This holds even on a rich-corpus turn with plenty of evidence: a fabricated citation is wrong regardless of how much OTHER evidence you have. If you're generalizing or inferring beyond what a chunk literally states, don't attach that chunk's citation to the inferred part — say it's an inference, or leave it uncited. (2026-08-07, Chat Master/LLM Agent, live finding: react_draft cited chunk [4] for "Comprehensive program members (MMA and Long-Term Care)" when the kept chunks — 2 of them, 401 chars total — never mentioned MMA, LTC, or Comprehensive at all.)
    - **gaps_closed** / **gaps_open**: name specific missing pieces, not "need more info." gaps_closed is what THIS round's tool result actually resolved (empty array most rounds — only list something here if this round is what closed it); gaps_open is everything still unresolved after incorporating this round's evidence. Both are lists, not prose — a gap tracker downstream reads these directly.
    - Chunks you do NOT keep are not deleted — they stay recallable this turn via recall_evidence (see manifest) using the ref shown in that chunk's set-aside note. Don't re-run rag for something you already retrieved; recall it instead.
    - **On the round where you finalize (is_complete=true): still fill out evidence_review.** This is the round the answer actually shipped from — its keep-list and running_answer are the audit trail for what grounded it. Skipping evidence_review here because "the answer field already has it" defeats the point: evidence_review is the structured, trackable record; "answer" is the user-facing prose.
1d. **CITE IT, OR LABEL IT — never blank, never bluff.** Three and only three valid response shapes:

    SHAPE 1 — GROUNDED (rag returned usable content, even partial):
      → Answer with CITATIONS. A partial sourced answer is valid.
      → Do NOT demote a grounded-but-partial answer to "no verified answer" just because
        you can't confirm a complete list. Present what was found; note it may not be
        exhaustive. "Here is what I found: [content]. There may be additional items not
        covered in available materials." is correct. Never discard grounded content.

    SHAPE 2 — FULL MISS (every rag tier returned nothing usable):
      → Surface a helpful best-effort/general answer — directional guidance, next steps
        ("contact the payer for the exact figure", "please upload their manual").
      → ALWAYS carry explicit label: "This wasn't found in our materials." Label is mandatory.
      → NEVER fabricate specifics: no invented deadlines, codes, rates, or amounts.
        Where the specific fact is missing, defer explicitly. General framing is allowed;
        invented facts are not.

    SHAPE 3 — FORBIDDEN: an ungrounded answer presented as sourced (bluff), OR a grounded
      answer thrown away as "no answer" (blank). Both are wrong.

    Validator: enforce citations on grounded answers, enforce label on full-miss answers.
    Do NOT blank grounded content because the list isn't confirmed complete.
2. NPI + PML / FL Medicaid enrollment (e.g. "Is NPI X set up for PML?"): use check_provider_credentialing FIRST (pass org_slug + npi). If unavailable, fall back to healthcare_npi_lookup for NPPES info.
3. ICD-10, diagnosis/procedure codes, CPT, HCPCS, Medicare/Medicaid coverage (NCD/LCD), "what does code … mean": use healthcare_query as the FIRST tool — NOT rag.
3b. Mobius product identity — "why the name Mobius", "what does Mobius mean", "who is Mobius", "what is Mobius", "what can Mobius do", "how does Mobius work", "tell me about Mobius", "what does the name mean", or any what/why/who/how question where the subject is Mobius itself: use **product_help_search** as the FIRST tool — NOT rag. Mobius is our own product; the authoritative answer is in the product knowledge base, not the internet.
4. NPI number only (no PML, no code/coverage question): use healthcare_npi_lookup or healthcare_query for NPPES registry facts.
5. **org_npi_lookup** or **search_org_names** when the user wants **NPI(s) for an organization by name**: e.g. "NPI for Acme", "find the NPIs for Aspire Health",
    "list billing NPIs for …", "look up NPI for org …". Use **inputs.org_name** from the message.
6. refuse for PHI (specific patient data) and clinical guidance only.
7. If rag returns good content → is_complete=true, synthesize answer. Assembled/partial content IS a good answer — see rule 1d.
8b. **web_scrape**: pass **scrape_mode** in inputs — **quick** (one page, default), **medium** (≤3 depth, 6 pages), **detailed** (≤5 depth, 50 pages, ≤10 doc downloads). Use **quick** unless the question needs a broader crawl or many linked documents.
9. Max {{ max_iterations }} reasoning rounds — if still no answer, escalate honestly with what was found.
9b. **Credentialing / NPPES tools** often include a **Summary** in the tool trace plus long **Result** markdown. If Success is true and the Summary answers the user, set **is_complete=true** immediately — do **not** call the same tool again in a new round.
10. If a tool result shows success (e.g. "Report stored", "Step 11 done", "report generated", "You can ask any question about it") → set is_complete=true and answer MUST confirm that the report or output was generated successfully. Do NOT say "I cannot generate" when the tool already succeeded.
11. When "Recent conversation" is present: treat the prior assistant reply as the current answer. If the user is asking for something that answer did NOT provide (e.g. a link, URL, specific page, more detail, a number), the answer is INSUFFICIENT — do NOT set is_complete=true. Call rag or web_scrape and only set is_complete=true after you have tool results to fulfill the request."""


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
        return REACT_NO_TOOLS_PROMPT

    if mode not in ("agentic", "quick"):
        mode = "copilot"
    _env = _react_jinja_env()
    mode_block = _env.from_string(_REACT_MODE_BLOCK_TEMPLATES[mode]).render(max_iterations=max_iterations)
    # Mirrored by hand from react_loop.py's _RAG_CALL_CEILING (2026-08-16,
    # Ananth's call): agentic gets a raised rag-call ceiling (RAG's own
    # per-slot latency allowance for chat.thinking makes extra calls
    # affordable here in a way it isn't for other modes). The LLM-facing
    # text must say the real number -- telling it "3, no more" when the
    # pipeline actually allows 6 would make it stop early and waste the
    # budget this override exists to grant.
    _rag_call_ceiling = 6 if mode == "agentic" else 3
    critical_rules_rendered = _env.from_string(REACT_CRITICAL_RULES_TEXT).render(
        max_iterations=max_iterations, rag_call_ceiling=_rag_call_ceiling,
    )
    _base_prompt_text = f"""
{REACT_IDENTITY_TEXT}
{mode_block}
{_tool_manifest_module.get_tool_manifest(allowed=allowed_tools)}

{REACT_RESPONSE_SHAPE_TEXT}

{REACT_FORMAT_RULES_TEXT}

{critical_rules_rendered}
"""
    # 2026-05-06 — splice mobius-user profile (rendered_prompt) so the
    # planner / ReAct picks tools and frames intermediate thinking in
    # the user's preferred voice + autonomy style. No-op when profile
    # is None (un-onboarded).
    from app.pipeline.personalization import splice_user_profile
    return splice_user_profile(_base_prompt_text, user_profile)


@dataclass(frozen=True)
class ResolvedCompositionPrompt:
    """Return shape for the v2 resolvers — carries composition_id/hash
    alongside the text so the caller can thread them into ``_call_llm_json``
    for llm_calls attribution (2026-07-30 fix: these were resolved + logged
    but never passed through to the actual LLM call, so llm_calls.
    composition_id/hash stayed NULL for every react/critic call despite the
    composition path resolving correctly — found via live Cloud Logging
    inspection after Chat Architecture reported output_intent=None end to
    end; see docs/REACT_PHASE_A_IMPLEMENTATION_PLAN.md)."""

    system_prompt: str
    composition_id: int | None
    composition_hash: str | None


def resolve_react_system_prompt_v2(
    max_iterations: int,
    chat_mode: str,
    user_profile: dict | None,
    allowed_tools: list[str] | None,
    agent_role: str,
) -> "ResolvedCompositionPrompt | None":
    """v2 block-composition path (Phase A, docs/REACT_PHASE_A_IMPLEMENTATION_PLAN.md).

    Mirrors app/responder/final.py's MOBIUS_PROMPT_SOURCE=composition pattern:
    flag-gated by the caller, fail-soft here (any miss/error returns None so
    the caller falls back to ``_react_reasoning_system()`` — never breaks a
    live turn). Verified byte-identical to the legacy path for the same
    inputs via scratchpad/parity_check_composition.py (AC-6 parity).

    ``agent_role`` selects react_explore/react_synthesize/react_draft — all
    three currently resolve to IDENTICAL content (addressing/attribution
    infrastructure only, per the signed agent_role scope ruling); this does
    NOT drive temperature or role-flavored text in Phase A.
    """
    from app.pipeline.personalization import _enabled as _personalization_enabled
    from app.services.prompt_manager import resolve_composition_sync

    mode = (chat_mode or "copilot").strip().lower()
    is_no_tools = (mode == "task") or (allowed_tools is not None and len(allowed_tools) == 0)

    try:
        if is_no_tools:
            rc = resolve_composition_sync("react.no_tools", conditions={}, template_vars={})
            if rc and rc.system_prompt.strip():
                logger.info("[react] v2 composition prompt module=react_no_tools hash=%s", rc.composition_hash)
                return ResolvedCompositionPrompt(rc.system_prompt, rc.composition_id, rc.composition_hash)
            return None

        if mode not in ("agentic", "quick"):
            mode = "copilot"
        env = _react_jinja_env()
        mode_block_text = env.from_string(_REACT_MODE_BLOCK_TEMPLATES[mode]).render(
            max_iterations=max_iterations
        ).strip("\n")
        tool_manifest_text = _tool_manifest_module.get_tool_manifest(allowed=allowed_tools)

        rendered_profile = ""
        if user_profile and isinstance(user_profile, dict):
            rendered_profile = (user_profile.get("rendered_prompt") or "").strip()
        has_user_profile = bool(_personalization_enabled() and rendered_profile)

        # Mirrored by hand from _react_reasoning_system's own computation
        # above (2026-08-16) -- react.critical_rules (v4) needs this same
        # var on the v2 composition path too, or agentic turns routed
        # through composition would render the literal "{{ rag_call_ceiling
        # }}" instead of the real number.
        rag_call_ceiling = 6 if mode == "agentic" else 3

        rc = resolve_composition_sync(
            f"react.{agent_role}",
            conditions={"has_user_profile": has_user_profile},
            template_vars={
                "mode_block_text": mode_block_text,
                "tool_manifest_text": tool_manifest_text,
                "user_profile_text": rendered_profile,
                "max_iterations": max_iterations,
                "rag_call_ceiling": rag_call_ceiling,
            },
        )
        if rc and rc.system_prompt.strip():
            logger.info(
                "[react] v2 composition prompt module=react_%s hash=%s", agent_role, rc.composition_hash
            )
            # Leading "\n" — matches the legacy f-string's own leading newline
            # (an artifact of its layout, not of any block's content); see
            # scratchpad/parity_check_composition.py for the verified derivation.
            return ResolvedCompositionPrompt("\n" + rc.system_prompt, rc.composition_id, rc.composition_hash)
        return None
    except Exception as exc:  # fail-soft: never break a turn on a resolution error
        logger.warning("[react] v2 composition resolve failed (agent_role=%s), using hardcoded: %s", agent_role, exc)
        return None


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
    *,
    composition_id: int | None = None,
    composition_hash: str | None = None,
    reasoning_depth: str | None = None,
    latency_budget_ms: int | None = None,
) -> str:
    """Call LLM and return raw string (expect JSON). When ctx is provided, uses llm_manager and appends usage to ctx.usages.

    ``composition_id``/``composition_hash``: pass through when the caller
    resolved its system prompt via the v2 block-composition path (see
    ``resolve_react_system_prompt_v2``/``resolve_critic_system_prompt_v2``),
    so the actual ``llm_calls`` row can be attributed — mirrors the same
    param LLM Agent added to ``generate()``/``generate_sync()`` for the
    integrator's composition path. None (the default) for the legacy path.

    ``reasoning_depth``/``latency_budget_ms`` (2026-08-04): react's model-
    bandit selection criteria — see governor.py's
    ``agent_role_to_reasoning_depth()``/``latency_budget_ms()`` for how
    react derives these (agent_role for the former, the governor's own
    hard-stop time-accounting for the latter). Threaded straight through
    to ``generate()``/``ModelRouter.select()`` (LLM Agent's side); both
    None by default — no caller has to change to get today's exact
    behavior. Only threaded on the ``ctx is not None`` (async ``generate``)
    path below — ``generate_sync()`` doesn't accept these yet, so the
    legacy sync fallback branch is untouched.
    """
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
                    composition_id=composition_id,
                    composition_hash=composition_hash,
                    reasoning_depth=reasoning_depth,
                    latency_budget_ms=latency_budget_ms,
                )
            )
            return (raw or "").strip(), usage
        from app.services.llm_manager import generate_sync
        raw, usage = generate_sync(
            prompt, stage="planner", max_tokens=max_tokens, parser=False, mode=None,
            composition_id=composition_id, composition_hash=composition_hash,
        )
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


# ── Prior-resolved-entities keyword overlap (2026-08-12, Task #90) ────────
# Deliberately a small, self-contained stopword list rather than an NLP
# dependency -- this is a best-effort heuristic (Chat Master: "fine, gap
# text carries enough signal"), not a precision-critical matcher. Tokens
# <=2 chars are dropped too (initials/units add noise, not signal).
_OVERLAP_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "and", "or", "but", "if", "of", "in", "on", "at", "to", "for", "with",
    "about", "against", "between", "into", "through", "during", "before",
    "after", "from", "up", "down", "this", "that", "these", "those", "it",
    "its", "what", "which", "who", "whom", "when", "where", "why", "how",
    "do", "does", "did", "doing", "have", "has", "had", "having", "will",
    "would", "should", "could", "can", "may", "might", "must", "not",
    "compare", "comparing", "deadline", "deadlines", "please", "also",
})
_OVERLAP_WORD_RE = re.compile(r"[a-z0-9]+")


def _overlap_tokens(text: str) -> set[str]:
    """Lowercase, strip punctuation, drop stopwords/short tokens -- for
    best-effort keyword-overlap matching between a query and a prior
    turn's free-text gap description. Not a precise index lookup."""
    words = _OVERLAP_WORD_RE.findall((text or "").lower())
    return {w for w in words if len(w) > 2 and w not in _OVERLAP_STOPWORDS}


# ── Reasoning-context builder ─────────────────────────────────────────────


def build_reasoning_context(
    ctx: PipelineContext,
    tool_results: list[dict],
    iteration: int,
    max_iterations: int | None = None,
    gap_status: str | None = None,
    rag_call_history: list[dict] | None = None,
    evidence_review_latest: dict | None = None,
    exhausted_tools: list[str] | None = None,
) -> str:
    """Build the context the model reasons over each iteration.

    ``gap_status``/``rag_call_history`` (2026-08-06, Task #48, Chat
    Architecture spec — EvidenceLedger phase 1): code-computed entirely
    by the caller (react_loop.py, from ``ctx._rag_call_history``) BEFORE
    this function runs — no LLM inference happens here, this function
    only renders. Replaces the old per-round telemetry prose block
    (fictional-arms-era "Strategy arms tried", then its real-but-
    unstructured successor "rag calls made so far this turn") with a
    single named ``[Evidence Ledger]`` block. Rendered UNCONDITIONALLY
    every round the history is non-empty -- this is the actual fix for
    the bug that made the previous reframe signal invisible: that
    signal lived inside react_loop.py's tool-result text, gated behind
    ``if not success:``, which never fired when rag returned real-but-
    wrong chunks (confirmed live, Amerigroup case -- 3 rounds, identical
    dispatch_path/chosen_slot/status, success=True throughout since
    chunks were non-empty). The ledger has no such gate: ``gap_status``
    is computed and shown regardless of whether the last call
    "succeeded" by the content-length heuristic.

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

    # Anchor the model on the current user question FIRST — before context,
    # tool results, and prior turns. With 14k+ token system prompts, gemini-flash
    # tends to anchor on the most-recent token cluster, which at round 1 is the
    # tail of the manifest. Placing the question early ensures it's the primary
    # signal the model reasons against.
    _q_early = (getattr(ctx, "effective_message", None) or ctx.message or "").strip()
    if _q_early:
        parts.append(f"Current user question: {_q_early}")

    # A typed card is already being rendered for this turn from tool data
    # (section_hint → pre_built_sections). The draft must not duplicate what
    # the card shows — the user sees BOTH, and restating deadlines/portal/fax/
    # forms/levels reads as stutter (Ananth screenshot review 2026-08-10).
    _card_hints = [
        (r.get("section_hint") or {}).get("section_format")
        for r in (tool_results or [])
        if isinstance(r, dict) and r.get("section_hint")
    ]
    if any(_card_hints):
        parts.append(
            "A structured card is ALREADY being displayed to the user with this turn's "
            f"tool data ({', '.join(sorted({h for h in _card_hints if h}))}). Your answer "
            "must NOT restate what that card shows — no repeating deadlines, day counts, "
            "portal URLs, fax/phone numbers, form names, appeal levels, or document lists. "
            "Write a short lead-in naming what the card contains, then add only what the "
            "card cannot: why it matters for THIS claim, urgency, the single next action, "
            "or a caveat. Repeating card contents is a formatting error."
        )

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

        if getattr(ctx, "is_continuation", False):
            # Task #83 (2026-08-11, Chat Master): an explicit continuation
            # resubmit (think-mode escalation, or the "Continue gathering"
            # chip, Task #84) needs the model building on what it actually
            # already found -- e.g. a table it already assembled -- not a
            # 400-char pronoun-resolution snippet or a keyword-guessed
            # transform intent. Same budget transform_previous_answer
            # already uses for the same reason (full prior substance,
            # bounded so it doesn't blow the prompt budget).
            from app.skills.builtin.transform_previous import (
                _PREVIOUS_ANSWER_CHAR_BUDGET as _CONTINUATION_PREVIEW,
            )
            MOST_RECENT_PREVIEW = _CONTINUATION_PREVIEW
            OLDER_PREVIEW = 200
            preamble = (
                "Recent conversation (the FIRST 'Assistant:' below is the "
                "MOST RECENT answer, shown in full — this is a CONTINUATION "
                "of that answer, not a fresh question. Build on what you "
                "already found; do not restart research on ground you've "
                "already covered.):\n"
            )
        elif is_transform:
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

    # ── Previously resolved (this thread) (2026-08-12, Chat Master
    # directive, Task #90) ────────────────────────────────────────────
    # last_turns/previous_thread_summary above cover the last ~3 turns
    # adequately (confirmed live, Task #89) -- this reaches further back
    # (ctx.prior_resolved_entities, up to 8 turns, gated on
    # ctx.is_continuation in state_load.py) for a TARGETED lookup on
    # longer multi-entity threads, e.g. picking up Aetna's resolved
    # facts from turn 2 on turn 6 without loading the whole window.
    # gap_text is free-text model prose ("Molina's timely filing
    # deadlines"), not a structured entity ID -- matching against the
    # current query is a best-effort keyword-overlap heuristic
    # (stopwords + punctuation stripped both sides), not a precise
    # index lookup. Deliberately NOT deduped across turns -- a more
    # recent resolution of the same entity should be visible alongside
    # an older one, not silently hidden.
    _prior_entities = getattr(ctx, "prior_resolved_entities", None) or []
    if _prior_entities:
        _query_for_match = (getattr(ctx, "effective_message", None) or ctx.message or "")
        _query_tokens = _overlap_tokens(_query_for_match)
        _matched_entities = [
            e for e in _prior_entities
            if isinstance(e, dict) and _query_tokens & _overlap_tokens(str(e.get("gap_text") or ""))
        ]
        if _matched_entities:
            _resolved_lines = [
                "Previously resolved (this thread) — facts settled on earlier turns, "
                "matched to this question by keyword overlap with what was resolved. "
                "Use these directly; do NOT re-search for them:"
            ]
            for e in _matched_entities[:5]:  # cap -- best-effort, not exhaustive
                _resolved_lines.append(
                    f"- (turn -{e.get('turn_index', '?')}) resolved \"{e.get('gap_text')}\": "
                    + str(e.get("react_draft") or "")[:400]
                )
            parts.append("\n".join(_resolved_lines))

    # ── Evidence Ledger (2026-08-06, Task #48, Chat Architecture spec) ───
    # Code-computed by the caller (react_loop.py, from ctx._rag_call_history)
    # BEFORE this function runs — rendered here verbatim, no LLM inference.
    # Replaces two predecessors: the "5-arm bandit" (precision/recall/
    # hybrid/google/llm_direct — stopped matching reality once Task #36
    # made rag() a single comprehensive call), and this block's own
    # immediate successor (real per-call history as prose), which fixed
    # the fictional-arms problem but still relied on react_loop.py's
    # `if not success:` gate to surface a reframe signal — invisible
    # whenever rag returned real-but-wrong chunks (confirmed live:
    # Amerigroup case, 3 rounds, identical dispatch_path/chosen_slot/
    # status, success=True throughout since chunks were non-empty).
    # gap_status is rendered UNCONDITIONALLY here, success or not — that
    # gate is what actually needed removing, not the wording.
    _ledger_history = (
        rag_call_history if rag_call_history is not None
        else list(getattr(ctx, "_rag_call_history", []))
    )
    # 2026-08-07 (Chat Master, #41(b), live-query finding): this block used
    # to render ONLY when _ledger_history was non-empty -- i.e. only on
    # turns that called rag. Confirmed live: a COB-reconsideration turn
    # that only ever called appeals_* tools never rendered [Evidence
    # Ledger] AT ALL, not just "missing the exhausted-tools line" -- the
    # whole block was invisible for the entire turn. exhausted_tools now
    # renders independently of rag history so appeals-only (or any
    # non-rag) turns still get this signal in the one block the model is
    # explicitly instructed (rule 1c-2) to read every round -- previously
    # the only place this appeared was a paragraph tacked onto the end of
    # reasoning_context via failure_hint_for_prompt(), which existed but
    # wasn't prominent enough: rounds 5 and 6 of the live turn both
    # re-attempted appeals_get_playbook after it was already exhausted.
    _exhausted = list(exhausted_tools or [])
    if _ledger_history or _exhausted:
        _ledger_lines = ["[Evidence Ledger]", f"round: {iteration}"]
        if _ledger_history:
            _strategy_tried = [
                "citable retrieval" if h.get("citable_required") else "relaxed retrieval"
                for h in _ledger_history
            ]
            _tool_tried = ["rag" for _ in _ledger_history]
            _dispatch_path_history = [h.get("dispatch_path") or "n/a" for h in _ledger_history]
            _ledger_lines.extend([
                f"strategy_tried: {_strategy_tried}",
                f"tool_tried: {_tool_tried}",
                f"dispatch_path_history: {_dispatch_path_history}",
                f"gap_status: \"{gap_status or 'progressing'}\"",
                "When gap_status is \"stagnant\": the last two rag calls converged on the same "
                "internal strategy and outcome — calling rag again with the same or similar "
                "query will not surface new information. A real content gap (the fact isn't "
                "documented anywhere) looks identical to a source gap (a document exists but "
                "isn't indexed yet) from rag's output alone — consider lookup_authoritative_sources "
                "before concluding the corpus doesn't have it, or switch to a materially "
                "different approach.",
            ])
            # 2026-08-07 (Task #41(a), Chat Master directive): RAG's own
            # Observer agent's verdict/reason for the most recent call —
            # more authoritative than gap_status's dispatch_path-equality
            # heuristic, since it's RAG explaining WHY it stopped, not
            # this file inferring it from repeated fields. Confirmed live:
            # reason="vector_filled_to_capacity" means the search found
            # plenty of candidates and hit a ranking/volume cap — a
            # STRUCTURAL limit, not a vocabulary gap, so reframing with
            # different terms is unlikely to help even on gap_status=
            # "progressing" (which only compares the last 2 calls and
            # could still miss this). Absent on responses that predate
            # this field or don't set it — omitted rather than shown as
            # "None" to avoid implying a signal that isn't really there.
            _latest_observer_reason = _ledger_history[-1].get("observer_final_reason")
            _latest_observer_verdict = _ledger_history[-1].get("observer_final_verdict")
            if _latest_observer_reason or _latest_observer_verdict:
                _ledger_lines.append(
                    f"observer_final_verdict: {_latest_observer_verdict!r}, "
                    f"observer_final_reason: {_latest_observer_reason!r} — this is RAG's own "
                    "Observer explaining why the last call stopped, more authoritative than "
                    "gap_status alone. A reason indicating a structural/capacity limit (e.g. "
                    "\"...filled_to_capacity\") means reframing with different terms won't help — "
                    "that's not a vocabulary gap, treat it the same as gap_status=\"stagnant\" "
                    "regardless of what gap_status itself says."
                )
        if _exhausted:
            _ledger_lines.extend([
                f"exhausted_tools: {_exhausted}",
                "These tools have failed repeatedly this turn with no new evidence since — "
                "do NOT call them again, re-phrasing the inputs will not help. Pick a "
                "genuinely different tool, or finalize with what you already have.",
            ])
        parts.append("\n".join(_ledger_lines))
    # 2026-08-07 (Ananth, directly): react's own accumulated verdict on the
    # evidence so far, code-carried from the last round's evidence_review
    # (see rule 1c/REACT_RESPONSE_SHAPE_TEXT) — displayed unconditionally
    # so the running answer and remaining gaps are visible artifacts, not
    # buried inside a "thought" string that gets discarded each round.
    _ev_latest = evidence_review_latest or getattr(ctx, "_evidence_review_latest", None)
    if isinstance(_ev_latest, dict) and (_ev_latest.get("running_answer") or _ev_latest.get("gaps_open")):
        _ev_lines = [
            "[Evidence Review — your own running verdict, from last round]",
            f"running_answer: {_ev_latest.get('running_answer') or '(none yet)'}",
            f"gaps_open: {_ev_latest.get('gaps_open') or []}",
            f"gaps_closed: {_ev_latest.get('gaps_closed') or []}",
        ]
        # 2026-08-07 (Chat Master, Task #65, live-query finding, cid
        # d288d009): code-computed, not a self-report -- react cannot be
        # trusted to notice its own evidence is thin (confirmed live: 2
        # kept chunks, 401 chars, none containing the question's key
        # terms, and running_answer still stated specifics confidently,
        # cited to a chunk that didn't say them). The trigger isn't zero
        # evidence -- an empty corpus already hedges correctly ("could
        # not be found"). It's specifically the middle case: some
        # plausible-looking evidence that doesn't cover the SPECIFIC
        # claims being made. This block makes that condition an explicit,
        # unavoidable instruction for the round, not something react has
        # to self-diagnose.
        if _ev_latest.get("sparse_evidence"):
            _ev_lines.append(
                f"EVIDENCE IS THIN: only {_ev_latest.get('kept_chunk_count', 0)} chunk(s), "
                f"{_ev_latest.get('kept_chunk_chars', 0)} chars kept. Your running_answer (and "
                "final answer, if you finalize this round) MUST hedge explicitly: state only "
                "facts that are literally present in the kept chunk text, and say plainly what "
                "you cannot confirm — \"Limited sources available; here's what I found: [X]. "
                "I cannot confirm [Y].\" Do NOT state specific numbers, names, or eligibility "
                "rules unless they are actually written in the kept chunks. If the question "
                "asks for more than the kept evidence supports, say so instead of filling the "
                "gap with a plausible-sounding guess."
            )
        _ev_lines.append(
            "If running_answer already answers the question with confidence, set "
            "is_complete=true now instead of gathering more evidence you don't need."
        )
        parts.append("\n".join(_ev_lines))
    if getattr(ctx, "_google_search_tried_this_turn", False):
        parts.append(
            "google_search has already been tried this turn and returned no "
            "usable results. Only a still-valid rag retry (per rule 1b) or "
            "answering from model knowledge with explicit caveats remain."
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
            "When **Summary** is present, treat it as a quick orientation, but the full "
            "**Result** below it is the actual evidence — read all of it before deciding "
            "you lack information. Do not re-run the same tool if Result already answers the ask."
        )
        # 2026-08-07 (Ananth, directly, live-query finding): this used to
        # cap every tool result to a 320-head + 400-tail slice of the raw
        # string once it exceeded 600 chars. For rag specifically (which
        # never sets result_summary), that meant only chunk 1 and the tail
        # of the last chunk of a multi-chunk corpus_search response ever
        # reached react -- confirmed live: a 7.8k-char, 7-chunk result had
        # its one chunk containing the literal answer (authority=
        # authoritative, ranked #4) sliced out of the middle and silently
        # dropped, so round 3 concluded "cannot answer" over evidence it
        # never actually saw. No tool gets a length cap now -- if a result
        # is expensive enough to warrant trimming, that's a per-tool
        # decision the skill itself makes (result_summary), not a blind
        # string slice applied uniformly after the fact.
        for r in tool_results:
            raw = r.get("result") or ""
            summ = (r.get("result_summary") or "").strip()
            result_bits = []
            if summ:
                result_bits.append(f"[Summary]\n{summ}")
            result_bits.append(f"[Full result — {len(raw)} chars, complete, not truncated]\n{raw}")
            result_preview = "\n\n".join(result_bits)
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
    #   • Tool call:    { "thought": "...", "evidence_review": {"keep": [...], "running_answer": "...", "gaps_closed": [...], "gaps_open": [...]},
    #                     "tool": "...", "inputs": {...}, "is_complete": false }
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
        "  • Need another tool → set is_complete=false with the tool name and inputs, and "
        "include evidence_review if any tool result is present above.\n"
        "  • Have the answer  → set is_complete=true, tool=null, and put the full "
        "answer in the \"answer\" field.\n"
        "Prose responses cannot be parsed and will be discarded."
    )

    return "\n\n".join(parts)
