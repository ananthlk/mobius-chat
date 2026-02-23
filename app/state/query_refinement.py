"""Query refinement: decide when to ask user to confirm/rephrase, and reframe for retrieval."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Heuristics for when to ask for refinement
VAGUE_MIN_WORDS = 3  # Very short queries
MULTI_INTENT_SUBQUESTION_THRESHOLD = 3  # Many subquestions may indicate ambiguity
LOW_CONFIDENCE_THRESHOLD = 0.4  # intent_score near 0.5 with low confidence

# Patterns suggesting a complete scenario (don't ask user to narrow)
# Must describe a specific case (age, income, patient, location), not just mention payer in abstract question
_CONCRETE_SCENARIO_PATTERNS = (
    r"\b\d+\s*(year|yo|years?\s*old)\b",  # age
    r"\$\d+",  # income / dollar amounts
    r"\b\d+\s*(kid|child|dependent|month|mo)\b",  # dependents, income period
    r"\b(lives?|in|from)\s+[\w\s]+(florida|fl|tampa|tx|california)\b",  # location
    r"\b(patient|client)\b.*\b(qualify|eligibility|enroll)\b",  # patient scenario
    r"\b(patient|client)\b.*\b(medicaid|medicare|sunshine|united)\b",  # patient + payer
)


def _has_concrete_scenario(user_message: str) -> bool:
    """True if the question describes a complete scenario (age, income, location, etc.)."""
    import re

    text = (user_message or "").strip().lower()
    if len(text) < 30:
        return False
    for pat in _CONCRETE_SCENARIO_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False


def need_query_refinement(plan: Any, *, user_message: str = "") -> tuple[bool, list[str]]:
    """Decide if we should ask the user to confirm or rephrase before answering.

    Returns (should_ask, refinement_suggestions).
    refinement_suggestions: list of alternative phrasings (same query rephrased, or alternatives).
    """
    subquestions = getattr(plan, "subquestions", None) or []
    if not subquestions:
        return (False, [])

    suggestions: list[str] = []

    # Very short / vague: single subquestion with few words
    if len(subquestions) == 1:
        text = (getattr(subquestions[0], "text", None) or "").strip()
        words = len(text.split())
        if words < VAGUE_MIN_WORDS:
            suggestions.append(text)  # Echo back as suggestion
            return (True, suggestions)

    # Many subquestions: user may have asked multiple things
    # Skip refinement when question describes a complete scenario (age, income, location, etc.)
    # — user wants all parts answered, not to pick one
    if len(subquestions) >= MULTI_INTENT_SUBQUESTION_THRESHOLD:
        if _has_concrete_scenario(user_message):
            return (False, [])
        for sq in subquestions[:3]:
            t = getattr(sq, "text", None) or ""
            if t.strip():
                suggestions.append(t.strip())
        return (True, suggestions)

    # Low confidence / ambiguous intent
    for sq in subquestions:
        score = getattr(sq, "intent_score", None)
        if score is not None:
            try:
                s = float(score)
                if abs(s - 0.5) < (0.5 - LOW_CONFIDENCE_THRESHOLD):
                    # Score near 0.5 = ambiguous
                    suggestions.append(getattr(sq, "text", "") or "")
                    return (True, suggestions)
            except (TypeError, ValueError):
                pass

    return (False, [])


def reframe_for_retrieval(
    question: str,
    intent: str | None,
    question_intent: str | None = None,
    *,
    last_refined_query: str | None = None,
    jurisdiction: dict | None = None,
    is_followup: bool = False,
) -> str:
    """Reframe the question for retrieval. Strips jurisdiction from question, recombines with jurisdiction.

    When is_followup and last_refined_query: use last_refined_query (with prior topic) + jurisdiction.
    Otherwise: strip jurisdiction from question, then recombine with jurisdiction.
    """
    from app.state.intent_jurisdiction import strip_jurisdiction_from_intent
    from app.state.jurisdiction import jurisdiction_to_summary

    q = (question or "").strip()
    if not q:
        return q

    # Follow-up: use last_refined_query (expanded from prior turn) + jurisdiction
    if is_followup and last_refined_query and (last_refined_query or "").strip():
        base = strip_jurisdiction_from_intent(last_refined_query)
        if not base:
            base = (last_refined_query or "").strip()
        summary = jurisdiction_to_summary(jurisdiction or {})
        if summary and summary.lower() not in base.lower():
            return f"{base} for {summary}".strip()
        return base

    # Strip jurisdiction from question, recombine with jurisdiction
    if jurisdiction:
        base = strip_jurisdiction_from_intent(q)
        if base != q or not base:
            base = base or q
        summary = jurisdiction_to_summary(jurisdiction)
        if summary and summary.lower() not in base.lower():
            return f"{base} for {summary}".strip()
        return base

    return q
