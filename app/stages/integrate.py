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
    if s == "integrator":
        return "Integrator"
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
    sources_summary = [
        {"index": s.get("index", i + 1), "document_name": s.get("document_name") or "document", "confidence_label": s.get("confidence_label")}
        for i, s in enumerate(all_sources)
    ]
    # Top sources with text for the enricher to cite verbatim.
    # Sort by match_score descending; cap count + char limit by mode to control
    # integrator prompt size (10×1500 = 15K chars was the main latency driver).
    _is_quick = getattr(ctx, "chat_mode", None) == "quick"
    _src_cap = 4 if _is_quick else 7
    _src_chars = 600 if _is_quick else 1000
    _sorted_sources = sorted(all_sources, key=lambda x: -(float(x.get("match_score") or 0)))
    source_texts = [
        {
            "index": s.get("index", i + 1),
            "title": (s.get("document_name") or "document")[:200],
            "text": (s.get("text") or "")[:_src_chars],
        }
        for i, s in enumerate(_sorted_sources[:_src_cap])
        if (s.get("text") or "").strip()
    ]

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
    final_message, integrator_usage = format_response(
        plan,
        answers,
        user_message=ctx.message,
        emitter=emitter,
        message_chunk_callback=_stream_answer_chunk,
        retrieval_metadata=retrieval_metadata,
        sources_summary=sources_summary,
        jurisdiction_summary=jurisdiction_summary,
        user_provided_context=getattr(ctx, "user_provided_context", None),
        workflow_selection_ui=_workflow_selection_ui,
        correlation_id=ctx.correlation_id,
        thread_id=ctx.thread_id,
        config_sha=_cfg_sha,
        phi_detected=False,
        llm_stage=_integ_stage,
        mode=getattr(ctx, "chat_mode", None),
        previous_thread_summary=getattr(ctx, "previous_thread_summary", None),
        user_profile=getattr(ctx, "user_profile", None),
        react_draft=getattr(ctx, "react_draft", None),
        source_texts=source_texts or None,
        task_context=_task_ctx,
        instant_rag_context=_instant_rag_ctx,
        recital_context=_recital_ctx,
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

    if integrator_usage:
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
        if isinstance(u, dict) and u.get("stage") in ("integrator", "integrator_roster"):
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
            # Layer 2 appeals integration — inject suggested_actions if LLM omitted it.
            # The LLM is instructed to populate this for denial/appeal queries, but may
            # silently drop optional fields. We detect the intent here as a reliable fallback.
            if not parsed.get("suggested_actions"):
                _denial_keywords = (
                    "denial", "denied", "appeal", "reconsideration",
                    "carc", "rarc", "dispute", "overturn", "adjustment reason",
                    "claim adjustment", "remark code",
                )
                _user_msg_lower = (getattr(ctx, "message", "") or "").lower()
                if any(kw in _user_msg_lower for kw in _denial_keywords):
                    parsed["suggested_actions"] = [
                        {
                            "type": "external_link",
                            "label": "Open Appeals Agent",
                            "url": "https://mobius-appeals-prototype-ortabkknqa-uc.a.run.app",
                            "icon": "⚖️",
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
                            display_message = _answer_card_json_for_client(
                                mode, inner_da, sections_out, extra_from=inner_parsed
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
                                    display_message = _answer_card_json_for_client(
                                        mode,
                                        res["direct_answer"],
                                        sections_out,
                                        extra_from=res if isinstance(res, dict) else inner_parsed,
                                    )
                                elif isinstance(first.get("resolution"), str):
                                    # resolution is plain text (schema: "answer text")
                                    mode = inner_parsed.get("mode") if inner_parsed.get("mode") in ("FACTUAL", "CANONICAL", "BLENDED", "RECITAL") else "FACTUAL"
                                    display_message = _answer_card_json_for_client(
                                        mode, first["resolution"], [], extra_from=inner_parsed
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
                            display_message = _answer_card_json_for_client(
                                mode,
                                res["direct_answer"],
                                sections_out,
                                extra_from=res if isinstance(res, dict) else parsed,
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
        _recital_text = (_recital_ctx or {}).get("text") if _recital_ctx else None
        if _recital_text:
            display_message = json.dumps({
                "mode": "RECITAL",
                "direct_answer": "From the Mobius founding essay:",
                "recital": {"verbatim": _recital_text},
            })
        else:
            display_message = json.dumps({
                "mode": "FACTUAL",
                "direct_answer": FALLBACK_TRY_AGAIN,
                "sections": [],
            })

    # If we never produced valid AnswerCard JSON, show try-again so the card always formats
    def _recital_fallback_card() -> str:
        _rt = (_recital_ctx or {}).get("text") if _recital_ctx else None
        if _rt:
            return json.dumps({"mode": "RECITAL", "direct_answer": "From the Mobius founding essay:", "recital": {"verbatim": _rt}})
        return json.dumps({"mode": "FACTUAL", "direct_answer": FALLBACK_TRY_AGAIN, "sections": []})

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
        if r.get("stage") in ("integrator", "integrator_roster"):
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
        "router_by_stage": router_by_stage[:40] if router_by_stage else [],
    }

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
    )

    pws = getattr(ctx, "pending_workflow_selection", None)
    if isinstance(pws, list) and pws:
        payload["clarification_options"] = merge_clarification_option_lists(
            payload.get("clarification_options"),
            pws,
        )
        ctx.pending_workflow_selection = []

    ctx.response_payload = payload
