"""Stage: format response, build response payload."""
import json
import logging
import os
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

from app.chat_config import get_config_sha
from app.communication.assistant_envelope import (
    build_assistant_envelope_v1,
    enrich_sources_open_hrefs,
    resolve_tool_fired,
)
from app.communication.followup_next_steps_quality import (
    filter_next_steps_and_questions,
    normalize_followup_line_list,
)
from app.communication.workflow_selection import merge_clarification_option_lists
from app.communication.json_display_sanitize import (
    DEFAULT_BLEED_FALLBACK,
    finalize_answer_card_json_for_client,
)
from app.communication.gate import send_to_user
from app.pipeline.context import PipelineContext
from app.responder import format_response
from app.responder.final_parallel import format_response_parallel
from app.services.cost_model import compute_cost
from app.services.model_registry import integrator_llm_stage, per_call_router_composite
from app.state.jurisdiction import get_jurisdiction_from_active, jurisdiction_to_summary

# Badge keys for source_confidence_strip
BADGE_APPROVED_AUTHORITATIVE = "approved_authoritative"
BADGE_APPROVED_INFORMATIONAL = "approved_informational"
BADGE_PROCEED_WITH_CAUTION = "proceed_with_caution"
BADGE_AUGMENTED_WITH_GOOGLE = "augmented_with_google"
BADGE_INFORMATIONAL_ONLY = "informational_only"
BADGE_NO_SOURCES = "no_sources"

# Optional AnswerCard fields kept on client `message` so assistant_envelope can populate Details.
_ANSWER_CARD_ENVELOPE_KEYS = (
    "citations",
    "confidence_note",
    "required_variables",
    "followups",
    # Phase 13.7 — rolling thread summary. Persistence captures it
    # via ctx.thread_summary before the card gets rebuilt here, but
    # the response payload to the client also benefits from carrying
    # the field (frontend audit/inspect, future "thread summary
    # tooltip" UI, e2e bench detection). Without this key in the
    # allowlist, the verify probe sees thread_summary: None on the
    # response even though the DB has it.
    "thread_summary",
    # Layer 2 appeals integration — action chips rendered below the answer.
    "suggested_actions",
    # Enricher fields (two-phase streaming): correction, takeaways, gaps.
    "correction",
    "takeaways",
    "gaps",
    # Recital mode — verbatim text block with optional document_id + section.
    "recital",
    # next_questions_for_user — suggested follow-up prompts for the user.
    "next_questions_for_user",
    # 2026-07-30: the enricher's own classification of what kind of deliverable
    # this answer is (read/report/email/sms/emr/appeal/payor_report) + a
    # display-ready summary distinct from direct_answer. Moved here from a
    # ReAct-side attempt (Flash wouldn't comply via prompt instruction) — the
    # enricher runs a stronger model and already has full sources, so produces
    # both fields itself now. Without these two keys, they'd be silently
    # dropped before reaching the client (this allowlist is a positive filter).
    "output_intent",
    "display_summary",
    # 2026-08-05: always-populated 2-4 sentence TL;DR for the Summary tab
    # (Chat Master spec — "detailed answer" feature). Distinct from
    # direct_answer, which stays a one-sentence fallback for when no
    # draft exists; tldr_summary is always written, not a fallback path.
    "tldr_summary",
    # 2026-08-05: backend-computed "Try with Think mode" escalation hint
    # (Chat Master spec). Injected into `parsed` in run_integrate before
    # this allowlist copy runs — never LLM-produced, but flows through
    # the same copy mechanism as the LLM-produced fields above.
    "suggest_escalate",
    # 2026-08-07: react's own pre-integrator synthesis (Chat FE / Ananth's
    # ruling — Summary tab = react_draft, including on history reload).
    # Backend-computed (ctx.react_draft, set at orchestrator.py:708),
    # never LLM-produced. Rides this same final_message JSON blob so it
    # survives save_turn's persistence without a new DB column — the FE
    # reads card.react_draft on reload the same way it reads
    # card.direct_answer today.
    "react_draft",
)


def _answer_card_json_for_client(
    mode: str,
    direct_answer: str,
    sections: list[Any],
    *,
    extra_from: dict[str, Any] | None = None,
) -> str:
    card: dict[str, Any] = {"mode": mode, "direct_answer": direct_answer, "sections": sections}
    src = extra_from or {}
    for k in _ANSWER_CARD_ENVELOPE_KEYS:
        v = src.get(k)
        if v is not None:
            card[k] = v
    return json.dumps(card)


def _answer_step_label(stage: str) -> str:
    """User-facing label for answer pipeline LLM steps (shown in Answer insights UI)."""
    s = (stage or "").strip().lower()
    static = {
        "plan": "Planning",
        "planner": "Planning",
        "rag": "Library research & draft",
        "integrator_roster": "Composing your report answer",
        "integrator": "Composing your answer",
        "integrator_a": "Composing your answer",
        "integrator_critic": "Critique & citations",
        "integrator_enrichment": "Next steps & follow-ups",
        "context": "Context assembly",
        "badge": "Safety badge",
        "classifier": "Classifier",
        "critique": "Critique",
        "adjudicator": "Quality review",
        "phi_detector": "Privacy check",
    }
    if s in static:
        return static[s]
    if s.startswith("react_"):
        suffix = s.split("_", 1)[-1] if "_" in s else ""
        try:
            n = int(suffix)
            return f"Reasoning (round {n})"
        except ValueError:
            return "Reasoning"
    if s == "web_search":
        return "Web search answer"
    if s == "web_scrape":
        return "Web page read"
    if s == "npi_lookup":
        return "NPI registry lookup"
    if s == "roster_report":
        return "Credentialing report"
    if s == "credentialing_qa":
        return "Report Q&A"
    if s == "healthcare_query":
        return "Healthcare lookup"
    if s.startswith("tool_"):
        return f"Tool: {(s[5:] or 'step').replace('_', ' ')}"
    return (stage or "LLM step").replace("_", " ").strip().title()


def _display_stage_name(stage: str) -> str:
    """Short table header for LLM Performance (matches product copy)."""
    s = (stage or "").strip().lower()
    if s in ("plan", "planner"):
        return "Planner"
    if s == "rag":
        return "RAG"
    if s == "integrator_roster":
        return "Roster integrator"
    if s in ("integrator", "integrator_a"):
        return "Integrator"
    if s == "integrator_critic":
        return "Critic"
    if s == "integrator_enrichment":
        return "Enrichment"
    if s.startswith("react_"):
        suf = s.split("_", 1)[-1] if "_" in s else ""
        try:
            return f"Reasoning R{int(suf)}"
        except ValueError:
            return "Reasoning"
    if s == "adjudicator":
        return "Quality audit"
    if s == "web_search":
        return "Web search"
    if s == "web_scrape":
        return "Web scrape"
    if s == "npi_lookup":
        return "NPI lookup"
    if s == "roster_report":
        return "Roster report"
    if s == "credentialing_qa":
        return "Credentialing QA"
    if s == "healthcare_query":
        return "Healthcare query"
    if s.startswith("tool_"):
        return (s[5:] or "tool").replace("_", " ").title()
    if s in ("badge", "classifier", "critique", "phi_detector", "context"):
        return (stage or s).replace("_", " ").title()
    return (stage or "Step").replace("_", " ").title()


def _adjudication_sources_payload(all_sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Full retrieval chunks for post-run adjudication (client-facing ``sources`` stay short)."""
    try:
        max_per = max(2000, min(100_000, int(os.environ.get("MOBIUS_ADJ_SOURCE_MAX_CHARS", "16000"))))
    except ValueError:
        max_per = 16000
    all_sources = all_sources or []
    rows: list[dict[str, Any]] = []
    for i, s in enumerate(all_sources):
        if not isinstance(s, dict):
            continue
        raw_text = (s.get("text") or "").strip()
        if len(raw_text) > max_per:
            raw_text = raw_text[:max_per] + "\n... [truncated: MOBIUS_ADJ_SOURCE_MAX_CHARS]"
        rows.append(
            {
                "index": s.get("index", i + 1),
                "document_id": s.get("document_id"),
                "document_name": s.get("document_name") or s.get("name") or "document",
                "page_number": s.get("page_number"),
                "source_type": s.get("source_type"),
                "match_score": s.get("match_score"),
                "confidence": s.get("confidence"),
                "confidence_label": s.get("confidence_label"),
                "text": raw_text,
                "url": s.get("url"),
            }
        )
    return enrich_sources_open_hrefs(rows)


def breakdown_row_from_usage(
    u: dict[str, Any],
    resolved_stage: str | None = None,
) -> dict[str, Any]:
    """Build one ``usage_breakdown`` row from an llm_manager usage dict (integrator + post-run patch)."""
    stage = ((resolved_stage or u.get("stage") or "") if isinstance(u, dict) else "").strip() or "unknown"
    row: dict[str, Any] = {
        "stage": stage,
        "step_label": _answer_step_label(stage),
        "display_stage": _display_stage_name(stage),
        "model": u.get("model") or "",
        "provider": u.get("provider") or "",
        "input_tokens": int(u.get("input_tokens") or 0),
        "output_tokens": int(u.get("output_tokens") or 0),
        "cost_usd": round(compute_cost(u), 6),
    }
    if isinstance(u, dict):
        if u.get("latency_ms") is not None:
            try:
                row["latency_ms"] = int(u["latency_ms"])
            except (TypeError, ValueError):
                pass
        if u.get("llm_call_id"):
            row["llm_call_id"] = str(u["llm_call_id"])
        if "is_ab_call" in u:
            row["is_ab_call"] = bool(u.get("is_ab_call"))
        err = u.get("error_type") or u.get("error")
        row["call_status"] = "error" if err else "ok"
        # ModelRouter transparency (llm_manager.generate)
        if u.get("router_reason"):
            row["router_reason"] = str(u["router_reason"])[:4000]
        if u.get("router_selection"):
            row["router_selection"] = str(u["router_selection"])[:120]
        if "router_exploration_round" in u:
            row["router_exploration_round"] = bool(u.get("router_exploration_round"))
        if "router_circuit_relief" in u:
            row["router_circuit_relief"] = bool(u.get("router_circuit_relief"))
        if u.get("router_candidates_eligible") is not None:
            try:
                row["router_candidates_eligible"] = int(u["router_candidates_eligible"])
            except (TypeError, ValueError):
                pass
        if u.get("router_candidates_after_breaker") is not None:
            try:
                row["router_candidates_after_breaker"] = int(u["router_candidates_after_breaker"])
            except (TypeError, ValueError):
                pass
        if u.get("router_avg_quality_at_pick") is not None:
            try:
                row["router_avg_quality_at_pick"] = float(u["router_avg_quality_at_pick"])
            except (TypeError, ValueError):
                pass
        if u.get("router_quality_samples_at_pick") is not None:
            try:
                row["router_quality_samples_at_pick"] = int(u["router_quality_samples_at_pick"])
            except (TypeError, ValueError):
                pass
        # Post-run QA: per-call scores written to llm_calls and merged into usage_breakdown
        if u.get("quality_score") is not None:
            try:
                row["quality_score"] = round(float(u["quality_score"]), 3)
            except (TypeError, ValueError):
                pass
        if u.get("quality_source"):
            row["quality_source"] = str(u["quality_source"]).strip()[:200]
        if u.get("router_composite_at_pick") is not None:
            try:
                row["router_composite_at_pick"] = round(float(u["router_composite_at_pick"]), 4)
            except (TypeError, ValueError):
                pass
        br = u.get("router_composite_breakdown")
        if isinstance(br, dict) and br:
            row["router_composite_breakdown"] = br
        # Per-call selection inputs (2026-08-04) -- a SECOND allowlist from
        # llm_manager.generate()'s out_usage, independent of it. Fields added
        # there don't automatically reach here; found live (Ananth asked
        # whether these were actually observable, they weren't -- out_usage
        # had them, this row-builder didn't).
        if u.get("router_bandit_mode"):
            row["router_bandit_mode"] = str(u["router_bandit_mode"])
        if u.get("router_reasoning_depth"):
            row["router_reasoning_depth"] = str(u["router_reasoning_depth"])
        if u.get("router_latency_budget_ms") is not None:
            try:
                row["router_latency_budget_ms"] = int(u["router_latency_budget_ms"])
            except (TypeError, ValueError):
                pass
        if u.get("router_candidates_trimmed_by_latency_budget") is not None:
            try:
                row["router_candidates_trimmed_by_latency_budget"] = int(
                    u["router_candidates_trimmed_by_latency_budget"]
                )
            except (TypeError, ValueError):
                pass
        if "router_latency_budget_exhausted" in u:
            row["router_latency_budget_exhausted"] = bool(u.get("router_latency_budget_exhausted"))
    ok = row.get("call_status") != "error"
    lat = row.get("latency_ms")
    cost = row.get("cost_usd")
    q_sc = row.get("quality_score")
    try:
        pc, pbrk = per_call_router_composite(
            lat,
            cost,
            q_sc,
            ok,
            stage=str(row.get("stage") or ""),
            provider=str(row.get("provider") or ""),
            model=str(row.get("model") or ""),
            input_tokens=int(row.get("input_tokens") or 0),
            output_tokens=int(row.get("output_tokens") or 0),
        )
        row["per_call_composite"] = round(float(pc), 4)
        row["per_call_composite_breakdown"] = {
            k: round(float(v), 4) if isinstance(v, (int, float)) else v
            for k, v in pbrk.items()
        }
    except (TypeError, ValueError):
        pass
    return row


def _top_corpus_hit(sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_score = -1.0
    for src in sources or []:
        if not isinstance(src, dict):
            continue
        raw = src.get("match_score")
        if raw is None:
            raw = src.get("confidence")
        try:
            sc = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            sc = 0.0
        if sc > best_score:
            best_score = sc
            best = src
    if not best:
        return None
    return {
        "document_name": best.get("document_name"),
        "page_number": best.get("page_number"),
        "match_score": best.get("match_score"),
        "confidence": best.get("confidence"),
    }


from app.services.doc_assembly import (
    RETRIEVAL_SIGNAL_CORPUS_ONLY,
    RETRIEVAL_SIGNAL_CORPUS_PLUS_GOOGLE,
    RETRIEVAL_SIGNAL_GOOGLE_ONLY,
    RETRIEVAL_SIGNAL_NO_SOURCES,
)


def _pick_integrator_mode() -> str:
    """Return 'parallel' or 'sequential' based on env config.

    MOBIUS_INTEGRATOR_MODE=parallel|sequential forces a path.
    MOBIUS_INTEGRATOR_PARALLEL_PCT=0-100 samples when MODE is unset.
    Default: sequential (0% parallel) for safe rollout.
    """
    import random
    forced = (os.environ.get("MOBIUS_INTEGRATOR_MODE") or "").strip().lower()
    if forced in ("parallel", "sequential"):
        return forced
    try:
        pct = float(os.environ.get("MOBIUS_INTEGRATOR_PARALLEL_PCT") or "0")
    except ValueError:
        pct = 0.0
    return "parallel" if random.random() * 100 < pct else "sequential"


def _appeals_hint_pseudo_sources(tool_section_hints: list[dict] | None) -> list[dict]:
    """Appeals tools (appeals_find_carc/appeals_lookup_rules) return sources=[] --
    their rule text only reaches the card via pre_built_sections (a copy-verbatim
    passthrough), never via source_texts. That makes citations[] structurally blind
    to appeals content, since citations are only ever built from source_texts
    (2026-08-07: Ananth -- 'integrator should have access to everything react
    collected'). Walks both known appeals data shapes (appeals_find_carc's
    data.matches[].rules[], appeals_lookup_rules's data.rules[]) and produces one
    pseudo-source per rule so its rule_statement/appeal_argument text is citable
    the same way retrieved corpus chunks are. Not folded into ctx.sources/
    all_sources -- this only feeds source_texts/sources_summary, so it can't shift
    the source-confidence badge (which is computed from all_sources separately)."""
    out: list[dict] = []
    for h in (tool_section_hints or []):
        if not isinstance(h, dict):
            continue
        data = h.get("data")
        if not isinstance(data, dict):
            continue
        rule_groups = data.get("matches") or [data]
        for group in rule_groups:
            if not isinstance(group, dict):
                continue
            for rule in (group.get("rules") or []):
                if not isinstance(rule, dict):
                    continue
                parts = [rule.get("rule_statement"), rule.get("appeal_argument")]
                text = " ".join(p.strip() for p in parts if p and p.strip())
                if not text:
                    continue
                out.append({
                    "document_name": rule.get("rule_name") or rule.get("rule_id") or "Appeals rule",
                    "text": text,
                })
    return out


def _build_rag_chunks(
    all_sources: list[dict],
    tool_section_hints: list[dict] | None,
    chat_mode: str | None,
) -> list[dict]:
    """Unified RAG-chunk pool for the enricher prompt (Task #58, factory model,
    2026-08-07). ctx.rag_chunks (the upstream pool this reads from, via
    all_sources -- same underlying data, ReAct's ctx.rag_chunks is a plain
    alias of ctx.sources) is deliberately uncapped -- react's evidence-review
    keep-set can exceed 7, no upstream ceiling. Capping is a consumption-side
    decision, made here, not baked into the pool itself. Replaces the old
    two-list sources_summary/source_texts split with one list carrying both
    the lightweight index (document_name) and the citable text, plus
    authority/score/filler_strategy so the enricher can weight citations
    rather than treat every chunk as equally trustworthy.

    Appeals tools return sources=[] -- their rule text is folded in here too
    (see _appeals_hint_pseudo_sources) so citations[] isn't blind to them.
    This bridge stays until the typed tool_outputs.appeals citation channel
    (still being designed) supersedes it.
    """
    is_quick = chat_mode == "quick"
    cap = 4 if is_quick else 7
    chars = 600 if is_quick else 1000
    sorted_sources = sorted(
        all_sources, key=lambda x: -(float(x.get("rerank_score") or x.get("match_score") or 0))
    )
    chunks = [
        {
            "index": i + 1,
            "document_name": (s.get("document_name") or "document")[:200],
            "text": (s.get("text") or "")[:chars],
            "authority": s.get("authority") or s.get("confidence_label"),
            "score": s.get("rerank_score") if s.get("rerank_score") is not None else s.get("match_score"),
            "filler_strategy": s.get("filler_strategy"),
        }
        for i, s in enumerate(sorted_sources[:cap])
        if (s.get("text") or "").strip()
    ]
    appeals_pseudo = _appeals_hint_pseudo_sources(tool_section_hints)
    next_idx = len(chunks) + 1
    for j, ps in enumerate(appeals_pseudo):
        chunks.append({
            "index": next_idx + j,
            "document_name": ps["document_name"][:200],
            "text": ps["text"][:chars],
            "authority": "appeals_rule",
            "score": None,
            "filler_strategy": "appeals",
        })
    return chunks


def _cap_nested_strings(obj: Any, max_len: int = 1200, max_list: int = 20) -> Any:
    """Recursively caps string leaves (with an explicit truncation marker,
    never silent) and list lengths in an arbitrarily-nested dict/list
    structure. Used for ctx.tool_outputs, whose per-family nested shape
    (Task #58) has already changed twice in one day -- a generic byte-budget
    cap that works regardless of the exact nested structure is safer than
    hand-rolled per-field logic that would silently miss whatever field gets
    added next."""
    if isinstance(obj, str):
        if len(obj) > max_len:
            return obj[:max_len] + f"...[+{len(obj) - max_len} chars truncated]"
        return obj
    if isinstance(obj, dict):
        return {k: _cap_nested_strings(v, max_len, max_list) for k, v in obj.items()}
    if isinstance(obj, list):
        capped = [_cap_nested_strings(v, max_len, max_list) for v in obj[:max_list]]
        if len(obj) > max_list:
            capped.append(f"[+{len(obj) - max_list} more items truncated]")
        return capped
    return obj


def _build_tool_outputs_for_prompt(tool_outputs: dict | None) -> dict:
    """Caps ctx.tool_outputs for the prompt (Task #58, factory model). Shape
    is typed-by-family as shipped in react_loop.py: {"appeals": {"letter":
    {...}|absent, "rules": [...]|absent, "playbook": {...}|absent,
    "validation": {...}|absent}, "analytics": [...], "authoritative_sources":
    [...]} -- NOT raw per-tool-call records (an earlier same-day commit had
    that shape; superseded). Caps via _cap_nested_strings rather than
    per-field logic, since the nested shape is still evolving."""
    if not isinstance(tool_outputs, dict) or not tool_outputs:
        return {}
    return {
        family: _cap_nested_strings(content)
        for family, content in tool_outputs.items()
        if content is not None
    }


def _build_reasoning_ledger(react_trace_rounds: list[dict] | None) -> list[dict]:
    """Flattens ctx.react_trace_rounds[].enrichment into the formatter's
    primary reasoning input (Task #58/#48, factory model + EvidenceLedger,
    final shape per react_loop.py commit 42fb8ad). Under the factory model
    react enriches inline per round (learned/running_answer/gaps_closed/
    gaps_open); the enricher formats from this rather than re-deriving it
    from raw evidence. gaps_closed/gaps_open are lists (a same-day interim
    shape had gaps as one free-text field -- superseded, don't reintroduce).
    Rounds without an enrichment key are skipped, not padded, so the ledger
    degrades gracefully to empty (react_draft-only) rather than erroring."""
    out = []
    for r in (react_trace_rounds or []):
        if not isinstance(r, dict):
            continue
        enr = r.get("enrichment")
        if not isinstance(enr, dict):
            continue
        entry: dict = {"round": r.get("round"), "tool": r.get("tool")}
        for k in ("learned", "running_answer"):
            v = enr.get(k)
            if v:
                entry[k] = str(v)[:500]
        for k in ("gaps_closed", "gaps_open"):
            v = enr.get(k)
            if v:
                entry[k] = [str(g)[:200] for g in v][:5]
        if len(entry) > 2:  # more than just round/tool
            out.append(entry)
    return out


def _backend_extras_for_stub(ctx: PipelineContext) -> dict[str, Any]:
    """react_draft/suggest_escalate for a stub/fallback card built OUTSIDE the normal
    `if isinstance(parsed, dict):` block (Task #68) -- total JSON parse failure or
    AnswerCard-validation failure both replace display_message wholesale, bypassing
    the injection that block normally does. Same condition logic as that block,
    factored out so a stub never silently drops these backend-computed fields
    the way the bleed-detection branches did before this fix."""
    extra: dict[str, Any] = {}
    react_draft = getattr(ctx, "react_draft", None)
    if react_draft and react_draft.strip():
        extra["react_draft"] = react_draft
    evidence_empty = not (react_draft or "").strip() or len((react_draft or "").strip()) < 50
    stalled = (
        getattr(ctx, "react_unfinished_reason", None) == "no_path_forward"
        or getattr(ctx, "react_groundedness_passed", None) is False
        or evidence_empty
    )
    if stalled and getattr(ctx, "chat_mode", None) != "agentic":
        extra["suggest_escalate"] = True
    return extra


def _default_source_confidence(
    retrieval_signals: list[str],
    all_sources: list[dict],
    answer_set: dict | None = None,
) -> str:
    """Compute default badge from retrieval signals. Layer-aware when answer_set provides layer_used."""
    retrieval_signals = retrieval_signals or []
    all_sources = all_sources or []

    # Layer-based override — takes priority over signal when layer_used is present
    if answer_set:
        layers = [v.get("layer_used") for v in answer_set.values() if isinstance(v, dict)]
        layers = [l for l in layers if l is not None]
        if layers:
            max_layer = max(layers)
            if max_layer == 5:
                return BADGE_NO_SOURCES
            if max_layer == 4:
                return BADGE_INFORMATIONAL_ONLY
            if max_layer == 3:
                has_url_source = any(
                    s.get("url") or s.get("source_type") == "web" for s in all_sources
                )
                return BADGE_APPROVED_INFORMATIONAL if has_url_source else BADGE_INFORMATIONAL_ONLY
            # max_layer <= 2: fall through to existing signal-based logic

    # Existing signal-based logic (unchanged)
    if not retrieval_signals:
        return BADGE_NO_SOURCES
    if RETRIEVAL_SIGNAL_NO_SOURCES in retrieval_signals:
        return BADGE_NO_SOURCES
    if RETRIEVAL_SIGNAL_GOOGLE_ONLY in retrieval_signals:
        return BADGE_INFORMATIONAL_ONLY
    if RETRIEVAL_SIGNAL_CORPUS_PLUS_GOOGLE in retrieval_signals:
        return BADGE_AUGMENTED_WITH_GOOGLE
    labels = [s.get("confidence_label") for s in all_sources if s.get("confidence_label")]
    if any(l == "process_with_caution" for l in labels):
        return BADGE_PROCEED_WITH_CAUTION
    if all(l == "process_confident" for l in labels) and labels:
        return BADGE_APPROVED_AUTHORITATIVE
    if labels:
        return BADGE_APPROVED_INFORMATIONAL
    return BADGE_APPROVED_INFORMATIONAL


def run_integrate(
    ctx: PipelineContext,
    emitter: Callable[[str], None] | None = None,
) -> None:
    """Format response via integrator LLM, build response_payload."""
    plan = ctx.plan
    if not plan:
        return

    answers = ctx.answers or []
    all_sources = ctx.sources if ctx.sources is not None else []
    usages = [u for u in (ctx.usages or []) if isinstance(u, dict)]
    retrieval_signals = ctx.retrieval_signals if ctx.retrieval_signals is not None else []
    answer_set = ctx.answer_set if isinstance(getattr(ctx, "answer_set", None), dict) else {}

    default_source_confidence = _default_source_confidence(
        retrieval_signals, all_sources, answer_set=answer_set
    )
    # Answer from active skill output (report/NPI lookup) → approved_informational
    if getattr(ctx, "active_skill_reference", False):
        default_source_confidence = BADGE_APPROVED_INFORMATIONAL
    retrieval_metadata = {
        "default_source_confidence": default_source_confidence,
        "instruction": "We expect you to use the highest-rated document(s). If you override, set source_confidence_override and explain in confidence_note.",
    }

    # Mode cap: if any subquestion was answered by Layer 4 (reasoning), CANONICAL is not permitted
    layer4_used = any(
        (v.get("layer_used") or 0) >= 4
        for v in answer_set.values()
        if isinstance(v, dict)
    )
    if layer4_used:
        retrieval_metadata["layer4_used"] = True
        retrieval_metadata["instruction"] = (
            retrieval_metadata["instruction"]
            + " NOTE: One or more answers came from general reasoning (Layer 4)."
            " Set mode to FACTUAL or BLENDED — never CANONICAL for Layer 4 content."
        )
    # rag_chunks (Task #58, factory model): unified pool, sourced from the
    # uncapped ctx.rag_chunks alias when present (falls back to all_sources
    # for turns/paths that predate the field), capped here for prompt size.
    _rag_chunks_pool = getattr(ctx, "rag_chunks", None)
    if _rag_chunks_pool is None:
        _rag_chunks_pool = all_sources
    rag_chunks = _build_rag_chunks(
        _rag_chunks_pool, getattr(ctx, "tool_section_hints", None), getattr(ctx, "chat_mode", None),
    )
    tool_outputs = _build_tool_outputs_for_prompt(getattr(ctx, "tool_outputs", None))
    reasoning_ledger = _build_reasoning_ledger(getattr(ctx, "react_trace_rounds", None))

    # Stream only the direct-answer plain text (see format_response); never raw partial JSON.
    from app.storage.progress import append_message_chunk

    def _stream_answer_chunk(chunk: str) -> None:
        if chunk:
            append_message_chunk(ctx.correlation_id, chunk)

    active = (ctx.merged_state or {}).get("active")
    jurisdiction_summary = None
    if active:
        j = get_jurisdiction_from_active(active)
        jurisdiction_summary = jurisdiction_to_summary(j) or None

    _cfg_sha = get_config_sha() or None
    _integ_stage = integrator_llm_stage(ctx)
    _pws_pre = getattr(ctx, "pending_workflow_selection", None)
    _workflow_selection_ui: dict[str, Any] | None = None
    if isinstance(_pws_pre, list) and len(_pws_pre) > 0:
        _workflow_selection_ui = {
            "active": True,
            "slots": [
                str(g.get("slot") or "").strip()
                for g in _pws_pre
                if isinstance(g, dict) and (g.get("slot") or "").strip()
            ],
        }
    # Build a compact task_context summary for the integrator so it can
    # generate task-relevant next_questions_for_user and next_steps.
    _task_ctx: dict | None = None
    _task_raw = getattr(ctx, "react_task_list_data", None)
    if isinstance(_task_raw, dict) and isinstance(_task_raw.get("tasks"), list):
        _tasks = _task_raw["tasks"]
        _task_ctx = {
            "total": len(_tasks),
            "filters": _task_raw.get("filters") or {},
            "tasks": [
                {k: t.get(k) for k in ("task_id", "title", "kind", "status", "severity", "deadline", "assignee") if t.get(k)}
                for t in _tasks[:10]
            ],
        }

    # Instant-RAG context: when every source came from a user-uploaded doc,
    # signal the integrator to always generate profile-aware follow-up questions.
    _instant_rag_ctx: dict | None = None
    if all_sources and all(
        bool(s.get("instant_rag") or s.get("source_type") == "instant_rag")
        for s in all_sources if isinstance(s, dict)
    ):
        _up = getattr(ctx, "user_profile", None) or {}
        _instant_rag_ctx = {
            "is_uploaded_document": True,
            "user_role": _up.get("role") or _up.get("job_title") or "",
            "user_org": _up.get("org_name") or "",
        }

    _recital_ctx: dict | None = getattr(ctx, "recital", None) if getattr(ctx, "recital", None) else None
    logger.info(
        "[recital] integrate entry — ctx.recital=%r has_verbatim=%s has_text=%s",
        type(_recital_ctx).__name__,
        bool(_recital_ctx and _recital_ctx.get("verbatim")),
        bool(_recital_ctx and _recital_ctx.get("text")),
    )
    _integ_path = _pick_integrator_mode()
    ctx.integrator_mode = "P" if _integ_path == "parallel" else "S"
    logger.info("[integrate] integrator_mode=%s", ctx.integrator_mode)

    _shared_integ_kwargs = dict(
        emitter=emitter,
        message_chunk_callback=_stream_answer_chunk,
        retrieval_metadata=retrieval_metadata,
        rag_chunks=rag_chunks or None,
        jurisdiction_summary=jurisdiction_summary,
        user_provided_context=getattr(ctx, "user_provided_context", None),
        workflow_selection_ui=_workflow_selection_ui,
        correlation_id=ctx.correlation_id,
        thread_id=ctx.thread_id,
        config_sha=_cfg_sha,
        phi_detected=False,
        mode=getattr(ctx, "chat_mode", None),
        previous_thread_summary=getattr(ctx, "previous_thread_summary", None),
        user_profile=getattr(ctx, "user_profile", None),
        react_draft=getattr(ctx, "react_draft", None),
        tool_outputs=tool_outputs or None,
        reasoning_ledger=reasoning_ledger or None,
        task_context=_task_ctx,
        instant_rag_context=_instant_rag_ctx,
        recital_context=_recital_ctx,
        tool_section_hints=getattr(ctx, "tool_section_hints", None) or None,
    )

    integrator_usage: dict | None = None
    integrator_usages: list[dict] = []

    if _integ_path == "parallel":
        final_message, integrator_usages = format_response_parallel(
            plan, answers, user_message=ctx.message,
            llm_stage="integrator_a",
            **_shared_integ_kwargs,
        )
        integrator_usage = integrator_usages[0] if integrator_usages else None
    else:
        final_message, integrator_usage = format_response(
            plan, answers, user_message=ctx.message,
            llm_stage=_integ_stage,
            **_shared_integ_kwargs,
        )

    # Post-process: when ctx.recital.verbatim is set, upgrade the integrator's
    # card to mode=RECITAL and inject the verbatim text. The integrator still
    # runs so sources, next_questions, thread_summary, etc. are all preserved —
    # RECITAL is a rendering mode, not a reason to skip the full answer card.
    # (The mode-specific BLENDED prompt hardcodes "Set mode = 'BLENDED'" which
    # wins over the conditional recital rule; we correct it here post-hoc.)
    if _recital_ctx and _recital_ctx.get("verbatim") and _recital_ctx.get("text"):
        try:
            _fm_parsed = json.loads(final_message) if final_message else {}
            if isinstance(_fm_parsed, dict):
                _fm_parsed["mode"] = "RECITAL"
                _rec_field: dict = {"verbatim": _recital_ctx["text"]}
                if _recital_ctx.get("document_id"):
                    _rec_field["document_id"] = _recital_ctx["document_id"]
                if _recital_ctx.get("section"):
                    _rec_field["section"] = _recital_ctx["section"]
                _fm_parsed["recital"] = _rec_field
                # 2026-08-07 (Chat Master, RECITAL bleed hardening): the
                # BLENDED-hardcode override documented above is confirmed,
                # not hypothetical -- this whole block exists BECAUSE the
                # model sometimes ignores the recital instruction and
                # produces a normal synthesized card anyway. Forcing
                # mode=RECITAL alone doesn't remove what the model already
                # wrote in that case -- a paraphrased/wrong version of the
                # verbatim content (e.g. an appeal letter) could still sit
                # in sections[]/direct_answer/display_summary right next
                # to the correct recital.verbatim. Belt (prompt already
                # says "Do NOT include sections[]. Do NOT paraphrase") +
                # suspenders (this structural clear) -- the verbatim field
                # is authoritative once mode is force-set here, so any
                # synthesized content alongside it is always wrong by
                # definition and must not render.
                _fm_parsed["sections"] = []
                # direct_answer's presence (not its content) is required by
                # the AnswerCard validator below with no RECITAL exemption --
                # popping the key entirely trips that check and destroys the
                # whole card (including the correct recital.verbatim) via the
                # try-again-stub fallback. Overwrite with a safe, non-bleeding
                # placeholder instead of removing the key.
                _fm_parsed["direct_answer"] = "See the verbatim text below."
                _fm_parsed.pop("display_summary", None)
                final_message = json.dumps(_fm_parsed)
                logger.info("[recital] post-process upgrade → mode=RECITAL injected into integrator card")
        except (json.JSONDecodeError, TypeError):
            logger.warning("[recital] post-process failed to parse integrator JSON — mode left as-is")

    ctx.final_message = final_message

    # Phase 13.7 — extract the integrator's rolling thread summary out
    # of the AnswerCard JSON so the persistence layer can stamp it into
    # chat_turns.context_summary. The frontend gets the same field via
    # final_message; persistence wants a strict string column. We don't
    # rely on the field being present (legacy prompts and parse-failure
    # fallbacks won't have it) — None is fine, the persist path falls
    # back to the regex-based build_context_summary for those cases.
    _ts_emitted = False
    _ts_mode: str | None = None
    try:
        if final_message:
            _parsed = json.loads(final_message)
            if isinstance(_parsed, dict):
                _ts_mode = _parsed.get("mode") if isinstance(_parsed.get("mode"), str) else None
                # Long rolling brief (this thread's running memory). Extracted
                # independently of the short label below; persisted to
                # chat_threads.summary_long and fed back as
                # previous_thread_summary next turn.
                _tstate = _parsed.get("thread_state")
                if isinstance(_tstate, str) and _tstate.strip():
                    ctx.thread_state = _tstate.strip()[:600]

                # New "detailed answer" tab (2026-08-05, Chat Master spec):
                # the enricher already produces display_summary (the full,
                # formatted-per-output_intent deliverable — required on
                # every complete turn) + output_intent. Fire a detail_ready
                # SSE event as soon as the integrator's JSON parses,
                # mirroring append_draft_answer's draft_ready pattern, so
                # Chat FE can render the detail tab without waiting for the
                # full "completed" payload.
                #
                # 2026-08-07 (Chat Master follow-up): confirmed via direct
                # DB pull that sections[] is genuinely rich on real turns
                # (e.g. an appeals turn with detailed CARC rule citations)
                # even when display_summary was completely empty on that
                # SAME turn -- the original display_summary-only guard
                # would have skipped detail_ready entirely for turns just
                # like that one, hiding real content the integrator DID
                # produce. Now fires whenever EITHER has content, and
                # carries sections[]/citations[]/takeaways[]/next_steps[]
                # verbatim so Chat FE can reuse its existing typed-section
                # card-body renderer instead of prose-only.
                _detail = _parsed.get("display_summary")
                _oi = _parsed.get("output_intent")
                _tldr = _parsed.get("tldr_summary")
                _detail_sections = _parsed.get("sections")
                _detail_citations = _parsed.get("citations")
                _detail_takeaways = _parsed.get("takeaways")
                _detail_next_steps = _parsed.get("next_steps")
                _has_detail_prose = isinstance(_detail, str) and _detail.strip()
                _has_detail_sections = isinstance(_detail_sections, list) and bool(_detail_sections)
                # 2026-08-07 (Chat Master decision): Summary tab stays
                # permanently pinned to react_draft -- tldr_summary is
                # repurposed as the Answer tab's LEAD item (hierarchy:
                # tldr_summary -> display_summary -> sections[] ->
                # citations/takeaways/next_steps). Threaded through here
                # so Chat FE gets it in the same event as everything else
                # it now needs for that tab.
                _has_tldr = isinstance(_tldr, str) and _tldr.strip()
                if _has_detail_prose or _has_detail_sections or _has_tldr:
                    try:
                        from app.storage.progress import append_detail_answer
                        append_detail_answer(
                            ctx.correlation_id,
                            _detail if _has_detail_prose else "",
                            output_intent=_oi if isinstance(_oi, str) else None,
                            tldr_summary=_tldr if _has_tldr else None,
                            sections=_detail_sections if _has_detail_sections else None,
                            citations=_detail_citations if isinstance(_detail_citations, list) and _detail_citations else None,
                            takeaways=_detail_takeaways if isinstance(_detail_takeaways, list) and _detail_takeaways else None,
                            next_steps=_detail_next_steps if isinstance(_detail_next_steps, list) and _detail_next_steps else None,
                        )
                    except Exception:
                        logger.debug(
                            "append_detail_answer failed (cid=%s)",
                            getattr(ctx, "correlation_id", "?"), exc_info=True,
                        )

                # Loud-fail visibility (2026-08-05, Chat Master — same
                # pattern as thread_summary below): these three are
                # supposed to be on every complete turn, but nothing was
                # catching it when the model silently dropped them (found
                # manually, see record_detail_fields_emitted's docstring).
                # RECITAL mode is excluded — its schema never includes
                # output_intent/display_summary/tldr_summary at all (it's
                # a verbatim-text response, not a synthesized answer), so
                # their absence there is correct, not a compliance miss.
                if _parsed.get("mode") in ("FACTUAL", "CANONICAL", "BLENDED"):
                    _detail_missing_fields: list[str] = []
                    if not (isinstance(_oi, str) and _oi.strip()):
                        _detail_missing_fields.append("output_intent")
                    if not (isinstance(_detail, str) and _detail.strip()):
                        _detail_missing_fields.append("display_summary")
                    if not (isinstance(_tldr, str) and _tldr.strip()):
                        _detail_missing_fields.append("tldr_summary")
                    if _detail_missing_fields:
                        logger.warning(
                            "[phase13.7] integrator emitted AnswerCard missing %s "
                            "(cid=%s mode=%s).",
                            ",".join(_detail_missing_fields),
                            getattr(ctx, "correlation_id", "?")[:8],
                            _parsed.get("mode", "?"),
                        )
                    try:
                        from app.services.phase_13_7_metrics import record_detail_fields_emitted
                        record_detail_fields_emitted(
                            missing_fields=_detail_missing_fields,
                            mode=_parsed.get("mode") if isinstance(_parsed.get("mode"), str) else None,
                        )
                    except Exception:
                        pass

                _ts = _parsed.get("thread_summary")
                if isinstance(_ts, str) and _ts.strip():
                    # Cap at ~600 chars to match the legacy regex-built
                    # summary's storage budget; extra is dropped.
                    ctx.thread_summary = _ts.strip()[:600]
                    _ts_emitted = True
                else:
                    # BETA-sprint Move 2 — loud-fail when the integrator
                    # produces a valid AnswerCard but is missing the
                    # required thread_summary field. Tells ops "the
                    # prompt is fine, the model just dropped the field"
                    # vs the JSONDecodeError below which means the
                    # whole response was unparseable. Both are 'sidebar
                    # summary will be NULL for this turn,' but the
                    # remediation differs.
                    logger.warning(
                        "[phase13.7] integrator emitted AnswerCard without "
                        "thread_summary field (cid=%s mode=%s). Sidebar "
                        "rolling summary will be NULL for this turn.",
                        getattr(ctx, "correlation_id", "?")[:8],
                        _parsed.get("mode", "?"),
                    )
    except (json.JSONDecodeError, TypeError) as _e:
        # Non-JSON final_message (e.g. fallback path); leave None. Log
        # the cid + truncated head so ops can correlate to the integrator
        # response logs and see what shape the model actually returned.
        logger.warning(
            "[phase13.7] integrator final_message not JSON-parseable "
            "(cid=%s err=%s); thread_summary unavailable for this turn. "
            "head=%r",
            getattr(ctx, "correlation_id", "?")[:8],
            type(_e).__name__,
            (final_message or "")[:120],
        )

    # BETA-sprint Move 3 — structured metric. Fired regardless of which
    # branch above set _ts_emitted; aggregate the rate to detect prompt
    # drift or model-compliance regressions.
    try:
        from app.services.phase_13_7_metrics import record_thread_summary_emitted
        record_thread_summary_emitted(emitted=_ts_emitted, mode=_ts_mode)
    except Exception:
        # Metric emission is fire-and-forget — never breaks the turn.
        pass

    # Response-side PHI audit (2026-04-20). Mirror of the resolve-stage
    # hook for user-input side: LLM outputs can contain PHI too (the
    # model may echo back identifiers that were in RAG context, or
    # fabricate PII-shaped strings). HIPAA requires both sides in the
    # audit trail. Fire-and-forget — the writer itself logs on failure.
    try:
        from app.storage.phi_audit_log import audit_if_phi
        audit_if_phi(
            final_message or "",
            correlation_id=ctx.correlation_id,
            thread_id=ctx.thread_id,
            event_type="response_phi_detected",
            stage="integrate",
            model_used=(integrator_usage or {}).get("model") if isinstance(integrator_usage, dict) else None,
            action_taken="logged_only",
        )
    except Exception:
        pass  # audit must never break the turn

    if integrator_usages:
        # Parallel path: 3 usage dicts (A=core, B=critic, C=enrichment)
        usages = list(usages) + integrator_usages
        if isinstance(integrator_usages[0], dict):
            ctx.integrator_llm_call_id = integrator_usages[0].get("llm_call_id")
            ctx.integrator_model_id = integrator_usages[0].get("model")
    elif integrator_usage:
        # Sequential path: single usage dict
        usages = list(usages) + [integrator_usage]
        if isinstance(integrator_usage, dict):
            ctx.integrator_llm_call_id = integrator_usage.get("llm_call_id")
            ctx.integrator_model_id = integrator_usage.get("model")
    else:
        usages = list(usages)

    total_input = sum(int(u.get("input_tokens") or 0) for u in usages)
    total_output = sum(int(u.get("output_tokens") or 0) for u in usages)
    total_cost = sum(compute_cost(u) for u in usages)
    integrator_model = None
    for u in reversed(usages):
        if isinstance(u, dict) and u.get("stage") in ("integrator", "integrator_roster", "integrator_a"):
            integrator_model = u.get("model")
            break
    model_used = integrator_model or ((usages[0].get("model") or None) if usages else None)

    response_sources = enrich_sources_open_hrefs(
        [
            {
                "index": s.get("index", i + 1),
                "document_id": s.get("document_id"),
                "document_name": s.get("document_name") or "document",
                "page_number": s.get("page_number"),
                "source_type": s.get("source_type"),
                "match_score": s.get("match_score"),
                "confidence": s.get("confidence"),
                "text": (s.get("text") or "")[:200],
                "cite_text": (s.get("text") or "").strip()[:500],
                "url": s.get("url"),
            }
            for i, s in enumerate(all_sources)
        ]
    )
    adjudication_sources = _adjudication_sources_payload(all_sources)

    source_confidence_strip = default_source_confidence
    cited_source_indices: list[int] = []
    resolutions: list[dict[str, Any]] = []
    closed_task_ids: list[str] = []
    open_task_ids: list[str] = []
    next_steps: list[dict[str, Any]] = []
    next_questions_for_user: list[dict[str, Any]] = []
    integrator_ui_blocks: list[Any] = []
    # When we cannot parse the response (LLM error, plain text), show a friendly try-again card
    FALLBACK_TRY_AGAIN = DEFAULT_BLEED_FALLBACK
    display_message: str = final_message or ""
    try:
        raw = (final_message or "").strip()
        # Strip "json " prefix (LLM sometimes returns "json {...}")
        if raw.lower().startswith("json "):
            raw = raw[5:].lstrip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw = "\n".join(lines).strip()
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            _ub = parsed.get("ui_blocks")
            if isinstance(_ub, list):
                integrator_ui_blocks = _ub
            # suggest_escalate (2026-08-05, Chat Master spec) — backend-computed,
            # never LLM-produced. True iff the turn genuinely stalled (no_path_forward
            # self-report, or the in-process critic found blocking groundedness
            # issues -- react_groundedness_passed's existing meaning, repurposed
            # here rather than re-derived) AND the caller isn't already in the
            # deepest-reasoning mode (chat_mode="agentic" == the "thinking"
            # bandit_mode value -- nowhere further to escalate to from there).
            # Deliberately synchronous: the alternative (the post-run adjudicator's
            # verdict) isn't computed until 30-70s after this response is already
            # sent -- a button appearing that late would be confusing, not useful.
            #
            # 2026-08-05 follow-up (Chat Master): quick/normal-mode corpus misses
            # were falling through both conditions above -- the loop "completes"
            # cleanly (no unfinished_reason) and the groundedness critic often
            # doesn't run outside agentic mode (groundedness_passed stays None,
            # which is deliberately NOT treated as a failure). So a turn whose
            # react_draft is essentially "nothing found" produced no escalation
            # hint at all. ctx.react_draft is the same field draft_ready streams
            # (set at orchestrator.py:708, right before append_draft_answer) --
            # a short one here means react itself had nothing to synthesize from,
            # independent of whether unfinished_reason or the critic ever fired.
            # 50 chars is a floor for "not literally empty," not a quality bar --
            # short-but-legitimate answers (e.g. FACTUAL mode's one-sentence
            # scope) are a separate, larger population this must not catch;
            # tune only against evidence of false positives there.
            _groundedness_passed = getattr(ctx, "react_groundedness_passed", None)
            _react_draft = getattr(ctx, "react_draft", None)
            logger.info(
                "[react_draft] present=%s len=%d correlation_id=%s",
                _react_draft is not None, len((_react_draft or "")),
                getattr(ctx, "correlation_id", "?"),
            )
            _evidence_empty = not (_react_draft or "").strip() or len((_react_draft or "").strip()) < 50
            _stalled = (
                getattr(ctx, "react_unfinished_reason", None) == "no_path_forward"
                or _groundedness_passed is False
                or _evidence_empty
            )
            if _stalled and getattr(ctx, "chat_mode", None) != "agentic":
                parsed["suggest_escalate"] = True

            # cta_confirm_authoritative (2026-08-07, Task #41(a) follow-up,
            # Chat Master directive) -- same additive pattern as
            # suggest_escalate above: computed once in orchestrator.py right
            # after run_react() returns (so the draft_ready event already
            # carries it with zero added latency), read here unchanged so
            # the completed card carries it too, for reload / history and
            # any FE path that only reads the persisted card.
            if getattr(ctx, "cta_confirm_authoritative", False):
                parsed["cta_confirm_authoritative"] = True

            # react_draft persistence (2026-08-07, Chat FE / Ananth's ruling:
            # Summary tab = react_draft, always, including on history reload).
            # Live turns already get react_draft via the draft_ready SSE event
            # (fires before the integrator even runs) -- this is the RELOAD
            # gap: draft_ready isn't persisted, so a reloaded thread fell back
            # to the stored card's direct_answer (integrator output, not
            # react's own synthesis). save_turn's signature is an explicit
            # column enumeration (no spread) -- adding a real DB column is
            # more surface than this needs. Backend-computed, same pattern as
            # suggest_escalate: inject into the parsed dict here so it rides
            # the EXISTING final_message JSON blob that already gets
            # persisted verbatim via save_turn -- no new column, no schema
            # change, and the FE reads it the same way it reads
            # card.direct_answer today, just a different key.
            if _react_draft and _react_draft.strip():
                parsed["react_draft"] = _react_draft

            # Layer 2 appeals integration — inject suggested_actions if LLM omitted it.
            # The LLM is instructed to populate this for denial/appeal queries, but may
            # silently drop optional fields. We detect the intent here as a reliable fallback.
            _user_msg_lower = (getattr(ctx, "message", "") or "").lower()
            if not parsed.get("suggested_actions"):
                _denial_keywords = (
                    "denial", "denied", "appeal", "reconsideration",
                    "carc", "rarc", "dispute", "overturn", "adjustment reason",
                    "claim adjustment", "remark code",
                )
                if any(kw in _user_msg_lower for kw in _denial_keywords):
                    parsed["suggested_actions"] = [
                        {
                            "type": "external_link",
                            "label": "Open Appeals Agent",
                            "url": "https://mobius-appeals-prototype-ortabkknqa-uc.a.run.app",
                            "icon": "⚖️",
                        }
                    ]
            # Credentialing: move roster link to suggested_actions (replaces inline
            # direct_answer chip references until credentialing_card block is wired).
            if not parsed.get("suggested_actions"):
                _cred_base_url = (
                    os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL") or ""
                ).strip().rstrip("/").split("/report")[0]
                _cred_keywords = (
                    "credentialing", "pml status", "medicaid enrollment",
                    "roster reconciliation", "provider enrollment",
                )
                if _cred_base_url and any(kw in _user_msg_lower for kw in _cred_keywords):
                    parsed["suggested_actions"] = [
                        {
                            "type": "external_link",
                            "label": "Open Credentialing Report",
                            "url": _cred_base_url,
                            "icon": "📋",
                        }
                    ]
            # Extract display_message for frontend AnswerCard (avoids raw JSON in card)
            da = parsed.get("direct_answer")
            secs = parsed.get("sections")
            if isinstance(da, str) and isinstance(secs, list):
                # direct_answer sometimes contains raw JSON (LLM nested resolutions inside it)
                da_stripped = da.strip()
                if da_stripped.startswith("```json") or (da_stripped.startswith("{") and ("resolutions" in da_stripped[:200] or "direct_answer" in da_stripped[:200])):
                    try:
                        inner = da_stripped
                        if inner.lower().startswith("```json"):
                            inner = inner[7:].strip()
                        if inner.startswith("```"):
                            inner = inner[3:].lstrip()
                        if inner.endswith("```"):
                            inner = inner[:-3].rstrip()
                        inner_parsed = json.loads(inner)
                        if not isinstance(inner_parsed, dict):
                            raise ValueError("inner not dict")
                        # Case 1: inner is full AnswerCard at top level
                        inner_da = inner_parsed.get("direct_answer")
                        inner_secs = inner_parsed.get("sections")
                        if isinstance(inner_da, str) and isinstance(inner_secs, list) and not (
                            inner_da.strip().startswith("{") or inner_da.strip().startswith("```")
                        ):
                            mode = inner_parsed.get("mode") if inner_parsed.get("mode") in ("FACTUAL", "CANONICAL", "BLENDED", "RECITAL") else "FACTUAL"
                            sections_out = []
                            for s in (inner_secs or []):
                                sec = dict(s) if isinstance(s, dict) else {}
                                if not sec.get("label") and sec.get("title"):
                                    sec["label"] = sec.get("title", "")
                                sections_out.append(sec)
                            # Task #68: merge, don't replace -- inner_parsed is a fresh
                            # parse of nested/bled JSON found INSIDE parsed["direct_answer"];
                            # it never carries backend-injected fields (react_draft,
                            # suggest_escalate) that were added to the OUTER parsed dict
                            # above. inner_parsed's own keys still win on overlap (it's the
                            # corrected/nested content), but parsed's unique keys survive.
                            display_message = _answer_card_json_for_client(
                                mode, inner_da, sections_out, extra_from={**parsed, **inner_parsed}
                            )
                        else:
                            # Case 2: inner has resolutions; extract from first resolution
                            res_list = inner_parsed.get("resolutions")
                            if isinstance(res_list, list) and len(res_list) > 0:
                                first = res_list[0]
                                res = first.get("resolution") if isinstance(first.get("resolution"), dict) else first
                                if isinstance(res, dict) and isinstance(res.get("direct_answer"), str) and isinstance(res.get("sections"), list):
                                    mode = res.get("mode") if res.get("mode") in ("FACTUAL", "CANONICAL", "BLENDED", "RECITAL") else "FACTUAL"
                                    sections_out = []
                                    for s in (res.get("sections") or []):
                                        sec = dict(s) if isinstance(s, dict) else {}
                                        if not sec.get("label") and sec.get("title"):
                                            sec["label"] = sec.get("title", "")
                                        sections_out.append(sec)
                                    # Task #68: same merge as above -- res is nested inside
                                    # inner_parsed, further from parsed's backend-injected
                                    # fields, so this branch is the most likely to have
                                    # silently dropped react_draft/suggest_escalate.
                                    _res_extra = res if isinstance(res, dict) else inner_parsed
                                    display_message = _answer_card_json_for_client(
                                        mode,
                                        res["direct_answer"],
                                        sections_out,
                                        extra_from={**parsed, **_res_extra},
                                    )
                                elif isinstance(first.get("resolution"), str):
                                    # resolution is plain text (schema: "answer text")
                                    mode = inner_parsed.get("mode") if inner_parsed.get("mode") in ("FACTUAL", "CANONICAL", "BLENDED", "RECITAL") else "FACTUAL"
                                    display_message = _answer_card_json_for_client(
                                        mode, first["resolution"], [], extra_from={**parsed, **inner_parsed}
                                    )
                    except (json.JSONDecodeError, TypeError, ValueError):
                        pass
                else:
                    # Normal AnswerCard
                    mode = parsed.get("mode") if parsed.get("mode") in ("FACTUAL", "CANONICAL", "BLENDED", "RECITAL") else "FACTUAL"
                    sections_out = []
                    for s in (secs or []):
                        sec = dict(s) if isinstance(s, dict) else {}
                        if not sec.get("label") and sec.get("title"):
                            sec["label"] = sec.get("title", "")
                        sections_out.append(sec)
                    display_message = _answer_card_json_for_client(mode, da, sections_out, extra_from=parsed)
            elif parsed.get("resolutions"):
                # Top-level resolutions format; extract first for AnswerCard
                r = parsed.get("resolutions")
                if isinstance(r, list) and len(r) > 0:
                    first = r[0]
                    if isinstance(first, dict):
                        res = first.get("resolution") if isinstance(first.get("resolution"), dict) else first
                        if isinstance(res.get("direct_answer"), str) and isinstance(res.get("sections"), list):
                            mode = res.get("mode") if res.get("mode") in ("FACTUAL", "CANONICAL", "BLENDED", "RECITAL") else "FACTUAL"
                            sections_out = []
                            for s in (res.get("sections") or []):
                                sec = dict(s) if isinstance(s, dict) else {}
                                if not sec.get("label") and sec.get("title"):
                                    sec["label"] = sec.get("title", "")
                                sections_out.append(sec)
                            # Task #68: same merge -- res is nested inside the top-level
                            # "resolutions" list, not the outer parsed dict where
                            # react_draft/suggest_escalate were injected.
                            _res_extra = res if isinstance(res, dict) else {}
                            display_message = _answer_card_json_for_client(
                                mode,
                                res["direct_answer"],
                                sections_out,
                                extra_from={**parsed, **_res_extra},
                            )
            override = parsed.get("source_confidence_override")
            if override and str(override).strip() in (
                BADGE_APPROVED_AUTHORITATIVE,
                BADGE_APPROVED_INFORMATIONAL,
                BADGE_PROCEED_WITH_CAUTION,
                BADGE_AUGMENTED_WITH_GOOGLE,
                BADGE_INFORMATIONAL_ONLY,
                BADGE_NO_SOURCES,
            ):
                source_confidence_strip = str(override).strip()
            indices = parsed.get("cited_source_indices")
            if isinstance(indices, list):
                cited_source_indices = [
                    int(x) for x in indices
                    if isinstance(x, (int, float)) and 1 <= int(x) <= len(all_sources)
                ]
            r = parsed.get("resolutions")
            if isinstance(r, list):
                resolutions = [x for x in r if isinstance(x, dict)]
            v = parsed.get("closed_task_ids")
            if isinstance(v, list):
                closed_task_ids[:] = [str(x) for x in v if x]
            v = parsed.get("open_task_ids")
            if isinstance(v, list):
                open_task_ids[:] = [str(x) for x in v if x]
            ns = parsed.get("next_steps")
            if isinstance(ns, list):
                next_steps = normalize_followup_line_list(ns, default_clickable=False)
            nq = parsed.get("next_questions_for_user")
            if isinstance(nq, list):
                next_questions_for_user = normalize_followup_line_list(nq, default_clickable=True)
    except (json.JSONDecodeError, TypeError, ValueError):
        # Unparseable response (e.g. integrator exception → plain text): show try-again as AnswerCard
        _raw_truncated = (final_message or "")[:2000] + ("..." if len(final_message or "") > 2000 else "")
        logger.warning(
            "Integrate: could not parse final_message as JSON; sending try-again stub. raw (truncated): %s",
            _raw_truncated,
        )
        # Task #68: this stub is built OUTSIDE the `if isinstance(parsed, dict):` block
        # (parsed was never successfully computed -- that's why we're in this except
        # clause), so the react_draft/suggest_escalate injection that block normally
        # does never ran. See _backend_extras_for_stub.
        _stub_extra = _backend_extras_for_stub(ctx)
        _recital_text = (_recital_ctx or {}).get("text") if _recital_ctx else None
        if _recital_text:
            display_message = json.dumps({
                "mode": "RECITAL",
                "direct_answer": "From the Mobius founding essay:",
                "recital": {"verbatim": _recital_text},
                **_stub_extra,
            })
        else:
            display_message = json.dumps({
                "mode": "FACTUAL",
                "direct_answer": FALLBACK_TRY_AGAIN,
                "sections": [],
                **_stub_extra,
            })

    # If we never produced valid AnswerCard JSON, show try-again so the card always formats
    def _recital_fallback_card() -> str:
        # Task #68: same principle as the except-clause stub above -- this replaces
        # display_message WHOLESALE when validation fails, so it must not silently
        # drop react_draft/suggest_escalate either. See _backend_extras_for_stub.
        _stub_extra = _backend_extras_for_stub(ctx)
        _rt = (_recital_ctx or {}).get("text") if _recital_ctx else None
        if _rt:
            return json.dumps({"mode": "RECITAL", "direct_answer": "From the Mobius founding essay:", "recital": {"verbatim": _rt}, **_stub_extra})
        return json.dumps({"mode": "FACTUAL", "direct_answer": FALLBACK_TRY_AGAIN, "sections": [], **_stub_extra})

    try:
        check = json.loads(display_message) if display_message else {}
        _mode = check.get("mode") if isinstance(check, dict) else None
        _recital_valid = _mode == "RECITAL" and isinstance((check.get("recital") or {}).get("verbatim"), str) and (check.get("recital") or {}).get("verbatim", "").strip()
        if not isinstance(check, dict) or _mode not in ("FACTUAL", "CANONICAL", "BLENDED", "RECITAL") or "direct_answer" not in check or (not _recital_valid and not isinstance(check.get("sections"), list)):
            _msg_truncated = (display_message or "")[:2000] + ("..." if len(display_message or "") > 2000 else "")
            logger.warning(
                "Integrate: display_message not valid AnswerCard; sending try-again stub. message (truncated): %s",
                _msg_truncated,
            )
            display_message = _recital_fallback_card()
    except (json.JSONDecodeError, TypeError, ValueError):
        _msg_truncated = (display_message or "")[:2000] + ("..." if len(display_message or "") > 2000 else "")
        logger.warning(
            "Integrate: display_message not parseable; sending try-again stub. message (truncated): %s",
            _msg_truncated,
        )
        display_message = _recital_fallback_card()

    # Never ship nested JSON or raw AnswerCard-shaped strings inside direct_answer
    display_message = finalize_answer_card_json_for_client(
        display_message,
        fallback_text=FALLBACK_TRY_AGAIN,
    )

    # Deterministic: only accept task IDs that exist in the plan (upsert-only, no LLM-invented ids)
    _subs = (getattr(plan, "subquestions", None) or []) if plan else []
    valid_sq_ids = {str(sq.id) for sq in _subs}
    if valid_sq_ids:
        closed_task_ids[:] = [x for x in closed_task_ids if str(x) in valid_sq_ids]
        open_task_ids[:] = [x for x in open_task_ids if str(x) in valid_sq_ids]
        resolutions[:] = [r for r in resolutions if isinstance(r, dict) and str(r.get("sq_id", "")) in valid_sq_ids]

    usage_breakdown: list[dict[str, Any]] = []
    has_plan_usage = bool(getattr(plan, "llm_usage", None))
    for i, u in enumerate(usages):
        u_stage = ((u.get("stage") or "") if isinstance(u, dict) else "").strip()
        if u_stage.startswith("react_"):
            stage = u_stage
        elif u_stage:
            stage = u_stage
        elif i == 0 and has_plan_usage:
            stage = "plan"
        elif integrator_usage is not None and i == len(usages) - 1:
            stage = (integrator_usage.get("stage") or "integrator") if isinstance(integrator_usage, dict) else "integrator"
        else:
            stage = "rag"
        row = breakdown_row_from_usage(u, resolved_stage=stage)
        usage_breakdown.append(row)

    try:
        config_sha = get_config_sha() or None
    except Exception:
        config_sha = None

    stages_list = [str(r.get("stage") or "") for r in usage_breakdown]
    pipeline_kind = "react" if any(s.startswith("react_") for s in stages_list) else "legacy"
    total_latency_ms = 0
    for r in usage_breakdown:
        lm = r.get("latency_ms")
        if lm is None:
            continue
        try:
            total_latency_ms += int(lm)
        except (TypeError, ValueError):
            pass
    integ_explore: bool | None = None
    for r in reversed(usage_breakdown):
        if r.get("stage") in ("integrator", "integrator_roster", "integrator_a"):
            v = r.get("is_ab_call")
            integ_explore = bool(v) if v is not None else None
            break

    def _snip_router(s: str, n: int = 280) -> str:
        t = (s or "").strip()
        return t if len(t) <= n else t[: n - 1] + "…"

    router_by_stage: list[dict[str, Any]] = []
    for r in usage_breakdown:
        if not r.get("router_reason"):
            continue
        router_by_stage.append(
            {
                "stage": r.get("display_stage") or r.get("stage"),
                "model": r.get("model"),
                "mode": r.get("router_selection"),
                "exploration": r.get("router_exploration_round"),
                "circuit_relief": r.get("router_circuit_relief"),
                "reason": _snip_router(str(r.get("router_reason") or "")),
                "composite_pg": r.get("router_composite_at_pick"),
                "composite_call": r.get("per_call_composite"),
            }
        )
    _active_j = (ctx.merged_state or {}).get("active")
    _juris_d: dict[str, Any] = get_jurisdiction_from_active(_active_j) if _active_j else {}
    llm_performance: dict[str, Any] = {
        "pipeline": pipeline_kind,
        "primary_model": (model_used or "").strip(),
        "total_latency_ms": total_latency_ms,
        "total_cost_usd": round(total_cost, 6),
        "config_sha": config_sha,
        "jurisdiction_summary": jurisdiction_summary,
        "jurisdiction": {
            "payer": str(_juris_d.get("payor") or ""),
            "state": str(_juris_d.get("state") or ""),
            "program": str(_juris_d.get("program") or ""),
        },
        "top_source": _top_corpus_hit(response_sources),
        "integrator_exploration": integ_explore,
        "integrator_mode": ctx.integrator_mode,
        "router_by_stage": router_by_stage[:40] if router_by_stage else [],
    }

    # Task #68 (final root cause): ctx.final_message was set way earlier (right
    # after the RECITAL post-process block, before react_draft/suggest_escalate
    # injection, bleed-branch handling, and stub/fallback construction all ran)
    # -- it's the PRE-processing value. persistence.save_turn's main success
    # path (orchestrator.py) persists ctx.final_message, not display_message /
    # ctx.response_payload["message"] -- so every fixup made to display_message
    # in this function was silently invisible to the DB column the whole time,
    # only ever reaching the live API response. Re-sync here so persisted and
    # served are the same value again.
    ctx.final_message = display_message

    payload = {
        "status": "completed",
        "correlation_id": ctx.correlation_id,
        "message": display_message,
        "plan": plan.model_dump(),
        "thinking_log": (ctx.thinking_chunks if ctx.thinking_chunks is not None else []),
        "response_source": "plan",
        "model_used": model_used,
        "llm_error": None,
        "tokens_used": {"input_tokens": total_input, "output_tokens": total_output},
        "usage_breakdown": usage_breakdown,
        "llm_performance": llm_performance,
        "cost_usd": round(total_cost, 6),
        "sources": response_sources,
        "adjudication_sources": adjudication_sources,
        "source_confidence_strip": source_confidence_strip,
        "cited_source_indices": cited_source_indices,
        "thread_id": ctx.thread_id,
    }
    if resolutions:
        payload["resolutions"] = resolutions
    if closed_task_ids:
        payload["closed_task_ids"] = closed_task_ids
    if open_task_ids:
        payload["open_task_ids"] = open_task_ids
    roster_step_outputs = getattr(ctx, "roster_step_outputs", None)
    if roster_step_outputs:
        payload["roster_step_outputs"] = roster_step_outputs
    report_run_id = getattr(ctx, "report_run_id", None)
    if report_run_id:
        payload["report_run_id"] = report_run_id
    roster_report_pdf = getattr(ctx, "roster_report_pdf_base64", None)
    roster_report_final_md = getattr(ctx, "roster_report_final_md", None)
    if roster_report_pdf and isinstance(roster_report_pdf, str) and len(roster_report_pdf) > 0:
        payload["roster_report_pdf_base64"] = roster_report_pdf
        logger.info("Roster payload: PDF included (%d bytes)", len(roster_report_pdf))
    if roster_report_final_md and isinstance(roster_report_final_md, str) and len(roster_report_final_md.strip()) > 0:
        payload["roster_report_final_md"] = roster_report_final_md
        has_charts = "data:image/png;base64," in roster_report_final_md
        logger.info("Roster payload: final_md included (%d chars, charts=%s)", len(roster_report_final_md), has_charts)

    _att_kind = getattr(ctx, "roster_report_attachments_kind", None)
    if isinstance(_att_kind, str) and _att_kind.strip().lower() in ("reconciliation", "credentialing"):
        payload["roster_report_attachments_kind"] = _att_kind.strip().lower()

    cred_copilot = getattr(ctx, "credentialing_copilot", None)
    if isinstance(cred_copilot, dict) and cred_copilot.get("run_id"):
        payload["credentialing_copilot"] = cred_copilot

    _tf = resolve_tool_fired(ctx)
    payload["tool_fired"] = _tf
    answer_card_dict: dict[str, Any] | None = None
    try:
        _ac = json.loads(display_message)
        if isinstance(_ac, dict) and _ac.get("mode") in ("FACTUAL", "CANONICAL", "BLENDED"):
            answer_card_dict = _ac
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    next_steps, next_questions_for_user = filter_next_steps_and_questions(
        next_steps,
        next_questions_for_user,
        response_sources=response_sources,
        answer_card=answer_card_dict,
    )
    if next_steps:
        payload["next_steps"] = next_steps
    if next_questions_for_user:
        payload["next_questions_for_user"] = next_questions_for_user

    _md_for_envelope = roster_report_final_md if isinstance(roster_report_final_md, str) else None
    _has_pdf = bool(roster_report_pdf and isinstance(roster_report_pdf, str) and len(roster_report_pdf) > 0)
    _cred = getattr(ctx, "credentialing_copilot", None)
    _pipeline_gate: dict | None = None
    if isinstance(_cred, dict) and (_cred.get("run_id") or "").strip():
        _pipeline_gate = {
            **_cred,
            "plan_kind": "credentialing_copilot",
            "thread_id": ctx.thread_id,
        }
    # Inject task_list block when the ReAct tool attached task data to context
    _task_data = getattr(ctx, "react_task_list_data", None)
    if isinstance(_task_data, dict) and isinstance(_task_data.get("tasks"), list):
        integrator_ui_blocks = [
            {
                "type": "task_list",
                "tasks": _task_data["tasks"],
                "filters": _task_data.get("filters") or {},
                "allow_create": bool(_task_data.get("allow_create", True)),
                "allow_resolve": bool(_task_data.get("allow_resolve", True)),
            }
        ] + integrator_ui_blocks

    # Inject document_download block when fetch_document attached matches
    _dl_data = getattr(ctx, "react_document_download_data", None)
    if isinstance(_dl_data, dict) and isinstance(_dl_data.get("documents"), list) and _dl_data["documents"]:
        integrator_ui_blocks = [
            {
                "type": "document_download",
                "documents": _dl_data["documents"],
                "query": _dl_data.get("query") or "",
            }
        ] + integrator_ui_blocks

    _cred_card_data = getattr(ctx, "react_credentialing_card_data", None)

    payload["assistant_envelope"] = build_assistant_envelope_v1(
        answer_card=answer_card_dict,
        ui_blocks_raw=integrator_ui_blocks,
        tool_fired=_tf,
        response_sources=response_sources,
        next_steps=next_steps,
        next_questions_for_user=next_questions_for_user,
        roster_report_final_md=_md_for_envelope,
        has_roster_pdf=_has_pdf,
        resolutions=resolutions,
        source_confidence_strip=source_confidence_strip,
        pipeline_human_gate=_pipeline_gate,
        credentialing_card_data=_cred_card_data,
    )

    pws = getattr(ctx, "pending_workflow_selection", None)
    if isinstance(pws, list) and pws:
        payload["clarification_options"] = merge_clarification_option_lists(
            payload.get("clarification_options"),
            pws,
        )
        ctx.pending_workflow_selection = []

    ctx.response_payload = payload
