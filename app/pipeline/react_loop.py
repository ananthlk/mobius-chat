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
import itertools as _itertools
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

# Final-round self-report instruction (2026-07-30, Ananth's framing): react's
# last round is a distinct prompt "mode" — the model knows no further rounds
# follow, so instead of a generic honest-escalation string bolted on after
# the loop exits, the model self-reports its own terminal state as part of
# its normal JSON output for that round. Context-message-only (like
# round_directive/is_guidance_round) — deliberately NOT a system-prompt
# block, staying inside Phase A's existing boundary that the 12-section
# context message is Python-built, not DB-composed. Read at the exhausted-
# iterations fallback below; fail-soft if the model doesn't comply (falls
# back to the pre-existing generic string, zero regression risk).
_REACT_FINAL_ROUND_INSTRUCTION = (
    "This is your FINAL round — there is no round after this one, so do NOT "
    "request another tool call even if you think one would help.\n"
    "If you can fully answer: respond normally (is_complete: true, answer, "
    "sources, confidence).\n"
    "If you CANNOT fully answer: set is_complete: false, tool: null, and "
    "ALSO include these three fields so the user gets an honest, specific "
    "explanation instead of a generic refusal:\n"
    "  \"unfinished_reason\": one of \"need_more_time\" (you have a "
    "promising lead but ran out of rounds — more searching would likely "
    "help), \"need_more_info\" (you're missing a specific fact, document, "
    "or clarification only the user can supply), or \"no_path_forward\" "
    "(you've exhausted the available tools/angles and the information is "
    "likely not reachable this way),\n"
    "  \"unfinished_summary\": 1-2 sentences on what you checked and what, "
    "if anything, you found — specific to this question, not generic,\n"
    "  \"unblock_ask\" (only when unfinished_reason is \"need_more_info\"): "
    "the SPECIFIC question or piece of information that would let you "
    "continue — concrete (e.g. \"the exact plan name\", \"a link to their "
    "provider manual\", \"the specific service date\"), never generic "
    "(\"more details\")."
)

# Structural-exhaustion offramp (2026-08-04, Ananth: "why are we forcing 3
# rounds when we already exhausted... doesn't it have to be dynamic"). The
# self-report above is a MANDATORY instruction that only fires when rn ==
# max_it (literally no round left). This one fires EARLIER, once
# ReactRetryGuard.structurally_exhausted() is true (zero successes, 2+
# genuinely different tools already failed) — but on a round that ISN'T
# the mechanical last one, so it must NOT claim "no round after this" (that
# would be false) or forbid further tool calls (the model may still have a
# real angle left). It only makes the self-report format LEGAL early,
# as an option — an additive offramp, not a forced early stop. Same three
# fields, same downstream rendering in the exhausted-iterations fallback;
# only the eligibility condition and the framing text differ.
_REACT_STRUCTURAL_EXHAUSTION_OFFRAMP = (
    "You've now tried multiple different approaches for this and none have "
    "worked (see the failed-attempts list above) — you're not required to "
    "stop, keep going if you have a genuinely different angle left to try. "
    "But if you don't, you don't have to keep spending rounds on it: you "
    "may respond now with is_complete: false, tool: null, and the same "
    "\"unfinished_reason\"/\"unfinished_summary\"/\"unblock_ask\" fields "
    "described for the final round (see this turn's later context if you've "
    "seen that instruction before) — an honest, specific explanation is "
    "always better than a forced extra attempt that's unlikely to help."
)

import httpx

from app.communication.plan_display import emit_jurisdiction_context, jurisdiction_summary
from app.communication.tool_output_envelope import compose_mobius_tool_envelope
from app.pipeline.context import PipelineContext
# NB: TOOL_MANIFEST is read lazily inside react/prompts._react_reasoning_system
# so MCP-registered tools land in the planner prompt even if they registered
# after this module was imported. No top-level snapshot here on purpose.
from app.planner.schemas import Plan, SubQuestion
from app.services.doc_assembly import (
    RETRIEVAL_SIGNAL_GOOGLE_ONLY,
    RETRIEVAL_SIGNAL_NO_SOURCES,
    RETRIEVAL_SIGNAL_ROSTER_COMPLETE,
    RETRIEVAL_SIGNAL_SYSTEM_CONTEXT,
)
from app.services.non_patient_rag import answer_non_patient
from app.services.reasoning_agent import answer_reasoning
from app.services.tool_agent import (
    REACT_TOOL_SUMMARY_KEY,
    answer_tool,
    _react_summary_from_long_markdown,
)
from app.skills.document_upload import DOCUMENT_UPLOAD_SKILL_MARKDOWN, format_thread_uploads_markdown

# 2026-04-18 disconnect — credentialing helpers removed:
#   _CREDENTIALING_DUAL_FINALIZE_TOOLS frozenset
#   _credentialing_copilot_turn_markdown()
#   _envelope_routes_to_reconciliation()
#   _format_billing_npi_options_markdown()
#   import from app.pipeline.credentialing_envelope
#
# _attach_result_summary below was originally named
# _attach_credentialing_result_summary but is generic "truncate long
# tool output into a concise Summary block" logic used by the healthcare
# lookup branches too. Retained (renamed) because those remain in the
# tool dispatch.
def _attach_result_summary(
    out: dict[str, Any],
    result_text: str,
    *,
    summary_heading: str,
    long_threshold: int = 800,
) -> dict[str, Any]:
    """Add result_summary when prose is long (NPPES/healthcare tools).

    The LLM-facing reasoning context will read result_summary first; the
    full markdown stays in the response for the user. Keeps the planner
    from wasting rounds re-calling the same tool because its full output
    truncates in the context window."""
    txt = (result_text or "").strip()
    if len(txt) > long_threshold:
        summ = _react_summary_from_long_markdown(txt, heading=summary_heading)
        if summ:
            out = dict(out)
            out["result_summary"] = summ
    return out


# ── Evidence memory (2026-08-07, Ananth, directly) ──────────────────────────
#
# Companion to build_reasoning_context's no-truncation fix: once every
# chunk of every rag call is fully visible every round, context grows
# every round too. react now actively curates via evidence_review
# ("keep": [chunk numbers]) instead of us silently deciding for it. What
# it doesn't keep is NOT deleted -- it's stashed here so a later round
# can pull a set-aside chunk back up (recall_evidence) without spending
# one of the 3 rag-call budget slots re-querying for something already
# retrieved. Regex, not a structured chunk list, because the only stable
# contract between corpus_search's _format_context and this file is the
# "[N] header\ntext" shape already rendered into tool_results[i]["result"]
# -- reparsing that is simpler than threading a second, parallel
# structured chunk list through the whole tool-dispatch path.
_CHUNK_HEADER_RE = re.compile(r"^\[(\d+)\]\s", re.MULTILINE)


def _extract_chunk_blocks(raw: str) -> list[tuple[int, str]]:
    """Split rag-formatted text into (chunk_number, full_block) pairs.

    Returns [] for text that isn't chunk-numbered (e.g. NPPES prose) --
    callers must treat that as "nothing to prune/store", not an error."""
    if not raw:
        return []
    matches = list(_CHUNK_HEADER_RE.finditer(raw))
    if not matches:
        return []
    blocks = []
    for idx, m in enumerate(matches):
        start = m.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw)
        try:
            chunk_num = int(m.group(1))
        except ValueError:
            continue
        blocks.append((chunk_num, raw[start:end].rstrip()))
    return blocks


def _store_evidence_memory(ctx: PipelineContext, call_idx: int, raw: str) -> None:
    """Snapshot every chunk of a rag result into ctx._evidence_memory
    BEFORE any pruning happens, so recall_evidence can always retrieve
    the original text regardless of what a later evidence_review discards."""
    blocks = _extract_chunk_blocks(raw)
    if not blocks:
        return
    memory: list[dict] = getattr(ctx, "_evidence_memory", None)  # type: ignore[assignment]
    if memory is None:
        memory = []
        ctx._evidence_memory = memory  # type: ignore[attr-defined]
    for chunk_num, block in blocks:
        header, _, text = block.partition("\n")
        memory.append({
            "call": call_idx, "chunk": chunk_num,
            "header": header.strip(), "text": text.strip(),
        })


def _prune_kept_chunks(raw: str, keep: list[int], call_idx: int) -> str:
    """Rewrite a rag result to only the chunks react's evidence_review
    marked relevant. Chunks left out get a short set-aside note carrying
    their recall_evidence ref ("<call_idx>.<chunk_num>") instead of
    silently vanishing. Falls back to the untouched original when the
    text isn't chunk-numbered, or when keep matches nothing real (a bad
    "keep" list must never destroy evidence)."""
    blocks = _extract_chunk_blocks(raw)
    if not blocks:
        return raw
    keep_set = set(keep)
    kept, set_aside = [], []
    for chunk_num, block in blocks:
        if chunk_num in keep_set:
            kept.append(block)
        else:
            set_aside.append(f"{call_idx}.{chunk_num}")
    if not kept:
        return raw
    out = "\n\n".join(kept)
    if set_aside:
        out += (
            f"\n\n[{len(set_aside)} chunk(s) reviewed and set aside as not relevant to this "
            f"question: refs {set_aside}. Still available — call recall_evidence with these "
            "refs if a later round needs one of them; do not re-run rag for this.]"
        )
    return out


# 2026-08-07 (Chat Master, Task #65, live-query finding, cid d288d009):
# fabrication-on-sparse-corpus. 2 kept chunks, 401 chars total, none
# containing MMA/LTC/Comprehensive -- react_draft still stated "For
# Comprehensive program members (MMA and Long-Term Care)..." cited as
# [4]. evidence_review's running_answer had no signal distinguishing
# "genuinely confident, well-supported" from "thin evidence, extrapolated
# past what it supports" -- both look like ordinary prose. LLM Agent's
# refinement: the trigger isn't zero evidence (an empty corpus already
# hedges correctly, "could not be found") -- it's specifically the
# middle case, SOME plausible-looking evidence that doesn't actually
# cover the question's specific claims. A count/char threshold is a
# coarse proxy for that middle case, not a semantic detector -- it can't
# know whether 3 kept chunks genuinely answer the question or not, but
# it CAN flag "this round has very little to work with," which is
# exactly the condition under which a model should hedge rather than
# extrapolate.
_SPARSE_EVIDENCE_CHUNK_THRESHOLD = 3
_SPARSE_EVIDENCE_CHAR_THRESHOLD = 500


def _kept_chunk_stats(raw: str, keep: list[int]) -> tuple[int, int]:
    """(count, total_text_chars) for the chunks in `keep`, measuring only
    the chunk TEXT (not the "[N] Doc Name" header) -- header length isn't
    evidence. Returns (0, 0) for non-chunked results or an empty keep list."""
    blocks = _extract_chunk_blocks(raw)
    if not blocks or not keep:
        return (0, 0)
    keep_set = set(keep)
    count = 0
    chars = 0
    for chunk_num, block in blocks:
        if chunk_num not in keep_set:
            continue
        count += 1
        _header, _, text = block.partition("\n")
        chars += len(text.strip())
    return (count, chars)


def _all_chunk_stats(raw: str) -> tuple[int, int]:
    """(count, total_text_chars) for EVERY chunk in `raw`, unfiltered --
    the fast-mode round-1 early exit runs before any evidence_review
    "keep" decision exists, so there's no keep-list to filter by yet.
    Returns (0, 0) for non-chunked results."""
    blocks = _extract_chunk_blocks(raw)
    if not blocks:
        return (0, 0)
    count = 0
    chars = 0
    for _chunk_num, block in blocks:
        count += 1
        _header, _, text = block.partition("\n")
        chars += len(text.strip())
    return (count, chars)


# 2026-08-07 (Chat Master relaying Ananth's UX contract, Task #65 follow-up
# on fast/"quick" mode): the round-1 early exit used to ship whatever the
# first rag call returned verbatim, rich or thin, as react_draft --
# confirmed live as the same fabrication mechanism as d288d009 (no
# reasoning round, no evidence_review, nothing to hedge with). Fix keeps
# BOTH halves of Ananth's contract: (1) always stream something fast --
# neither branch below adds a full reasoning round, so latency is
# unchanged either way; (2) never fabricate -- thin evidence gets an
# honest, code-constructed hedge instead of a confident-looking raw dump.
# Both count and chars must pass for the rich-evidence (current,
# unchanged) path; failing either routes to the hedge.
#
# _FAST_MODE_MIN_SCORE removed from the gate 2026-08-07 (Ananth, directly,
# live finding): it used to be AND'd in alongside count/chars -- a 15-chunk,
# 11,685-char turn (well past both volume thresholds) still fell into the
# hedge path because rerank_score wasn't populated for those chunks
# (top_score computed as 0.00). Zero score isn't the same as thin evidence;
# a substantial corpus is substantial regardless of whether a score field
# happens to be populated. Score is still computed and logged for
# diagnostics on both paths, just no longer decides the branch.
_FAST_MODE_MIN_CHUNKS = 3
_FAST_MODE_MIN_CHARS = 500


def _build_fast_mode_hedge(raw: str, chunk_count: int) -> str:
    """Short, honest react_draft for the thin-evidence fast-mode path --
    constructed from the ACTUAL retrieved text (a literal excerpt), never
    an LLM call, so it carries zero fabrication risk of its own. That's
    the whole point: a quick LLM "summarize this" pass could still
    hallucinate past what the excerpt supports, defeating the fix.

    2026-08-07 (Ananth, directly, live finding): the first version of
    this text ("Limited sources... I couldn't confirm specific details")
    read as a blanket dismissal even when real, partial work WAS done --
    a live turn had 4 real appeal rules synthesized correctly in the
    Answer tab while the Summary hedge implied nothing useful was found
    at all. Calibrated wording: lead with what was actually found (real
    work happened, here it is), frame the limitation as incompleteness
    ("may not be the full picture") rather than total inability, since
    that's what's actually true -- this is still a genuine excerpt, not
    a confident answer, just not phrased as if nothing was found."""
    blocks = _extract_chunk_blocks(raw)
    snippet = ""
    if blocks:
        _header, _, text = blocks[0][1].partition("\n")
        snippet = text.strip()[:280]
        if len(text.strip()) > 280:
            snippet = snippet.rstrip() + "…"
    if not snippet:
        return (
            "I didn't find enough in our materials to answer this with confidence. "
            "For a more thorough look, try Think mode."
        )
    plural = "s" if chunk_count != 1 else ""
    return (
        f"Found {chunk_count} relevant passage{plural} on this topic — here's what "
        f"they say: {snippet} This may not be the complete picture for your specific "
        "question. For a fuller, more thorough answer, try Think mode."
    )


# 2026-08-07 (Ananth, directly — "always let ReAct summarize, even on
# early exit," a grace rule, not optional): the pure code-constructed
# hedge above was a deliberate zero-fabrication-risk choice, but Ananth's
# explicit call is that thin evidence should still get ONE real
# synthesis attempt before hedging -- "the user gets an attempt + honest
# uncertainty, not just uncertainty." This is a genuine reversal of the
# earlier "no LLM call on this path" constraint, not an oversight --
# Ananth accepted the latency tradeoff explicitly ("one pass instead of
# two rounds"). The citation-discipline instruction below is the SAME
# rule #65 added to evidence_review (rule 1c-2) -- reused here verbatim
# in spirit so this one-shot path doesn't reopen the exact fabrication
# risk #65 was built to close. _build_fast_mode_hedge remains the
# fallback when this call fails or returns nothing usable.
_FAST_MODE_SYNTHESIS_SYSTEM = (
    "You are Mobius, answering from LIMITED retrieved evidence in fast mode. "
    "Write a short, honest, best-effort answer (2-4 sentences) using ONLY facts "
    "literally present in the evidence below. State a specific number, name, "
    "rule, or eligibility detail ONLY if it is directly written in the evidence "
    "-- if you are inferring or generalizing beyond what's literally there, say "
    "so explicitly instead of stating it as settled fact. If the evidence "
    "doesn't fully answer the question, say plainly what's missing rather than "
    "guessing. Output ONLY the answer text -- no JSON, no preamble, no "
    "meta-commentary about these instructions."
)

# 2026-08-07 (Ananth, directly, live finding): the grace rule applies to
# BOTH fast-mode early-exit paths, not just thin evidence -- react_draft
# on the Summary tab must always be a synthesized, human-readable answer,
# never raw chunk text. The rich-evidence path used to ship the raw "[1]
# Sunshine Provider Manual...[2] Provider_Manual.pdf..." dump directly;
# confirmed live the Answer tab (integrator, which DOES synthesize from
# ctx) looked correct while Summary showed the raw dump verbatim. Same
# synthesis mechanism, different wording -- there's no need to frame
# substantial evidence as "limited" or hedge about missing coverage when
# the volume threshold already passed; citation discipline (state only
# what's literally there) still applies regardless.
_FAST_MODE_RICH_SYNTHESIS_SYSTEM = (
    "You are Mobius, answering from retrieved evidence in fast mode. Write a "
    "clear, direct, human-readable answer (2-5 sentences) using ONLY facts "
    "literally present in the evidence below. State a specific number, name, "
    "rule, or eligibility detail ONLY if it is directly written in the "
    "evidence -- if you are inferring or generalizing beyond what's literally "
    "there, say so explicitly instead of stating it as settled fact. Output "
    "ONLY the answer text -- no JSON, no preamble, no meta-commentary about "
    "these instructions."
)


def _fast_mode_synthesize_answer(query: str, raw_text: str, ctx: PipelineContext, stage: str, system: str = _FAST_MODE_SYNTHESIS_SYSTEM) -> str | None:
    """One lightweight LLM pass over the raw retrieved text -- NOT a
    full reasoning round (no tool-call decision schema, no
    evidence_review, no JSON parsing required of the response). Returns
    None on any failure/empty response so the caller can fall back to
    a safe default (the code-constructed hedge on the thin path, the raw
    text itself on the rich path) rather than crash or ship nothing.

    reasoning_depth="fast" + latency_budget_ms (2026-08-08, Chat Master:
    "same pattern you used for the parallel integrator... there's no
    reason to run it on Pro"): confirmed live this session -- a real
    turn's trace showed this exact call landing on Gemini Pro at 12.61s
    for a single lightweight max_tokens=350 synthesis pass. Same two
    signals as final_parallel.py's Call A/B/C: latency_budget_ms is the
    hard pre-filter (ModelRouter.select() trims candidates whose
    ema_latency_ms exceeds it), reasoning_depth="fast" is the
    complementary soft bias among whatever survives. 2000ms budget --
    slightly more headroom than the parallel integrator's smallest call
    (Call C, 1500ms/512 tokens) since this call's max_tokens=350 is
    comparable but the fast/thin-evidence path can't budget-check first
    the way the parallel path's own verification batch did.

    Streaming (2026-08-08, Chat Master addendum, shipped in the same
    commit as the fast-model signals above): "stream its output the same
    way the regular exit path does... check how the regular exit path
    streams and match that pattern." That path is final.py's
    _emit_integrator_chunks -- generate()/_call_llm_json() aren't
    token-streaming APIs (the complete response comes back in one piece),
    so "streaming" there means the SAME thing it means here: chunk the
    complete text and emit each piece via the "message" SSE event
    (append_message_chunk), reusing _emit_integrator_chunks directly
    rather than re-implementing its chunk-size math. The FE only renders
    "message" chunks before draft_ready fires (app.ts:
    `!draftEmitted && onStreamingMessage`) and this call always completes
    before orchestrator.py's append_draft_answer, so ordering is safe --
    no double-render, same guarantee format_response's own chunking
    already relies on. Streaming is cosmetic -- a failure here must never
    take down the actual synthesized answer."""
    try:
        user = f"User question: {query}\n\nAvailable evidence:\n{raw_text}"
        # 2026-08-08 (live truncation, Ananth watching): max_tokens=350 was too
        # tight -- confirmed truncation mid-word/mid-number ("...it is 36" instead
        # of "365") on gemini-2.5-flash. The installed vertexai SDK exposes no
        # thinking_config/thinking_budget param to control it directly (checked:
        # GenerationConfig.__init__ has no such field), so Gemini 2.5's default
        # thinking behavior can consume output-token budget invisibly, leaving too
        # little room for the visible answer when max_tokens is this small.
        # Widened with real headroom rather than guessing at a minimal bump.
        raw = _call_llm_json(
            system, user, max_tokens=2048, ctx=ctx, stage=stage,
            reasoning_depth="fast", latency_budget_ms=2000,
        )
        answer = (raw or "").strip()
        if not answer:
            return None
        try:
            from app.responder.final import _emit_integrator_chunks
            from app.storage.progress import append_message_chunk
            _emit_integrator_chunks(answer, lambda chunk: append_message_chunk(ctx.correlation_id, chunk))
        except Exception:
            logger.debug("fast-mode synthesis streaming failed (stage=%s); answer still returned", stage, exc_info=True)
        return answer
    except Exception:
        logger.warning("fast-mode synthesis call failed (stage=%s); falling back to raw/hedge", stage, exc_info=True)
        return None


from app.state.jurisdiction import rag_filters_from_active

# ---------------------------------------------------------------------------
# ReAct decision JSON (reasoning LLM returns a single JSON object)
# ---------------------------------------------------------------------------


# Phase 1i (2026-04-18) — JSON decision parsing moved to
# app.pipeline.react.parsing. Re-imported here so that the existing
# `run_react` body below and any external call sites referencing these
# via react_loop keep working with no changes. New code should import
# directly from app.pipeline.react.parsing.
from app.pipeline.react.parsing import (  # noqa: F401 — re-exported for back-compat
    _extract_balanced_json_object,
    _parse_react_decision_dict_obj,
    _parse_react_decision_json,
    _react_fallback_org_npi_lookup_decision,
    _strip_markdown_json_fence,
)


# ---------------------------------------------------------------------------
# Constants + prompt helpers (Phase 1i 2026-04-18 — moved to
# app.pipeline.react.prompts). Re-imported here for back-compat with
# existing callers; new code should import directly from the new module.
# ---------------------------------------------------------------------------

from app.pipeline.react.prompts import (  # noqa: F401 — re-exported for back-compat
    QUICK_MODE_TRUNCATED_CHARS,
    REACT_MAX_ROUNDS_AGENTIC,
    REACT_MAX_ROUNDS_COPILOT,
    REACT_MAX_ROUNDS_QUICK,
    _call_llm_json,
    _get_config_sha,
    _react_reasoning_system,
    _react_round_headline,
    build_reasoning_context,
    react_chat_mode_label,
    react_max_iterations_for_mode,
)

# Kept only for reference; the body now lives in app.pipeline.react.prompts.
# The re-imports above provide the same names at the old import path.


# ── Corpus confidence threshold (tunable) ──────────────────────────────
#
# ``answer_non_patient`` filters retrieved chunks by
# ``_score_chunk_for_confidence_filter(chunk) >= confidence_min``. The
# score map in app/services/non_patient_rag.py assigns:
#
#   process_confident     0.9
#   process_with_caution  0.55
#   abstain               0.3
#
# Pre-2026-04-19 threshold was 0.5 — which dropped "abstain" chunks
# silently. Live validation on Sunshine Health H0036 revealed the
# failure mode: the RAG backend retrieved Sunshine Provider Manual
# pages (general medical-necessity framework) but they scored in the
# abstain band on a specific-code question. Planner got zero chunks,
# emitted "I didn't find anything specific", burned all rounds
# searching — while the chunks were available the whole time via a
# different code path (shown as citations in the final card but never
# used in the reasoning).
#
# Lowering to 0.3 admits abstain-labeled chunks as partial evidence.
# The planner can now synthesize from them. Guidance mode (rounds
# after ceil(0.8 * max_it)) shifts the planner from "hunt for the
# authoritative answer" to "produce a hedged answer from what we
# have" — abstain-grade evidence is exactly the input that mode was
# designed to work with. The critic keeps the resulting drafts
# grounded by flagging any claim not supported by the admitted
# chunks.
#
# The env var MOBIUS_REACT_CORPUS_CONFIDENCE_MIN lets operators tune
# without a code change since we expect to iterate on this knob.
# Clamped to [0.0, 1.0]; malformed values fall back to the default.

_CORPUS_CONFIDENCE_MIN_DEFAULT = 0.3


def _corpus_confidence_min(chat_mode: str | None = None) -> float:
    """Resolve the confidence_min used by react_loop's search_corpus call.

    Reads MOBIUS_REACT_CORPUS_CONFIDENCE_MIN at call time (not module
    load) so tests can monkeypatch the env var and production changes
    don't need a worker restart. Invalid values fall back to the
    default silently — this is a tuning knob, not an invariant.

    Fast/quick mode uses a lower threshold (0.1 default, overridable via
    MOBIUS_REACT_CORPUS_CONFIDENCE_MIN_QUICK) so it accepts more chunks
    on the first retrieval pass rather than returning empty-handed.
    """
    import math

    is_quick = react_chat_mode_label(chat_mode) == "quick"
    env_key = "MOBIUS_REACT_CORPUS_CONFIDENCE_MIN_QUICK" if is_quick else "MOBIUS_REACT_CORPUS_CONFIDENCE_MIN"
    default = 0.1 if is_quick else _CORPUS_CONFIDENCE_MIN_DEFAULT

    raw = (os.environ.get(env_key) or "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    if not math.isfinite(v):
        return default
    return max(0.0, min(1.0, v))


# ---------------------------------------------------------------------------
# Query strategy classifier
# ---------------------------------------------------------------------------
#
# Lightweight rule-based classifier that selects the retrieval arm for
# auto mode.  Runs in <1ms — no LLM call. The three arms:
#
#   precision  BM25-only.   Best for exact codes, IDs, verbatim phrases.
#              Fires when the query contains a clinical code (HCPCS, CPT,
#              ICD-10, revenue code, NDC).
#
#   recall     vector-only. Best for conceptual / paraphrased questions.
#              Fires when the query is clearly explanatory ("explain",
#              "criteria for", "overview of", "what does X mean").
#
#   corpus     hybrid RRF.  Default when neither signal is strong.
#              Runs both arms in parallel and fuses via reciprocal rank.
#
# The classifier feeds strategy_selected so the thinking-chain and the
# retrieval panel show WHAT was chosen and WHY, not just "auto".

import re as _re

_HCPCS_RE  = _re.compile(r'\b[A-Z]\d{4}\b')                    # H0036, T1017
_CPT_RE    = _re.compile(r'\b\d{4}[A-Z0-9]\b')                 # 90837, 99213
_ICD10_RE  = _re.compile(r'\b[A-Z]\d{2}(?:\.\d+)?\b')          # F32.1, Z23
_REV_RE    = _re.compile(r'\brevenue\s+code\s+\d{3,4}\b', _re.I)
_NDC_RE    = _re.compile(r'\b\d{11}\b|\bndc\b', _re.I)

_CONCEPTUAL_PHRASES = (
    "what does", "what is the", "what are the",
    "explain", "describe", "overview", "criteria for",
    "difference between", "how does", "why does", "philosophy",
    "general policy", "guidance on", "approach to",
    "what counts as", "definition of", "meaning of",
)


def _classify_query_strategy(query: str) -> tuple[str, str]:
    """Return (mode, reason) for a search_corpus call.

    mode is one of: ``precision`` | ``recall`` | ``corpus`` (hybrid).
    reason is a short phrase shown in strategy_selected.note.
    """
    q_lower = query.lower()

    # Code-heavy → precision (BM25 handles exact tokens best)
    if _HCPCS_RE.search(query):
        return "precision", "HCPCS code in query"
    if _CPT_RE.search(query):
        return "precision", "CPT code in query"
    if _ICD10_RE.search(query):
        return "precision", "ICD-10 code in query"
    if _REV_RE.search(query):
        return "precision", "revenue code in query"
    if _NDC_RE.search(query):
        return "precision", "NDC/drug code in query"

    # Conceptual phrasing → recall (vector handles semantics better)
    if any(phrase in q_lower for phrase in _CONCEPTUAL_PHRASES):
        return "recall", "conceptual question"

    # Default → hybrid
    return "corpus", "mixed query"


# ---------------------------------------------------------------------------
# Tool executor (skeleton: search_corpus only)
# ---------------------------------------------------------------------------

# When tools use generate_sync / provider.generate_with_usage, stage may be missing — map for LLM performance UI.
# 2026-04-18 disconnect — removed seven credentialing/roster entries
# (lookup_npi, run_credentialing_report, validate_credentialing_step,
# run_roster_reconciliation_report, ask_credentialing_npi,
# find_org_locations, find_associated_providers_at_locations) because the
# underlying tool branches are gone.
_TOOL_STAGE_FOR_USAGE: dict[str, str] = {
    "rag": "rag",              # promoted planner-facing name (2026-07-14)
    "search_corpus": "rag",
    # Day 6 (2026-04-20): lazy_corpus_search shares the ``rag`` stage for
    # analytics so it appears alongside the heavy corpus_search in
    # llm_calls breakdowns, but with its own tool name so dashboards
    # can separate fast vs heavy retrieval paths.
    "lazy_corpus_search": "rag",
    # Sprint 2 #0.2 (2026-04-24): retrieval-mode taxonomy. recall and
    # precision share the rag analytics stage so we can compare hit
    # rates per mode. Aliases are normalized in _normalize_tool_name.
    # The prompt-facing name is now ``explore_search`` (2026-05-01),
    # but the canonical key in this map stays ``recall_search`` so
    # existing analytics aggregations keep working.
    "recall_search": "rag",
    "precision_search": "rag",
    # 2026-04-25: fetch_document is metadata-only — no LLM stage,
    # but we register it under 'rag' analytics for dashboards that
    # bucket all corpus-touching skills together.
    "fetch_document": "rag",
    "google_search": "web_search",
    "web_scrape": "web_scrape",
    "healthcare_query": "healthcare_query",
    "healthcare_npi_lookup": "healthcare_query",
    "document_upload_skill": "document_upload",
    "list_thread_document_uploads": "document_upload",
    # Phase B.1: instant-RAG — search scoped to an uploaded document.
    "search_uploaded_document": "rag",
}


def _resolve_upload_document_id(active: dict, upload_id: str) -> str | None:
    """Phase B.1 helper — resolve an ``upload_id`` to the stored ``document_id``.

    Reads ``active.uploaded_files[]`` (populated on upload by
    ``_handle_instant_rag_upload`` in main.py). Returns the first record's
    ``document_id`` matching ``upload_id`` with a non-empty ``document_id``.
    Uploads without a ``document_id`` (e.g. roster-reconciliation files with
    no searchable chunks) are silently skipped.

    Returns None if no match; the caller converts that to a failed
    tool_result so the retry guard records it and the planner can pivot.
    """
    if not upload_id:
        return None
    files = active.get("uploaded_files") or []
    for u in files:
        if not isinstance(u, dict):
            continue
        if str(u.get("upload_id") or "") != upload_id:
            continue
        doc_id = str(u.get("document_id") or "").strip()
        if doc_id:
            return doc_id
    return None


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


# ── Tool-name aliasing (Sprint 2 #0.2, 2026-04-24) ──────────────────
#
# The retrieval taxonomy was renamed to make planner intent explicit:
#
#   search_corpus   — hybrid BM25 ⊕ vector (default).
#   recall_search   — vector-only broad recall  (was lazy_corpus_search).
#   precision_search — BM25-only exact-phrase   (new).
#
# We accept human-friendly aliases the planner / ReAct may emit since
# the manifest documents both canonical names AND aliases. Normalize
# at the dispatch boundary so every code path downstream sees the
# canonical name.
#
# Adding an alias: append to the appropriate set below. Document it in
# tool_manifest.py too so the planner sees it in the prompt.

_TOOL_ALIASES: dict[str, str] = {
    # search_corpus aliases (hybrid is the default — many ways to ask for it)
    "corpus":                "search_corpus",
    "corpus_search":         "search_corpus",
    "default_search":        "search_corpus",
    "hybrid_search":         "search_corpus",
    "hybrid":                "search_corpus",

    # vector-only broad-recall aliases. Canonical name remains
    # ``recall_search`` for code-path stability (dispatcher at
    # line ~660 checks ``tool == "recall_search"``); the prompt-
    # facing name is now ``explore_search`` (2026-05-01) — the
    # word "recall" was ambiguous to LLMs (read as "the tool for
    # empty returns" rather than "the broader-semantic-net tool").
    # "explore" is the everyday-English signal for "scan widely,
    # find what's out there."
    "explore_search":        "recall_search",   # new prompt-facing name
    "explore":               "recall_search",
    "lazy_corpus_search":    "recall_search",   # back-compat: oldest name
    "broad":                 "recall_search",
    "broad_search":          "recall_search",
    "vector_search":         "recall_search",
    "semantic_search":       "recall_search",

    # precision_search aliases (BM25-only)
    "exact":                 "precision_search",
    "exact_match":           "precision_search",
    "keyword_search":        "precision_search",
    "bm25_search":           "precision_search",
    "bm25":                  "precision_search",
    "lookup":                "precision_search",
}


def _normalize_tool_name(tool: str) -> str:
    """Canonicalize a planner-emitted tool name.

    Returns the canonical name when ``tool`` is a known alias; passes
    through unchanged otherwise. Case- and whitespace-tolerant.
    """
    if not isinstance(tool, str):
        return tool
    key = tool.strip().lower()
    return _TOOL_ALIASES.get(key, tool)


# Tools that write/mutate state. In "manual" autonomy mode the user
# wants to be guided, not have the assistant act on their behalf.
_SENSITIVE_TOOLS: frozenset[str] = frozenset({
    "create_task",
    "resolve_task",
    "patch_task",
})


# ── citable_required passthrough (2026-08-05) ────────────────────────
#
# RAG's corpus_search_agent contract gained a real citable_required param
# (Retriever, Task: tool-manifest swap prep) -- previously dead, no caller
# could set it. Chat Architecture's ruling: chat decides this, RAG's own
# authority_requirement/allocator_override stay off chat's request body
# entirely (those are Router-internal gates chat must not duplicate).
#
# Deterministic keyword rule, not new inference -- queries that inherently
# demand verifiable sources (payor policy, prior auth, coverage, claims)
# set citable_required=True; everything else omits the key and lets the
# Router decide. Checked concretely (not assumed) that no existing planner
# signal already classifies this -- the only pre-dispatch lexicon data on
# ctx is jurisdiction/payer-identity tagging (state_extractor.py), not
# topical intent. Real-time query tagging from RAG's Lexicon would be the
# better long-term source; noted as a future improvement, not built here.
_CITABLE_TERMS = (
    "prior auth", "prior authorization", "authorization", "pre-authorization",
    "coverage", "covered", "covers", "benefit", "payor", "payer", "insurance",
    "claim", "denial", "policy", "medical necessity", "formulary",
)


def _citable_required(query: str) -> bool:
    q = (query or "").lower()
    return any(term in q for term in _CITABLE_TERMS)


# ── clarify_questions false-positive guard (2026-08-08, Chat Master directive) ──
#
# Retriever's routing_keys.clarify_questions occasionally fires CLARIFY on a
# specific, well-formed query -- a classifier false positive, not a genuine
# ambiguity. Two independent filters before the terminal bypass is allowed to
# fire; both must pass:
#
# 1. Specificity: the clarify text must actually NAME the ambiguity (a payer,
#    a code, a date, a plan) -- generic boilerplate like "Could you clarify
#    what topic this relates to?" names nothing and is itself the tell that
#    the classifier failed, not a real signal to relay to the user.
# 2. Context resolution: if chat's own carried-forward jurisdiction (state/
#    payor, from ctx.merged_state["active"]) already appears in the query
#    text, whatever RAG thinks is ambiguous is something chat already knows
#    -- don't stop and ask the user something chat can already answer for
#    itself.
#
# Deliberately keyword/substring heuristics, not new inference -- same
# "checked concretely, no existing signal already does this" bar as
# _citable_required above.
_GENERIC_CLARIFY_PATTERNS = (
    "clarify what topic",
    "clarify your question",
    "what topic this relates",
    "more details",
    "more information",
    "what you mean",
    "what you're asking",
    "what you are asking",
    "please specify",
    "please clarify",
    "could you clarify",
    "can you clarify",
    "what would you like to know",
    "rephrase your question",
)

_CLARIFY_SPECIFICITY_TERMS = (
    "payer", "payor", "plan", "code", "date", "service", "provider",
    "member", "policy", "authorization", "form", "diagnosis", "cpt",
    "hcpcs", "icd", "state", "program", "carrier", "network",
)


def _is_specific_clarify_question(question: str) -> bool:
    """A real clarify signal names the actual ambiguity; a classifier
    false-positive is generic boilerplate. Reject if it matches a known
    generic template, or has no domain-specific term AND no digit/proper-
    noun-shaped token to anchor it to something concrete."""
    q = (question or "").strip()
    if not q:
        return False
    ql = q.lower()
    if any(pat in ql for pat in _GENERIC_CLARIFY_PATTERNS):
        return False
    if any(term in ql for term in _CLARIFY_SPECIFICITY_TERMS):
        return True
    if any(ch.isdigit() for ch in q):
        return True
    # A capitalized multi-word token run (e.g. a payer/org name) mid-sentence
    # is a reasonable proxy for "names something specific" without a full NER pass.
    words = q.split()
    return any(w[0].isupper() for w in words[1:] if w and w[0].isalpha())


def _clarify_context_already_resolves(query: str, active: dict | None) -> bool:
    """True when chat's own carried-forward jurisdiction (state/payor) is
    already present in the query text -- RAG's clarify signal is asking
    about something chat already has an answer for, so it should be
    advisory, not terminal."""
    if not active:
        return False
    try:
        from app.state.jurisdiction import get_jurisdiction_from_active
        jurisdiction = get_jurisdiction_from_active(active)
    except Exception:
        return False
    ql = (query or "").lower()
    known_terms = [
        str(t).strip() for t in (jurisdiction.get("state"), jurisdiction.get("payor"))
        if t and str(t).strip()
    ]
    return any(term.lower() in ql for term in known_terms)


def _should_bypass_on_clarify(
    clarify_questions: list[str],
    query: str,
    active: dict | None,
) -> bool:
    """Both filters must pass for the terminal clarify bypass to fire.
    False means: treat clarify_questions as advisory -- continue the
    search rather than stopping the loop."""
    if not clarify_questions:
        return False
    if not any(_is_specific_clarify_question(q) for q in clarify_questions):
        return False
    if _clarify_context_already_resolves(query, active):
        return False
    return True


# ── Dynamic enrichment sufficiency (Task #76, 2026-08-08, Chat Master ruling) ──
# Whether react's own answer is already good enough that the integrator can
# skip Call A's LLM call entirely and structure react_draft deterministically
# (see app.responder.deterministic_format). Built entirely from ctx fields
# ReAct already sets every turn -- no new ReAct instrumentation needed.
def _is_sufficient_for_deterministic_pass(ctx: PipelineContext) -> bool:
    """True when react's answer needs no further LLM enhancement:
    - quick-mode round-1 early exit (the existing fast-mode-exit path), OR
    - a short, clean run: <=3 rounds, no open gaps on the last round, no
      unfinished-reason flag, and a substantive (>=200 char) react_draft.
    Approved formula (Chat Master, Task #76) -- do not loosen without a new
    ruling, this gates whether an LLM call runs at all."""
    chat_mode = getattr(ctx, "chat_mode", None)
    rounds_used = getattr(ctx, "react_rounds_used", None) or 0

    if chat_mode == "quick" and rounds_used == 1:
        return True

    if getattr(ctx, "react_unfinished_reason", None) is not None:
        return False
    if rounds_used > 3:
        return False

    trace_rounds = getattr(ctx, "react_trace_rounds", None) or []
    if trace_rounds:
        last = trace_rounds[-1]
        enr = last.get("enrichment") if isinstance(last, dict) else None
        gaps_open = (enr or {}).get("gaps_open") or []
        if gaps_open:
            return False

    react_draft = getattr(ctx, "react_draft", None) or ""
    if len(react_draft.strip()) < 200:
        return False

    return True


def _compute_baseline_citable_and_relax_eligible(
    rag_history: list[dict],
    rag_call_number: int,
    force_citable_required: bool | None,
    query: str,
) -> tuple[bool, bool]:
    """(baseline_citable, relax_eligible) for this rag call.

    2026-08-07 (Task #41(a) follow-up, "confirm from authoritative
    sources" CTA re-submit): force_citable_required overrides the
    keyword heuristic outright and ALSO disables relax-eligibility --
    the whole point of the button is a guarantee of citable-only
    sources for this turn; silently relaxing away from that on an
    empty result would defeat what the user explicitly asked for. An
    honest "nothing authoritative found" is the correct outcome here,
    not a quiet fallback to non-authoritative sources.

    Extracted as a pure function (2026-08-07) specifically so this
    decision is unit-testable without needing a full run_react()
    integration test that mocks deep enough to bypass it entirely."""
    baseline_citable = (
        force_citable_required if force_citable_required is not None
        else _citable_required(query)
    )
    prior_rag_call = rag_history[-1] if rag_history else None
    relax_eligible = bool(
        force_citable_required is None
        and rag_call_number == 2
        and baseline_citable
        and prior_rag_call is not None
        and prior_rag_call.get("citable_required")
        and prior_rag_call.get("n_chunks") == 0
    )
    return baseline_citable, relax_eligible


def _compute_gap_status(rag_call_history: list[dict]) -> str:
    """EvidenceLedger phase 1 (Task #48, Chat Architecture spec,
    2026-08-06) -- mechanical, code-only detection of whether the last
    two rag calls converged on the same internal strategy and outcome
    without closing anything. Subsumes Task #50: this IS the reframe
    signal now, replacing the old `if not success:` gate inside the rag
    dispatch block, which never fired when rag returned real-but-wrong
    chunks (confirmed live: Amerigroup case -- 3 rounds, identical
    dispatch_path/chosen_slot/status, success=True throughout since
    chunks were non-empty). No LLM inference here -- pure function over
    the same telemetry corpus_search.py already returns.

    "empty" is excluded from the stagnant check deliberately: two
    genuinely empty results in a row on citable_required=True is what
    the relax-then-reframe protocol (rule 1b) already handles as its own
    distinct path -- this function isn't meant to double-fire on that.
    """
    if len(rag_call_history) < 2:
        return "fresh" if not rag_call_history else "progressing"
    last = rag_call_history[-1]
    prev = rag_call_history[-2]
    stagnant = (
        last.get("dispatch_path") == prev.get("dispatch_path")
        and last.get("chosen_slot") == prev.get("chosen_slot")
        and last.get("status") not in (None, "empty")
    )
    return "stagnant" if stagnant else "progressing"


def _execute_tool(
    tool: str,
    inputs: dict,
    ctx: PipelineContext,
    emitter=None,
) -> dict:
    """Execute a tool and return standardized result dict."""
    # Normalize alias → canonical before any dispatch logic runs.
    tool = _normalize_tool_name(tool)
    active = (ctx.merged_state or {}).get("active") or {}

    # Pattern B — autonomy gate. "manual" means guide-only: the user
    # opted out of the assistant executing write operations on their behalf.
    # "confirm_first" and "automatic" proceed; confirm_first relies on the
    # LLM soft-ask injected via rendered_prompt (true confirmation round-
    # trip requires a UI change; wired as a future enhancement).
    if tool in _SENSITIVE_TOOLS:
        from app.pipeline.personalization import autonomy_for
        mode = autonomy_for(getattr(ctx, "user_profile", None), sensitive=True)
        if mode == "manual":
            result_text = (
                f"Your preferences are set to guide-only for actions like '{tool}'. "
                "I won't take this action for you — here's what to do instead: "
                "open the Tasks panel and make the change yourself, or update your "
                "autonomy preference if you'd like me to handle this automatically."
            )
            return {
                "tool": tool,
                "success": False,
                "result": result_text,
                "signal": "autonomy_blocked",
                "sources": [],
            }

    def emit(msg: str) -> None:
        if emitter and msg:
            emitter(str(msg).strip())

    def emit_signal(envelope) -> None:
        if emitter:
            emitter(envelope.to_dict())

    _rn = getattr(ctx, "react_rounds_used", None)

    if tool == "refuse":
        reason = inputs.get("reason", "PHI or clinical guidance")
        emit(f"⊘ {reason}")
        ctx.react_bypass_integrate = True
        ctx.final_message = reason
        ctx.plan = _make_react_plan(ctx)
        ctx.sources = []
        ctx.retrieval_signals = [RETRIEVAL_SIGNAL_NO_SOURCES]
        ctx.answer_set = {}
        return {
            "tool": "refuse",
            "success": False,
            "result": reason,
            "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
            "sources": [],
            "is_terminal": True,
        }

    # ── Evidence recall (2026-08-07, Ananth, directly) ─────────────────
    # Purely local — no HTTP, no rag-budget spend. Looks up chunks a
    # prior round's evidence_review set aside (see _store_evidence_memory
    # / _prune_kept_chunks above) instead of re-querying rag for
    # information already retrieved this turn.
    if tool == "recall_evidence":
        refs_in = inputs.get("refs") or []
        if not isinstance(refs_in, list):
            refs_in = [refs_in]
        refs = [str(r).strip() for r in refs_in if str(r).strip()]
        memory: list[dict] = getattr(ctx, "_evidence_memory", None) or []
        found, missing = [], []
        for ref in refs:
            rec = next((m for m in memory if f"{m['call']}.{m['chunk']}" == ref), None)
            if rec:
                found.append(f"[{ref}] {rec['header']}\n{rec['text']}")
            else:
                missing.append(ref)
        if found:
            result_text = "\n\n".join(found)
            if missing:
                result_text += f"\n\n[Not found: {missing} — refs are \"call.chunk\", e.g. \"2.4\".]"
            emit(f"✓ Recalled {len(found)} chunk(s) from evidence memory")
            return {"tool": "recall_evidence", "success": True, "result": result_text, "signal": None, "sources": []}
        emit("⊘ No matching chunks in evidence memory for the given refs")
        return {
            "tool": "recall_evidence",
            "success": False,
            "result": f"No chunks found for refs {refs}. Refs are \"call.chunk\" (e.g. \"2.4\") from an earlier evidence_review set-aside note.",
            "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
            "sources": [],
        }

    # ── Curator tools (Phase 13.5) ───────────────────────────────────
    # Surface URLs Mobius knows about even when not yet indexed; let the
    # planner offer to ingest one on demand.
    if tool == "lookup_authoritative_sources":
        from app.pipeline.curator_tools import call_lookup_authoritative_sources
        emit("◌ Searching curator registry for known sources…")
        _src_result = call_lookup_authoritative_sources(inputs)
        _src_count = len(_src_result.get("sources") or [])
        if _src_count:
            emit(f"✓ Found {_src_count} known source(s) in registry")
        else:
            emit("⊘ No matching sources in registry")
        return _src_result

    if tool == "ingest_url":
        from app.pipeline.curator_tools import call_ingest_url
        url = (inputs.get("url") or "").strip()
        if not url:
            emit("⊘ ingest_url called without a url")
            return {
                "tool": "ingest_url",
                "success": False,
                "result": "ingest_url requires a 'url' input",
                "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
                "sources": [],
            }
        emit(f"◌ Fetching + indexing {url}…")
        _ingest_result = call_ingest_url(inputs)
        if _ingest_result.get("success"):
            emit(f"✓ Ingested and indexed: {url}")
        else:
            emit(f"⊘ Ingest failed for {url}")
        return _ingest_result

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

    if tool in ("rag", "search_corpus"):
        # "rag" is the new planner-facing name; "search_corpus" is the back-compat alias.
        # Both dispatch to the same corpus-then-external cascade.
        query = inputs.get("query") or (ctx.effective_message or ctx.message)
        rag_overrides = rag_filters_from_active(active) or {}

        from app.communication.emit_envelope import (
            make_query_understood, make_strategy_selected,
            make_retrieval_complete, make_fallback_triggered,
        )
        # ── citable_required relax-then-reframe: 3-call bounded protocol ────
        # (2026-08-06, Chat Architecture spec, replacing the pre-cutover
        # mode="auto"→"d" escalation cascade.) That model assumed chat
        # itself needed to escalate corpus→external across calls with a
        # caller-supplied mode; post Task #36, rag's own router (Shape→
        # Pool→Router→Fillers) already runs a→b→c→d→s internally in ONE
        # call, and the current corpus_search.py never reads inputs["mode"]
        # or inputs["k"] at all (confirmed by grep) — passing them was dead
        # weight. The one real lever chat has left is citable_required:
        #   call 1: baseline citable_required (keyword rule).
        #   call 2 (ONLY if call 1 was citable_required=True and returned 0
        #     chunks): citable_required relaxed to False — not to answer
        #     from, purely to learn the correct terminology / the actual
        #     section that covers this, from non-citable sources.
        #   call 3 (ONLY after a real call-2 relax): citable_required
        #     restored, expects a materially reformulated query built from
        #     what call 2 surfaced.
        #   call 4+: HARD STOP below — "non-negotiable, no fallback to more
        #     grinding rounds" (Chat Architecture ruling, 2026-08-06). A 4th
        #     call would just repeat the same internal router escalation
        #     rag already ran on call 1 — see Retriever's design writeup
        #     (2026-08-06) for why blind retry past this point is zero-value.
        # See react/prompts.py rule 1b for the LLM-facing protocol.
        _rag_history: list[dict] = list(getattr(ctx, "_rag_call_history", []))
        _rag_call_number = len(_rag_history) + 1
        _force_citable = getattr(ctx, "force_citable_required", None)
        _baseline_citable, _relax_eligible = _compute_baseline_citable_and_relax_eligible(
            _rag_history, _rag_call_number, _force_citable, query,
        )
        _prior_rag_call = _rag_history[-1] if _rag_history else None
        _reframe_eligible = (
            _rag_call_number == 3
            and _prior_rag_call is not None
            and _prior_rag_call.get("rag_phase") == "relaxed"
        )

        if _rag_call_number >= 4:
            # Hard stop — do not dispatch a 4th network call. Return
            # immediately so the LLM gets an unambiguous terminal signal
            # instead of grinding another round for zero new information.
            emit("  ↓ rag call budget (3) exhausted for this question — answer honestly with what's been found.")
            return {
                "tool": "search_corpus",
                "success": False,
                "result": (
                    "[RAG_BUDGET_EXHAUSTED] 3 rag calls already made for this question "
                    "(initial, relaxed, reframed) — this is a genuine gap, not a phrasing "
                    "problem. Do NOT call rag again. Answer honestly per SHAPE 2 (labeled "
                    "full-miss, rule 1d) with what's been found so far, or use a different "
                    "tool if one genuinely applies."
                ),
                "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
                "sources": [],
                "usage": None,
                "rag_phase": "exhausted",
                "rag_call_number": _rag_call_number,
            }

        if _relax_eligible:
            _effective_citable = False
            _rag_phase = "relaxed"
        elif _reframe_eligible:
            _effective_citable = _baseline_citable
            _rag_phase = "reframed"
        else:
            _effective_citable = _baseline_citable
            _rag_phase = "auto" if _rag_call_number == 1 else "repeat"

        emit_signal(make_query_understood(
            ctx.correlation_id, query=query, intent_summary=query[:120],
            round=_rn, thread_id=ctx.thread_id,
        ))
        # Phase B.4 — parallel retrieval.
        #
        # When the thread has instant_rag uploads, the user's uploaded doc
        # IS policy for them. If the planner picks search_corpus, we fan
        # out a parallel lazy-RAG search against each upload (capped) so
        # the integrator gets BOTH curated-corpus chunks AND upload chunks
        # in one retrieval round.
        #
        # Why this matters (from the 2026-04-17 shakedown): the planner
        # correctly picked search_corpus for "what does Sunshine say about
        # H0036" even when the user had a Sunshine doc attached, because
        # the reasoning prompt favored the payer keyword. Fan-out means
        # ambiguous phrasing no longer forces a binary choice — the
        # integrator sees both pools and merges them. No extra planner
        # round, no retry-guard churn, just better evidence per turn.
        #
        # We deliberately do NOT fan out the other direction (from
        # search_uploaded_document → search_corpus). When the user says
        # "my doc" the intent is scoped; adding corpus noise would hurt.
        upload_candidates = [
            u for u in (active.get("uploaded_files") or [])
            if isinstance(u, dict)
            and str(u.get("document_id") or "").strip()
            and str(u.get("purpose") or "") != "roster_reconciliation"
        ]
        # Cap at 3 parallel upload searches per turn. Most threads have 1;
        # beyond 3 we start to dilute the integrator's context budget
        # faster than we add signal.
        upload_candidates = upload_candidates[:3]

        _strategy_reason = (
            f"and your attached doc{'s' if len(upload_candidates) > 1 else ''}"
            if upload_candidates else None
        )
        emit_signal(make_strategy_selected(
            ctx.correlation_id, mode=_rag_phase,
            reason=_strategy_reason,
            round=_rn, thread_id=ctx.thread_id,
        ))

        # Run all retrievals concurrently. ThreadPoolExecutor (not asyncio)
        # because answer_non_patient + lazy_rag_search are both sync and
        # asyncio integration across the stack is a separate project.
        import concurrent.futures as _cf
        from app.services.instant_rag_search import lazy_rag_search

        def _run_corpus() -> tuple[str, list[dict], dict | None, str, bool, dict]:
            # 2026-04-28 — corpus retrieval moved to mobius-rag's
            # corpus_search skill, fronted by the mobius-os gateway
            # at {OS_API_URL}/api/v1/skills/corpus_search per the
            # extraction plan (docs/CORPUS_RETRIEVAL_SKILL_EXTRACTION_
            # PLAN.md). Skill handler in app/skills/builtin/corpus_
            # search.py.
            #
            # Fallback policy: if the skill is unconfigured (OS_API_URL
            # unset) or its HTTP transport fails, fall back to the
            # legacy answer_non_patient path so chat keeps working
            # while mobius-os ramps up. The legacy path returns
            # single-arm pgvector via /api/query (the same code that
            # carried chat earlier today). We DON'T fall back when
            # the skill returned cleanly with zero chunks — that's a
            # legitimate "no corpus match" answer; falling back to
            # the same backend would just repeat the empty result.
            from app.skills.registry import SkillCall, dispatch
            try:
                env = dispatch(
                    SkillCall(
                        name="search_corpus",
                        inputs={
                            "query": query,
                            **({"include_document_ids": rag_overrides.get("include_document_ids")}
                               if rag_overrides.get("include_document_ids") else {}),
                            **({"citable_required": True} if _effective_citable else {}),
                        },
                        question=query,
                        user_message=ctx.message,
                        thread_id=ctx.thread_id,
                        active_context=active,
                        mode=getattr(ctx, "chat_mode", "copilot") or "copilot",
                        emitter=emitter,
                        pipeline_ctx=ctx,
                    )
                )
                # ``extra.error`` is set by the skill ONLY on transport /
                # config failures (os_api_url_unset, http_5xx, ConnectionError,
                # etc.). A clean "ran successfully, found nothing" result
                # has no error key.
                skill_error = (env.extra or {}).get("error") if env else None
                if skill_error:
                    logger.warning(
                        "search_corpus skill error=%r; falling back to "
                        "legacy answer_non_patient", skill_error,
                    )
                    raise RuntimeError(f"skill_error:{skill_error}")
            except Exception as _e:
                logger.warning(
                    "search_corpus skill unavailable (%s); falling back to "
                    "legacy retriever", _e,
                )
                return answer_non_patient(
                    question=query,
                    k=10,
                    confidence_min=_corpus_confidence_min(getattr(ctx, "chat_mode", None)),
                    emitter=emitter,
                    correlation_id=ctx.correlation_id,
                    subquestion_id="react_1",
                    rag_filter_overrides=rag_overrides,
                    thread_id=ctx.thread_id,
                    phi_detected=False,
                    config_sha=_get_config_sha() or None,
                    mode=getattr(ctx, "chat_mode", None),
                ) + (False, {})  # no golden, no telemetry on legacy fallback
            # Map SkillEnvelope → 6-tuple; 5th element carries golden_explicit so
            # the fact-store fast-exit (short certified facts, empty sources) can
            # bypass the >80-char success gate and the merged_signal override.
            # 6th element is the real pipeline_trace telemetry (status,
            # dispatch_path, chosen_slot, n_chunks, ...) — see corpus_search.py's
            # _run() for the exact shape. Used to build the rag call history and
            # the real (not fabricated) reframe signal below.
            sources_dicts = [s.to_dict() for s in env.sources]
            return (
                env.text or "",
                sources_dicts,
                env.usage,
                env.signal or "no_sources",
                bool((env.extra or {}).get("golden")),
                (env.extra or {}).get("pipeline_trace") or {},
            )

        def _run_upload(doc_id: str) -> tuple[str, list[dict], dict | None, str]:
            try:
                return lazy_rag_search(
                    document_id=doc_id, question=query, k=5, emitter=None,
                )
            except Exception as _e:
                # Don't let one upload's failure kill the corpus result.
                logger.warning(
                    "[B.4] parallel lazy_rag_search failed for doc=%s: %s",
                    doc_id, _e,
                )
                return ("", [], None, "no_sources")

        _workers = 1 + len(upload_candidates)
        with _cf.ThreadPoolExecutor(max_workers=_workers) as pool:
            corpus_future = pool.submit(_run_corpus)
            upload_futures = [
                (u, pool.submit(_run_upload, str(u.get("document_id"))))
                for u in upload_candidates
            ]

            # Corpus is the "primary" path — its failure is semantically
            # different from an upload miss. Materialize each result
            # independently so partial failure still returns something.
            try:
                corpus_answer, corpus_sources, corpus_usage, corpus_signal, _corpus_golden_explicit, _corpus_telemetry = corpus_future.result()
            except Exception as _e:
                logger.warning("[B.4] corpus search failed: %s", _e)
                corpus_answer, corpus_sources, corpus_usage, corpus_signal, _corpus_golden_explicit, _corpus_telemetry = (
                    "", [], None, "no_sources", False, {},
                )
            upload_results = [(u, f.result()) for u, f in upload_futures]

        # Merge: the integrator downstream doesn't care that two tools ran;
        # it wants a single result block with sources it can cite.
        merged_sources: list[dict] = list(corpus_sources or [])
        upload_chunks_total = 0
        fanned_out_to: list[str] = []
        upload_chunk_previews: list[str] = []  # short per-upload strings for the tool result
        for u, (u_answer, u_sources, _u_usage, u_signal) in upload_results:
            upload_chunks_total += len(u_sources or [])
            if u_sources:
                fanned_out_to.append(str(u.get("upload_id") or ""))
                merged_sources.extend(u_sources)
                # Distilled preview for the reasoning-context payload —
                # the integrator composes from sources[], but the planner
                # on the next round reads the result string.
                fname = str(u.get("filename") or "upload")
                head = (u_answer or "")[:600]
                upload_chunk_previews.append(
                    f"From attached doc '{fname}' ({len(u_sources)} chunks):\n{head}"
                )

        # Cap total chunks going downstream — 15 is a reasonable ceiling.
        # Preserve head-from-each (corpus + uploads) rather than truncate at
        # the tail which would drop all upload evidence.
        _MAX_MERGED = 15
        if len(merged_sources) > _MAX_MERGED:
            merged_sources = merged_sources[:_MAX_MERGED]

        # Build the result string. Corpus answer is the spine; upload
        # snippets are appended with clear separators so the integrator
        # can cite them distinctly.
        if upload_chunk_previews:
            merged_result = (corpus_answer or "") + "\n\n---\n\n" + "\n\n---\n\n".join(upload_chunk_previews)
            # User-facing: "found N passages from your document" is
            # clearer than "uploads: N chunks" — passages map to reading,
            # chunks map to engineering.
            emit(
                f"  ✓ found {upload_chunks_total} passage"
                f"{'s' if upload_chunks_total != 1 else ''} from your attached "
                f"doc{'s' if len(upload_candidates) > 1 else ''}"
            )
        else:
            merged_result = corpus_answer or ""

        # Success if EITHER path contributed usable evidence. Golden fast-exits
        # (fact-store strategy-s) may have short answers and empty corpus_sources
        # by design — bypass the >80-char gate for those.
        success = (
            bool(merged_result and len(merged_result.strip()) > 80 and corpus_signal != RETRIEVAL_SIGNAL_NO_SOURCES)
            or upload_chunks_total > 0
            or (_corpus_golden_explicit and bool(merged_result))
        )

        # Signal favors whichever path had hits — corpus_only when we got
        # anything; no_sources only when both pools returned empty. This
        # matches what the 0.19 retry guard expects for recording
        # success/failure on the (search_corpus, inputs) pair.
        # _corpus_golden_explicit: fact-store fast-exit has no chunks but IS a
        # valid corpus hit — don't let empty corpus_sources override the signal.
        if corpus_signal != RETRIEVAL_SIGNAL_NO_SOURCES and (corpus_sources or _corpus_golden_explicit):
            merged_signal = corpus_signal
        elif upload_chunks_total > 0:
            merged_signal = "corpus_only"  # keep shape; integrator treats it the same
        else:
            merged_signal = RETRIEVAL_SIGNAL_NO_SOURCES

        emit_signal(make_retrieval_complete(
            ctx.correlation_id,
            chunks_returned=len(merged_sources),
            tool="search_corpus",
            mode=_rag_phase,
            round=_rn,
            thread_id=ctx.thread_id,
        ))

        if not success:
            emit_signal(make_fallback_triggered(
                ctx.correlation_id,
                from_tool="search_corpus",
                to_tool="google_search",
                reason="corpus returned no usable evidence",
                round=_rn,
                thread_id=ctx.thread_id,
            ))

        # ── Real reframe signal (2026-08-06) ─────────────────────────────
        # Replaces the old improvement_hint/term_partition/fast_exit block,
        # which was dead two ways: `"env" in dir()` never held (env is a
        # local inside the _run_corpus closure above, invisible in this
        # scope), and even if it had, those keys don't exist in the new
        # reduced telemetry dict (corpus_search.py's Phase 1 cutover
        # shrank the old ~15-field pipeline_trace to ~10 real fields).
        # Built from _corpus_telemetry instead, which IS real (threaded
        # through _run_corpus's return above). Also records this call's
        # outcome in ctx._rag_call_history so the next round's relax/
        # reframe decision, and the LLM's own view of it in
        # react/prompts.py's reasoning context, have real signal instead
        # of the old fictional arms-tried narrative.
        _status = _corpus_telemetry.get("status")
        _dispatch_path = _corpus_telemetry.get("dispatch_path")
        _chosen_slot = _corpus_telemetry.get("chosen_slot")
        _n_chunks = _corpus_telemetry.get("n_chunks", len(corpus_sources or []))
        _hit_fact_store = any(
            s.get("filler_strategy") == "fact_store" for s in (corpus_sources or [])
        )

        # clarify_questions terminal signal (2026-08-08, Chat Master directive):
        # Retriever's routing_keys.clarify_questions is populated when its own
        # posture classification is CLARIFY/CLARIFY_REPHRASE -- the corpus
        # genuinely cannot answer without the user disambiguating first. This is
        # a DIFFERENT signal from "nothing found" (status=no_retrieval with an
        # empty clarify_questions still goes through the normal relax/reframe
        # protocol below) -- more searching won't close a disambiguation gap, so
        # treat it as terminal exactly like the "refuse" tool above: stop the
        # loop immediately (react_bypass_integrate skips the integrator entirely
        # -- orchestrator.py emits ctx.final_message as a plain message, no
        # answer-card chrome), don't fall through to relax/reframe or the
        # google_search fallback.
        #
        # 2026-08-08 follow-up (Chat Master, false-positive guard): the RAG
        # classifier occasionally fires CLARIFY on a specific, well-formed
        # query. _should_bypass_on_clarify gates the terminal stop on two
        # checks (see its docstring): the clarify text must actually name the
        # ambiguity, AND chat's own carried-forward jurisdiction must not
        # already resolve it. When either check fails, treat clarify_questions
        # as advisory -- attempt ONE immediate fallback search with
        # citable_required forced off rather than stopping the turn on what's
        # likely a classifier miss.
        _clarify_questions = _corpus_telemetry.get("clarify_questions") or []
        if _status == "no_retrieval" and _clarify_questions:
            if _should_bypass_on_clarify(_clarify_questions, query, active):
                _clarify_text = (
                    _clarify_questions[0] if len(_clarify_questions) == 1
                    else "\n".join(f"- {q}" for q in _clarify_questions)
                )
                emit(f"  ↓ Needs clarification: {_clarify_text}")
                ctx.react_bypass_integrate = True
                ctx.final_message = _clarify_text
                ctx.plan = _make_react_plan(ctx)
                ctx.sources = []
                ctx.retrieval_signals = [RETRIEVAL_SIGNAL_NO_SOURCES]
                ctx.answer_set = {}
                return {
                    "tool": "search_corpus",
                    "success": False,
                    "result": _clarify_text,
                    "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
                    "sources": [],
                    "clarify_questions": _clarify_questions,
                }

            emit("  ↓ clarify signal looked advisory — retrying with citable_required off…")
            try:
                from app.skills.registry import SkillCall, dispatch as _skill_dispatch
                _fallback_env = _skill_dispatch(
                    SkillCall(
                        name="search_corpus",
                        inputs={
                            "query": query,
                            **({"include_document_ids": rag_overrides.get("include_document_ids")}
                               if rag_overrides.get("include_document_ids") else {}),
                            # citable_required deliberately omitted -- False.
                        },
                        question=query,
                        user_message=ctx.message,
                        thread_id=ctx.thread_id,
                        active_context=active,
                        mode=getattr(ctx, "chat_mode", "copilot") or "copilot",
                        emitter=emitter,
                        pipeline_ctx=ctx,
                    )
                )
                _fallback_sources = [s.to_dict() for s in (_fallback_env.sources or [])]
                if _fallback_sources or (_fallback_env.text or "").strip():
                    corpus_answer = _fallback_env.text or ""
                    corpus_sources = _fallback_sources
                    corpus_signal = _fallback_env.signal or corpus_signal
                    _corpus_telemetry = (_fallback_env.extra or {}).get("pipeline_trace") or _corpus_telemetry
                    _status = _corpus_telemetry.get("status")
                    _dispatch_path = _corpus_telemetry.get("dispatch_path")
                    _chosen_slot = _corpus_telemetry.get("chosen_slot")
                    _n_chunks = _corpus_telemetry.get("n_chunks", len(corpus_sources or []))
                    merged_sources = list(corpus_sources or [])
                    merged_result = corpus_answer or ""
                    success = bool(merged_result and len(merged_result.strip()) > 80)
                    merged_signal = corpus_signal
            except Exception as _e:
                logger.warning("[clarify-fallback] non-citable retry failed: %s", _e)
            # Fall through to the normal success/reframe handling below with
            # whichever result (fallback or original empty) is now current --
            # no second bypass, no infinite retry, exactly one fallback attempt.

        ctx._rag_call_history = _rag_history + [{  # type: ignore[attr-defined]
            "citable_required": _effective_citable,
            "n_chunks": _n_chunks,
            "status": _status,
            "dispatch_path": _dispatch_path,
            "chosen_slot": _chosen_slot,
            "rag_phase": _rag_phase,
            "call_number": _rag_call_number,
            # 2026-08-07 (Task #41(a)): RAG's own Observer verdict/reason
            # for the chosen slot -- e.g. verdict="SATISFIED",
            # reason="vector_filled_to_capacity". More authoritative than
            # the dispatch_path/chosen_slot/status equality heuristic
            # gap_status uses -- surfaced in [Evidence Ledger] below.
            "observer_final_reason": _corpus_telemetry.get("observer_final_reason"),
            "observer_final_verdict": _corpus_telemetry.get("observer_final_verdict"),
        }]

        if not success:
            _reframe_lines: list[str] = [
                f"RAG signal (call {_rag_call_number}/3): status={_status or 'unknown'}, "
                f"citable_required={_effective_citable}, chunks={_n_chunks}, "
                f"dispatch_path={_dispatch_path or 'n/a'}, chosen_slot={_chosen_slot or 'n/a'}."
            ]
            if _rag_phase == "relaxed":
                _reframe_lines.append(
                    "This was call 2 — RELAXED (citable_required off) after call 1 (citable-required) "
                    "came back empty. This call is for terminology/context acquisition, not to answer "
                    "from directly. Use whatever it surfaced, even non-citable, to build call 3's query "
                    "(rule 1b)."
                )
            elif _rag_phase == "reframed":
                _reframe_lines.append(
                    "This was call 3 — REFRAMED (citable_required back on) using what call 2 taught you. "
                    "If still empty, this is a genuine citable-corpus gap, not a phrasing problem — this "
                    "was your last rag call for this question. Answer honestly now (rule 1d, SHAPE 2)."
                )
            elif _prior_rag_call is not None:
                _reframe_lines.append(
                    "Router already ran its own internal strategy escalation inside this one call — "
                    "re-calling rag with the same query will not surface new information. Only retry with "
                    "a query that changes the actual matched terms (rule 1b)."
                )
            merged_result = (merged_result or "") + "\n\n[Retrieval signal for reframe]\n" + "\n".join(_reframe_lines)

        return {
            "tool": "search_corpus",  # keep tool name stable for retry-guard + observability
            "success": success,
            "result": merged_result,
            "signal": merged_signal,
            "sources": merged_sources,
            "golden": _corpus_golden_explicit,
            "golden_explicit": _corpus_golden_explicit,
            "usage": corpus_usage,  # upload side makes no LLM calls (Phase B.1 design)
            # Real per-call telemetry (2026-08-06) — replaces the dead
            # improvement_hint/fast_exit fields (confirmed by grep: no
            # external caller ever read them).
            "rag_phase": _rag_phase,
            "rag_call_number": _rag_call_number,
            "citable_required_used": _effective_citable,
            "status": _status,
            "dispatch_path": _dispatch_path,
            "chosen_slot": _chosen_slot,
            "n_chunks": _n_chunks,
            "hit_fact_store": _hit_fact_store,
            # Phase B.4 observability — downstream code can inspect this to
            # know whether fan-out happened, and the logs name the upload_ids.
            "fanned_out_to": fanned_out_to,
            "upload_chunks_total": upload_chunks_total,
        }

    if tool == "recall_search":
        # Thin alias — routes to the mobius-rag corpus_search skill with
        # mode="recall" (vector-only arm). Replaced the old
        # retrieve_for_chat → retriever_backend → ChromaDB path.
        query = inputs.get("query") or (ctx.effective_message or ctx.message)
        from app.communication.emit_envelope import (
            make_query_understood, make_strategy_selected, make_retrieval_complete,
        )
        emit_signal(make_query_understood(
            ctx.correlation_id, query=query, intent_summary=query[:120],
            round=_rn, thread_id=ctx.thread_id,
        ))
        emit_signal(make_strategy_selected(
            ctx.correlation_id, mode="recall",
            round=_rn, thread_id=ctx.thread_id,
        ))
        from app.skills.registry import SkillCall, dispatch
        try:
            env = dispatch(
                SkillCall(
                    name="search_corpus",
                    inputs={"query": query, "mode": "recall", "k": 16},
                    question=query,
                    user_message=ctx.message,
                    thread_id=ctx.thread_id,
                    active_context=active,
                    mode=getattr(ctx, "chat_mode", "copilot") or "copilot",
                    emitter=emitter,
                    pipeline_ctx=ctx,
                )
            )
            skill_error = (env.extra or {}).get("error") if env else None
            if skill_error:
                raise RuntimeError(f"skill_error:{skill_error}")
        except Exception as exc:
            logger.warning("recall_search skill failed: %s", exc, exc_info=True)
            return {
                "tool": "recall_search",
                "success": False,
                "result": f"Recall search failed: {exc}",
                "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
                "sources": [],
            }
        sources_out = [s.to_dict() for s in env.sources]
        success = bool(sources_out)
        emit_signal(make_retrieval_complete(
            ctx.correlation_id,
            chunks_returned=len(sources_out),
            tool="recall_search",
            mode="recall",
            round=_rn,
            thread_id=ctx.thread_id,
        ))
        if not success:
            emit("\u2193 Recall scan found nothing matching this query.")
        return {
            "tool": "recall_search",
            "success": success,
            "result": env.text or f"Found {len(sources_out)} chunks via vector recall.",
            "signal": env.signal or RETRIEVAL_SIGNAL_NO_SOURCES,
            "sources": sources_out,
            "usage": env.usage,
        }

    if tool == "precision_search":
        # Thin alias — routes to the mobius-rag corpus_search skill with
        # mode="precision" (BM25-only arm). Replaced the old
        # retrieve_for_chat → retriever_backend → BM25 path.
        query = inputs.get("query") or (ctx.effective_message or ctx.message)
        from app.communication.emit_envelope import (
            make_query_understood, make_strategy_selected, make_retrieval_complete,
        )
        emit_signal(make_query_understood(
            ctx.correlation_id, query=query, intent_summary=query[:120],
            round=_rn, thread_id=ctx.thread_id,
        ))
        emit_signal(make_strategy_selected(
            ctx.correlation_id, mode="precision",
            round=_rn, thread_id=ctx.thread_id,
        ))
        from app.skills.registry import SkillCall, dispatch
        try:
            env = dispatch(
                SkillCall(
                    name="search_corpus",
                    inputs={"query": query, "mode": "precision", "k": 10},
                    question=query,
                    user_message=ctx.message,
                    thread_id=ctx.thread_id,
                    active_context=active,
                    mode=getattr(ctx, "chat_mode", "copilot") or "copilot",
                    emitter=emitter,
                    pipeline_ctx=ctx,
                )
            )
            skill_error = (env.extra or {}).get("error") if env else None
            if skill_error:
                raise RuntimeError(f"skill_error:{skill_error}")
        except Exception as exc:
            logger.warning("precision_search skill failed: %s", exc, exc_info=True)
            return {
                "tool": "precision_search",
                "success": False,
                "result": f"Precision search failed: {exc}",
                "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
                "sources": [],
            }
        sources_out = [s.to_dict() for s in env.sources]
        success = bool(sources_out)
        emit_signal(make_retrieval_complete(
            ctx.correlation_id,
            chunks_returned=len(sources_out),
            tool="precision_search",
            mode="precision",
            round=_rn,
            thread_id=ctx.thread_id,
        ))
        if not success:
            emit("\u2193 Precision search found no exact-phrase matches.")
        return {
            "tool": "precision_search",
            "success": success,
            "result": env.text or f"Found {len(sources_out)} exact-match chunks.",
            "signal": env.signal or RETRIEVAL_SIGNAL_NO_SOURCES,
            "sources": sources_out,
            "usage": env.usage,
        }

    if tool == "search_uploaded_document":
        # Phase B.1 — Instant RAG query tool.
        #
        # The ingest side (upload → extract → chunk → embed → store in
        # published_rag_metadata) already exists: main.py:387 _handle_instant_rag_upload
        # proxies to the Instant RAG skill, and its chat_rag consumer writes
        # the chunks into the same table the main corpus uses. Those chunks
        # are searchable via the retriever's ``include_document_ids`` filter.
        #
        # This tool scopes a RAG query to a SINGLE uploaded document so the
        # reasoner can answer questions like "what does the doc I just
        # uploaded say about X" without mixing in stale corpus chunks.
        #
        # Input contract:
        #   upload_id: the ``upload_id`` from active.uploaded_files[] (same
        #              id surfaced to the UI). Resolves to document_id.
        #   query:     free-text question.
        #
        # If upload_id is missing or doesn't resolve to a document_id (e.g.
        # the user passed a roster-reconciliation upload_id, which has no
        # searchable chunks), return success=False with a hint so the
        # planner can pivot.
        upload_id = (inputs.get("upload_id") or "").strip()
        query = inputs.get("query") or (ctx.effective_message or ctx.message)

        # Snapshot what the thread actually has so diagnostic logging + the
        # failure message can show the real state (not just "no match").
        # 2026-04-17 debug showed the planner correctly picking this tool
        # but the lookup failing silently with no way to tell why.
        _all_files = [
            u for u in (active.get("uploaded_files") or [])
            if isinstance(u, dict)
        ]
        _file_summary = [
            {
                "upload_id": str(f.get("upload_id") or ""),
                "filename":  str(f.get("filename") or ""),
                "purpose":   str(f.get("purpose") or ""),
                "document_id": str(f.get("document_id") or ""),
            }
            for f in _all_files
        ]
        logger.info(
            "[instant-rag] dispatch: input upload_id=%r, %d files on thread: %s",
            upload_id, len(_file_summary), _file_summary,
        )

        if not upload_id:
            # Fall-through: if exactly one record has a usable document_id,
            # use it. Loosened from "purpose==instant_rag AND document_id"
            # to just "document_id is set" — some records written before
            # the Phase 0.17/B.1 persistence fixes may have missing/empty
            # purpose but still have a valid document_id that works.
            # Strictly filtering on purpose silently excluded them.
            candidates = [
                u for u in _all_files
                if str(u.get("document_id") or "").strip()
                and str(u.get("purpose") or "") != "roster_reconciliation"
            ]
            if len(candidates) == 1:
                upload_id = str(candidates[0].get("upload_id") or "")
                logger.info(
                    "[instant-rag] auto-resolved upload_id=%r from single candidate.",
                    upload_id,
                )
            elif len(candidates) > 1:
                logger.info(
                    "[instant-rag] multiple candidates (%d); planner must pass upload_id.",
                    len(candidates),
                )

        document_id = _resolve_upload_document_id(active, upload_id)
        if not document_id:
            # Build a specific failure message that tells the planner (and
            # us in logs) exactly why this failed. Silent "no match" forced
            # a live debugging session on 2026-04-17.
            available = [
                f"{f['filename']} (upload_id={f['upload_id']}, has_doc_id={bool(f['document_id'])})"
                for f in _file_summary
                if f["filename"] or f["upload_id"]
            ]
            if not _file_summary:
                why = "No uploads on this thread."
            elif not upload_id:
                why = (
                    "No upload_id provided and auto-resolution didn't pick one "
                    f"(found {len(_file_summary)} uploads, but {'none' if not available else 'multiple'} "
                    f"were usable). Available: {available}."
                )
            else:
                matching = [f for f in _file_summary if f["upload_id"] == upload_id]
                if not matching:
                    why = f"upload_id={upload_id!r} not found in thread. Available: {available}."
                elif not matching[0]["document_id"]:
                    why = (
                        f"upload_id={upload_id!r} matches {matching[0]['filename']!r} but its "
                        f"document_id is empty — the upload likely failed mid-ingest. "
                        f"Re-upload the file or use list_thread_document_uploads to see state."
                    )
                else:
                    why = f"upload_id={upload_id!r} matched but document_id lookup returned empty."
            logger.warning("[instant-rag] resolution failed: %s", why)
            emit(f"  ⊘ search_uploaded_document: {why[:140]}")
            return {
                "tool": "search_uploaded_document",
                "success": False,
                "result": (
                    f"Cannot search uploaded document. {why} "
                    "Use list_thread_document_uploads to see what's available, "
                    "or pick a different tool."
                ),
                "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
                "sources": [],
            }

        emit(f"◌ Reading your attached document: {(query or '')[:60]}…")
        # §5b wait-or-defer: pgvector probe poll.
        # Gate readiness via rag_published_embeddings (pgvector, ready ~9s warm /
        # ~17s cold measured). Do NOT use instant_rag_uploads.chunks_count or
        # published_rag_metadata — both reflect the slow chat_pg sync (~130s).
        # Strategy: initial probe first (no sleep). If empty, poll for up to
        # _INDEXING_POLL_MAX_S regardless of any catalog hint — the catalog
        # field (_known_row_count) is unreliable on fresh uploads because the
        # background watcher hasn't synced yet (defaults to -1, not 0).
        _INDEXING_POLL_MAX_S = 18   # covers ~17s cold-start measured by Ananth
        _INDEXING_POLL_INTERVAL_S = 2
        import time as _time_mod
        from app.services.instant_rag_search import lazy_rag_search

        # Initial probe (no sleep) — if doc already queryable, skip the wait.
        answer, sources, usage, signal = lazy_rag_search(
            document_id=document_id, question=query, k=10, emitter=None
        )

        _poll_ran = False
        _poll_timed_out = False
        if not sources:
            # Nothing yet — poll pgvector directly so the user gets an in-turn
            # answer when the doc lands quickly. Most uploads are ready within
            # the window; the bypass path fires only on cold-start stragglers.
            _poll_ran = True
            _waited = 0
            while _waited < _INDEXING_POLL_MAX_S:
                _time_mod.sleep(_INDEXING_POLL_INTERVAL_S)
                _waited += _INDEXING_POLL_INTERVAL_S
                answer, sources, usage, signal = lazy_rag_search(
                    document_id=document_id, question=query, k=10, emitter=None
                )
                if sources:
                    emit(f"  ✓ Document ready after {_waited}s — searching now…")
                    break
            else:
                _poll_timed_out = True

        # Run the final search with the emitter (for user-visible logging) only
        # if sources weren't found yet and the poll didn't already exhaust its
        # window (after a timeout another call won't help).
        if not sources and not _poll_timed_out:
            answer, sources, usage, signal = lazy_rag_search(
                document_id=document_id,
                question=query,
                k=10,
                emitter=emitter,
            )

        success = bool(sources) and signal != RETRIEVAL_SIGNAL_NO_SOURCES
        _hint = (usage or {}).get("vector_count_hint")  # >0=indexed/query-miss, 0/None=not indexed
        # _is_indexing: initial 18s poll exhausted with no pgvector results.
        _is_indexing = not success and _poll_timed_out

        if _is_indexing:
            # §5b deferred auto-deliver: keep the turn alive rather than closing
            # with a false promise. Emit a holding note (keeps SSE open + indicator
            # pulsing), then continue polling in-band. When the doc lands the real
            # query runs through the integrator and the answer streams back —
            # the user never re-asks. Only the hard-cap path actually bypasses.
            _doc_filename = next(
                (f.get("filename") for f in _all_files
                 if str(f.get("document_id", "")).strip() == document_id),
                None,
            ) or upload_id or "your document"
            emit(f"  ⏳ {_doc_filename} is still indexing — I'll answer as soon as it's ready…")
            _DEFERRED_MAX_S = 4 * 60   # 4 min hard cap (Cloud Run max-request ≫ this)
            _DEFERRED_INTERVAL_S = 3
            _deferred_waited = 0
            _keepalive_every = 12      # emit a progress note every ~12s
            while _deferred_waited < _DEFERRED_MAX_S:
                _time_mod.sleep(_DEFERRED_INTERVAL_S)
                _deferred_waited += _DEFERRED_INTERVAL_S
                answer, sources, usage, signal = lazy_rag_search(
                    document_id=document_id, question=query, k=10, emitter=None
                )
                if sources:
                    _total_wait = _INDEXING_POLL_MAX_S + _deferred_waited
                    emit(f"  ✓ Document ready after {_total_wait}s — answering now…")
                    success = True
                    _is_indexing = False
                    break
                if _deferred_waited % _keepalive_every == 0:
                    emit(f"  ⏳ Still indexing ({_INDEXING_POLL_MAX_S + _deferred_waited}s)…")
            # _is_indexing still True only if the hard cap fired — genuinely give up.

        if _is_indexing:
            # Hard timeout (>~4 min with no pgvector result). Bypass integrator
            # so the message is deterministic, not LLM-generated.
            _doc_filename = next(
                (f.get("filename") for f in _all_files
                 if str(f.get("document_id", "")).strip() == document_id),
                None,
            ) or upload_id or "your document"
            _status_msg = (
                f"**{_doc_filename}** is taking longer than expected to process. "
                "I'll answer your question as soon as it's ready — no need to ask again."
            )
            emit("  ⏳ Hard cap reached — deferring to background watcher.")
            ctx.react_bypass_integrate = True
            ctx.final_message = _status_msg
            ctx.plan = _make_react_plan(ctx)
            ctx.sources = []
            ctx.retrieval_signals = []
            ctx.answer_set = {}
            return {
                "tool": "search_uploaded_document",
                "success": False,
                "result": _status_msg,
                "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
                "sources": [],
            }

        if not success:
            if _hint is not None and _hint > 0:
                # Document is indexed but the query didn't match semantically.
                # Common cause: procedural query like "summarize this document".
                emit(f"  ↓ No matching content found (doc has {_hint} indexed chunks — try a different query).")
                _fail_reason = (
                    f"The document IS indexed ({_hint} chunks in the vector store) "
                    "but this query found no matching content. "
                    "If the user asked to summarize, use a content-based query on the NEXT call "
                    "(e.g. the document filename or apparent topic), not 'summarize this document'."
                )
            else:
                emit("  ↓ Your uploaded doc didn't contain this.")
                _fail_reason = "No content found in the uploaded document for this query."
        else:
            _fail_reason = ""
        return {
            "tool": "search_uploaded_document",
            "success": success,
            # Raw chunk text (no LLM synth in the tool). Integrator at
            # the end of the turn does the single synthesis pass.
            "result": answer or _fail_reason,
            "result_summary": _fail_reason if not success else "",
            "signal": signal,
            "sources": sources or [],
            "usage": usage,
            # Expose the resolved document_id for downstream observability.
            "resolved_document_id": document_id,
            "vector_count_hint": _hint,
        }

    if tool == "google_search":
        query = inputs.get("query") or (ctx.effective_message or ctx.message)
        emit(f"◌ Searching the web for: {(query or '')[:60]}…")
        # Real signal (2026-08-06) — replaces the old "5-arm bandit" write
        # (ctx._strategy_arms_tried). google_search having been tried IS
        # real, unlike the fictional bandit it used to feed; surfaced to
        # the LLM in react/prompts.py's build_reasoning_context.
        ctx._google_search_tried_this_turn = True  # type: ignore[attr-defined]
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
        if not success:
            _result_msg = (
                "[GOOGLE_EXHAUSTED] Web search returned no usable results. "
                "Answer from model knowledge with appropriate caveats, or "
                "set is_complete=true and tell the user the information "
                "could not be located in any source."
            )
        else:
            _result_msg = answer or ""
        return {
            "tool": "google_search",
            "success": success,
            "result": _result_msg,
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

    # 2026-04-18 disconnect — seven tool branches removed:
    #   lookup_npi, find_org_locations,
    #   find_associated_providers_at_locations,
    #   run_credentialing_report, validate_credentialing_step,
    #   run_roster_reconciliation_report, ask_credentialing_npi
    # These were credentialing/roster entry points on the half-
    # integrated skill server. With the chat-side UI gone (commit 1)
    # and service modules going away in commit 3, the planner
    # manifest (commit 2 below) no longer advertises them so no
    # tool dispatch can reach here. The tools will come back as
    # proper skill integrations with typed envelope contracts.

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
            out_h = _attach_result_summary(
                out_h, answer, summary_heading="**Healthcare lookup (codes / NPPES / coverage):**"
            )
        return out_h

    if tool == "healthcare_npi_lookup":
        # NPPES lookup by NPI number (no PML).
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
            out_n = _attach_result_summary(
                out_n, answer, summary_heading="**NPPES / registry (by NPI number):**"
            )
        return out_n

    # ── Task manager tools ────────────────────────────────────────────────────
    # Routed through answer_tool → SkillSpec registry
    # (app/skills/builtin/tasks.py). The skill handler writes the
    # structured task_list payload to ctx.react_task_list_data; the
    # text answer + signal flow back via the legacy 4-tuple.
    if tool in ("list_tasks", "create_task", "resolve_task"):
        question = inputs.get("question") or (ctx.effective_message or ctx.message) or ""
        answer, sources, usage, signal = answer_tool(
            question,
            emitter=emitter,
            tool_hint_override=tool,
            user_message=ctx.message,
            active_context=active,
            skill_search_mode=ctx.chat_mode,
            pipeline_ctx=ctx,
            tool_inputs=inputs,
        )
        success = bool(answer and "error" not in (answer or "").lower()[:20])
        # golden: this arm produced an authoritative answer; finalize without
        # further retrieval. Inferred when the skill returned content + a valid
        # signal (non-empty, non-NO_SOURCES). Skill can also opt-in explicitly.
        _answer_golden = (
            success
            and bool((answer or "").strip())
            and (signal or "") not in ("", RETRIEVAL_SIGNAL_NO_SOURCES)
        )
        return {
            "tool": tool,
            "success": success,
            "result": answer or "",
            "signal": signal,
            "sources": sources or [],
            "usage": usage,
            "golden": _answer_golden,
        }

    # ── Skill registry fallback (MCP + builtin skills not handled above) ─────
    # Any tool registered via register_mcp_skills() or app.skills.builtin.*
    # lands here. The registry dispatch is the universal fallback for tools
    # the planner picked but that aren't hardcoded in the branches above.
    from app.skills import registry as _skill_registry
    if _skill_registry.has(tool):
        _question = (ctx.merged_state or {}).get("message") or ""
        call = _skill_registry.SkillCall(
            name=tool,
            inputs=inputs or {},
            question=_question,
            user_message=_question,
            thread_id=ctx.thread_id,
            active_context=active,
            mode=getattr(ctx, "chat_mode", None) or "copilot",
            emitter=emitter,
            pipeline_ctx=ctx,
            extra_out=None,
        )
        emit(f"◌ {tool.replace('_', ' ').title()}…")
        env = _skill_registry.dispatch(call)
        # product_feedback returns an editable capture_card in extra; hand it to
        # the client via ctx (orchestrator attaches it to response_payload).
        _cc = (env.extra or {}).get("capture_card") if env.extra else None
        if _cc:
            ctx.capture_card = _cc
        _demo = (env.extra or {}).get("demo") if env.extra else None
        if _demo:
            ctx.demo = _demo
        _recital = (env.extra or {}).get("recital") if env.extra else None
        if isinstance(_recital, dict) and _recital.get("verbatim"):
            ctx.recital = {**_recital, "text": env.text or ""}  # type: ignore[attr-defined]
        # env.success is the authoritative outcome flag (Task #30 fix) —
        # MCP failures now set it False explicitly instead of being
        # indistinguishable from success by text shape. Kept ANDed with
        # the existing text checks as a belt-and-suspenders guard for
        # skills that don't yet set success but still return an empty or
        # "Unknown skill" text on failure.
        _skill_success = env.success and bool(env.text and not env.text.startswith("Unknown skill"))
        # golden: explicit opt-in via env.extra["golden"], or inferred when
        # the skill returned content + sources + a non-empty signal.
        # Operational skills (create_task, list_uploads, etc.) return no sources
        # and RETRIEVAL_SIGNAL_NO_SOURCES, so they never trip this gate.
        _golden_explicit = bool((env.extra or {}).get("golden"))
        _skill_golden = bool(
            _golden_explicit
            or (
                _skill_success
                and env.sources
                and (env.signal or "") not in ("", RETRIEVAL_SIGNAL_NO_SOURCES)
            )
        )
        _tr: dict = {
            "tool": tool,
            "success": _skill_success,
            "result": env.text or f"{tool} returned no content.",
            "signal": env.signal,
            "sources": [s.to_dict() for s in env.sources],
            "golden": _skill_golden,
            "golden_explicit": _golden_explicit,
        }
        # Passthrough structured section hints from analytics tools so the integrator
        # can render typed panels (table/stats/bars) instead of defaulting to bullets.
        # Two shapes supported:
        #   multi : extra.sections = [{section_format, section_title, table_headers, table_rows}, ...]
        #   single: extra.section_format / extra.table_headers / extra.table_rows (flat, legacy)
        _ex = env.extra or {}

        def _parse_hint(h: dict) -> dict:
            """Normalise one raw section-hint dict; returns {} if unusable."""
            out: dict = {}
            for k in ("section_format", "section_title", "table_headers", "items"):
                if h.get(k) is not None:
                    out[k] = h[k]
            # Strategy Agent uses table_rows; normalise to rows for the integrator
            _r = h.get("table_rows") or h.get("rows")
            if _r is not None:
                out["rows"] = _r
            return out

        _sections_raw = _ex.get("sections")
        if isinstance(_sections_raw, list):
            # Multi-section path — collect all valid hints
            _hints = [_parse_hint(s) for s in _sections_raw if isinstance(s, dict)]
            _hints = [h for h in _hints if h.get("section_format")]
            if _hints:
                _tr["section_hints"] = _hints  # plural key for multi-section
        else:
            # Single-section path (flat extra keys, backward-compatible)
            _hint = _parse_hint(_ex)
            if _hint.get("section_format"):
                _tr["section_hint"] = _hint  # singular key
        return _tr

    # ── Appeals Agent — direct HTTP dispatch ─────────────────────────────
    # These 5 tools bypass MCP and call the appeals REST API directly so
    # the router treats them as Tier 1 (same weight as rag).
    if tool in {
        "appeals_find_carc", "appeals_lookup_rules", "appeals_get_playbook",
        "appeals_validate_claim", "appeals_assemble_letter",
    }:
        import json as _json
        import os as _os

        _appeals_base = (
            _os.environ.get("APPEALS_AGENT_URL", "")
            or "https://mobius-appeals-prototype-ortabkknqa-uc.a.run.app"
        ).rstrip("/")

        def _appeals_get(path: str, **params):
            with httpx.Client(timeout=30.0) as _c:
                _r = _c.get(f"{_appeals_base}{path}", params={k: v for k, v in params.items() if v is not None})
                _r.raise_for_status()
                return _r.json()

        def _appeals_post(path: str, body: dict):
            with httpx.Client(timeout=120.0) as _c:
                _r = _c.post(f"{_appeals_base}{path}", json=body)
                _r.raise_for_status()
                return _r.json()

        def _no_src():
            return {"tool": tool, "success": False, "result": f"[{tool}] failed", "signal": RETRIEVAL_SIGNAL_NO_SOURCES, "sources": []}

        try:
            if tool == "appeals_find_carc":
                desc = (inputs.get("denial_description") or "").strip()
                payor = (inputs.get("payor") or "").strip()
                emit(f"◌ Identifying denial code from: {desc[:60]}…")
                all_carcs = _appeals_get("/carc")
                desc_lower = desc.lower()
                _KEYWORD_HINTS = [
                    (["cob","coordination","secondary","primary insurance","other insurance","medicare primary"], "cob_secondary_payor"),
                    (["timely","filing","late","deadline","past deadline","time limit"], "timely_filing"),
                    (["auth","authorization","prior auth","pre-auth","not authorized","not approved"], "auth_required"),
                    (["missing","documentation","records","not on file","incomplete","no referral"], "missing_information"),
                    (["duplicate","already paid","previously processed","same claim"], "duplicate"),
                    (["not covered","exclusion","not a covered"], "not_covered"),
                    (["eligibility","not eligible","not enrolled","member not","no coverage"], "eligibility"),
                    (["coding","unbundling","modifier","cpt","hcpcs","procedure code"], "coding_mismatch"),
                    (["bundled","inclusive","component","included in"], "bundled_service"),
                    (["fee schedule","rate","allowed amount","maximum allowable"], "fee_schedule"),
                    (["medical necessity","not medically necessary","experimental"], "medical_necessity"),
                    (["referral","referral not","no referral","referral required"], "referral"),
                    (["provider type","not credentialed","out of network","not participating"], "provider_type"),
                    (["deductible","copay","coinsurance","cost share"], "cost_share"),
                ]
                arch_scores: dict[str, int] = {}
                for kws, arch in _KEYWORD_HINTS:
                    score = sum(1 for kw in kws if kw in desc_lower)
                    if score: arch_scores[arch] = arch_scores.get(arch, 0) + score
                scored = []
                for entry in all_carcs:
                    c = entry.get("carc", 0)
                    title_lower = (entry.get("title") or "").lower()
                    s = arch_scores.get(entry.get("archetype",""), 0)*2 + sum(1 for w in desc_lower.split() if len(w)>3 and w in title_lower)
                    if s > 0: scored.append((s, entry))
                scored.sort(key=lambda x: -x[0])
                matches = []
                for _, entry in scored[:4]:
                    try:
                        rd = _appeals_get(f"/rules/{entry['carc']}")
                        rules = rd.get("rules", []) if isinstance(rd, dict) else rd
                    except Exception:
                        rules = []
                    matches.append({
                        "carc": entry["carc"], "title": entry.get("title",""),
                        "archetype": entry.get("archetype",""), "rule_count": len(rules), "rules": rules,
                    })
                top = matches[0] if matches else {}
                result_data = {
                    "matches": matches, "top_carc": top.get("carc"),
                    "top_archetype": top.get("archetype",""),
                    "suggestion": (
                        f"Most likely CARC {top.get('carc')} ({top.get('title','')}). "
                        f"Each rule's appeal_argument is the assertion to make in the letter."
                    ) if top else "Could not identify CARC — check the EOB for the exact code.",
                }
                emit(f"✓ Likely CARC {top.get('carc')} — {top.get('title','')[:50]}" if top else "⊘ Could not identify denial code")
                return {
                    "tool": tool, "success": bool(matches),
                    "result": _json.dumps(result_data),
                    "signal": None if matches else RETRIEVAL_SIGNAL_NO_SOURCES,
                    "sources": [],
                    "section_hint": {"section_format": "appeals_rules", "label": "Appeal rules",
                                     "data": {**result_data, "admin_url": f"{_appeals_base}/admin/rules-library"}} if matches else None,
                }

            if tool == "appeals_lookup_rules":
                carc = inputs.get("carc") or inputs.get("code")
                payor = (inputs.get("payor") or "").strip() or None
                if not carc:
                    return {**_no_src(), "result": "[appeals_lookup_rules] carc is required"}
                emit(f"◌ Looking up CARC {carc} rules…")
                data = _appeals_get(f"/rules/{carc}", payor=payor)
                rules = data.get("rules", []) if isinstance(data, dict) else data
                n = len(rules)
                carc_info = _appeals_get(f"/carc-config/{carc}")
                result_data = {
                    "carc": carc,
                    "carc_title": carc_info.get("title", f"CARC {carc}") if isinstance(carc_info, dict) else f"CARC {carc}",
                    "archetype": carc_info.get("archetype", "") if isinstance(carc_info, dict) else "",
                    "payor": payor or "all",
                    "rules_found": n,
                    "rules": rules,
                }
                emit(f"✓ {n} rule{'s' if n!=1 else ''} for CARC {carc}")
                return {
                    "tool": tool, "success": n > 0,
                    "result": _json.dumps(result_data),
                    "signal": None if n > 0 else RETRIEVAL_SIGNAL_NO_SOURCES,
                    "sources": [],
                    "section_hint": {"section_format": "appeals_rules", "label": "Appeal rules", "data": {**result_data, "admin_url": f"{_appeals_base}/admin/rules-library"}} if n > 0 else None,
                }

            if tool == "appeals_get_playbook":
                payor = (inputs.get("payor") or "").strip()
                carc_group = (inputs.get("carc_group") or "").strip()
                carc = inputs.get("carc") or 0
                lookup = carc_group or str(carc) if carc else carc_group
                if not payor or not lookup:
                    return {**_no_src(), "result": "[appeals_get_playbook] payor and (carc_group or carc) are required"}
                emit(f"◌ Checking {payor} playbook…")
                try:
                    pb = _appeals_get(f"/playbook/{payor}/{lookup}")
                    found = True
                except httpx.HTTPStatusError as _e:
                    if _e.response.status_code == 404 and carc and carc_group:
                        try:
                            pb = _appeals_get(f"/playbook/{payor}/{carc}")
                            found = True
                        except Exception:
                            pb = {"message": f"No playbook for {payor}. Default FL Medicaid: 60 days, certified mail."}
                            found = False
                    else:
                        pb = {"message": f"No playbook for {payor}. Default FL Medicaid: 60 days, certified mail."}
                        found = False
                result_data = {"found": found, **pb}
                # 2026-08-07 (Ananth, directly, live-query finding): this used
                # to report success=True + signal=None whenever the HTTP call
                # succeeded, even when the fetched playbook had neither a
                # deadline nor a submission method -- indistinguishable from a
                # real hit to ReactRetryGuard._is_zero_result (only fires on
                # signal=="no_sources"), so consecutive_failures_per_tool never
                # incremented and the tool-exhaustion block never fired.
                # Confirmed live: 7 consecutive appeals_get_playbook calls, all
                # "found" but content-empty ("?d deadline · "), burned rounds
                # 2-9 of a 10-round budget with zero loop-detection -- the
                # retry-guard machinery that should have caught this already
                # exists, it just never saw a failure signal. Mirrors
                # appeals_find_carc's existing pattern (n>0 -> None else
                # RETRIEVAL_SIGNAL_NO_SOURCES) immediately above -- this
                # handler had just never adopted it.
                days = pb.get("deadline_appeal_days") if found else None
                method = (pb.get("submission_method") or "").strip() if found else ""
                usable = found and (days is not None or bool(method))
                if usable:
                    emit(f"✓ {payor} playbook: {days if days is not None else '?'}d deadline · {method}")
                elif found:
                    emit(f"⚠ {payor} playbook found but has no deadline/method data")
                else:
                    emit(f"✓ No playbook for {payor} — using FL defaults")
                return {
                    "tool": tool, "success": found,
                    "result": _json.dumps(result_data),
                    "signal": None if usable else RETRIEVAL_SIGNAL_NO_SOURCES,
                    "sources": [],
                    "section_hint": (
                        {"section_format": "appeals_playbook", "label": "Appeal playbook", "data": result_data}
                        if usable else None
                    ),
                }

            if tool == "appeals_validate_claim":
                carc = inputs.get("carc")
                if not carc:
                    return {**_no_src(), "result": "[appeals_validate_claim] carc is required"}
                emit(f"◌ Running AI recommendation for CARC {carc}…")
                try:
                    rules_raw = _appeals_get(f"/rules/{carc}")
                    rules = rules_raw.get("rules", [])[:8] if isinstance(rules_raw, dict) else rules_raw[:8]
                except Exception:
                    rules = []
                body = {
                    "carc": carc, "payor": inputs.get("payor") or "", "amount": inputs.get("amount") or "",
                    "dos": inputs.get("dos") or "",
                    "inv_signals": inputs.get("inv_signals") or {},
                    "rules": [{"rule_id": r.get("rule_id",""), "rule_name": r.get("rule_name",""),
                               "rule_statement": r.get("rule_statement",""), "triggers_when": r.get("triggers_when",""),
                               "appeal_argument": r.get("appeal_argument","")} for r in rules],
                }
                result = _appeals_post("/validate-rules", body)
                action = result.get("action", "appeal")
                conf = result.get("confidence", "medium")
                emit(f"✓ Recommendation: {action} ({conf})")
                return {
                    "tool": tool, "success": True,
                    "result": _json.dumps(result),
                    "signal": None,
                    "sources": [],
                }

            if tool == "appeals_assemble_letter":
                carc = inputs.get("carc")
                if not carc:
                    return {**_no_src(), "result": "[appeals_assemble_letter] carc is required"}
                emit(f"◌ Assembling appeal letter for CARC {carc} / {inputs.get('payor','?')} — takes 30–90s…")
                body = {k: inputs.get(k) for k in [
                    "carc","payor","amount","dos","denial_date","carc_group",
                    "action_items","inv_signals","action_path","session_id",
                ] if inputs.get(k) is not None}
                result = _appeals_post("/assemble", body)
                letter = result.get("letter_draft") or result.get("letter") or ""
                wc = len(letter.split()) if letter else 0
                emit(f"✓ Letter assembled ({wc} words)")
                if letter:
                    # RECITAL verbatim passthrough (Chat Architecture, 2026-08-06,
                    # LLM Agent coordinating the enricher side). Root cause: a
                    # fully-assembled legal letter was flowing through TWO lossy
                    # paraphrase passes -- react's own "write an answer" LLM step,
                    # then the integrator's enricher LLM step -- either of which
                    # can silently drop or reword content that must survive
                    # verbatim. `ctx.recital` reuses the SAME mechanism the
                    # skill-registry dispatch path already sets from
                    # env.extra["recital"] (see the `if _skill_registry.has(tool)`
                    # branch above) -- integrate.py's existing post-process step
                    # reads it, sets mode="RECITAL", and injects recital.verbatim
                    # into the final card regardless of what the enricher's LLM
                    # call produces for direct_answer/sections. `is_terminal`
                    # (below) stops react's OWN reasoning from getting a chance
                    # to paraphrase it into a prose "answer" first -- it's the
                    # same flag `refuse` sets, but WITHOUT react_bypass_integrate:
                    # refuse skips the integrator entirely (a bare status string,
                    # no AnswerCard); this needs the integrator to still run and
                    # build a real card (citations, next_steps, mode=RECITAL
                    # chrome) around the untouched letter.
                    ctx.recital = {"verbatim": True, "text": letter}  # type: ignore[attr-defined]
                return {
                    "tool": tool, "success": bool(letter),
                    "result": letter or _json.dumps(result),
                    "signal": None if letter else RETRIEVAL_SIGNAL_NO_SOURCES,
                    "sources": [],
                    "is_terminal": bool(letter),
                }

        except Exception as _exc:
            emit(f"⊘ {tool} error: {_exc}")
            return {**_no_src(), "result": f"[{tool}] Error: {_exc}"}

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
    _cred_card = extra.get("credentialing_card")
    if isinstance(_cred_card, dict) and (_cred_card.get("npi") or _cred_card.get("org")):
        ctx.react_credentialing_card_data = _cred_card
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
    # 2026-08-07 (Task #58, Chat Architecture directive, Ananth's ruling on
    # envelope taxonomy) -- integrate.py's source_texts today reads only the
    # top-7-by-score slice of ctx.sources for the enricher prompt, silently
    # dropping the rest. ctx.sources itself is already the FULL deduped,
    # unified pool (every rag call this turn, every filler arm merged --
    # RAG's own /api/retriever/answer response is already unified before it
    # reaches corpus_search.py; filler_strategy is carried as a per-chunk
    # detail field, not a separation) with rich per-chunk metadata
    # (authority, rerank_score, confidence_label, filler_strategy, document
    # identity) via SourceRef.to_dict() -- it just wasn't exposed under a
    # name integrate.py could read the FULL set from. ctx.rag_chunks is that
    # name: a plain alias, not new data collection, so integrate.py can
    # choose how much of it to use instead of being hard-capped upstream.
    # 2026-08-07 (Task #58, schema approved by coordinator) -- field names
    # renamed from SourceRef.to_dict()'s shape to the approved contract
    # (authority->authority_level, confidence_label->confidence,
    # rerank_score->score). Still the full unified pool, uncapped -- no
    # top-7 slicing here, that's integrate.py's consumption-layer choice.
    ctx.rag_chunks = [
        {
            "index": s.get("index"),
            "text": s.get("text", ""),
            "document_name": s.get("document_name", ""),
            "authority_level": s.get("authority"),
            "confidence": s.get("confidence_label"),
            "score": s.get("rerank_score") if s.get("rerank_score") is not None else s.get("original_score"),
            "filler_strategy": s.get("filler_strategy"),
            # 2026-08-08 (Chat FE, inline [N] citation footnotes): already on
            # SourceRef, just never copied through here -- card.sources[] (the
            # FE's numbered bottom list) is built positionally from THIS list,
            # so it needs page_number/locator same as document_name. document_id
            # is what openDocReaderPanel(documentId, pageNumber, citeText)
            # (app.ts:4160, existing doc-reader panel infra) needs to open the
            # actual section -- same two fields the existing Sources-tab click
            # handler already passes it (app.ts:7736), just not on THIS list yet.
            "page_number": s.get("page_number"),
            "document_id": s.get("document_id"),
        }
        for s in ctx.sources
    ]
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
    # Collect section_hint entries from this turn's tool results so the integrator
    # can render typed panels (table/stats/bars) for analytics tool outputs.
    # ctx.react_tool_results is assigned by reference at the top of run_react() so
    # it stays in sync with every append inside the loop — no need to pass it here.
    _all_tr = list(getattr(ctx, "react_tool_results", None) or []) + list(getattr(ctx, "seed_tool_results", None) or [])
    _all_hints: list[dict] = []
    _hinted_tr_ids: set[int] = set()
    for _tr in _all_tr:
        _had_hint = False
        if _tr.get("section_hint"):
            _all_hints.append(_tr["section_hint"])
            _had_hint = True
        if _tr.get("section_hints"):
            _all_hints.extend(_tr["section_hints"])
            _had_hint = True
        if _had_hint:
            _hinted_tr_ids.add(id(_tr))
    if _all_hints:
        ctx.tool_section_hints = _all_hints

    # 2026-08-07 (Task #58, schema approved by coordinator) -- typed tool
    # outputs, grouped by tool family, not a flat dict[name, raw-string].
    # rag is excluded -- that's ctx.rag_chunks' job. No-duplicate rule: a
    # call whose section_hint already carries this data (_hinted_tr_ids,
    # built above) is skipped here so the enricher doesn't receive the
    # same content twice through two different channels.
    #
    # Field names/shapes below are grounded in live calls to the appeals
    # API (curl'd directly, not guessed): /rules/{carc} returns rule_id/
    # rule_name/rule_statement/appeal_argument/authority exactly matching
    # the approved contract. appeals_find_carc nests rules per-candidate
    # (matches[i].rules); appeals_lookup_rules has them top-level -- both
    # feed the same unified "rules" list. "validation" is a 4th key beyond
    # the 3 named in the approved schema (letter/rules/playbook) --
    # appeals_validate_claim's recommendation+confidence output has nowhere
    # else to go and dropping it would contradict the entire point of this
    # task (integrator needs ALL collected information); flagged in the
    # commit/report rather than silently fit into an existing key.
    _appeals_letter: dict | None = None
    _appeals_rules: list[dict] = []
    _appeals_playbook: dict | None = None
    _appeals_validation: dict | None = None
    _authoritative_sources: list = []
    _analytics: list = []

    def _parse_json_result(raw: str) -> Any:
        try:
            return json.loads(raw) if raw else None
        except (TypeError, ValueError):
            return None

    for _tr in _all_tr:
        if id(_tr) in _hinted_tr_ids:
            continue
        _t = _tr.get("tool")
        if not _t or _t in ("rag", "search_corpus") or not _tr.get("success"):
            continue
        _raw = _tr.get("result") or ""

        if _t == "appeals_assemble_letter":
            # Sourced from ctx.recital when present (already the verbatim
            # text, set by the dispatch handler) rather than re-parsing
            # _raw, which is plain letter text here, not JSON.
            _rec = getattr(ctx, "recital", None)
            if isinstance(_rec, dict) and _rec.get("text"):
                _appeals_letter = {
                    "verbatim": _rec["text"],
                    "document_id": _rec.get("document_id"),
                    "section": _rec.get("section"),
                }
        elif _t in ("appeals_find_carc", "appeals_lookup_rules"):
            _parsed = _parse_json_result(_raw)
            if isinstance(_parsed, dict):
                if isinstance(_parsed.get("rules"), list):
                    _appeals_rules.extend(_parsed["rules"])
                for _m in (_parsed.get("matches") or []):
                    if isinstance(_m, dict) and isinstance(_m.get("rules"), list):
                        _appeals_rules.extend(_m["rules"])
        elif _t == "appeals_get_playbook":
            _parsed = _parse_json_result(_raw)
            if isinstance(_parsed, dict) and _parsed.get("found"):
                _candidate = {k: v for k, v in _parsed.items() if k != "found"}
                # Later usable calls win over earlier empty ones -- "usable"
                # (has a deadline or method) always beats a found-but-empty
                # result, matching the zero-result fix's own definition.
                _usable = bool(_candidate.get("deadline_appeal_days") is not None or _candidate.get("submission_method"))
                if _appeals_playbook is None or _usable:
                    _appeals_playbook = _candidate
        elif _t == "appeals_validate_claim":
            _parsed = _parse_json_result(_raw)
            if isinstance(_parsed, dict):
                _appeals_validation = _parsed
        elif _t == "lookup_authoritative_sources":
            _parsed = _parse_json_result(_raw)
            if isinstance(_parsed, dict) and isinstance(_parsed.get("sources"), list):
                _authoritative_sources.extend(_parsed["sources"])

    _appeals: dict[str, Any] = {}
    if _appeals_letter is not None:
        _appeals["letter"] = _appeals_letter
    if _appeals_rules:
        _appeals["rules"] = _appeals_rules
    if _appeals_playbook is not None:
        _appeals["playbook"] = _appeals_playbook
    if _appeals_validation is not None:
        _appeals["validation"] = _appeals_validation

    _tool_outputs: dict[str, Any] = {}
    if _appeals:
        _tool_outputs["appeals"] = _appeals
    if _analytics:
        _tool_outputs["analytics"] = _analytics
    if _authoritative_sources:
        _tool_outputs["authoritative_sources"] = _authoritative_sources
    if _tool_outputs:
        ctx.tool_outputs = _tool_outputs

    # 2026-08-07 (Task #58, schema approved by coordinator) -- reasoning_trace
    # is an alias of react_trace_rounds under the approved name, not a
    # rename: react_trace_rounds also feeds the existing react_trace
    # diagnostics panel below and shouldn't be disturbed. Uncapped here --
    # LLM Agent's stated preference is to receive everything and do their
    # own round-selection/capping at the prompt-construction layer, same
    # principle as the result-field truncation rule.
    _reasoning_trace = getattr(ctx, "react_trace_rounds", None)
    if _reasoning_trace:
        ctx.reasoning_trace = list(_reasoning_trace)

    # react_trace diagnostics panel (2026-07-31) — one per turn, same
    # "diagnostic-only, doesn't affect the answer" tier as retrieval_trace.
    # Defensive: a bug in trace-building must never break the actual
    # finalize this function exists for. Guarded on react_trace_rounds
    # being non-empty so this is a no-op for finalize paths that never
    # entered the round loop at all (e.g. a pre-loop short-circuit).
    try:
        _rounds = getattr(ctx, "react_trace_rounds", None)
        if _rounds:
            import time as _rt_time_mod
            from app.communication.emit_envelope import make_react_trace
            from app.pipeline.react.governor import product_promise_enabled as _rt_pp_enabled_fn
            _rt_start = getattr(ctx, "react_turn_start_monotonic", None)
            _rt_elapsed = (_rt_time_mod.monotonic() - _rt_start) if _rt_start is not None else None
            env = make_react_trace(
                correlation_id=ctx.correlation_id,
                mode=react_chat_mode_label(getattr(ctx, "chat_mode", None)),
                max_rounds=getattr(ctx, "react_max_rounds", None) or len(_rounds),
                rounds_used=getattr(ctx, "react_rounds_used", None) or len(_rounds),
                governor_enabled=_rt_pp_enabled_fn(),
                rounds=_rounds,
                groundedness_floor_ran=bool(getattr(ctx, "react_groundedness_floor_ran", False)),
                groundedness_passed=getattr(ctx, "react_groundedness_passed", None),
                final_directive=getattr(ctx, "product_promise_directive", None),
                unfinished_reason=getattr(ctx, "react_unfinished_reason", None),
                unfinished_summary=getattr(ctx, "react_unfinished_summary", None),
                unblock_ask=getattr(ctx, "react_unblock_ask", None),
                total_elapsed_s=round(_rt_elapsed, 1) if _rt_elapsed is not None else None,
                hard_ceiling_s=getattr(ctx, "react_hard_ceiling_s", None),
                groundedness_score=getattr(ctx, "react_groundedness_score", None),
                thread_id=ctx.thread_id,
            )
            chunks = getattr(ctx, "thinking_chunks", None)
            if isinstance(chunks, list):
                chunks.append(env.to_dict())
    except Exception as _rt_exc:  # pragma: no cover — defensive, matches _maybe_emit_retrieval_trace
        logger.debug("react_trace emit failed: %s", _rt_exc)


# ---------------------------------------------------------------------------
# ReAct main loop
# ---------------------------------------------------------------------------


def _checkpoint_best_evidence(ctx: PipelineContext, tool_results: list[dict]) -> None:
    """Mid-loop truncation recovery checkpoint (Task #29,
    docs/MIDTURN_TRUNCATION_RECOVERY_SPEC.md §1). If a timeout kills the
    turn before a real draft answer exists (react_loop never returns, so
    orchestrator.py's append_draft_answer() call after run_react() never
    fires), this is what a "Continue" retry has to hand off instead of
    nothing. Called after every round's tool dispatch — deliberately
    overwrites rather than accumulates, since each call is either the
    same best evidence as last round or strictly better.

    Reuses append_draft_answer()'s existing draft_ready channel rather
    than inventing a parallel one — same mechanism orchestrator.py
    already uses for the POST-react draft, so the checkpoint-read side
    (worker/run.py, LLM Agent's side of Task #29) has exactly one place
    to check regardless of whether the turn got far enough to finish
    react_loop or was killed mid-round. Deliberately does NOT distinguish
    "genuine draft" from "mid-loop evidence snapshot" here — that's a
    read-side classification (was react_loop still running when the
    timeout hit?), not something the write call needs to encode.

    Selection logic mirrors the two existing "best available tool
    result" fallbacks in this file (the parse-failure fallback and the
    exhausted-iterations fallback) — most recent *successful* result,
    preferring the fuller ``result`` field over ``result_summary``.
    No-ops (silently) when nothing usable exists yet — most turns finish
    in 1-3 rounds and never need this at all; this only matters for the
    turns that don't.

    Task #33 (2026-08-05): this writes RAW tool-result text — retrieved
    document chunks, "[1] file.pdf (p.18) ..." citation dumps — not a
    synthesized answer. It used to call append_draft_answer(), which
    fires a live draft_ready SSE event; every round, that streamed raw
    evidence to the client as if it were the draft answer (and could
    clobber a real draft already set). Uses append_evidence_checkpoint()
    instead — same durable-stash purpose for Task #29's mid-turn
    recovery (get_checkpoint() already reads this as the "evidence"
    quality, one tier below a real "draft"), no live event."""
    best = next((tr for tr in reversed(tool_results) if tr.get("success")), None)
    if best is None:
        return
    text = (best.get("result") or "").strip()
    if len(text) < 40:
        text = (best.get("result_summary") or "").strip()
    if not text or len(text) < 40:
        return
    try:
        from app.storage.progress import append_evidence_checkpoint
        append_evidence_checkpoint(ctx.correlation_id, text)
    except Exception:
        # Checkpointing must never break the actual turn it's protecting.
        logger.debug("checkpoint_best_evidence failed (cid=%s)", getattr(ctx, "correlation_id", "?"), exc_info=True)


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
    skip_retry: bool = False,
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
        skip_retry: when True, return the first result without sleeping or
            retrying. Used by fast/quick mode to avoid adding latency on
            transient errors.

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

    if skip_retry:
        return result

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


# ── Round 0: system_context short-circuit ─────────────────────────────────
#
# Logic lives in app.pipeline.react.round0 — see that module for the full
# contract. Re-exports below keep the legacy import paths working for
# tests and any external callers.

from app.pipeline.react.round0 import (  # noqa: E402 — grouped with other react imports above
    ROUND0_SENTINEL as _ROUND0_SENTINEL,
    build_round_context_prefix as _round0_context_prefix,
    try_system_context_round0 as _try_system_context_round0,
)


def _cache_preaudited_critic_skip(
    ctx: PipelineContext,
    tool_results: list[dict],
    rn: int,
) -> tuple[bool, str]:
    """Decide whether to skip the critic on this finalization.

    Skip criteria (ALL must hold):
      1. ``CACHE_ASSIST_SKIP_CRITIC_WHEN_PREAUDITED != 0`` (env kill switch)
      2. ``rn == 1`` — the LLM is finalizing without having picked a tool
         this turn (the only tool_result present is the cache seed from
         ``round_virtual=0``)
      3. The only tool result in this turn's history is the cache seed —
         i.e. no real tool was invoked. Mixed cache+fresh finalization
         still runs the critic because the blend is a new artifact that
         wasn't audited before.
      4. The cache candidates surfaced to the LLM were ALL
         ``critic_approved=True`` at their original write time. Partially
         approved cache still runs the critic (defense in depth against
         the LLM picking the non-approved candidate).

    Returns ``(skip, reason)``. ``reason`` is diagnostic (e.g. "cache
    seed absent", "mixed cache+fresh", "not all candidates approved").
    """
    import os
    raw = (os.environ.get("CACHE_ASSIST_SKIP_CRITIC_WHEN_PREAUDITED") or "1").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False, "env_disabled"

    if rn != 1:
        return False, f"not_round_1(rn={rn})"

    if not tool_results:
        return False, "no_tool_results"

    # Tool results should contain exactly the cache seed and nothing else.
    non_cache = [
        tr for tr in tool_results
        if not (tr.get("tool") == "cached_answer_lookup" and tr.get("round_virtual") == 0)
    ]
    if non_cache:
        return False, "mixed_cache_and_fresh_tool_results"

    cache_entries = [
        tr for tr in tool_results
        if tr.get("tool") == "cached_answer_lookup" and tr.get("round_virtual") == 0
    ]
    if not cache_entries:
        return False, "cache_seed_absent"

    candidates = getattr(ctx, "cache_candidates", None) or []
    if not candidates:
        return False, "no_candidates_on_ctx"

    all_approved = all(bool(c.get("critic_approved")) for c in candidates)
    if not all_approved:
        return False, "not_all_candidates_critic_approved"

    return True, "all_gates_passed"


def run_react(ctx: PipelineContext, emitter=None) -> None:
    """
    ReAct loop: Reason → Act → Observe → Repeat.
    Sets ctx.final_message, ctx.sources, ctx.retrieval_signals, ctx.answer_set.
    """
    from app.pipeline.active_context import load_active_context, load_failed_query
    from app.pipeline.message_resolver import detect_skill_reference, resolve_pronouns

    # Preflight timing inside ReAct (2026-04-29) — same pattern as
    # orchestrator's outer ``[preflight]`` markers. Helps pin down the
    # 11-15s gap we saw on follow-ups between USE_REACT log and the
    # first generate_content call.
    import time as _t_mod
    _cid_short = (getattr(ctx, "correlation_id", "") or "")[:8]
    _t_react_pf = _t_mod.perf_counter()
    def _react_pf(label: str, t_prev: float) -> float:
        now = _t_mod.perf_counter()
        ms = int((now - t_prev) * 1000)
        if ms >= 50:
            logger.info(
                "[react-preflight] cid=%s step=%s elapsed_ms=%d",
                _cid_short, label, ms,
            )
        return now

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

    _t_react_pf = _react_pf("preflight_pronoun", _t_react_pf)

    # Load active context from state (for follow-up detection)
    ctx.active_context = load_active_context(ctx.merged_state, ctx.last_turns)
    _t_react_pf = _react_pf("preflight_active_context", _t_react_pf)

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

    # Round 0: system_context short-circuit (2026-04-22). When the caller
    # supplied pre-loaded ground truth (story layer, skill card), try to
    # answer from it directly before entering the tool loop. Returns True
    # when a complete answer was produced; caller returns immediately.
    #
    # Skip for task mode: task callers supply system_context as structured
    # input for the ReAct loop to reason over, not as a short-circuit target.
    # Round 0's 800-token cap truncates multi-rule responses; the main loop
    # has no such cap and handles the full system_context correctly.
    _is_task_mode = react_chat_mode_label(getattr(ctx, "chat_mode", None)) == "task"
    if not _is_task_mode and _try_system_context_round0(ctx, emitter):
        _react_pf("preflight_round0_short_circuit_taken", _t_react_pf)
        return
    _t_react_pf = _react_pf("preflight_round0_check", _t_react_pf)

    # Emit jurisdiction
    active = (ctx.merged_state or {}).get("active") or {}
    reset_reason = (ctx.merged_state or {}).get("_reset_reason")
    emit_jurisdiction_context(active, reset_reason, emitter)
    _t_react_pf = _react_pf("preflight_jurisdiction", _t_react_pf)

    mode_label = react_chat_mode_label(getattr(ctx, "chat_mode", None))
    max_it = react_max_iterations_for_mode(getattr(ctx, "chat_mode", None))
    emit("I'm breaking down your question and choosing the right source…")
    emit(
        f"  (Up to {max_it} reasoning rounds — {mode_label}: "
        f"{'more tool passes when needed' if mode_label == 'agentic' else 'faster path; you can steer on the next message'}.)"
    )
    # Seed tool_results with pre-populated entries from the orchestrator
    # (e.g. cache-assist's cached_answer_lookup result when cache_mode
    # is 'active'). The entries already carry a ``round_virtual: 0``
    # marker so downstream code can distinguish real round-N tool
    # calls from pre-round-1 injections.
    seed = list(getattr(ctx, "seed_tool_results", None) or [])
    tool_results: list[dict] = seed
    ctx.react_tool_results = tool_results  # mutable ref — stays in sync as loop appends
    all_sources: list[dict] = []
    for s in seed:
        seed_sources = s.get("sources") or []
        if isinstance(seed_sources, list):
            all_sources.extend(seed_sources)
    final_signal = RETRIEVAL_SIGNAL_NO_SOURCES
    last_tool: str | None = None
    # 2026-05-06: pass user_profile so splice_user_profile appends
    # rendered_prompt to the planner/ReAct system prompt.
    # This value is the default AND the fail-soft fallback for the v2
    # composition path below (MOBIUS_PROMPT_SOURCE=composition) — computed
    # once here exactly as before, so the flag-off path has zero behavior
    # change (docs/REACT_PHASE_A_IMPLEMENTATION_PLAN.md).
    reasoning_system = _react_reasoning_system(
        max_it,
        mode_label,
        getattr(ctx, "user_profile", None),
        allowed_tools=getattr(ctx, "allowed_tools", None),
    )
    _react_prompt_source_v2 = (os.environ.get("MOBIUS_PROMPT_SOURCE") or "").strip().lower() == "composition"
    # Set alongside `reasoning_system` whenever the v2 path resolves — passed
    # into _call_llm_json below so the actual llm_calls row gets attributed
    # (2026-07-30 fix: resolution + logging existed, but nothing threaded
    # these into the LLM call itself, so llm_calls.composition_id/hash stayed
    # NULL despite the composition path resolving and rendering correctly).
    _reasoning_composition_id: int | None = None
    _reasoning_composition_hash: str | None = None

    # Product Promise governor (docs/REACT_PRODUCT_PROMISE_SPEC.md), behind
    # MOBIUS_PRODUCT_PROMISE_ENABLED (default off — Chat Architecture ruling,
    # 2026-07-30: this flag is the ONLY thing authorized to bypass the
    # existing critic_enabled()/should_run_critic() gates; when off, nothing
    # below this point changes anything). See governor.py's module docstring
    # for the explicit scope boundary (does not touch composition selection).
    import time as _pp_time_mod
    from app.pipeline.react.governor import product_promise_enabled as _pp_enabled_fn
    _pp_enabled = _pp_enabled_fn()
    _pp_contract = None
    _pp_turn_start = _pp_time_mod.monotonic()
    # Stashed unconditionally (not just when the governor is on) so
    # _finalize_response can report total elapsed time in the react_trace
    # diagnostics panel regardless of MOBIUS_PRODUCT_PROMISE_ENABLED.
    ctx.react_turn_start_monotonic = _pp_turn_start
    _pp_extension_rounds_used = 0
    # Query-intent reasoning-depth floor (2026-08-04, Chat Architecture +
    # Ananth's live testing: "generate a report" and "is X covered?"
    # shouldn't get identical per-round effort). Computed once, pre-Round-1,
    # unconditionally (cheap keyword check on the raw message, no cost to
    # computing it even when the governor ends up off and nothing reads it)
    # — see governor.py's extract_query_intent_floor()/resolve_reasoning_
    # depth() for the combiner semantics (floor only, never lowers what the
    # stage already earned).
    from app.pipeline.react.governor import extract_query_intent_floor
    _pp_query_intent_floor = extract_query_intent_floor(
        getattr(ctx, "effective_message", None) or ctx.message or ""
    )
    if _pp_enabled:
        from app.pipeline.react.governor import default_contract_for_mode, scale_ceiling_for_intent
        _pp_contract = default_contract_for_mode(mode_label)
        if _pp_query_intent_floor is not None:
            import dataclasses as _pp_dataclasses
            _pp_scaled_ceiling = scale_ceiling_for_intent(_pp_contract.hard_ceiling_s, _pp_query_intent_floor)
            if _pp_scaled_ceiling != _pp_contract.hard_ceiling_s:
                _pp_contract = _pp_dataclasses.replace(_pp_contract, hard_ceiling_s=_pp_scaled_ceiling)

    # react_trace diagnostics panel (see make_react_trace, emit_envelope.py)
    # — per-round entries collected as the loop runs, emitted once at turn
    # end by _finalize_response. Populated regardless of governor flag
    # state (directive/reason are just None per-round when it's off).
    ctx.react_trace_rounds = []
    ctx.react_groundedness_floor_ran = False
    ctx.react_groundedness_passed = None
    # Continuous groundedness heuristic (critic.py's compute_groundedness_
    # heuristic) — stays None unless MOBIUS_REACT_GROUNDEDNESS_HEURISTIC is
    # on; see critic.py's module notes for why this is gated separately
    # from react_groundedness_passed (the boolean gate keeps deciding loop
    # continuation either way).
    ctx.react_groundedness_score = None
    ctx.react_unfinished_reason = None
    ctx.react_unfinished_summary = None
    ctx.react_unblock_ask = None
    # Stashed for the SAME reason as react_max_rounds/react_rounds_used
    # above — _pp_contract is local to this function, _finalize_response is
    # a separate module-level function that needs its own ctx-side copy.
    # 2026-08-04 (Chat Architecture, Bandit Agent reward-signal review):
    # this was ALWAYS None in every real react_trace before this fix — the
    # field existed on make_react_trace()'s signature but nothing ever
    # actually passed a value for it. None when the governor is off
    # (no contract exists to read a ceiling from).
    ctx.react_hard_ceiling_s = _pp_contract.hard_ceiling_s if _pp_contract is not None else None

    # Phase 0.7: smart-retry guard — tracks failed attempts so we don't repeat
    # the same (tool, inputs) when no new evidence has come in, and enables
    # fail-fast when every round errors.
    from app.pipeline.react_retry_guard import ReactRetryGuard
    retry_guard = ReactRetryGuard()

    # Sprint A.1: track whether the critic has flagged any round during
    # this turn. If a later round's completion gets approved AFTER a
    # previous flag, that's a system self-correction event worth
    # promoting to task-manager analytics. First-try approvals stay
    # chat-side-only (too common to warrant promotion).
    _critic_retries_this_turn = 0

    # Sprint A.1 commit 3: emit a structured signal at the transition
    # round (first round where guidance mode activates). The planner's
    # instruction change is visible in the thinking trail via the
    # headline; the envelope makes the event analytics-queryable.
    _guidance_mode_emitted = False

    # Product-feedback cadence signal (docs/feedback-agent-spec.md §4B/§6):
    # compute once per turn (not per round) and stash on ctx so
    # build_reasoning_context can inject it. Fully guarded — any failure leaves
    # ctx.feedback_signal None and the loop behaves exactly as before. Gated by
    # FEEDBACK_PERIODIC_ENABLED (default on; the ceiling still lives in code).
    try:
        from app.pipeline.react.feedback_signal import maybe_set_feedback_signal
        maybe_set_feedback_signal(ctx)
    except Exception as _fb_e:  # never let feedback break the hot path
        logger.debug("feedback signal skipped: %s", _fb_e)

    # Per-round tracing intentionally omitted: the ReAct iteration body
    # has many early-return paths (finalize_response, break,
    # rounds_exhausted) that make reliable span-close logic risky
    # without re-indenting the whole 200-line body. Coverage we keep:
    # (a) the outer run_pipeline span wraps the full turn, (b) every
    # Vertex LLM call inside the loop has its own span. That's enough
    # to answer "how long did round N's LLM + tool take" by summing
    # children in Cloud Trace — no per-round parent span needed.

    _react_pf("preflight_TOTAL_to_iteration_start", _t_react_pf)
    # Product Promise governor (docs/REACT_PRODUCT_PROMISE_SPEC.md, behind
    # MOBIUS_PRODUCT_PROMISE_ENABLED — see governor.py's module docstring for
    # the full scope note). `itertools.count()` + an explicit bound check
    # (rather than `range(max_it)`) so `max_it` can grow mid-turn when the
    # governor grants an "extend" — incrementing `max_it` before the next
    # iteration's check runs is enough; no other line in this ~600-line loop
    # body needs to change or re-indent. When the flag is off, `max_it`
    # never changes, so this is byte-for-byte equivalent to the original
    # `for iteration in range(max_it)`.
    for iteration in _itertools.count():
        if iteration >= max_it:
            break
        rn = iteration + 1
        # Keep ctx.react_rounds_used current so whichever exit path
        # the loop takes (finalize, break, exception-to-integrator
        # fallback), _publish_completed reads the correct round count.
        ctx.react_rounds_used = rn
        ctx.react_max_rounds = max_it

        # Structured signal at the guidance-mode transition.
        if not _guidance_mode_emitted:
            from app.pipeline.react.prompts import is_guidance_round
            if is_guidance_round(iteration, max_it):
                _guidance_mode_emitted = True
                if emitter:
                    from app.communication.emit_envelope import make_guidance_mode_activated
                    tools_used = [r.get("tool") for r in tool_results if r.get("tool")]
                    emitter(make_guidance_mode_activated(
                        correlation_id=ctx.correlation_id,
                        round=rn,
                        rounds_remaining=max_it - iteration,
                        tools_used_so_far=list(tools_used),
                        thread_id=ctx.thread_id,
                        user_id=getattr(ctx, "user_id", None),
                    ).to_dict())

        # Product Promise governor: pre-round directive (search/consolidate/
        # extend/finalize only — proposes_complete isn't known until the
        # model responds this round, so "complete" is unreachable here; see
        # governor.py). Computed BEFORE composition resolution below because
        # it now also SELECTS the composition when the governor is on (Chat
        # Architecture ruling, 2026-07-30, option (b): directive replaces
        # react_agent_role() as the live selector for the same 3
        # byte-identical compositions — Phase B/temperature-routing is
        # closed, directive owns that intent now).
        _pp_pre_directive = None
        _pp_pre_reason = None
        _pp_elapsed_s = 0.0
        if _pp_enabled and _pp_contract is not None:
            from app.pipeline.react.governor import RoundState, evaluate
            _pp_elapsed_s = _pp_time_mod.monotonic() - _pp_turn_start
            _pp_pre_state = RoundState(
                proposes_complete=False, self_reported_confidence=None, critic_verdict=None,
                groundedness_passed=None, elapsed_s=_pp_elapsed_s,
                base_rounds_remaining=max_it - iteration,
                extension_rounds_available=_pp_contract.max_extension_rounds - _pp_extension_rounds_used,
            )
            _pp_pre_directive, _pp_pre_reason = evaluate(_pp_contract, _pp_pre_state)
            logger.info(
                "[react] product_promise directive=%s round=%d/%d elapsed_s=%.1f reason=%s",
                _pp_pre_directive, rn, max_it, _pp_elapsed_s, _pp_pre_reason,
            )

        # Round headline: prefer the governor's real per-round reasoning
        # (directive + reason) over the static positional label
        # (_react_round_headline — "Scoping"/"Grounding"/etc, which is keyed
        # purely on round index+mode and doesn't reflect what's actually
        # happening this round). Same underlying data now also collected
        # into ctx.react_trace_rounds below for the react_trace diagnostics
        # panel — this is the user-facing half of the same fix.
        if _pp_pre_directive is not None:
            headline = f"{_pp_pre_directive} — {_pp_pre_reason}"
        else:
            headline = _react_round_headline(iteration, max_it)
        emit(f"  Round {rn}/{max_it} — {headline}")
        emit(f"  Reasoning round {rn}/{max_it}…")

        # v2 composition path (Phase A, docs/REACT_PHASE_A_IMPLEMENTATION_PLAN.md):
        # resolve per round via react_agent_role (flag off / governor off) or
        # the governor's directive (flag on) — react_explore/synthesize/draft
        # stay byte-identical content either way; only the selector differs.
        # Fail-soft: a miss or error leaves `reasoning_system` at its last-good
        # value (the pre-loop legacy default on round 1, or the prior round's
        # resolved value), never breaks the turn.
        _agent_role = None
        if _react_prompt_source_v2:
            from app.pipeline.react.prompts import react_agent_role, resolve_react_system_prompt_v2
            if _pp_enabled and _pp_pre_directive is not None:
                from app.pipeline.react.governor import directive_to_agent_role
                _agent_role = directive_to_agent_role(_pp_pre_directive)
            else:
                _agent_role = react_agent_role(iteration, max_it)
            _resolved = resolve_react_system_prompt_v2(
                max_it, mode_label, getattr(ctx, "user_profile", None),
                getattr(ctx, "allowed_tools", None), _agent_role,
            )
            if _resolved is not None:
                reasoning_system = _resolved.system_prompt
                _reasoning_composition_id = _resolved.composition_id
                _reasoning_composition_hash = _resolved.composition_hash
            # else: leave reasoning_system AND the composition_id/hash at
            # whatever they were (this round's fail-soft fallback reuses the
            # prior value, so the attribution must stay in sync with it —
            # resetting composition_id/hash to None here would misattribute
            # a still-composition-sourced prompt as legacy/untracked.

        # Model-bandit selection criteria (2026-08-04, react-prep — scoped
        # with Chat Architecture/LLM Agent/Eval, LLM Agent's router.select()
        # side already live). Both None when the governor is off, matching
        # its fail-soft posture everywhere else in this module — the bandit
        # falls back to its existing mode-derived default either way.
        #
        # Derives reasoning_depth DIRECTLY from _pp_pre_directive (not via
        # agent_role) — caught live 2026-08-04 (Ananth, real turn: "feels
        # like the fast mode is not triggering right") that routing through
        # agent_role loses the search/consolidate/extend/finalize
        # distinction, since consolidate+extend both collapse to the same
        # "synthesize" bucket despite being opposite in intent (consolidate
        # = time pressure = should favor speed; extend = deliberately
        # spending MORE budget on quality = should favor thinking) — see
        # directive_to_reasoning_depth()'s docstring in governor.py for the
        # full mapping and reasoning.
        #
        # Computed here (before the trace-append below) rather than right
        # at the _call_llm_json call site further down, so the SAME values
        # both drive the actual model-selection call AND populate the
        # react_trace diagnostics panel — one computation, not two that
        # could silently drift apart.
        _bandit_reasoning_depth = None
        _bandit_latency_budget_ms = None
        if _pp_enabled:
            from app.pipeline.react.governor import directive_to_reasoning_depth, resolve_reasoning_depth
            # resolve_reasoning_depth: floor semantics only -- the query-
            # intent floor computed pre-loop can RAISE this round's depth
            # above what the directive alone earned (e.g. a "consolidate"
            # round on a report query still gets thinking), never lower it
            # below the directive's own choice.
            _bandit_reasoning_depth = resolve_reasoning_depth(
                directive_to_reasoning_depth(_pp_pre_directive), _pp_query_intent_floor,
            )
            if _pp_contract is not None:
                from app.pipeline.react.governor import latency_budget_ms as _pp_latency_budget_ms
                _bandit_latency_budget_ms = _pp_latency_budget_ms(
                    _pp_contract, _pp_elapsed_s, _pp_pre_directive,
                )

        # Per-round entry for the react_trace diagnostics panel (see
        # make_react_trace in emit_envelope.py). Collected on ctx (like
        # ctx.react_tool_results/react_last_tool above) so _finalize_response
        # can build+emit the trace once at turn end without every one of
        # its ~15 call sites needing a new parameter.
        ctx.react_trace_rounds.append({
            "round": rn,
            "directive": _pp_pre_directive,
            "reason": _pp_pre_reason,
            "agent_role": _agent_role,
            "composition_id": _reasoning_composition_id,
            "elapsed_s": round(_pp_elapsed_s, 1) if _pp_enabled else None,
            "reasoning_depth": _bandit_reasoning_depth,
            "latency_budget_ms": _bandit_latency_budget_ms,
        })

        # Governor active → suppress the old round-index guidance instruction
        # rather than let it collide with round_directive below. The two
        # fire on different signals (is_guidance_round: iteration vs. 80% of
        # max_it; round_directive: elapsed_s vs. soft_target_s) and WILL
        # disagree — e.g. an agentic round 3-of-10 with an expensive tool
        # call already past soft_target_s: round-index says "still
        # exploring," wall-clock says "consolidate now." Sending both to the
        # model is a contradictory prompt on exactly the case this governor
        # exists to fix (Product Promise session, 2026-07-30). Narrow fix,
        # no signature change: build_reasoning_context only adds the
        # guidance text when max_iterations is not None (prompts.py:722-725),
        # so passing None here suppresses it cleanly. A full replacement of
        # is_guidance_round with round_directive-derived context (needing a
        # real signature change) stays a tracked follow-up, not done here.
        _pp_suppress_guidance = _pp_enabled and _pp_contract is not None

        # ── EvidenceLedger phase 1 (Task #48, Chat Architecture spec,
        # 2026-08-06) — code-computed, no LLM inference. Subsumes Task #50:
        # gap_status=="stagnant" IS the reframe signal, mechanically
        # detected from ctx._rag_call_history instead of relying on the
        # `if not success:` gate inside the rag dispatch block, which
        # silently never fired when rag returned real-but-wrong chunks
        # (confirmed live: Amerigroup case, 3 rounds of identical
        # dispatch_path/chosen_slot/status, success=True throughout since
        # chunks were non-empty). build_reasoning_context renders this
        # unconditionally each round — no gate to miss.
        _ledger_history: list[dict] = list(getattr(ctx, "_rag_call_history", []))
        _gap_status = _compute_gap_status(_ledger_history)

        reasoning_context = build_reasoning_context(
            ctx, tool_results, rn,
            max_iterations=(None if _pp_suppress_guidance else max_it),
            gap_status=_gap_status,
            rag_call_history=_ledger_history,
            exhausted_tools=retry_guard.exhausted_tools(),
        )
        # Directive text appended to context — NOT a composition/system-
        # prompt block (see governor.py's module docstring for why).
        if _pp_enabled and _pp_contract is not None and _pp_pre_directive is not None:
            from app.pipeline.react.governor import round_directive_text
            reasoning_context = reasoning_context + "\n\n" + round_directive_text(
                round_n=rn, max_rounds=max_it, elapsed_s=_pp_elapsed_s,
                soft_target_s=_pp_contract.soft_target_s, directive=_pp_pre_directive, reason=_pp_pre_reason,
            )
        # Final-round self-report instruction (see constant docstring above).
        # rn==max_it is the actual last round even when Product Promise has
        # grown max_it mid-turn via "extend" — max_it already reflects the
        # new ceiling by the time this round runs, so this fires on the
        # correct (possibly-extended) final round, not a stale one.
        #
        # elif (not a separate `if`): the two instructions must never stack
        # in the same context message — the offramp's "you're not required
        # to stop" framing would directly contradict the final-round
        # instruction's "do NOT request another tool call" on the round
        # where both conditions happen to be true at once (max_it reached
        # AND structurally exhausted) — final-round wins since it's the
        # stricter, actually-true statement (no round after this one).
        if rn == max_it:
            reasoning_context = reasoning_context + "\n\n" + _REACT_FINAL_ROUND_INSTRUCTION
        elif retry_guard.structurally_exhausted():
            reasoning_context = reasoning_context + "\n\n" + _REACT_STRUCTURAL_EXHAUSTION_OFFRAMP
        # system_context (2026-04-22): when Round 0 fell through to the
        # tool loop (NEEDS_TOOLS sentinel), surface the caller-supplied
        # verified data to every subsequent reasoning round. Tools can
        # then complement — not re-derive — what's already known.
        sys_ctx_for_rounds = (getattr(ctx, "system_context", None) or "").strip()
        if sys_ctx_for_rounds:
            reasoning_context = _round0_context_prefix(sys_ctx_for_rounds) + reasoning_context
        # Inject already-failed attempts into the prompt so the LLM sees
        # them and picks differently.
        hint = retry_guard.failure_hint_for_prompt()
        if hint:
            reasoning_context = f"{reasoning_context}\n\n{hint}"

        # _bandit_reasoning_depth/_bandit_latency_budget_ms computed earlier
        # in this iteration (see the react_trace block above) — reused here
        # rather than recomputed, so the same values both drive this call
        # and the diagnostics panel.
        decision_raw = _call_llm_json(
            reasoning_system,
            reasoning_context,
            ctx=ctx,
            stage=f"react_{rn}",
            composition_id=_reasoning_composition_id,
            composition_hash=_reasoning_composition_hash,
            reasoning_depth=_bandit_reasoning_depth,
            latency_budget_ms=_bandit_latency_budget_ms,
        )

        decision = _parse_react_decision_json(decision_raw)

        if decision is None:
            # Parse-failure prose fallback: the LLM produced plain prose
            # instead of JSON. Trust it as a synthesised final answer ONLY
            # on a guidance round (round >= 80% of max) — model synthesising
            # a final answer is the primary case; first fixed 2026-05-11 —
            # and only when the prose does NOT look like a tool-call
            # request.
            #
            # 2026-08-04 (Ananth, live agentic-mode turn: "feels like the
            # auto escalation mode did not fully kick on and extend"): this
            # used to ALSO trust the prose on any flat `iteration >= 2`
            # (round 3+), regardless of the mode's actual round budget.
            # That threshold was a no-op for copilot/task (max_it=3) —
            # is_guidance_round(2, 3) is already True there, same round —
            # but for agentic (max_it=10, guidance doesn't start until
            # round 8) it fired 5 rounds early: a round-4 parse failure on
            # a "give me a detailed report" turn had the model's own
            # "I'm still gathering information, broadening my search"
            # narration shipped as the finished answer, with 6 rounds of
            # budget left and the governor's extend/structural-exhaustion
            # logic never reached (this path returns before either runs).
            # Dropped the flat threshold — is_guidance_round alone already
            # covers every mode correctly since it scales with max_it.
            from app.pipeline.react.prompts import is_guidance_round
            raw_prose = (decision_raw or "").strip()
            # Pattern: prose that starts with a known tool name is a
            # misformatted tool-call request, not a synthesised answer.
            _TOOL_CALL_RE = re.compile(
                r'^\s*(?:search_corpus|lookup_npi|web_search|google_search)\b', re.I
            )
            _prose_looks_like_answer = (
                raw_prose
                and len(raw_prose) >= 40
                and not _TOOL_CALL_RE.search(raw_prose)
            )

            # 2026-08-07 (Chat Master, live-query finding — Ananth's
            # screenshot, cid 50fda638): a parse failure on a NON-guidance
            # round used to fall straight to the last-tool-output fallback
            # below, even when the tool's raw result was machine JSON never
            # meant for a user (e.g. appeals_validate_claim's {"raw_text":
            # "...", "rules_validated": 4}) -- confirmed live: round 7 of 10
            # wrote a genuinely good, complete prose answer that just wasn't
            # JSON-wrapped, but wasn't a guidance round yet, so the prose
            # fallback below didn't trust it either, and the raw
            # validate-claim JSON blob shipped as the user-facing answer
            # (score 0.50, degraded card).
            #
            # Gated on NOT already being the guidance-round-trusted-prose
            # case: when it IS that case, retrying would just burn an LLM
            # call for a response we were already going to accept unchanged
            # (confirmed regression risk: test_react_parse_fallback.py's
            # copilot round-3 case, where guidance-round prose must ship
            # byte-for-byte identical to before this fix). The retry exists
            # for the case that check does NOT cover.
            if not (_prose_looks_like_answer and is_guidance_round(iteration, max_it)):
                preview = raw_prose[:320].replace("\n", " ")
                logger.warning("ReAct parse failure (stage=%s): %s", f"react_{rn}", preview)
                emit("  Could not parse model decision — retrying with format correction…")
                _retry_context = (
                    reasoning_context
                    + "\n\n---\nYOUR PREVIOUS RESPONSE COULD NOT BE PARSED AS JSON. "
                    "Respond with ONLY a single valid JSON object matching the schema above "
                    "— no prose before or after the JSON, no markdown code fences. If you "
                    "were about to give your final answer, put that exact content in the "
                    "\"answer\" field of the final-answer shape (is_complete=true, tool=null)."
                )
                decision_raw_retry = _call_llm_json(
                    reasoning_system, _retry_context, ctx=ctx, stage=f"react_{rn}_retry",
                    composition_id=_reasoning_composition_id, composition_hash=_reasoning_composition_hash,
                    reasoning_depth=_bandit_reasoning_depth, latency_budget_ms=_bandit_latency_budget_ms,
                )
                decision = _parse_react_decision_json(decision_raw_retry)
                # Always adopt the retry's text, success or not -- it's the
                # more recent, "final" attempt. If it also failed, re-derive
                # the prose-fallback signal from what the model just wrote
                # (its second try), not the stale first attempt.
                decision_raw = decision_raw_retry
                if decision is not None:
                    emit("  Format-correction retry succeeded.")
                else:
                    logger.warning(
                        "ReAct parse failure PERSISTED after retry (stage=%s): %s",
                        f"react_{rn}", (decision_raw or "")[:320].replace("\n", " "),
                    )
                    raw_prose = (decision_raw or "").strip()
                    _prose_looks_like_answer = (
                        raw_prose
                        and len(raw_prose) >= 40
                        and not _TOOL_CALL_RE.search(raw_prose)
                    )

        if decision is None:
            emit("  Could not parse model decision — stopping.")
            if _prose_looks_like_answer and is_guidance_round(iteration, max_it):
                logger.info(
                    "[parse-fallback] guidance round %d prose answer len=%d (cid=%s)",
                    rn, len(raw_prose), getattr(ctx, "correlation_id", "?")[:8],
                )
                round_label = "guidance" if is_guidance_round(iteration, max_it) else f"round {rn}"
                emit(f"  Using model's synthesised answer ({round_label}) as the response.")
                logger.info(
                    "[parse-fallback] %s prose answer len=%d (cid=%s)",
                    round_label, len(raw_prose), getattr(ctx, "correlation_id", "?")[:8],
                )
                _finalize_response(
                    ctx, raw_prose, all_sources,
                    final_signal if final_signal != RETRIEVAL_SIGNAL_NO_SOURCES else RETRIEVAL_SIGNAL_SYSTEM_CONTEXT,
                    last_tool, emitter,
                )
                return
            # Do not throw away a good tool result (common with Gemini after a large Step 2 payload).
            # Prefer the most recent *successful* result — if round 2 was an
            # empty external search and round 1 found good corpus chunks, we
            # want round 1's result, not round 2's empty one.
            if tool_results:
                best_tr = next(
                    (tr for tr in reversed(tool_results) if tr.get("success")),
                    tool_results[-1],  # fall back to last if nothing succeeded
                )
                last_tr = best_tr
                last_res = (last_tr.get("result") or "").strip()
                last_sum = (last_tr.get("result_summary") or "").strip()
                # 2026-08-07 (Chat Master, cid 50fda638 finding): several
                # tools (appeals_validate_claim, appeals_find_carc,
                # appeals_get_playbook, lookup_authoritative_sources) return
                # "result" as _json.dumps(...) -- machine JSON meant for the
                # integrator to interpret, never meant to be shown to a user
                # directly. Confirmed live: appeals_validate_claim's raw
                # {"raw_text": "...", "rules_validated": 4} shipped as the
                # user-facing answer when this fallback fired. A result_summary
                # (human prose) is still safe to use raw; the JSON-shaped
                # "result" is not -- skip this fallback for it entirely
                # rather than guess at a repair, so it falls through to the
                # honest-escalate path below instead of showing raw JSON.
                _res_is_raw_json = bool(last_res) and last_res[:1] in ("{", "[") and not last_sum
                last_res_for_fallback = "" if _res_is_raw_json else last_res
                usable = last_res_for_fallback if len(last_res_for_fallback) >= 40 else last_sum
                if usable and (len(usable) >= 40 or (last_sum and last_tr.get("success"))):
                    emit("  Using the last tool output as the answer.")
                    lt_sig = final_signal
                    if last_tr.get("success"):
                        body = last_res_for_fallback
                        if last_sum and last_res_for_fallback and len(last_res_for_fallback) > len(last_sum) + 80:
                            body = compose_mobius_tool_envelope(last_sum, last_res_for_fallback)
                        _finalize_response(ctx, body, all_sources, lt_sig, last_tr.get("tool") or last_tool, emitter)
                    else:
                        # Short failures (e.g. "No URL") still beat a generic escalate.
                        _finalize_response(
                            ctx,
                            last_res_for_fallback or last_sum,
                            all_sources,
                            RETRIEVAL_SIGNAL_NO_SOURCES,
                            last_tr.get("tool") or last_tool,
                            emitter,
                        )
                    return
            # 2026-04-18 disconnect: _react_fallback_org_npi_lookup_decision
            # routed mangled reasoner output to the lookup_npi tool, which
            # no longer exists. Without a replacement fallback the loop
            # just breaks here — the integrator then produces an honest
            # "couldn't parse" message instead of dispatching to a dead
            # tool. When credentialing rebuilds as a skill, the fallback
            # should route to that skill's API instead of a chat tool.
            if decision is None:
                break

        tool = decision.get("tool")
        inputs = decision.get("inputs") or {}
        is_complete = decision.get("is_complete", False)
        thought = (decision.get("thought") or "").strip()

        # ── Evidence review (2026-08-07, Ananth, directly) ─────────────
        # react's own keep/discard verdict on the last tool result, plus
        # its recomputed running answer and remaining gaps -- emitted as
        # its own distinct progress lines (not folded into the "thought"
        # line above) specifically so this is trackable telemetry: every
        # round's keep-list, running_answer, and gaps is independently
        # visible in chat_progress_events, not just baked into an LLM
        # prompt string that only this process ever reads.
        def _as_str_list(v: Any) -> list[str]:
            if not isinstance(v, list):
                return []
            return [str(x).strip() for x in v if str(x).strip()]

        _evidence_review = decision.get("evidence_review")
        if not isinstance(_evidence_review, dict):
            _evidence_review = None
        if _evidence_review:
            _keep_raw = _evidence_review.get("keep")
            _keep: list[int] = []
            if isinstance(_keep_raw, list):
                for _k in _keep_raw:
                    try:
                        _keep.append(int(_k))
                    except (TypeError, ValueError):
                        continue
            _running_answer = str(_evidence_review.get("running_answer") or "").strip()
            _gaps_closed = _as_str_list(_evidence_review.get("gaps_closed"))
            _gaps_open = _as_str_list(_evidence_review.get("gaps_open"))

            # 2026-08-07 (Chat Master, Task #65): stats computed from the
            # ORIGINAL raw text (before pruning overwrites it with the
            # set-aside note) so they reflect what was actually kept, not
            # a post-pruning string that's already been rewritten.
            _kept_count, _kept_chars = (0, 0)
            if tool_results:
                _kept_count, _kept_chars = _kept_chunk_stats(
                    tool_results[-1].get("result") or "", _keep,
                )
            _sparse_evidence = (
                bool(_keep)
                and (_kept_count < _SPARSE_EVIDENCE_CHUNK_THRESHOLD
                     or _kept_chars < _SPARSE_EVIDENCE_CHAR_THRESHOLD)
            )

            if tool_results and _keep:
                _last_call_idx = len(tool_results)
                _before_len = len((tool_results[-1].get("result") or ""))
                tool_results[-1]["result"] = _prune_kept_chunks(
                    tool_results[-1].get("result") or "", _keep, _last_call_idx,
                )
                _after_len = len(tool_results[-1]["result"])
                emit(f"  Evidence review: keeping chunks {_keep} from call {_last_call_idx} ({_before_len}→{_after_len} chars)")
                if _sparse_evidence:
                    emit(f"  ⚠ Thin evidence: {_kept_count} chunk(s), {_kept_chars} chars — hedge required")
            elif tool_results:
                emit("  Evidence review: no chunks marked relevant from the last result")

            if _running_answer:
                emit(f"  Running answer: {_running_answer}")
            if _gaps_closed:
                emit(f"  Gaps closed: {_gaps_closed}")
            if _gaps_open:
                emit(f"  Gaps open: {_gaps_open}")

            if _running_answer or _gaps_open or _gaps_closed:
                ctx._evidence_review_latest = {  # type: ignore[attr-defined]
                    "round": rn, "running_answer": _running_answer,
                    "gaps_closed": _gaps_closed, "gaps_open": _gaps_open,
                    "sparse_evidence": _sparse_evidence,
                    "kept_chunk_count": _kept_count, "kept_chunk_chars": _kept_chars,
                }

        # 2026-08-07 (Task #58, "factory model" directive, schema approved
        # by coordinator) -- this IS the EvidenceLedger (task #48): gap
        # tracking and tool-output enrichment are the same mechanism.
        # Written onto the SAME dict ctx.react_trace_rounds already
        # appended for this round (line ~3146, before the LLM call) --
        # mutated here now that thought/tool/inputs/evidence_review are
        # known, rather than a second parallel structure.
        #
        # raw_result_ref is a POINTER into ctx.tool_outputs
        # ({tool_name, call_index}), not a copy of the payload -- keeps
        # the trace lean, avoids duplicating tool output twice in ctx.
        # call_index is 1-based, counting occurrences of that tool_name
        # in tool_results up to and including the entry this round is
        # enriching (i.e. "the Nth call to this tool this turn") -- the
        # most literal addressable meaning available given tool_outputs
        # itself is typed/deduped per family (e.g. appeals.playbook
        # collapses repeated calls to the winning one), not a flat
        # indexable list per tool name.
        _raw_result_ref: dict | None = None
        if tool_results:
            _ref_tool = tool_results[-1].get("tool")
            _call_index = sum(1 for _t in tool_results if _t.get("tool") == _ref_tool)
            _raw_result_ref = {"tool_name": _ref_tool, "call_index": _call_index}

        if ctx.react_trace_rounds and ctx.react_trace_rounds[-1].get("round") == rn:
            ctx.react_trace_rounds[-1].update({
                "tool": tool,
                "inputs": inputs,
                "raw_result_ref": _raw_result_ref,
                "enrichment": {
                    "learned": thought,
                    "running_answer": _evidence_review.get("running_answer") if _evidence_review else "",
                    "gaps_closed": _as_str_list(_evidence_review.get("gaps_closed")) if _evidence_review else [],
                    "gaps_open": _as_str_list(_evidence_review.get("gaps_open")) if _evidence_review else [],
                } if (thought or _evidence_review) else None,
            })

        # Task mode: no tool calls ever. If the LLM tried to call a tool
        # despite the task-mode system prompt, finalize immediately.
        # We cannot rely on setting is_complete=True + tool=None because the
        # "empty answer → fall through" path (line ~2217) lets the loop
        # continue to round 2 where search_corpus fires. Instead, call
        # _finalize_response directly and return so execution never reaches
        # the tool dispatch block.
        if tool and react_chat_mode_label(getattr(ctx, "chat_mode", None)) == "task":
            _task_answer = (decision.get("answer") or thought or "").strip()
            logger.debug(
                "[task-mode] suppressing tool call '%s'; finalizing with answer len=%d (cid=%s)",
                tool, len(_task_answer), getattr(ctx, "correlation_id", "?")[:8],
            )
            _finalize_response(
                ctx, _task_answer, all_sources,
                RETRIEVAL_SIGNAL_SYSTEM_CONTEXT if getattr(ctx, "system_context", None) else RETRIEVAL_SIGNAL_NO_SOURCES,
                last_tool, emitter,
            )
            return

        if thought:
            emit(f"  → Round {rn}: {thought}")

        if is_complete or not tool:
            answer = decision.get("answer", "")
            # Product-feedback (docs/feedback-agent-spec.md §6): honor the
            # planner's offer_feedback ONLY when a cadence signal was actually
            # injected this turn — the eligibility ceiling lives in code, so the
            # model can't fabricate an ask when it isn't due.
            _of = decision.get("offer_feedback")
            if isinstance(_of, dict) and ctx.feedback_signal:
                ctx.offer_feedback = {
                    "kind": str(_of.get("kind") or ctx.feedback_signal.get("kind") or "generic"),
                    "trigger": "periodic",
                }
            if answer:
                # Product Promise governor — mandatory groundedness floor
                # (docs/REACT_PRODUCT_PROMISE_SPEC.md), behind
                # MOBIUS_PRODUCT_PROMISE_ENABLED, confidence_bar in
                # {"medium","high"} only — "low" (quick mode) trusts
                # self-report as today, no added call. Runs BEFORE and
                # INDEPENDENTLY of the existing critic_enabled()/
                # should_run_critic()-gated path below, which is completely
                # unchanged for every other case (Chat Architecture ruling,
                # 2026-07-30: this flag is the only thing authorized to
                # bypass those gates).
                if _pp_enabled and _pp_contract is not None and _pp_contract.confidence_bar in ("medium", "high"):
                    from app.pipeline.personalization import splice_user_profile as _pp_splice_profile
                    from app.pipeline.react.critic import (
                        CRITIC_SYSTEM_PROMPT as _pp_critic_prompt,
                        build_critic_user_message as _pp_build_critic_msg,
                        compute_groundedness_heuristic,
                        format_critique_as_observation as _pp_format_critique_obs,
                        groundedness_heuristic_enabled,
                        parse_critic_response as _pp_parse_critic,
                    )
                    from app.pipeline.react.governor import RoundState, evaluate

                    _pp_critic_system = _pp_splice_profile(_pp_critic_prompt, getattr(ctx, "user_profile", None))
                    _pp_critic_comp_id: int | None = None
                    _pp_critic_comp_hash: str | None = None
                    if _react_prompt_source_v2:
                        from app.pipeline.react.critic import resolve_critic_system_prompt_v2
                        _pp_resolved_critic = resolve_critic_system_prompt_v2(getattr(ctx, "user_profile", None))
                        if _pp_resolved_critic is not None:
                            _pp_critic_system = _pp_resolved_critic.system_prompt
                            _pp_critic_comp_id = _pp_resolved_critic.composition_id
                            _pp_critic_comp_hash = _pp_resolved_critic.composition_hash
                    _pp_critic_raw = _call_llm_json(
                        _pp_critic_system,
                        _pp_build_critic_msg(
                            question=ctx.effective_message or ctx.message or "",
                            draft_answer=answer, sources=all_sources, tool_results=tool_results,
                        ),
                        ctx=ctx, stage="critique", max_tokens=1200,
                        composition_id=_pp_critic_comp_id, composition_hash=_pp_critic_comp_hash,
                    )
                    _pp_critique = _pp_parse_critic(_pp_critic_raw)
                    _pp_groundedness_passed = not _pp_critique.has_blocking_issues
                    # react_trace diagnostics: record that the mandatory floor
                    # actually ran + its verdict, distinct from the separate
                    # (optional) critic_enabled()-gated path below.
                    ctx.react_groundedness_floor_ran = True
                    ctx.react_groundedness_passed = _pp_groundedness_passed
                    if groundedness_heuristic_enabled():
                        ctx.react_groundedness_score = compute_groundedness_heuristic(_pp_critique.issues)

                    _pp_elapsed_s = _pp_time_mod.monotonic() - _pp_turn_start
                    _pp_post_state = RoundState(
                        proposes_complete=True,
                        self_reported_confidence=decision.get("confidence"),
                        # The existing critic_enabled()-gated path below is
                        # separate and unmerged with this dedicated floor —
                        # its verdict isn't folded in here.
                        critic_verdict=None,
                        groundedness_passed=_pp_groundedness_passed,
                        elapsed_s=_pp_elapsed_s,
                        base_rounds_remaining=max_it - rn,
                        extension_rounds_available=_pp_contract.max_extension_rounds - _pp_extension_rounds_used,
                    )
                    _pp_directive, _pp_reason = evaluate(_pp_contract, _pp_post_state)
                    # Telemetry marker (Chat Architecture clarification,
                    # 2026-07-30): distinguishes a verified-clean "complete"
                    # from a budget-forced "finalize" for anything downstream
                    # that tracks turn quality — content is unaffected either way.
                    ctx.product_promise_directive = _pp_directive

                    if _pp_directive == "extend":
                        _pp_extension_rounds_used += 1
                        max_it += 1
                        tool_results.append({
                            "tool": "_product_promise_groundedness",
                            "success": False,
                            "result": _pp_format_critique_obs(_pp_critique.high_severity_issues),
                        })
                        continue

                    logger.info("[react] product_promise directive=%s round=%d reason=%s", _pp_directive, rn, _pp_reason)

                    if _pp_directive == "finalize":
                        # Same "Groundedness notice" ship-with-warning
                        # content as the existing critic_enabled() path
                        # below — no content swap, directive="finalize" is
                        # the only new signal (see the marker above).
                        warning_lines = [
                            "", "---",
                            "⚠ **Groundedness notice:** the following claims in this "
                            "answer could not be verified against the retrieved sources:",
                        ]
                        for i, issue in enumerate(_pp_critique.high_severity_issues, 1):
                            claim_preview = issue.claim
                            if len(claim_preview) > 150:
                                claim_preview = claim_preview[:150].rstrip() + "…"
                            warning_lines.append(f"  {i}. {claim_preview}")
                        warning_lines.append("Verify these specifically before acting on them.")
                        answer = answer.rstrip() + "\n" + "\n".join(warning_lines)
                        _finalize_response(ctx, answer, all_sources, final_signal, last_tool, emitter)
                        return
                    # directive == "complete": fall through to the existing
                    # logic below unchanged (which may still independently
                    # run the optional critic_enabled() path — intentional,
                    # per Chat Architecture: that path stays untouched).
                elif _pp_enabled and _pp_contract is not None:
                    # confidence_bar == "low" (quick mode): trust the
                    # self-reported completion as today, no added critic
                    # call. Telemetry-only marker so downstream analytics
                    # sees "complete" uniformly across every mode instead of
                    # None for quick mode vs. an explicit directive
                    # everywhere else — doesn't affect gating (Product
                    # Promise, 2026-07-30).
                    ctx.product_promise_directive = "complete"

                # ── Critic gate (Phase groundedness-v1) ──────────────
                # Before finalizing, audit the draft against collected
                # sources. If the critic flags high-severity ungrounded
                # claims AND we have rounds left, inject the critique as
                # a synthetic observation so the planner gets specific
                # feedback and runs another round. On the last round we
                # ship anyway (falling closed would mean no answer at
                # all on stubborn hallucinations) but append a warning.
                #
                # Gated behind MOBIUS_REACT_CRITIC env flag (default OFF
                # in the rollout commit) so operators can turn it on
                # per environment after validation.
                from app.pipeline.react.critic import (
                    CRITIC_SYSTEM_PROMPT,
                    build_critic_user_message,
                    compute_groundedness_heuristic,
                    critic_enabled,
                    format_critique_as_observation,
                    groundedness_heuristic_enabled,
                    parse_critic_response,
                )

                _cache_skip, _cache_skip_reason = _cache_preaudited_critic_skip(
                    ctx, tool_results, rn,
                )
                if _cache_skip:
                    # The finalized answer is grounded in an already-
                    # critic-approved cached turn; re-auditing is
                    # redundant work that adds 5–10s per turn. Skip
                    # straight to finalize. Emit a signal so the skip
                    # is visible in thinking_log + analytics.
                    if emitter:
                        from app.communication.emit_envelope import make_note
                        emitter(make_note(
                            correlation_id=ctx.correlation_id,
                            note=f"✓ Critic skipped: cache answer pre-audited ({_cache_skip_reason})",
                            round=rn,
                            thread_id=ctx.thread_id,
                            user_id=getattr(ctx, "user_id", None),
                        ).to_dict())
                    _finalize_response(
                        ctx, answer, all_sources, final_signal, last_tool, emitter,
                    )
                    return

                if critic_enabled():
                    # ── Deterministic invocation gate ──────────────────
                    # Skip the critic when the answer contains no specific
                    # verifiable claims (numeric facts, codes, deadlines).
                    # Process/policy prose has low hallucination risk and
                    # the critic LLM call adds 2–5s of latency for nothing.
                    from app.pipeline.react.critic import should_run_critic
                    _critic_should_run, _critic_gate_reason = should_run_critic(
                        answer=answer,
                        all_sources=all_sources,
                        final_signal=final_signal,
                        user_message=getattr(ctx, "message", "") or "",
                    )
                    if not _critic_should_run:
                        if emitter:
                            from app.communication.emit_envelope import make_note
                            emitter(make_note(
                                correlation_id=ctx.correlation_id,
                                note=f"✓ Critic skipped (gate): {_critic_gate_reason}",
                                round=rn,
                                thread_id=ctx.thread_id,
                                user_id=getattr(ctx, "user_id", None),
                            ).to_dict())
                        logger.debug(
                            "[critic-gate] skipping critic: %s (cid=%s)",
                            _critic_gate_reason,
                            (ctx.correlation_id or "")[:8],
                        )
                        _finalize_response(
                            ctx, answer, all_sources, final_signal, last_tool, emitter,
                        )
                        return

                    rounds_remaining = (max_it - rn)  # not counting this round's decision
                    # 2026-04-19 (Sprint A.1 commit 1): critic emits
                    # now produce structured envelopes via the
                    # make_critic_* helpers in
                    # app/communication/emit_envelope.py. The legacy
                    # emit(str) path still works elsewhere in the
                    # loop; we're migrating one block at a time.
                    from app.communication.emit_envelope import (
                        make_critic_approved,
                        make_critic_approved_after_retry,
                        make_critic_audit_started,
                        make_critic_flagged,
                        make_rounds_exhausted_with_warning,
                    )
                    _emit_env = emitter  # on_thinking accepts dicts now
                    cid = ctx.correlation_id
                    tid = ctx.thread_id
                    uid = getattr(ctx, "user_id", None)

                    if _emit_env:
                        _emit_env(make_critic_audit_started(
                            correlation_id=cid,
                            round=rn,
                            draft_length=len(answer or ""),
                            sources_count=len(all_sources or []),
                            thread_id=tid,
                            user_id=uid,
                        ).to_dict())
                    # Stage 'critique' (not 'react_critic') routes to the
                    # existing cheap-model bucket in model_registry:
                    #   - Latency cap: 15s (vs planner's 90s)
                    #   - Cost cap: $0.006 (vs planner's $0.12)
                    #   - Eligible models: Haiku / Flash class (critic is
                    #     a narrow JSON-audit task; doesn't need Sonnet)
                    #   - Listed in CHEAP_STAGES so the bandit treats it
                    #     accordingly.
                    # 'react_critic' would have fallen through to the
                    # planner bucket via the stage.startswith('react_')
                    # branch — wrong pool for this workload.
                    # 2026-05-06: critic also reads user_profile via
                    # rendered_prompt — lets it grade the draft against
                    # the user's preference shape (tone, experience
                    # level, autonomy gating) on top of grounding.
                    from app.pipeline.personalization import splice_user_profile as _splice_critic
                    _critic_system = _splice_critic(CRITIC_SYSTEM_PROMPT, getattr(ctx, "user_profile", None))
                    _critic_composition_id: int | None = None
                    _critic_composition_hash: str | None = None
                    if _react_prompt_source_v2:
                        from app.pipeline.react.critic import resolve_critic_system_prompt_v2
                        _resolved_critic = resolve_critic_system_prompt_v2(getattr(ctx, "user_profile", None))
                        if _resolved_critic is not None:
                            _critic_system = _resolved_critic.system_prompt
                            _critic_composition_id = _resolved_critic.composition_id
                            _critic_composition_hash = _resolved_critic.composition_hash
                    critic_raw = _call_llm_json(
                        _critic_system,
                        build_critic_user_message(
                            question=ctx.effective_message or ctx.message or "",
                            draft_answer=answer,
                            sources=all_sources,
                            tool_results=tool_results,
                        ),
                        ctx=ctx,
                        stage="critique",
                        max_tokens=1200,
                        composition_id=_critic_composition_id,
                        composition_hash=_critic_composition_hash,
                    )
                    critique = parse_critic_response(critic_raw)
                    if groundedness_heuristic_enabled():
                        ctx.react_groundedness_score = compute_groundedness_heuristic(critique.issues)

                    if critique.has_blocking_issues and rounds_remaining > 0:
                        # Inject the critique + keep going. Planner sees
                        # the flagged claims next round and either finds
                        # evidence or revises.
                        high = critique.high_severity_issues
                        if _emit_env:
                            _emit_env(make_critic_flagged(
                                correlation_id=cid,
                                round=rn,
                                total_issues=len(critique.issues),
                                high_severity=len(high),
                                flagged_claims=[i.claim for i in high],
                                rounds_remaining=rounds_remaining,
                                thread_id=tid,
                                user_id=uid,
                            ).to_dict())
                        # Track that this turn had a retry, so when a
                        # later round is approved we can emit
                        # critic_approved_after_retry (promoted) vs.
                        # plain critic_approved (chat-side only).
                        _critic_retries_this_turn += 1
                        tool_results.append({
                            "tool": "_critic",
                            "success": False,
                            "result": format_critique_as_observation(high),
                        })
                        # Round counter increments via `continue`; the
                        # reasoning_context builder will pick up the new
                        # synthetic observation on the next pass.
                        continue

                    if critique.has_blocking_issues and rounds_remaining == 0:
                        # Last round — ship anyway, but annotate so the
                        # reader sees this answer is suspect. Honest
                        # degradation beats silent hallucination.
                        warning_lines = [
                            "",
                            "---",
                            "⚠ **Groundedness notice:** the following claims in this "
                            "answer could not be verified against the retrieved sources:",
                        ]
                        for i, issue in enumerate(critique.high_severity_issues, 1):
                            claim_preview = issue.claim
                            if len(claim_preview) > 150:
                                claim_preview = claim_preview[:150].rstrip() + "…"
                            warning_lines.append(f"  {i}. {claim_preview}")
                        warning_lines.append(
                            "Verify these specifically before acting on them."
                        )
                        answer = answer.rstrip() + "\n" + "\n".join(warning_lines)
                        if _emit_env:
                            _emit_env(make_rounds_exhausted_with_warning(
                                correlation_id=cid,
                                round=rn,
                                unresolved_claims=[i.claim for i in critique.high_severity_issues],
                                thread_id=tid,
                                user_id=uid,
                            ).to_dict())
                    else:
                        # Critic approved. If this turn had any
                        # previous retries, this is a self-correction
                        # worth promoting to task-manager analytics.
                        # First-try approvals are the common case and
                        # stay chat-side-only.
                        if _emit_env:
                            if _critic_retries_this_turn > 0:
                                _emit_env(make_critic_approved_after_retry(
                                    correlation_id=cid,
                                    round=rn,
                                    retry_count=_critic_retries_this_turn,
                                    issues_resolved=[i.claim for i in critique.issues],
                                    thread_id=tid,
                                    user_id=uid,
                                ).to_dict())
                            else:
                                _emit_env(make_critic_approved(
                                    correlation_id=cid,
                                    round=rn,
                                    thread_id=tid,
                                    user_id=uid,
                                ).to_dict())

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
            # Task mode: do NOT fall through to the tool dispatch block
            # below.  When tool=None and answer="" we'd hit line ~2284
            # which defaults ``tool or "search_corpus"`` and silently runs
            # a corpus search — exactly what task mode forbids.  Finalize
            # immediately with whatever we have (empty string is fine;
            # caller sees a clean empty turn rather than a spurious search).
            if react_chat_mode_label(getattr(ctx, "chat_mode", None)) == "task":
                _finalize_response(
                    ctx,
                    ctx.final_message or "",
                    all_sources,
                    RETRIEVAL_SIGNAL_SYSTEM_CONTEXT if getattr(ctx, "system_context", None) else RETRIEVAL_SIGNAL_NO_SOURCES,
                    last_tool,
                    emitter,
                )
                return

        # Final-round self-report (see _REACT_FINAL_ROUND_INSTRUCTION) OR the
        # structural-exhaustion offramp (_REACT_STRUCTURAL_EXHAUSTION_OFFRAMP)
        # firing early: either way, the model was told it's legal to stop
        # here, and it did (tool is falsy, unfinished_reason present). Break
        # out of the loop NOW rather than default to `tool or "search_corpus"`
        # below — that would re-run the SAME call as a prior round (empty
        # inputs, same tool), which the retry-guard's duplicate-signature
        # check then blocks and finalizes through ITS OWN summary path,
        # never reaching the exhausted-iterations fallback below that
        # actually reads unfinished_reason/unfinished_summary/unblock_ask.
        #
        # `break`, not `continue`: on the true final round (rn==max_it) these
        # are equivalent — the loop's own `if iteration >= max_it: break`
        # bound check fires immediately on the next pass either way. But on
        # an EARLY offramp round (rn < max_it, structurally_exhausted), a
        # `continue` would just advance to iteration+1 and burn another round
        # instead of actually finalizing — caught live 2026-08-04 (Ananth:
        # "why are we forcing 3 rounds when we already exhausted... doesn't
        # it have to be dynamic") building the offramp itself: the FIRST
        # version of this offramp used the old `continue` and silently kept
        # looping past the round where the model had already said it was
        # done, all the way to max_it anyway — defeating the entire point.
        # Caught by a real scripted test asserting the exact round sequence,
        # not by inspection.
        if not tool and decision.get("unfinished_reason"):
            break

        # Phase 0.7: block repeat call if (tool, inputs) already failed and
        # no new evidence has come in since.
        blocked_by = retry_guard.should_block(
            tool=tool or "search_corpus",
            inputs=inputs,
            current_results_count=len(tool_results),
        )
        if blocked_by is not None:
            # Phase 0.19: distinguish tool-exhaustion ("this tool has failed
            # twice — re-phrasing won't help, pick a different tool") from
            # the Phase 0.7 same-signature block ("this exact call already
            # failed with no new evidence since").
            if blocked_by.error_code == "tool_exhausted":
                if emitter:
                    from app.communication.emit_envelope import make_tool_exhausted
                    emitter(make_tool_exhausted(
                        correlation_id=ctx.correlation_id,
                        round=rn,
                        tool=blocked_by.tool,
                        attempts=blocked_by.round,
                        thread_id=ctx.thread_id,
                        user_id=getattr(ctx, "user_id", None),
                    ).to_dict())
                skip_reason = "(skipped — tool exhausted; pick a different tool)"
            else:
                emit(
                    f"  ⊘ Already tried {blocked_by.tool} with these inputs "
                    f"(round {blocked_by.round}, {blocked_by.error_code or 'failed'}) "
                    f"— picking a different path."
                )
                skip_reason = "(skipped — previously failed with no new evidence since)"
            # Record a synthetic result so the LLM sees we acknowledged the skip
            # and won't re-pick the same thing next round.
            tool_results.append({
                "tool": tool or "search_corpus",
                "success": False,
                "result": skip_reason,
            })
            continue

        emit(f"  Using {tool or 'unknown'}…")
        # 2026-04-18 disconnect: contextual emit lines for the removed
        # credentialing tools deleted — those tools aren't in the manifest
        # so the planner can't pick them, and if it hallucinates the name
        # anyway the generic "Using <tool>…" above is enough.
        results_before = len(tool_results)
        # Phase 0.7 + 0.13: convert raised exceptions into a typed failed-tool
        # result AND auto-retry recoverable errors once, honoring the
        # retry_after_seconds hint on the classifier envelope. One retry per
        # call keeps the blast radius small; if it still fails, the retry
        # guard + fail-fast machinery take over.
        result = _execute_tool_with_retry(
            tool or "search_corpus", inputs, ctx, rn, emit, emitter,
            skip_retry=(mode_label == "quick"),
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
        # Preserve section hints so _finalize_response collects them into
        # ctx.tool_section_hints for the integrator's pre_built_sections.
        if result.get("section_hint"):
            tr_entry["section_hint"] = result["section_hint"]
        if result.get("section_hints"):
            tr_entry["section_hints"] = result["section_hints"]
        tool_results.append(tr_entry)
        _store_evidence_memory(ctx, len(tool_results), tr_entry.get("result") or "")
        _checkpoint_best_evidence(ctx, tool_results)

        # §5b bypass: if the tool marked ctx.react_bypass_integrate, exit the
        # ReAct loop immediately without calling the LLM for another round.
        # ctx.final_message was already set by the tool (plain status message).
        if getattr(ctx, "react_bypass_integrate", False):
            return

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

        # Golden-answer early exit: any specific-knowledge skill (registered
        # via the skill registry OR answer_tool) that sets result["golden"]=True
        # is declaring itself authoritative. Finalize immediately — do NOT let
        # the loop escalate to google_search or further retrieval, which would
        # anchor composition on web content and discard the skill's answer.
        #
        # golden is set by the dispatch return blocks above. Skills opt-in via:
        #   (a) env.extra["golden"] = True  — explicit skill-level opt-in
        #   (b) heuristic: success + sources + non-empty signal (inferred)
        # Operational skills (create_task, list_uploads, etc.) produce no sources
        # and RETRIEVAL_SIGNAL_NO_SOURCES so they never match.
        # golden_explicit bypasses the 30-char length gate — short certified facts
        # (e.g. a payer ID "68069") are valid terminal answers from the fact store.
        if (
            result.get("golden")
            and result.get("success")
            and (
                result.get("golden_explicit")
                or len((result.get("result") or "").strip()) >= 30
            )
        ):
            emit(f"  ✓ {last_tool}: authoritative answer — finalizing.")
            _finalize_response(
                ctx,
                (result.get("result") or "").strip(),
                all_sources,
                final_signal,
                last_tool,
                emitter,
            )
            return

        # Fast mode early exit: if round 1 returns a usable result, skip the
        # second LLM reasoning pass. Round 2 is still available as fallback when
        # round 1 fails or returns nothing (complex / multi-hop questions).
        #
        # 2026-08-07 (Chat Master relaying Ananth's UX contract, Task #65
        # follow-up): this used to ship round 1's raw result unconditionally
        # -- confirmed live (cid d288d009) as the same fabrication mechanism
        # #65 fixed for the normal reasoning path: no evidence_review ran
        # here at all, so a raw, mismatched chunk dump shipped as a
        # confident-looking answer. Neither branch below adds a reasoning
        # round -- latency is unchanged either way, per Ananth's "always
        # stream something fast" requirement.
        if (
            mode_label == "quick"
            and rn == 1
            and result.get("success")
            and len((result.get("result") or "").strip()) >= 30
        ):
            _raw_text = (result.get("result") or "").strip()
            _chunk_count, _total_chars = _all_chunk_stats(_raw_text)
            # 2026-08-07 (Ananth, directly, live screenshot -- "Can you tell
            # me how to appeal for a sunshine health COB denial?"): the
            # three-signal gate below only makes sense for chunk-numbered
            # rag results ("[N] Doc\ntext"). Non-rag tools (appeals_find_carc
            # etc.) return their OWN structured JSON, which _all_chunk_stats
            # correctly reads as 0 chunks -- but that's not "thin evidence,"
            # it's just a different tool's result shape. Without this
            # guard, EVERY successful non-rag fast-mode call got forced
            # into the hedge path regardless of how good the actual data
            # was -- confirmed live: appeals_find_carc found real CARC 22 +
            # rules, but shipped the fallback "couldn't confirm specific
            # details" hedge instead. Non-rag tools already gate their own
            # quality via `success` (see corpus_search.py's rag() vs.
            # appeals_get_playbook's usable-content check earlier today) --
            # the chunk/score heuristic is rag-specific and must stay that way.
            if _chunk_count == 0:
                emit("  ⚡ Fast mode: using first tool answer.")
                _finalize_response(ctx, _raw_text, all_sources, final_signal, last_tool, emitter)
                return
            _sources_for_score = result.get("sources") or []
            _top_score = max(
                (float(s.get("rerank_score") or 0.0) for s in _sources_for_score),
                default=0.0,
            )
            # 2026-08-07 (Ananth, directly, live finding): score used to
            # be AND'd into the gate alongside chunk count/chars -- a
            # turn with 15 chunks / 11,685 chars (well past both
            # thresholds) still fell into the hedge path because
            # rerank_score wasn't populated for those chunks (top_score
            # computed as 0.00, well under _FAST_MODE_MIN_SCORE). Zero
            # score is not the same as thin evidence -- a 15-chunk corpus
            # is substantial regardless of whether a score field happens
            # to be populated, and agentic mode on the SAME evidence gave
            # a fuller answer, confirming the volume was genuinely there.
            # Score no longer gates the early-exit decision at all; only
            # count/chars (the actual "is there enough here" question)
            # decide it now. _top_score is still computed and logged
            # below for diagnostics on both paths.
            _rich_evidence = (
                _chunk_count >= _FAST_MODE_MIN_CHUNKS
                and _total_chars >= _FAST_MODE_MIN_CHARS
            )
            if _rich_evidence:
                # 2026-08-07 (Ananth, directly, live finding): this used
                # to ship _raw_text verbatim -- the raw "[1] Sunshine
                # Provider Manual...[2] Provider_Manual.pdf..." dump was
                # what appeared on the Summary tab while the Answer tab
                # (integrator, which DOES synthesize from ctx) looked
                # correct. The grace rule applies here too: react_draft
                # must always be a synthesized, human-readable answer,
                # never raw chunk text. Falls back to _raw_text itself
                # (not the thin-path hedge) if synthesis fails -- rich
                # evidence is still substantial evidence even unsynthesized.
                emit("  ⚡ Fast mode: synthesizing from corpus evidence.")
                _synthesized_rich = _fast_mode_synthesize_answer(
                    (ctx.effective_message or ctx.message or ""), _raw_text, ctx,
                    stage=f"react_{rn}_fast_synthesis", system=_FAST_MODE_RICH_SYNTHESIS_SYSTEM,
                )
                _body_rich = _synthesized_rich if _synthesized_rich else _raw_text
                _finalize_response(ctx, _body_rich, all_sources, final_signal, last_tool, emitter)
                return
            # Thin evidence: ONE lightweight synthesis pass on what's
            # available (2026-08-07, Ananth, directly -- "always let
            # ReAct summarize, even on early exit," a grace rule) --
            # NOT a full reasoning round, so still materially faster
            # than agentic's 2-round path on the same evidence. Falls
            # back to the pure code-constructed hedge if the synthesis
            # call itself fails or returns nothing usable.
            # react_unfinished_reason="no_path_forward" is the EXISTING
            # signal integrate.py's suggest_escalate check already reads
            # (app/stages/integrate.py:1090-1096) — reusing it, not
            # duplicating the "Try with Think mode" wiring.
            emit(
                f"  ⚡ Fast mode: evidence too thin ({_chunk_count} chunks, {_total_chars} chars, "
                f"top score {_top_score:.2f}) — synthesizing a best-effort answer, then hedging."
            )
            _synthesized = _fast_mode_synthesize_answer(
                (ctx.effective_message or ctx.message or ""), _raw_text, ctx,
                stage=f"react_{rn}_fast_synthesis",
            )
            if _synthesized:
                emit("  ⚡ Fast mode: synthesis complete — appending Think mode suggestion.")
                _body = f"{_synthesized}\n\nFor a more complete, verified answer, try Think mode."
            else:
                emit("  ⚡ Fast mode: synthesis call failed — falling back to evidence excerpt.")
                _body = _build_fast_mode_hedge(_raw_text, _chunk_count)
            ctx.react_unfinished_reason = "no_path_forward"
            _finalize_response(ctx, _body, all_sources, final_signal, last_tool, emitter)
            return

        # 2026-04-18 disconnect: the roster-report early-exit (which
        # fired when a credentialing tool returned
        # RETRIEVAL_SIGNAL_ROSTER_COMPLETE) is gone along with those
        # tools. The generic "is_complete=true from the reasoner" path
        # still works for any remaining tool that returns a final answer.

        if result.get("is_terminal"):
            # Generalized 2026-08-06 for appeals_assemble_letter's RECITAL
            # passthrough (was hardcoded to refuse's empty-string case only
            # -- confirmed dead for any other tool since refuse ALSO sets
            # react_bypass_integrate, which exits the loop earlier at the
            # §5b bypass check above, so this branch was unreachable before
            # today). Uses the tool's own result text as the final answer
            # instead of assuming "nothing to show" -- correct for refuse
            # too (result="reason") if it's ever reached this path, and
            # required for appeals_assemble_letter (result=the letter).
            emit(f"  Stopping ({last_tool or 'terminal result'}).")
            _finalize_response(
                ctx,
                (result.get("result") or "").strip(),
                [],
                result.get("signal") or RETRIEVAL_SIGNAL_NO_SOURCES,
                last_tool,
                emitter,
            )
            return

        # 2026-04-18 disconnect: the dual-finalize early exit was tuned
        # for credentialing tools (find_org_locations + find_associated_
        # providers_at_locations) that returned summary+full-markdown in
        # one result. Those tools are gone; the generic "exhausted
        # iterations + last_tool has summary+markdown" fallback a few
        # lines below still handles any future tool that produces that
        # shape.

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

    # Task mode: never emit the corpus-search escalation.  If the loop
    # exhausted without a finalisation, return whatever partial answer we
    # accumulated (may be empty) rather than a message containing
    # "verified answer" / "searching the web" which would confuse callers
    # and trip test-detection strings.
    if react_chat_mode_label(getattr(ctx, "chat_mode", None)) == "task":
        _finalize_response(
            ctx,
            ctx.final_message or "",
            all_sources,
            RETRIEVAL_SIGNAL_SYSTEM_CONTEXT if getattr(ctx, "system_context", None) else RETRIEVAL_SIGNAL_NO_SOURCES,
            last_tool,
            emitter,
        )
        return

    emit("  No verified answer after checking materials and web — escalating honestly.")
    # Prefer the model's own final-round self-report (see
    # _REACT_FINAL_ROUND_INSTRUCTION above) over a generic string — `decision`
    # still holds the last executed round's parsed JSON (for-loops don't
    # scope in Python; a mid-parse failure sets it to None, guarded below).
    # Fail-soft: any reason the model didn't comply (older/non-compliant
    # model, malformed JSON, this round never actually reached the final-
    # round instruction) falls through to the pre-existing generic string —
    # zero regression risk on non-compliance.
    _generic_fallback = (
        "I wasn't able to find a verified answer to this question "
        "after checking our materials and searching the web. "
        "You may want to contact the payer directly or provide a link to their documentation."
    )
    _uf_reason = decision.get("unfinished_reason") if decision else None
    if _uf_reason in ("need_more_time", "need_more_info", "no_path_forward"):
        _uf_summary = (decision.get("unfinished_summary") or "").strip()
        _uf_unblock = (decision.get("unblock_ask") or "").strip()
        # react_trace diagnostics: record the self-report regardless of
        # which rendering branch below actually fires.
        ctx.react_unfinished_reason = _uf_reason
        ctx.react_unfinished_summary = _uf_summary or None
        ctx.react_unblock_ask = _uf_unblock or None
        parts = [_uf_summary] if _uf_summary else []
        if _uf_reason == "need_more_time" and mode_label != "agentic":
            parts.append(
                f"I ran out of rounds in {mode_label} mode before I could "
                "fully resolve this. Try asking again in **Agentic mode**, "
                "which allows more time and a deeper search."
            )
        elif _uf_reason == "need_more_info" and _uf_unblock:
            parts.append(f"To help me find this: {_uf_unblock}")
        else:
            # no_path_forward, OR need_more_time with no higher mode tier
            # left to offer, OR need_more_info without a specific enough
            # ask — genuinely stuck, nothing further react itself can try.
            parts.append(
                "I don't have a specific next step to try from here — "
                "rephrasing the question or providing more specific details "
                "(exact plan name, code, or a link to their documentation) "
                "may help."
            )
        honest = "\n\n".join(parts) if parts else _generic_fallback
    else:
        # 2026-08-08 (Ananth, directly, live finding): a parse failure here
        # (decision is None -- both the original attempt and the format-
        # correction retry failed) used to fall straight to the generic
        # "I wasn't able to find..." boilerplate, discarding whatever real
        # evidence react actually accumulated across EARLIER rounds that
        # DID parse successfully. Confirmed live: 5 rounds of real rag
        # results (genuinely relevant multidisciplinary-team/care-
        # coordination content), round 6's decision failed to parse twice,
        # and the boilerplate shipped even though the Answer tab's own
        # integrator synthesized a detailed, accurate answer from the SAME
        # ctx.rag_chunks seconds later. "It should probably provide the
        # basic answer it has" -- try the last round's own running_answer
        # first (it's exactly "the best answer built so far"), then a
        # quick recovery synthesis over what was actually retrieved,
        # before resorting to the boilerplate.
        _ev_latest = getattr(ctx, "_evidence_review_latest", None)
        _fallback_running_answer = (
            (_ev_latest or {}).get("running_answer", "").strip()
            if isinstance(_ev_latest, dict) else ""
        )
        _low_confidence_caveat = (
            "\n\n_I wasn't fully confident in this answer and couldn't complete "
            "my usual verification — treat it as a starting point, not a final word._"
        )
        if _fallback_running_answer:
            honest = _fallback_running_answer + _low_confidence_caveat
        else:
            # ctx.rag_chunks doesn't exist yet -- it's only set INSIDE
            # _finalize_response, which is the call we're building `honest`
            # for. all_sources is the same underlying data (SourceRef.to_dict()
            # shape: document_name/text/index), already accumulated across
            # every round's tool call and in scope right here.
            _fallback_evidence_text = "\n\n".join(
                f"[{s.get('index')}] {s.get('document_name', '')}\n{s.get('text', '')}"
                for s in (all_sources or []) if s.get("text")
            )
            _synth = (
                _fast_mode_synthesize_answer(
                    (ctx.effective_message or ctx.message or ""),
                    _fallback_evidence_text, ctx, stage=f"react_{rn}_recovery_synthesis",
                )
                if _fallback_evidence_text else None
            )
            honest = (_synth + _low_confidence_caveat) if _synth else _generic_fallback
    _finalize_response(ctx, honest, all_sources, RETRIEVAL_SIGNAL_NO_SOURCES, last_tool, emitter)
