"""Query refinement: decide when to ask user to confirm/rephrase, and reframe for retrieval."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Heuristics for when to ask for refinement
VAGUE_MIN_WORDS = 3  # Very short queries
MULTI_INTENT_SUBQUESTION_THRESHOLD = 3  # Many subquestions may indicate ambiguity
LOW_CONFIDENCE_THRESHOLD = 0.4  # intent_score near 0.5 with low confidence


def need_query_refinement(plan: Any) -> tuple[bool, list[str]]:
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
    if len(subquestions) >= MULTI_INTENT_SUBQUESTION_THRESHOLD:
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


def reframe_for_retrieval(question: str, intent: str | None, question_intent: str | None = None) -> str:
    """Reframe the question for better retrieval. Internal use; does not ask user.

    For canonical intent: expand to policy/process phrasing.
    For factual intent: keep or narrow to fact-seeking.
    Returns the reframed question text.
    """
    q = (question or "").strip()
    if not q:
        return q

    # Use question_intent or intent
    it = (question_intent or intent or "").lower()

    # For now, return as-is; LLM-based reframing can be added later
    if it == "canonical":
        # Could add: "process for", "how to", "requirements for" expansions
        return q
    if it == "factual":
        # Could add: fact-finding phrasing
        return q

    return q
