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
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
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
    latency_budget_ms: int | None = None,
    reasoning_depth: str | None = None,
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
        latency_budget_ms=latency_budget_ms,
        reasoning_depth=reasoning_depth,
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
    jurisdiction_summary: str | None = None,
    user_perspective: str | None = None,
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
    rag_chunks: list[dict] | None = None,
    tool_outputs: dict[str, list[dict]] | None = None,
    reasoning_ledger: list[dict] | None = None,
    task_context: dict | None = None,
    instant_rag_context: dict | None = None,
    recital_context: dict | None = None,
    tool_section_hints: list[dict] | None = None,
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
        rag_chunks=rag_chunks,
        jurisdiction_summary=jurisdiction_summary,
        user_perspective=user_perspective,
        user_provided_context=user_provided_context,
        workflow_selection_ui=workflow_selection_ui,
        previous_thread_summary=previous_thread_summary,
        react_draft=react_draft,
        tool_outputs=tool_outputs,
        reasoning_ledger=reasoning_ledger,
        task_context=task_context,
        instant_rag_context=instant_rag_context,
        recital_context=recital_context,
        tool_section_hints=tool_section_hints,
    )

    canonical_score = blended_canonical_score(plan)
    consolidator_type = choose_consolidator_type(
        canonical_score,
        cfg.prompts.consolidator_factual_max,
        cfg.prompts.consolidator_canonical_min,
    )
    logger.info("[parallel] consolidator_type=%s score=%.2f", consolidator_type, canonical_score)

    # ── Build the 3 system prompts ──
    # FACTUAL/BLENDED collapsed into one unified "answer" path (2026-08-07 architecture
    # directive) -- CANONICAL alone stays distinct. mode value stays 'FACTUAL' (see
    # final.py's choose_consolidator_type docstring for why it wasn't renamed).
    if consolidator_type == "canonical":
        mode_suffix = "Mode: CANONICAL — set mode='CANONICAL'; 2–4 sections, direct_answer=2–4 sentences.\n"
    else:
        mode_suffix = (
            "Mode: FACTUAL — set mode='FACTUAL'; 2–4 sections (requirements/definitions "
            "visible by default, others behind 'Show details'), direct_answer=1–3 sentences "
            "with specifics when the corpus supports them.\n"
        )

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

    def _emit_partial(part: str, data: dict[str, Any]) -> None:
        # Task: progressive streaming (docs/SPEC_PARALLEL_INTEGRATOR_STREAMING.md)
        # -- fires the moment EACH call completes, not after all three, so the
        # client can render tabs incrementally instead of blocking on the
        # slowest call. Never let a progress-emit failure break the turn.
        if not correlation_id or not data:
            return
        try:
            from app.storage.progress import append_integrator_partial
            append_integrator_partial(correlation_id, part, data)
        except Exception as e:
            logger.debug("[parallel] partial emit failed (part=%s): %s", part, e)

    try:
        _e("◌ Drafting answer — running 3 parallel LLM passes…")
        with ThreadPoolExecutor(max_workers=3) as pool:
            # latency_budget_ms: hard pre-filter in ModelRouter.select(), trims
            # candidates to those whose tracked ema_latency_ms fits the budget
            # before the Thompson draw runs (built earlier this session,
            # previously unused). Preferred over hardcoding a model name --
            # self-corrects if the fastest model today degrades or a faster one
            # becomes available. max_tokens on Call A reduced from 4096: under
            # the factory model react already did the reasoning, so Call A is
            # formatting a pre-structured answer, not generating one from
            # scratch -- its real output should be much smaller than the old
            # "full synthesis from raw sources" budget assumed.
            # reasoning_depth="fast" is a SOFT complement to latency_budget_ms's hard
            # filter -- biases the bandit's weight table toward latency among whatever
            # candidates survive the hard filter, rather than just constraining the
            # candidate pool. Task (Ananth, via Chat Master): "the bandit should know
            # that integrator calls have a hard latency constraint... so it routes to
            # Flash-class models rather than Pro" -- this is that awareness signal,
            # distinct from hardcoding a model name.
            # 2026-08-08 (live truncation, Ananth watching): the smaller max_tokens
            # values (2048/1024/512) were sized on the wrong assumption -- that the
            # full budget goes to visible output. Confirmed live: gemini-2.5-flash
            # calls were landing at a fraction of their budget with mid-word/
            # mid-number cutoffs (integrator_critic averaging 40 tokens from a
            # 1024 budget, integrator_enrichment 19 from 512). The installed
            # vertexai SDK has no thinking_config/thinking_budget param (checked:
            # GenerationConfig.__init__ has no such field) to control Gemini 2.5's
            # default thinking-token consumption directly, so max_tokens needs real
            # headroom on top of whatever thinking silently uses. Widened well past
            # the pre-today values (Call A was 4096) rather than guessing at a
            # minimal restore -- correctness over the latency optimization for now.
            fut_a = pool.submit(_call_llm, prompt_a, "integrator_a", 4096, **shared_kwargs, latency_budget_ms=3000, reasoning_depth="fast")
            fut_b = pool.submit(_call_llm, prompt_b, "integrator_critic", 3072, **shared_kwargs, latency_budget_ms=2000, reasoning_depth="fast")
            fut_c = pool.submit(_call_llm, prompt_c, "integrator_enrichment", 2048, **shared_kwargs, latency_budget_ms=1500, reasoning_depth="fast")
            # Wait for all three; collect results even if some fail. Each
            # branch now parses + emits its OWN partial the moment it lands,
            # rather than waiting for the other two (see _emit_partial above).
            for fut in as_completed([fut_a, fut_b, fut_c]):
                if fut is fut_a:
                    try:
                        text_a, usage_a = fut.result()
                        _e("  ✓ Core draft ready")
                        _card_a = _parse_answer_card(text_a)
                        if _card_a:
                            _partial_a = {
                                k: v for k, v in _card_a.items()
                                if k in ("mode", "direct_answer", "sections", "thread_summary", "correction")
                                and v is not None
                            }
                            _emit_partial("core", _partial_a)
                    except Exception as e:
                        logger.warning("[parallel:A] call failed: %s", e)
                        _e("  ⚠ Core draft failed — using fallback")
                elif fut is fut_b:
                    try:
                        text_b, usage_b = fut.result()
                        _e("  ✓ Critic pass done")
                        _critic_partial = _parse_json_response(text_b, "B")
                        if _critic_partial:
                            _emit_partial("citations", {
                                k: v for k, v in _critic_partial.items()
                                if k in ("citations", "cited_source_indices", "source_confidence_override", "confidence_note", "takeaways", "gaps", "correction")
                            })
                    except Exception as e:
                        logger.warning("[parallel:B] call failed: %s", e)
                else:
                    try:
                        text_c, usage_c = fut.result()
                        _e("  ✓ Enrichment pass done")
                        _enrich_partial = _parse_json_response(text_c, "C")
                        if _enrich_partial:
                            _emit_partial("enrichment", {
                                k: v for k, v in _enrich_partial.items()
                                if k in ("next_questions_for_user", "next_steps", "suggested_actions")
                            })
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

    # ── Inline citation footnotes (2026-08-08, Chat FE + Ananth) ──
    # card.sources[]: positionally aligned to the SAME rag_chunks list Call A
    # saw (1-based marker [N] in the prose -> sources[N-1]) -- built here, not
    # from the critic's citations[] (which is a deduped/curated subset that
    # wouldn't align to marker positions). Both Call A and the critic read the
    # same rag_chunks input, so a marker anchored to that shared, stable index
    # sidesteps the concurrency problem (same fix as correction's verbatim-
    # match requirement). Chat FE renders these as clickable superscripts +
    # a numbered bottom list, drops any marker with no corresponding entry.
    if rag_chunks:
        card["sources"] = [
            {
                "document_name": c.get("document_name") or c.get("doc_title") or "",
                "locator": (f"p. {c['page_number']}" if c.get("page_number") else None),
                "snippet": (c.get("text") or "")[:300] or None,
                # document_id + page_number (raw, not the "p. N" locator string)
                # are exactly openDocReaderPanel's own two positional args
                # (app.ts:4160) -- lets Chat FE wire click-to-open without a
                # second lookup or a new backend field.
                "document_id": c.get("document_id"),
                "page_number": c.get("page_number"),
            }
            for c in rag_chunks
        ]

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
        # correction (2026-08-08, Chat FE: inline redline needs {original,
        # corrected} -- Chat Master directive): critic is the evidence-
        # verification pass, the right place to catch react_draft/Call A
        # claims that rag_chunks/tool_outputs actually contradict. Only
        # overwrite Call A's own (rare, self-detected) correction when
        # critic found a real one -- a critic null must not silently erase
        # something Call A already flagged.
        correction = critic.get("correction")
        if isinstance(correction, dict) and correction.get("original") and correction.get("corrected"):
            card["correction"] = {
                "original": str(correction["original"]),
                "corrected": str(correction["corrected"]),
            }

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


# ── Task #76 (2026-08-08, Chat Master ruling): dynamic-enrichment background
# critic/enrichment ──
#
# When react's own answer is already sufficient (see react_loop.py's
# _is_sufficient_for_deterministic_pass), the integrator skips Call A's LLM
# call and structures react_draft deterministically instead (see
# deterministic_format.py) -- but Call B (citations/takeaways/gaps) and
# Call C (next_steps/suggested_actions) still run, "always run, but as
# fire-and-forget background jobs. Results populate diagnostics/sources tabs
# when they land, never block the Answer tab."
#
# A per-request `with ThreadPoolExecutor(...) as pool:` (the pattern
# format_response_parallel uses above) does NOT achieve this -- __exit__
# calls shutdown(wait=True), so the `with` block itself blocks the caller
# until B/C finish regardless of whether their results are read. This needs
# a executor that OUTLIVES the request: module-level, persistent, never
# shut down. Best-effort by design (Chat Master approved) -- a Cloud Run
# instance recycle mid-call means that turn's citations/next_steps just
# never land; --no-cpu-throttling on this service (already in deploy.sh)
# keeps background threads running post-response, which is what makes
# best-effort viable at all here rather than pure luck.
_dynamic_enrich_executor: ThreadPoolExecutor | None = None
_dynamic_enrich_executor_lock = threading.Lock()


def _get_dynamic_enrich_executor() -> ThreadPoolExecutor:
    global _dynamic_enrich_executor
    if _dynamic_enrich_executor is None:
        with _dynamic_enrich_executor_lock:
            if _dynamic_enrich_executor is None:
                _dynamic_enrich_executor = ThreadPoolExecutor(
                    max_workers=8, thread_name_prefix="dyn-enrich-bg",
                )
    return _dynamic_enrich_executor


def run_bc_background(
    consolidator_input_json: str,
    correlation_id: str,
    shared_kwargs: dict,
) -> None:
    """Fire-and-forget critic+enrichment calls for the dynamic-enrichment
    sufficient-answer path. Submits to the PERSISTENT background executor
    (not request-scoped) and returns immediately -- does not block the
    caller, does not wait for B/C. Each call patches the already-persisted
    chat_turns row via PersistencePort.patch_turn_card when it lands, and
    fires the existing integrator_partial progress event (same shape the
    live parallel-integrator streaming path already uses) for a still-open
    SSE connection to pick up."""
    cfg = get_chat_config()
    critic_system = cfg.prompts.integrator_parallel_critic_system
    enrichment_system = cfg.prompts.integrator_parallel_enrichment_system
    user_tmpl = cfg.prompts.integrator_user_template

    prompt_b = f"{critic_system}\n\n{user_tmpl.format(consolidator_input_json=consolidator_input_json)}"
    prompt_c = f"{enrichment_system}\n\n{user_tmpl.format(consolidator_input_json=consolidator_input_json)}"

    def _patch_and_emit(part: str, patch: dict) -> None:
        if not patch:
            return
        try:
            from app.persistence import get_persistence
            get_persistence().patch_turn_card(correlation_id, patch)
        except Exception:
            logger.warning("[dyn-enrich-bg:%s] patch_turn_card failed for cid=%s", part, correlation_id[:8], exc_info=True)
        try:
            from app.storage.progress import append_integrator_partial
            append_integrator_partial(correlation_id, part, patch)
        except Exception:
            logger.warning("[dyn-enrich-bg:%s] progress event failed for cid=%s", part, correlation_id[:8], exc_info=True)

    def _on_critic_done(fut: Future) -> None:
        try:
            text_b, _usage_b = fut.result()
        except Exception as e:
            logger.warning("[dyn-enrich-bg:critic] call failed for cid=%s: %s", correlation_id[:8], e)
            return
        critic = _parse_json_response(text_b, "dyn-enrich-bg-critic")
        if not critic:
            return
        patch: dict = {}
        if isinstance(critic.get("citations"), list):
            patch["citations"] = critic["citations"]
        indices = critic.get("cited_source_indices")
        if isinstance(indices, list):
            patch["cited_source_indices"] = [int(x) for x in indices if isinstance(x, (int, float))]
        if critic.get("confidence_note") and isinstance(critic["confidence_note"], str):
            patch["confidence_note"] = critic["confidence_note"]
        if isinstance(critic.get("takeaways"), list):
            patch["takeaways"] = critic["takeaways"]
        if isinstance(critic.get("gaps"), list):
            patch["gaps"] = critic["gaps"]
        correction = critic.get("correction")
        if isinstance(correction, dict) and correction.get("original") and correction.get("corrected"):
            patch["correction"] = {
                "original": str(correction["original"]),
                "corrected": str(correction["corrected"]),
            }
        _patch_and_emit("citations", patch)

    def _on_enrichment_done(fut: Future) -> None:
        try:
            text_c, _usage_c = fut.result()
        except Exception as e:
            logger.warning("[dyn-enrich-bg:enrichment] call failed for cid=%s: %s", correlation_id[:8], e)
            return
        enrich = _parse_json_response(text_c, "dyn-enrich-bg-enrichment")
        if not enrich:
            return
        patch = {}
        if isinstance(enrich.get("next_questions_for_user"), list):
            patch["next_questions_for_user"] = enrich["next_questions_for_user"]
        if isinstance(enrich.get("next_steps"), list):
            patch["next_steps"] = enrich["next_steps"]
        if isinstance(enrich.get("suggested_actions"), list):
            patch["suggested_actions"] = enrich["suggested_actions"]
        _patch_and_emit("enrichment", patch)

    executor = _get_dynamic_enrich_executor()
    fut_b = executor.submit(
        _call_llm, prompt_b, "integrator_critic", 3072, **shared_kwargs,
        latency_budget_ms=2000, reasoning_depth="fast",
    )
    fut_b.add_done_callback(_on_critic_done)
    fut_c = executor.submit(
        _call_llm, prompt_c, "integrator_enrichment", 2048, **shared_kwargs,
        latency_budget_ms=1500, reasoning_depth="fast",
    )
    fut_c.add_done_callback(_on_enrichment_done)
