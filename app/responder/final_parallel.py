"""Parallel integrator: 3 concurrent LLM calls (core / critic / enrichment).

Same input JSON as the sequential integrator; all three calls start simultaneously
via ThreadPoolExecutor. Wall-clock ≈ max(A,B,C) instead of A+B+C.

Call A (integrator_a)     — direct_answer + sections + thread_summary + correction
Call B (integrator_critic) — citations + confidence + takeaways + gaps
Call C (integrator_enrichment) — next_questions + next_steps + suggested_actions

Returns (merged_json_str, [usage_a, usage_b, usage_c]).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from app.chat_config import get_chat_config
from app.planner.schemas import Plan
from app.responder.final import (
    _build_consolidator_input_json,
    _emit_integrator_chunks,
    _fallback_message,
    _parse_answer_card,
    blended_canonical_score,
    choose_consolidator_type,
)
from app.services.usage import LLMUsageDict

logger = logging.getLogger(__name__)


def _call_llm(
    prompt: str,
    stage: str,
    max_tokens: int,
    config_sha: str | None,
    correlation_id: str | None,
    thread_id: str | None,
    phi_detected: bool,
    mode: str | None,
) -> tuple[str, dict[str, Any]]:
    from app.services.llm_manager import generate_sync
    return generate_sync(
        prompt,
        stage=stage,
        max_tokens=max_tokens,
        config_sha=config_sha,
        correlation_id=correlation_id,
        thread_id=thread_id,
        phi_detected=phi_detected,
        mode=mode,
    )


def _parse_json_response(text: str, label: str) -> dict[str, Any]:
    """Parse a JSON response from critic or enrichment call; return {} on failure."""
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
        logger.warning("[parallel:%s] response was not a dict", label)
        return {}
    except json.JSONDecodeError:
        try:
            import json_repair
            result = json_repair.loads(text)
            if isinstance(result, dict):
                return result
        except Exception:
            pass
        logger.warning("[parallel:%s] could not parse JSON response", label)
        return {}


def format_response_parallel(
    plan: Plan,
    stub_answers: list[str],
    user_message: str,
    emitter: Callable[[str], None] | None = None,
    message_chunk_callback: Callable[[str], None] | None = None,
    *,
    retrieval_metadata: dict | None = None,
    sources_summary: list[dict] | None = None,
    jurisdiction_summary: str | None = None,
    user_provided_context: str | None = None,
    workflow_selection_ui: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    thread_id: str | None = None,
    config_sha: str | None = None,
    phi_detected: bool = False,
    llm_stage: str = "integrator_a",
    mode: str | None = None,
    previous_thread_summary: str | None = None,
    user_profile: dict | None = None,
    react_draft: str | None = None,
    source_texts: list[dict] | None = None,
    task_context: dict | None = None,
    instant_rag_context: dict | None = None,
    recital_context: dict | None = None,
) -> tuple[str, list[LLMUsageDict]]:
    """Run 3 parallel LLM calls and merge into a single AnswerCard JSON.

    Returns (merged_json, [usage_a, usage_b, usage_c]). On failure falls back
    to the stub_answers concatenation with an empty usage list.
    """
    _subs = getattr(plan, "subquestions", None) or []
    if not _subs:
        return ("", [])

    from app.communication.json_display_sanitize import (
        DEFAULT_BLEED_FALLBACK,
        display_text_for_parsed_answer_card,
    )

    cfg = get_chat_config()

    consolidator_input_json = _build_consolidator_input_json(
        plan, stub_answers, user_message,
        retrieval_metadata=retrieval_metadata,
        sources_summary=sources_summary,
        jurisdiction_summary=jurisdiction_summary,
        user_provided_context=user_provided_context,
        workflow_selection_ui=workflow_selection_ui,
        previous_thread_summary=previous_thread_summary,
        react_draft=react_draft,
        source_texts=source_texts,
        task_context=task_context,
        instant_rag_context=instant_rag_context,
        recital_context=recital_context,
    )

    canonical_score = blended_canonical_score(plan)
    consolidator_type = choose_consolidator_type(
        canonical_score,
        cfg.prompts.consolidator_factual_max,
        cfg.prompts.consolidator_canonical_min,
    )
    logger.info("[parallel] consolidator_type=%s score=%.2f", consolidator_type, canonical_score)

    # ── Build the 3 system prompts ──
    if consolidator_type == "factual":
        mode_suffix = "Mode: FACTUAL — set mode='FACTUAL'; 2–3 sections, direct_answer=one operative fact.\n"
    elif consolidator_type == "canonical":
        mode_suffix = "Mode: CANONICAL — set mode='CANONICAL'; 2–4 sections, direct_answer=2–4 sentences.\n"
    else:
        mode_suffix = "Mode: BLENDED — set mode='BLENDED'; 2–4 sections, direct_answer=1–3 sentences with specifics.\n"

    core_system = cfg.prompts.integrator_parallel_core_system + mode_suffix
    critic_system = cfg.prompts.integrator_parallel_critic_system
    enrichment_system = cfg.prompts.integrator_parallel_enrichment_system

    # Splice user voice profile into core prompt (same logic as sequential path)
    try:
        _rp = (user_profile or {}).get("rendered_prompt", "") if isinstance(user_profile, dict) else ""
        if _rp and _rp.strip():
            core_system = (
                f"{core_system}\n\n"
                "VOICE DIRECTIVE (overrides section-count defaults above):\n"
                f"{_rp.strip()}\n"
            )
        elif user_profile:
            from app.pipeline.personalization import splice_user_profile
            core_system = splice_user_profile(core_system, user_profile)
    except Exception:
        pass

    user_tmpl = cfg.prompts.integrator_user_template
    prompt_a = f"{core_system}\n\n{user_tmpl.format(consolidator_input_json=consolidator_input_json)}"
    prompt_b = f"{critic_system}\n\n{user_tmpl.format(consolidator_input_json=consolidator_input_json)}"
    prompt_c = f"{enrichment_system}\n\n{user_tmpl.format(consolidator_input_json=consolidator_input_json)}"

    shared_kwargs = dict(
        config_sha=config_sha,
        correlation_id=correlation_id,
        thread_id=thread_id,
        phi_detected=phi_detected,
        mode=mode,
    )

    # ── Launch 3 concurrent calls ──
    text_a = text_b = text_c = ""
    usage_a: dict[str, Any] | None = None
    usage_b: dict[str, Any] | None = None
    usage_c: dict[str, Any] | None = None

    def _e(msg: str) -> None:
        if emitter and msg:
            emitter(msg)

    try:
        _e("◌ Drafting answer — running 3 parallel LLM passes…")
        with ThreadPoolExecutor(max_workers=3) as pool:
            fut_a = pool.submit(_call_llm, prompt_a, "integrator_a", 4096, **shared_kwargs)
            fut_b = pool.submit(_call_llm, prompt_b, "integrator_critic", 1024, **shared_kwargs)
            fut_c = pool.submit(_call_llm, prompt_c, "integrator_enrichment", 512, **shared_kwargs)
            # Wait for all three; collect results even if some fail
            for fut in as_completed([fut_a, fut_b, fut_c]):
                if fut is fut_a:
                    try:
                        text_a, usage_a = fut.result()
                        _e("  ✓ Core draft ready")
                    except Exception as e:
                        logger.warning("[parallel:A] call failed: %s", e)
                        _e("  ⚠ Core draft failed — using fallback")
                elif fut is fut_b:
                    try:
                        text_b, usage_b = fut.result()
                        _e("  ✓ Critic pass done")
                    except Exception as e:
                        logger.warning("[parallel:B] call failed: %s", e)
                else:
                    try:
                        text_c, usage_c = fut.result()
                        _e("  ✓ Enrichment pass done")
                    except Exception as e:
                        logger.warning("[parallel:C] call failed: %s", e)
    except Exception as e:
        logger.warning("[parallel] ThreadPoolExecutor failed: %s", e, exc_info=True)
        fb = _fallback_message(plan, stub_answers)
        _emit_integrator_chunks(fb, message_chunk_callback)
        return (fb, [])

    # ── Parse call A (core card) — must succeed ──
    card = _parse_answer_card(text_a)
    if card is None:
        logger.warning(
            "[parallel:A] could not parse core card; falling back to stub. head=%r",
            (text_a or "")[:200],
        )
        from app.communication.json_display_sanitize import (
            build_minimal_answer_card_preserving_metadata,
            extract_user_visible_text_from_integrator_raw,
        )
        visible = extract_user_visible_text_from_integrator_raw(text_a or "")
        if not visible.strip():
            visible = DEFAULT_BLEED_FALLBACK
        _emit_integrator_chunks(visible, message_chunk_callback)
        card = build_minimal_answer_card_preserving_metadata(visible, text_a or "")
        usages = [u for u in [usage_a, usage_b, usage_c] if u is not None]
        return (json.dumps(card), usages)

    # Stream the core direct_answer immediately
    card = dict(card)
    display_txt = display_text_for_parsed_answer_card(card)
    if not display_txt.strip():
        if previous_thread_summary and stub_answers:
            candidate = (stub_answers[0] if stub_answers else "").strip()
            if candidate and len(candidate) >= 20:
                display_txt = candidate[:8000]
    if not display_txt.strip():
        display_txt = DEFAULT_BLEED_FALLBACK
    card["direct_answer"] = display_txt
    _emit_integrator_chunks(display_txt, message_chunk_callback)

    # ── Merge call B (critic) ──
    critic = _parse_json_response(text_b, "B")
    if critic:
        citations = critic.get("citations")
        if isinstance(citations, list):
            card["citations"] = citations
        indices = critic.get("cited_source_indices")
        if isinstance(indices, list):
            card["cited_source_indices"] = [int(x) for x in indices if isinstance(x, (int, float))]
        override = critic.get("source_confidence_override")
        if override and isinstance(override, str) and override not in ("null", ""):
            card["source_confidence_override"] = override
        note = critic.get("confidence_note")
        if note and isinstance(note, str):
            card["confidence_note"] = note
        takeaways = critic.get("takeaways")
        if isinstance(takeaways, list):
            card["takeaways"] = takeaways
        gaps = critic.get("gaps")
        if isinstance(gaps, list):
            card["gaps"] = gaps

    # ── Merge call C (enrichment) ──
    enrich = _parse_json_response(text_c, "C")
    if enrich:
        nq = enrich.get("next_questions_for_user")
        if isinstance(nq, list):
            card["next_questions_for_user"] = nq
        ns = enrich.get("next_steps")
        if isinstance(ns, list):
            card["next_steps"] = ns
        sa = enrich.get("suggested_actions")
        if isinstance(sa, list):
            card["suggested_actions"] = sa

    usages = [u for u in [usage_a, usage_b, usage_c] if u is not None]
    return (json.dumps(card), usages)
