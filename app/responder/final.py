"""Final responder: turn plan + answers into one chat-friendly message via LLM (or fallback). Can stream the draft via message_chunk_callback."""
import asyncio
import json
import logging
from collections.abc import Callable

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
    for sq in plan.subquestions:
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


def _build_consolidator_input_json(plan: Plan, stub_answers: list[str], user_message: str) -> str:
    """Build JSON payload for consolidator: user_message, subquestions, answers."""
    subquestions = [{"id": sq.id, "text": sq.text} for sq in plan.subquestions]
    answers = []
    for i, sq in enumerate(plan.subquestions):
        ans = stub_answers[i] if i < len(stub_answers) else "[No answer yet]"
        answers.append({"sq_id": sq.id, "answer": (ans or "").strip()})
    payload = {
        "user_message": user_message.strip(),
        "subquestions": subquestions,
        "answers": answers,
    }
    return json.dumps(payload, indent=2)


def _parse_answer_card(text: str, emitter: Callable[[str], None] | None = None) -> dict | None:
    """Parse text as JSON and validate AnswerCard shape (mode, direct_answer, sections). Returns dict or None.
    Tries stdlib json first, then json_repair for malformed LLM output. Optionally emits progress to emitter."""
    if not text or not text.strip():
        return None
    text = text.strip()
    # Strip markdown code fence if present (e.g. ```json\n{...}\n```)
    if text.startswith("```"):
        _emit(emitter, "Stripping markdown code fence from consolidator output…")
        lines = text.split("\n")
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

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
        valid_intents = ("process", "requirements", "definitions", "exceptions", "references")
        for item in sections:
            if not isinstance(item, dict):
                continue
            intent = item.get("intent")
            if intent is not None and intent not in valid_intents:
                return None
        return data

    try:
        data = json.loads(text)
        out = _validate(data)
        if out is not None:
            _emit(emitter, "AnswerCard parsed (valid JSON).")
            return out
        _emit(emitter, "JSON valid but not AnswerCard shape; skipping json_repair.")
        return None
    except (json.JSONDecodeError, TypeError):
        _emit(emitter, "Standard JSON parse failed; trying json_repair…")
    try:
        import json_repair
        data = json_repair.loads(text)
        out = _validate(data)
        if out is not None:
            _emit(emitter, "AnswerCard parsed (json_repair).")
            logger.debug("AnswerCard parsed via json_repair")
            return out
        _emit(emitter, "json_repair produced output but not a valid AnswerCard.")
    except Exception as e:
        _emit(emitter, "json_repair could not fix JSON.")
        logger.debug("json_repair failed for AnswerCard: %s", e)
    return None


def _repair_json(cfg, invalid_text: str) -> str:
    """One retry: call LLM with repair prompt, return new text."""
    from app.services.llm_provider import get_llm_provider
    repair_system = getattr(cfg.prompts, "integrator_repair_system", None) or (
        "You returned invalid JSON. Return ONLY valid JSON that matches the AnswerCard schema. "
        "Do not include any commentary or markdown. Ensure all strings are quoted and arrays/objects are valid. "
        "Use the same content as before; do not add new facts."
    )
    repair_user = (
        "Your previous invalid output:\n\n" + invalid_text[:8000] + "\n\n"
        "Return ONLY valid JSON that matches the AnswerCard schema. Do not include any commentary or markdown."
    )
    prompt = f"{repair_system}\n\n{repair_user}"
    try:
        provider = get_llm_provider()
        text, _ = asyncio.run(provider.generate_with_usage(prompt))
        return (text or "").strip()
    except Exception as e:
        logger.warning("Repair JSON call failed: %s", e)
        return ""


def _fallback_message(plan: Plan, stub_answers: list[str]) -> str:
    """Simple concatenation without internal labels or repeated questions. Plain paragraphs."""
    parts: list[str] = []
    for i, sq in enumerate(plan.subquestions):
        ans = stub_answers[i] if i < len(stub_answers) else "[No answer yet]"
        parts.append(ans.strip())
    return "\n\n".join(p for p in parts if p)


async def _stream_integrator(
    prompt: str,
    message_chunk_callback: Callable[[str], None],
) -> str:
    """Stream integrator LLM output; emit each chunk immediately, then accumulate. Returns full text."""
    from app.services.llm_provider import get_llm_provider
    provider = get_llm_provider()
    full: list[str] = []
    async for chunk in provider.stream_generate(prompt):
        if chunk:
            message_chunk_callback(chunk)  # emit before storing so UI updates immediately
            full.append(chunk)
    return "".join(full)


def format_response(
    plan: Plan,
    stub_answers: list[str],
    user_message: str,
    emitter: Callable[[str], None] | None = None,
    message_chunk_callback: Callable[[str], None] | None = None,
) -> tuple[str, LLMUsageDict | None]:
    """Turn plan + answers into one chat-friendly message via LLM. If message_chunk_callback is set, stream the draft; returns (message, None) for usage when streaming. On LLM failure, returns fallback and None usage."""
    trace_entered("responder.final.format_response", subquestions=len(plan.subquestions))
    if not plan.subquestions:
        return ("", None)

    _emit(emitter, "Formatting the response for chat…")
    usage: LLMUsageDict | None = None

    try:
        from app.chat_config import get_chat_config
        from app.services.llm_provider import get_llm_provider

        cfg = get_chat_config()
        consolidator_input_json = _build_consolidator_input_json(plan, stub_answers, user_message)
        canonical_score = blended_canonical_score(plan)
        consolidator_type = choose_consolidator_type(
            canonical_score,
            cfg.prompts.consolidator_factual_max,
            cfg.prompts.consolidator_canonical_min,
        )
        consolidator_line = f"Consolidator: {consolidator_type.capitalize()} (blended canonical score: {canonical_score:.2f})"
        _emit(emitter, consolidator_line)
        logger.info("[consolidator] %s", consolidator_line)

        if consolidator_type == "factual":
            prompt_system = cfg.prompts.integrator_factual_system
        elif consolidator_type == "canonical":
            prompt_system = cfg.prompts.integrator_canonical_system
        else:
            prompt_system = cfg.prompts.integrator_blended_system
        prompt_user = cfg.prompts.integrator_user_template.format(
            consolidator_input_json=consolidator_input_json,
        )
        prompt = f"{prompt_system}\n\n{prompt_user}"

        if message_chunk_callback:
            text = asyncio.run(_stream_integrator(prompt, message_chunk_callback))
            text = (text or "").strip()
        else:
            provider = get_llm_provider()
            text, usage = asyncio.run(provider.generate_with_usage(prompt))
            text = (text or "").strip()

        if text:
            parsed = _parse_answer_card(text, emitter=emitter)
            if parsed is None and (text.strip().startswith("{") or "```" in text):
                _emit(emitter, "Repairing invalid JSON via LLM…")
                repaired = _repair_json(cfg, text)
                if repaired:
                    parsed = _parse_answer_card(repaired, emitter=emitter)
                    if parsed is not None:
                        text = repaired
            if parsed is not None:
                _emit(emitter, "Emitting canonical AnswerCard JSON to frontend.")
                # Emit canonical JSON so frontend receives clean JSON (no markdown fence)
                return (json.dumps(parsed), usage if not message_chunk_callback else None)
            # Not valid AnswerCard: return raw text so UI shows something
            logger.warning("Consolidator output was not valid AnswerCard JSON; returning as prose fallback")
            _emit(emitter, "Returning raw consolidator output (not valid AnswerCard).")
            return (text, usage if not message_chunk_callback else None)
    except Exception as e:
        logger.warning("Integrator LLM failed, using fallback: %s", e)
        _emit(emitter, "Using simple format.")

    return (_fallback_message(plan, stub_answers), None)
