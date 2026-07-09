"""Final responder: turn plan + answers into one chat-friendly message via LLM (or fallback). Can stream the draft via message_chunk_callback."""

import json
import logging
from collections.abc import Callable
from typing import Any

from app.communication.json_display_sanitize import (
    DEFAULT_BLEED_FALLBACK,
    build_minimal_answer_card_preserving_metadata,
    display_text_for_parsed_answer_card,
    extract_user_visible_text_from_integrator_raw,
)
from app.planner.schemas import Plan
from app.services.usage import LLMUsageDict
from app.trace_log import trace_entered

logger = logging.getLogger(__name__)

ConsolidatorType = str  # "factual" | "canonical" | "blended"


def _emit(emitter: Callable[[str], None] | None, msg: str) -> None:
    if emitter and msg.strip():
        emitter(msg.strip())


def blended_canonical_score(plan: Plan) -> float:
    """Average of (1 - intent_score) over sub-questions where intent_score is not None. Fallback 0.5."""
    scores: list[float] = []
    subquestions = getattr(plan, "subquestions", None) or []
    for sq in subquestions:
        s = getattr(sq, "intent_score", None)
        if s is not None:
            try:
                x = float(s)
                if 0 <= x <= 1:
                    scores.append(1.0 - x)
            except (TypeError, ValueError):
                pass
    if not scores:
        return 0.5
    return sum(scores) / len(scores)


def choose_consolidator_type(
    canonical_score: float,
    factual_max: float,
    canonical_min: float,
) -> ConsolidatorType:
    """Map blended canonical score to factual | canonical | blended."""
    if canonical_score < factual_max:
        return "factual"
    if canonical_score > canonical_min:
        return "canonical"
    return "blended"


def _build_consolidator_input_json(
    plan: Plan,
    stub_answers: list[str],
    user_message: str,
    *,
    retrieval_metadata: dict | None = None,
    sources_summary: list[dict] | None = None,
    jurisdiction_summary: str | None = None,
    user_provided_context: str | None = None,
    workflow_selection_ui: dict[str, Any] | None = None,
    previous_thread_summary: str | None = None,
    react_draft: str | None = None,
    source_texts: list[dict] | None = None,
) -> str:
    """Build JSON payload for consolidator: user_message, subquestions, answers, retrieval_metadata, sources_summary, jurisdiction_summary, user_provided_context, previous_thread_summary."""
    _subs = getattr(plan, "subquestions", None) or []
    _stub = stub_answers if stub_answers is not None else []
    subquestions = [{"id": sq.id, "text": sq.text} for sq in _subs]
    answers = []
    for i, sq in enumerate(_subs):
        ans = _stub[i] if i < len(_stub) else "[No answer yet]"
        answers.append({"sq_id": sq.id, "answer": (ans or "").strip()})
    payload = {
        "user_message": user_message.strip(),
        "subquestions": subquestions,
        "answers": answers,
    }
    if retrieval_metadata:
        payload["retrieval_metadata"] = retrieval_metadata
    if sources_summary:
        payload["sources_summary"] = sources_summary
    if jurisdiction_summary and jurisdiction_summary.strip():
        payload["jurisdiction_summary"] = jurisdiction_summary.strip()
    if user_provided_context and user_provided_context.strip():
        payload["user_provided_context"] = user_provided_context.strip()
    if workflow_selection_ui:
        payload["workflow_selection_ui"] = workflow_selection_ui
    # Phase 13.7 — rolling thread summary. The integrator gets the
    # PREVIOUS summary as input and is asked to refine it (not append)
    # to integrate this turn. Output goes back as ``thread_summary`` in
    # the AnswerCard JSON. ≤60 words. See prompt instructions.
    if previous_thread_summary and previous_thread_summary.strip():
        payload["previous_thread_summary"] = previous_thread_summary.strip()
    # Two-phase enricher: react_draft is what the user already saw; the
    # integrator enriches rather than restates. source_texts provides
    # verbatim chunks for accurate citation snippets.
    if react_draft and react_draft.strip():
        payload["react_draft"] = react_draft.strip()[:6000]
    if source_texts:
        payload["source_texts"] = source_texts
    return json.dumps(payload, indent=2)


def _extract_json_from_text(text: str) -> str:
    """Extract JSON object from text that may have markdown fences or leading/trailing prose."""
    text = (text or "").strip()
    if not text:
        return ""
    # Strip markdown code fence (```json ... ``` or ``` ... ```)
    if "```" in text:
        start = text.find("```")
        rest = text[start + 3 :].lstrip()
        if rest.lower().startswith("json"):
            rest = rest[4:].lstrip()
        end = rest.find("```")
        if end >= 0:
            rest = rest[:end].rstrip()
        text = rest
    # If text looks like it has JSON, try to find the outermost {...}
    if "{" in text and "}" in text:
        start = text.find("{")
        depth = 0
        for i, c in enumerate(text[start:], start):
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    return text.strip()


def _parse_answer_card(text: str, emitter: Callable[[str], None] | None = None) -> dict | None:
    """Parse text as JSON and validate AnswerCard shape (mode, direct_answer, sections). Returns dict or None.
    Tries stdlib json first, then json_repair for malformed LLM output. Optionally emits progress to emitter."""
    if not text or not text.strip():
        return None
    text = _extract_json_from_text(text)
    if not text:
        return None

    def _normalize_answer_card(data: dict) -> dict:
        """Coerce sections so one bad intent does not void the whole card (avoids losing details)."""
        valid_intents = ("process", "requirements", "definitions", "exceptions", "references")
        out = dict(data)
        sections = out.get("sections")
        if not isinstance(sections, list):
            return out
        fixed: list[dict] = []
        for item in sections:
            if not isinstance(item, dict):
                continue
            sec = dict(item)
            intent = sec.get("intent")
            if intent is None or intent not in valid_intents:
                if intent is not None and intent not in valid_intents:
                    logger.warning(
                        "Integrator AnswerCard: invalid section intent %r coerced to references",
                        intent,
                    )
                sec["intent"] = "references"
            fixed.append(sec)
        out["sections"] = fixed
        return out

    def _validate(data: object) -> dict | None:
        if not isinstance(data, dict):
            return None
        if "mode" not in data or "direct_answer" not in data or "sections" not in data:
            return None
        if data.get("mode") not in ("FACTUAL", "CANONICAL", "BLENDED"):
            return None
        sections = data.get("sections")
        if not isinstance(sections, list):
            return None
        data = _normalize_answer_card(data)
        return data

    def _try_parse(raw: str) -> dict | None:
        for parse_fn, label in [(json.loads, "json"), (_json_repair_loads, "json_repair")]:
            try:
                data = parse_fn(raw)
                return _validate(data)
            except Exception:
                pass
        return None

    try:
        out = _try_parse(text)
        if out is not None:
            logger.debug("AnswerCard parsed successfully")
            return out
    except Exception as e:
        logger.debug("AnswerCard parse failed: %s", e)
    return None


def _json_repair_loads(text: str) -> object:
    import json_repair
    return json_repair.loads(text)


# Phase 0.16b: the LLM-based ``_repair_json`` tier was deleted.
# ``_parse_answer_card`` already runs ``json.loads`` followed by the
# ``json_repair`` library (line ~172) — between them they handle the
# malformed shapes an LLM re-emission pass would have caught, without an
# additional API call. The LLM repair path was responsible for the
# Groq ``daily TPD exhausted`` leak (see worker logs 2026-04-17) and
# added $0.01 + 2-5s per malformed turn. If both stdlib + json_repair
# fail, the flow below now goes straight to
# ``extract_user_visible_text_from_integrator_raw`` which wraps the
# prose as FACTUAL and emits via the Phase 0.12 envelope — clean
# failure, no extra LLM burn.


def _emit_integrator_chunks(text: str, message_chunk_callback: Callable[[str], None] | None) -> None:
    """Simulate streaming for UI when using non-streaming llm_manager path."""
    if not message_chunk_callback or not text:
        return
    step = max(32, min(256, len(text) // 48 or 32))
    for i in range(0, len(text), step):
        message_chunk_callback(text[i : i + step])


def _fallback_message(plan: Plan, stub_answers: list[str]) -> str:
    """Simple concatenation without internal labels or repeated questions. Plain paragraphs."""
    parts: list[str] = []
    _subs = getattr(plan, "subquestions", None) or []
    _stub = stub_answers if stub_answers is not None else []
    for i, sq in enumerate(_subs):
        ans = _stub[i] if i < len(_stub) else "[No answer yet]"
        parts.append(ans.strip())
    return "\n\n".join(p for p in parts if p)


def format_response(
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
    llm_stage: str = "integrator",
    mode: str | None = None,
    previous_thread_summary: str | None = None,
    user_profile: dict | None = None,
    react_draft: str | None = None,
    source_texts: list[dict] | None = None,
) -> tuple[str, LLMUsageDict | None]:
    """Turn plan + answers into one chat-friendly message via llm_manager (integrator or integrator_roster).
    On LLM failure, returns fallback and None usage."""
    _subs = getattr(plan, "subquestions", None) or []
    trace_entered("responder.final.format_response", subquestions=len(_subs))
    if not _subs:
        return ("", None)

    # Formatting message emitted by orchestrator before integrate
    usage: LLMUsageDict | None = None

    try:
        from app.chat_config import get_chat_config
        from app.services.llm_manager import generate_sync

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
        )
        canonical_score = blended_canonical_score(plan)
        consolidator_type = choose_consolidator_type(
            canonical_score,
            cfg.prompts.consolidator_factual_max,
            cfg.prompts.consolidator_canonical_min,
        )
        consolidator_line = f"Consolidator: {consolidator_type.capitalize()} (blended canonical score: {canonical_score:.2f})"
        logger.info("[consolidator] %s", consolidator_line)
        _emit(
            emitter,
            f"  → Building answer card ({consolidator_type} mode, score {canonical_score:.2f})…",
        )

        if consolidator_type == "factual":
            prompt_system = cfg.prompts.integrator_factual_system
        elif consolidator_type == "canonical":
            prompt_system = cfg.prompts.integrator_canonical_system
        else:
            prompt_system = cfg.prompts.integrator_blended_system
        # 2026-05-06: integrator/consolidator is the user-facing voice —
        # tone + ai_experience_level matter most here. Splice user
        # profile rendered_prompt into the system block.
        try:
            from app.pipeline.personalization import splice_user_profile
            prompt_system = splice_user_profile(prompt_system, user_profile)
        except Exception:
            pass  # never let personalization break a real turn
        prompt_user = cfg.prompts.integrator_user_template.format(
            consolidator_input_json=consolidator_input_json,
        )
        prompt = f"{prompt_system}\n\n{prompt_user}"

        _emit(emitter, "  Draft composer: calling LLM to generate answer card…")
        text, usage = generate_sync(
            prompt,
            stage=llm_stage,
            max_tokens=2500,
            config_sha=config_sha,
            correlation_id=correlation_id,
            thread_id=thread_id,
            phi_detected=phi_detected,
            mode=mode,
        )
        text = (text or "").strip()

        if text:
            _emit(emitter, "  Validator: checking answer card (mode, direct_answer, sections)…")
            parsed = _parse_answer_card(text, emitter=emitter)
            # Phase 0.16b: no LLM-based repair tier. _parse_answer_card already
            # tries json.loads then json_repair — if both fail, fall straight
            # through to the FACTUAL-wrap fallback below.
            if parsed is not None:
                _emit(emitter, "  Final composer: answer card ready.")
                logger.debug("Emitting canonical AnswerCard JSON to frontend")
                parsed = dict(parsed)
                display_txt = display_text_for_parsed_answer_card(parsed)
                if not display_txt.strip():
                    # BETA-sprint Move 1 — JSON reliability on the
                    # transform path. The bench surfaced a failure
                    # mode where the integrator emits valid JSON but
                    # the direct_answer field bleeds (nested JSON,
                    # raw markdown). On a continuation turn (signaled
                    # by previous_thread_summary != None) we have a
                    # perfectly good stub answer — the transform
                    # skill's prose — sitting in stub_answers. Use it
                    # before going to the generic "trouble formatting"
                    # message. This converts a user-visible "rephrase
                    # your question" failure into the actual answer
                    # the model intended to give.
                    if previous_thread_summary and stub_answers:
                        candidate = (stub_answers[0] if stub_answers else "").strip()
                        # Require enough text that we're confident
                        # we're not papering over a real failure.
                        if candidate and len(candidate) >= 20:
                            display_txt = candidate[:8000]
                            logger.warning(
                                "[transform-path] integrator direct_answer "
                                "bled; recovered using stub answer (cid=%s, "
                                "stub_len=%d)",
                                (correlation_id or "?")[:8], len(candidate),
                            )
                if not display_txt.strip():
                    display_txt = DEFAULT_BLEED_FALLBACK
                parsed["direct_answer"] = display_txt
                _emit_integrator_chunks(display_txt, message_chunk_callback)
                # Emit canonical JSON so frontend receives clean JSON (no markdown fence)
                return (json.dumps(parsed), usage)
            # Not valid AnswerCard: never stream raw model JSON (common: resolutions-only blob).
            _log_truncated = (text or "")[:2000] + ("..." if len(text or "") > 2000 else "")
            logger.warning(
                "Consolidator output was not valid AnswerCard JSON; wrapping prose as FACTUAL. LLM response (truncated): %s",
                _log_truncated,
            )
            visible = extract_user_visible_text_from_integrator_raw(text)
            minimal = build_minimal_answer_card_preserving_metadata(visible, text)
            _emit_integrator_chunks(visible, message_chunk_callback)
            return (json.dumps(minimal), usage)
    except Exception as e:
        logger.warning(
            "Integrator LLM failed, using fallback (no valid response). exception=%s",
            e,
            exc_info=True,
        )
        logger.debug("Using simple format (integrator LLM failed)")

    fb = _fallback_message(plan, stub_answers)
    _emit_integrator_chunks(fb, message_chunk_callback)
    return (fb, None)
