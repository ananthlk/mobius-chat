"""Planner: decompose user message into subquestions (LLM + JSON) and classify patient vs non-patient.
Emits 'thinking' chunks via callback for streaming display.
"""
import asyncio
import json
import logging
import re
from collections.abc import Callable

from app.planner.schemas import Plan, SubQuestion, QuestionIntent
from app.trace_log import trace_entered

logger = logging.getLogger(__name__)

_VALID_INTENTS = frozenset({"factual", "canonical"})


def _emit(thinking_emitter: Callable[[str], None] | None, chunk: str) -> None:
    if thinking_emitter and chunk.strip():
        thinking_emitter(chunk.strip())


def _rule_based_decompose(text: str, parser_cfg) -> list[tuple[str, str, str | None, QuestionIntent | None, float | None]]:
    """Fallback: split on separators, return list of (id, text, kind, intent, intent_score). intent/intent_score are None."""
    separators = parser_cfg.decomposition_separators or [" and ", " also ", " then "]
    pattern = "|".join(re.escape(s) for s in separators)
    parts = re.split(pattern, text, flags=re.IGNORECASE)
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        parts = [text]
    return [(f"sq{i+1}", p, None, None, None) for i, p in enumerate(parts)]


def _classify_kind(text: str, parser_cfg) -> str:
    """Classify subquestion as patient or non_patient using keyword match."""
    keywords = parser_cfg.patient_keywords or [
        "my doctor", "my medication", "my visit", "my record", "my records", "my care",
        "what did my doctor", "do I qualify", "do we qualify", "my eligibility", "based on my",
        "my enrollment", "my coverage", "am I eligible", "are we eligible",
    ]
    patient_pattern = r"\b(" + "|".join(re.escape(k) for k in keywords) + r")\b"
    patient_re = re.compile(patient_pattern.replace(" ", r"\s+"), re.IGNORECASE)
    return "patient" if patient_re.search(text) else "non_patient"


_VALID_KINDS = frozenset({"patient", "non_patient"})


def _heuristic_intent(text: str) -> QuestionIntent | None:
    """Simple heuristic for question_intent when LLM does not provide it."""
    t = text.lower().strip()
    factual_starts = ("what is", "what are", "how many", "when ", "where ", "which ", "who ", "what date", "what number")
    canonical_starts = ("describe", "explain", "how does", "how do ", "what is the process", "summarize", "outline")
    if any(t.startswith(p) for p in factual_starts):
        return "factual"
    if any(t.startswith(p) for p in canonical_starts):
        return "canonical"
    return None


def _llm_decompose(
    message: str,
    context: str = "",
) -> tuple[list[tuple[str, str, str | None, QuestionIntent | None, float | None]] | None, dict | None]:
    """Call LLM to decompose message into subquestions and classify each (kind, question_intent, intent_score).
    Returns (list of (id, text, kind, intent, intent_score) or None on failure, llm_usage dict or None).
    """
    trace_entered("planner.parser._llm_decompose")
    try:
        from app.chat_config import get_chat_config
        from app.services.llm_provider import get_llm_provider
        cfg = get_chat_config()
        provider = get_llm_provider()
        system = cfg.prompts.decompose_system
        user = cfg.prompts.decompose_user_template.format(message=message, context=context or "")
        prompt = f"{system}\n\n{user}"
        logger.info("[parser] calling LLM for decomposition (model=%s)", getattr(provider, 'model_name', 'unknown'))
        try:
            raw, usage = asyncio.run(provider.generate_with_usage(prompt))
        except asyncio.TimeoutError as te:
            logger.error("[parser] LLM call timed out: %s", te)
            raise
        except Exception as llm_e:
            logger.error("[parser] LLM call raised exception: %s", llm_e, exc_info=True)
            raise
        logger.info("[parser] LLM decomposition returned len=%d", len(raw) if raw else 0)
        if not raw or not raw.strip():
            return (None, usage)
        if "subquestions" not in raw or "{" not in raw:
            logger.warning("LLM decomposition: response is not JSON (model may have answered). Using fallback.")
            return (None, usage)
        text = raw.strip()
        if "```" in text:
            start = text.find("```")
            if start >= 0:
                start = text.find("\n", start) + 1
                end = text.find("```", start)
                if end > start:
                    text = text[start:end]
        data = json.loads(text)
        sqs = data.get("subquestions") or []
        if not sqs or not isinstance(sqs, list):
            return (None, usage)
        out: list[tuple[str, str, str | None, QuestionIntent | None, float | None]] = []
        for i, item in enumerate(sqs):
            sid, stext, skind, sintent, sscore = f"sq{i+1}", "", None, None, None
            if isinstance(item, dict):
                sid = item.get("id") or f"sq{i+1}"
                stext = item.get("text") or ""
                skind = item.get("kind")
                if isinstance(skind, str):
                    skind = skind.strip().lower() if skind else None
                if skind not in _VALID_KINDS:
                    skind = None
                sintent = item.get("question_intent")
                if isinstance(sintent, str):
                    sintent = sintent.strip().lower() if sintent else None
                if sintent not in _VALID_INTENTS:
                    sintent = _heuristic_intent(stext) if stext else None
                raw_score = item.get("intent_score")
                if raw_score is not None:
                    try:
                        v = float(raw_score)
                        if 0 <= v <= 1:
                            sscore = round(v, 2)
                    except (TypeError, ValueError):
                        pass
            elif isinstance(item, str):
                stext = item
                sintent = _heuristic_intent(stext)
            if stext and isinstance(stext, str) and isinstance(sid, str):
                out.append((str(sid).strip() or f"sq{i+1}", stext.strip(), skind, sintent, sscore))
        return (out if out else None, usage)
    except Exception as e:
        logger.warning("LLM decomposition failed, using rule-based fallback: %s", e)
        return (None, None)


def parse(
    message: str,
    *,
    thinking_emitter: Callable[[str], None] | None = None,
    context: str = "",
) -> Plan:
    """Parse user message into a plan (subquestions + patient/non_patient).
    Uses LLM decomposition (JSON) when available; falls back to rule-based split.
    Calls thinking_emitter(chunk) for each 'thinking' fragment (e.g. for UI).
    """
    thinking_log: list[str] = []

    def emit(chunk: str) -> None:
        thinking_log.append(chunk)
        if thinking_emitter:
            thinking_emitter(chunk)

    emitter = emit

    text = (message or "").strip()
    _emit(emitter, "I'm reading your question and breaking it down.")
    if not text:
        _emit(emitter, "You didn't ask anything yet—please type a question.")
        return Plan(subquestions=[], thinking_log=thinking_log)

    from app.chat_config import get_chat_config
    parser_cfg = get_chat_config().parser

    # Step 1: Decompose (LLM first, then rule-based fallback). LLM also classifies when possible.
    triples, plan_usage = _llm_decompose(text, context=context or "")
    if not triples:
        _emit(emitter, "I'm splitting your message into clear parts.")
        triples = _rule_based_decompose(text, parser_cfg)
        plan_usage = None
    else:
        n = len(triples)
        _emit(emitter, f"I broke your question into {n} part{'s' if n != 1 else ''}.")

    # Step 2: Use LLM kind, intent, intent_score when present; otherwise classify with keywords / heuristic
    from app.services.retrieval_calibration import intent_to_score
    subquestions: list[SubQuestion] = []
    for sq_id, sq_text, llm_kind, llm_intent, llm_score in triples:
        kind = llm_kind if llm_kind in _VALID_KINDS else _classify_kind(sq_text, parser_cfg)
        intent = llm_intent if llm_intent in _VALID_INTENTS else _heuristic_intent(sq_text)
        score = llm_score if llm_score is not None else intent_to_score(intent)
        subquestions.append(SubQuestion(id=sq_id, text=sq_text, kind=kind, question_intent=intent, intent_score=score))
        snippet = sq_text[:50] + "..." if len(sq_text) > 50 else sq_text
        if kind == "non_patient":
            _emit(emitter, f"• {sq_id}: “{snippet}” — I can look this up.")
        else:
            _emit(emitter, f"• {sq_id}: “{snippet}” — This looks personal; I don’t have access to your records.")

    n_sq = len(subquestions)
    patient_count = sum(1 for sq in subquestions if sq.kind == "patient")
    if patient_count == 0:
        _emit(emitter, "Nothing personal in there—I can answer from what we have on file.")
    elif patient_count == n_sq:
        _emit(emitter, "These are about your own info—I can’t access that yet, so I’ll say so where it comes up.")
    else:
        _emit(emitter, f"One part is about your own info; I’ll answer the other {n_sq - patient_count} from our materials.")

    _emit(emitter, f"I’ll answer these {n_sq} part{'s' if n_sq != 1 else ''} for you.")
    return Plan(subquestions=subquestions, thinking_log=thinking_log, llm_usage=plan_usage)
