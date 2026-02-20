"""Refined query: store only reframed queries; update on slot fill vs replace on new question.

Flow:
- User: "how do I file an appeal" -> refined_query = "how do I file an appeal"
- System asks "for which payor?"
- User: "Sunshine Health" (slot fill) -> refined_query = "how do I file an appeal for Sunshine Health"
- User: "how do I check eligibility" (new question) -> refined_query = "how do I check eligibility"
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.state.jurisdiction import get_jurisdiction_from_active, jurisdiction_to_summary

logger = logging.getLogger(__name__)

# Patterns that indicate user is answering a slot, not asking new question
SLOT_ANSWER_PATTERNS = [
    re.compile(r"\b(sunshine|united|uhc|aetna|molina|humana|cigna|anthem)\s*(health)?\b", re.I),
    re.compile(r"\b(florida|texas|california|medicaid|medicare)\b", re.I),
    re.compile(r"\b(as a provider|as a member|as a patient)\b", re.I),
]

# Patterns that indicate new/different question
NEW_QUESTION_PATTERNS = [
    re.compile(r"\b(how do i|how do you|what is|what are|when does|where do)\b", re.I),
    re.compile(r"\b(also|and then|what about|different question|new topic)\b", re.I),
]


def looks_like_slot_answer(text: str) -> bool:
    """True if text looks like a jurisdiction/slot answer (e.g. 'Sunshine Health', 'Florida')."""
    t = (text or "").strip()
    if not t or len(t.split()) > 5:
        return False
    for pat in SLOT_ANSWER_PATTERNS:
        if pat.search(t):
            return True
    return False


def last_turn_was_clarification(last_turn: dict | None) -> bool:
    """True if last assistant message looks like a jurisdiction clarification ask."""
    if not last_turn:
        return False
    ac = (last_turn.get("assistant_content") or "").lower()
    return any(
        kw in ac
        for kw in ("health plan", "payer", "which", "specify", "state", "program", "medicare", "medicaid")
    )


def classify_message(
    user_text: str,
    last_turn: dict[str, Any] | None,
    open_slots: list[str],
    existing_refined_query: str | None,
) -> str:
    """Classify user message as slot_fill (same question + context) or new_question (different question).

    Returns: "slot_fill" | "new_question"
    """
    text = (user_text or "").strip()
    if not text:
        return "new_question"

    # If we have open_slots and user text looks like an answer (short, matches slot patterns)
    if open_slots:
        t_lower = text.lower()
        # Short reply that matches payer/state/program/role
        if len(text.split()) <= 5:
            for pat in SLOT_ANSWER_PATTERNS:
                if pat.search(text):
                    return "slot_fill"
        # Explicit "same" or "that one"
        if re.search(r"\b(same|that one|that|yes)\b", t_lower) and len(text.split()) <= 4:
            return "slot_fill"

    # If text looks like a new question (how/what/when + longer)
    for pat in NEW_QUESTION_PATTERNS:
        if pat.search(text) and len(text.split()) >= 4:
            return "new_question"

    # If we have existing refined query and user text is very short, likely slot fill
    if existing_refined_query and len(text.split()) <= 4 and not text.endswith("?"):
        return "slot_fill"

    return "new_question"


def build_refined_query(
    base_query: str,
    jurisdiction: dict[str, Any] | None,
) -> str:
    """Merge jurisdiction into base query. E.g. 'how do I file an appeal' + Sunshine Health -> 'how do I file an appeal for Sunshine Health'."""
    base = (base_query or "").strip()
    if not base:
        return base

    summary = jurisdiction_to_summary(jurisdiction or {})
    if not summary:
        return base

    # Avoid duplicating if already in base
    summary_lower = summary.lower()
    base_lower = base.lower()
    if summary_lower in base_lower:
        return base

    return f"{base} for {summary}".strip()


def compute_refined_query(
    classification: str,
    user_text: str,
    last_refined_query: str | None,
    merged_state: dict[str, Any],
    plan_subquestion_text: str | None,
) -> str:
    """Compute the new refined_query value.

    - slot_fill: merge jurisdiction from state into last_refined_query
    - new_question: use plan_subquestion_text (or user_text if plan not yet available)
    """
    if classification == "slot_fill" and last_refined_query:
        j = get_jurisdiction_from_active((merged_state or {}).get("active"))
        return build_refined_query(last_refined_query, j)

    if plan_subquestion_text and (plan_subquestion_text or "").strip():
        return (plan_subquestion_text or "").strip()

    return (user_text or "").strip()
