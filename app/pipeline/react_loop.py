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
        return {
            "tool": "refuse",
            "success": False,
            "result": "",
            "signal": RETRIEVAL_SIGNAL_NO_SOURCES,
            "sources": [],
            "is_terminal": True,
        }

    # ── Curator tools (Phase 13.5) ───────────────────────────────────
    # Surface URLs Mobius knows about even when not yet indexed; let the
    # planner offer to ingest one on demand.
    if tool == "lookup_authoritative_sources":
        from app.pipeline.curator_tools import call_lookup_authoritative_sources
        emit("◌ Searching curator registry for known sources…")
        return call_lookup_authoritative_sources(inputs)

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
        return call_ingest_url(inputs)

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
        rag_overrides = rag_filters_from_active(active) or {}

        from app.communication.emit_envelope import (
            make_query_understood, make_strategy_selected,
            make_retrieval_complete, make_fallback_triggered,
        )
        # ── Corpus-then-external cascade ─────────────────────────────────────
        # corpus_search_agent owns all internal strategy selection: it cascades
        # through b→c→d with up to 3 attempts per call. The chat layer passes
        # mode="auto" on the first search_corpus call (letting the agent decide)
        # and escalates to strategy d (external) only on a second call, after
        # the agent has already exhausted its internal fallbacks.
        _arms_tried: set[str] = getattr(ctx, "_strategy_arms_tried", set())
        _corpus_exhausted = "corpus" in _arms_tried

        if _corpus_exhausted:
            # Agent's full cascade already ran — escalate to external.
            logger.info(
                "search_corpus corpus already tried — escalating to "
                "strategy d (external) cid=%s",
                str(getattr(ctx, "correlation_id", ""))[:8],
            )
            _auto_mode = "d"
            _auto_reason = None
            _arms_tried = _arms_tried | {"google"}
        else:
            _auto_mode = "auto"
            _auto_reason = None
            _arms_tried = _arms_tried | {"corpus"}

        ctx._strategy_arms_tried = _arms_tried  # type: ignore[attr-defined]
        # Back-compat alias for any code that still reads the old name.
        ctx._search_corpus_modes_used = list(_arms_tried)  # type: ignore[attr-defined]

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
            if upload_candidates else _auto_reason
        )
        emit_signal(make_strategy_selected(
            ctx.correlation_id, mode=_auto_mode,
            reason=_strategy_reason,
            round=_rn, thread_id=ctx.thread_id,
        ))

        # Run all retrievals concurrently. ThreadPoolExecutor (not asyncio)
        # because answer_non_patient + lazy_rag_search are both sync and
        # asyncio integration across the stack is a separate project.
        import concurrent.futures as _cf
        from app.services.instant_rag_search import lazy_rag_search

        def _run_corpus() -> tuple[str, list[dict], dict | None, str]:
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
                            "mode": _auto_mode,
                            "k": 10,
                            **({"include_document_ids": rag_overrides.get("include_document_ids")}
                               if rag_overrides.get("include_document_ids") else {}),
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
                )
            # Map SkillEnvelope → 4-tuple the rest of this dispatcher consumes.
            sources_dicts = [s.to_dict() for s in env.sources]
            return (
                env.text or "",
                sources_dicts,
                env.usage,
                env.signal or "no_sources",
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
                corpus_answer, corpus_sources, corpus_usage, corpus_signal = corpus_future.result()
            except Exception as _e:
                logger.warning("[B.4] corpus search failed: %s", _e)
                corpus_answer, corpus_sources, corpus_usage, corpus_signal = (
                    "", [], None, "no_sources",
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

        # Success if EITHER path contributed usable evidence.
        success = (
            bool(merged_result and len(merged_result.strip()) > 80 and corpus_signal != RETRIEVAL_SIGNAL_NO_SOURCES)
            or upload_chunks_total > 0
        )

        # Signal favors whichever path had hits — corpus_only when we got
        # anything; no_sources only when both pools returned empty. This
        # matches what the 0.19 retry guard expects for recording
        # success/failure on the (search_corpus, inputs) pair.
        if corpus_signal != RETRIEVAL_SIGNAL_NO_SOURCES and corpus_sources:
            merged_signal = corpus_signal
        elif upload_chunks_total > 0:
            merged_signal = "corpus_only"  # keep shape; integrator treats it the same
        else:
            merged_signal = RETRIEVAL_SIGNAL_NO_SOURCES

        emit_signal(make_retrieval_complete(
            ctx.correlation_id,
            chunks_returned=len(merged_sources),
            tool="search_corpus",
            mode="auto",
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

        return {
            "tool": "search_corpus",  # keep tool name stable for retry-guard + observability
            "success": success,
            "result": merged_result,
            "signal": merged_signal,
            "sources": merged_sources,
            "usage": corpus_usage,  # upload side makes no LLM calls (Phase B.1 design)
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
        # §5b wait-or-defer: if the catalog shows 0 chunks (doc still indexing),
        # poll up to INDEXING_POLL_MAX_S before running the search — short-circuits
        # the "ask again" UX for docs that finish within the turn window.
        _INDEXING_POLL_MAX_S = 15
        _INDEXING_POLL_INTERVAL_S = 2
        _pre_poll_known_row_count = next(
            (int(f.get("row_count") or 0) for f in _all_files
             if str(f.get("document_id", "")).strip() == document_id),
            -1,
        )
        if _pre_poll_known_row_count == 0:
            import time as _time_mod
            try:
                from app.storage.instant_rag_catalog import get_by_document_id as _cat_get_rl
            except Exception:
                _cat_get_rl = None
            _waited = 0
            while _cat_get_rl and _waited < _INDEXING_POLL_MAX_S:
                _time_mod.sleep(_INDEXING_POLL_INTERVAL_S)
                _waited += _INDEXING_POLL_INTERVAL_S
                try:
                    _cat = _cat_get_rl(document_id)
                    if _cat and int(_cat.get("chunks_count") or 0) > 0:
                        emit(f"  ✓ Document indexed after {_waited}s — searching now…")
                        break
                except Exception:
                    break

        # Phase B.1 — lazy RAG. Skips J/P/D tagger + confidence filter +
        # rerank entirely (all three assume a corpus doc with document_tags
        # / policy_line_tags rows, which user uploads don't have until
        # promotion — see Phase B.7). Direct Chroma vector search scoped
        # to document_id; chunks flow into the integrator unchanged.
        from app.services.instant_rag_search import lazy_rag_search
        answer, sources, usage, signal = lazy_rag_search(
            document_id=document_id,
            question=query,
            k=10,
            emitter=emitter,
        )
        success = bool(sources) and signal != RETRIEVAL_SIGNAL_NO_SOURCES
        _hint = (usage or {}).get("vector_count_hint")  # 0=not indexed, >0=query miss, -1/None=probe failed
        # Cross-check: if thread state says 0 chunks for this doc, treat same as hint=0.
        # Handles the case where the Chroma probe fails (-1) but we know from the thread
        # catalog that no chunks have been written yet (row_count=0).
        _known_row_count = next(
            (int(f.get("row_count") or 0) for f in _all_files
             if str(f.get("document_id", "")).strip() == document_id),
            -1,
        )
        _is_indexing = (_hint == 0) or (_known_row_count == 0 and (_hint is None or _hint <= 0))
        if not success:
            if _is_indexing:
                # Ingest race condition — document registered but Chroma chunks
                # haven't landed yet. The §5b poll above already waited up to 15s;
                # still not ready → bypass the integrator entirely so no LLM round-trip
                # fires and no answer-card chrome (confidence badge, sources block) is
                # added to a plain status message.
                _doc_filename = next(
                    (f.get("filename") for f in _all_files
                     if str(f.get("document_id", "")).strip() == document_id),
                    None,
                ) or upload_id or "your document"
                _status_msg = (
                    f"**{_doc_filename}** is still being indexed. "
                    "I'll answer your question as soon as it's ready — "
                    "no need to ask again."
                )
                emit("  ⏳ Document still being indexed — bypassing integrator for status reply.")
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
            elif _hint is not None and _hint > 0:
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
        # Mark the "google" arm as tried in the 5-arm bandit so the planner
        # knows only "llm_direct" remains if this also returns nothing.
        _g_arms: set[str] = getattr(ctx, "_strategy_arms_tried", set())
        ctx._strategy_arms_tried = _g_arms | {"google"}  # type: ignore[attr-defined]
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
                "Only llm_direct arm remains: answer from model knowledge "
                "with appropriate caveats, or set is_complete=true and tell "
                "the user the information could not be located in any source."
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
        return {
            "tool": tool,
            "success": success,
            "result": answer or "",
            "signal": signal,
            "sources": sources or [],
            "usage": usage,
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
        return {
            "tool": tool,
            "success": bool(env.text and not env.text.startswith("Unknown skill")),
            "result": env.text or f"{tool} returned no content.",
            "signal": env.signal,
            "sources": [s.to_dict() for s in env.sources],
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
    all_sources: list[dict] = []
    for s in seed:
        seed_sources = s.get("sources") or []
        if isinstance(seed_sources, list):
            all_sources.extend(seed_sources)
    final_signal = RETRIEVAL_SIGNAL_NO_SOURCES
    last_tool: str | None = None
    # 2026-05-06: pass user_profile so splice_user_profile appends
    # rendered_prompt to the planner/ReAct system prompt.
    reasoning_system = _react_reasoning_system(
        max_it,
        mode_label,
        getattr(ctx, "user_profile", None),
        allowed_tools=getattr(ctx, "allowed_tools", None),
    )

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
    for iteration in range(max_it):
        rn = iteration + 1
        # Keep ctx.react_rounds_used current so whichever exit path
        # the loop takes (finalize, break, exception-to-integrator
        # fallback), _publish_completed reads the correct round count.
        ctx.react_rounds_used = rn
        headline = _react_round_headline(iteration, max_it)
        emit(f"  Round {rn}/{max_it} — {headline}")
        emit(f"  Reasoning round {rn}/{max_it}…")

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

        reasoning_context = build_reasoning_context(
            ctx, tool_results, rn, max_iterations=max_it,
        )
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
            # Parse-failure prose fallback: the LLM produced plain prose
            # instead of JSON.  Two cases where this prose IS the answer:
            #   (a) Guidance rounds (round >= 80% of max) — model synthesising
            #       final answer is the primary case; first fixed 2026-05-11.
            #   (b) Mid-hunt rounds (0-based iteration >= 2, i.e. round 3+)
            #       where the model has enough context to write a direct answer
            #       and the prose does NOT look like a tool-call request.
            #       Without this, we fall back to raw retrieval chunks which
            #       can confuse the integrator into "unable to find" even when
            #       the sources clearly contain the answer — seen 2026-05-29.
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
            if _prose_looks_like_answer and (
                is_guidance_round(iteration, max_it) or iteration >= 2
            ):
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
                    critic_enabled,
                    format_critique_as_observation,
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
                    )
                    critique = parse_critic_response(critic_raw)

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

        # Fast mode early exit: if round 1 returns a usable result, skip the
        # second LLM reasoning pass. Round 2 is still available as fallback when
        # round 1 fails or returns nothing (complex / multi-hop questions).
        if (
            mode_label == "quick"
            and rn == 1
            and result.get("success")
            and len((result.get("result") or "").strip()) >= 30
        ):
            emit("  ⚡ Fast mode: using first corpus answer.")
            _finalize_response(
                ctx,
                (result.get("result") or "").strip(),
                all_sources,
                final_signal,
                last_tool,
                emitter,
            )
            return

        # 2026-04-18 disconnect: the roster-report early-exit (which
        # fired when a credentialing tool returned
        # RETRIEVAL_SIGNAL_ROSTER_COMPLETE) is gone along with those
        # tools. The generic "is_complete=true from the reasoner" path
        # still works for any remaining tool that returns a final answer.

        if result.get("is_terminal"):
            emit("  Stopping (refuse).")
            _finalize_response(ctx, "", [], RETRIEVAL_SIGNAL_NO_SOURCES, last_tool, emitter)
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
    honest = (
        "I wasn't able to find a verified answer to this question "
        "after checking our materials and searching the web. "
        "You may want to contact the payer directly or provide a link to their documentation."
    )
    _finalize_response(ctx, honest, all_sources, RETRIEVAL_SIGNAL_NO_SOURCES, last_tool, emitter)
