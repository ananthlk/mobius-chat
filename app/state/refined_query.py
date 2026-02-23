"""Refined query: store only reframed queries; update on slot fill vs replace on new question.

Flow:
- User: "how do I file an appeal" -> refined_query = "how do I file an appeal"
- System asks "for which payor?"
- User: "Sunshine Health" (slot fill) -> refined_query = "how do I file an appeal for Sunshine Health"
- User: "how do I check eligibility" (new question) -> refined_query = "how do I check eligibility"
- User: "how about for United" (jurisdiction_change) -> same intent, jurisdiction = United
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.state.intent_jurisdiction import strip_jurisdiction_from_intent
from app.state.jurisdiction import get_jurisdiction_from_active, jurisdiction_to_summary

logger = logging.getLogger(__name__)

# Patterns for jurisdiction change: same intent, different jurisdiction (short jurisdiction mention only)
JURISDICTION_CHANGE_PATTERNS = [
    re.compile(r"^(?:how about|what about)\s+(?:for\s+)?(.+)$", re.I),  # "how about United" or "how about for United"
    re.compile(r"^and\s+for\s+(.+)$", re.I),
    re.compile(r"^(?:same question|same thing)\s+(?:for|with)\s+(.+)$", re.I),
]


def _looks_like_jurisdiction(text: str) -> bool:
    """True if captured part looks like a payer/state (short, matches slot patterns)."""
    t = (text or "").strip()
    if not t or len(t.split()) > 4:
        return False
    for pat in SLOT_ANSWER_PATTERNS:
        if pat.search(t):
            return True
    return False

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
    """Classify user message as slot_fill, jurisdiction_change, or new_question.

    Returns: "slot_fill" | "jurisdiction_change" | "new_question"
    """
    text = (user_text or "").strip()
    if not text:
        return "new_question"

    # Jurisdiction change: "how about for United", "what about for Molina" — same intent, swap jurisdiction
    if existing_refined_query:
        for pat in JURISDICTION_CHANGE_PATTERNS:
            m = pat.match(text)
            if m and _looks_like_jurisdiction(m.group(1)):
                return "jurisdiction_change"

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


def is_followup_continuation(
    user_text: str,
    last_turn: dict[str, Any] | None,
    last_refined_query: str | None,
) -> bool:
    """True when user message is a follow-up that references prior turn (e.g. 'can you search for it')."""
    text = (user_text or "").strip()
    if not text or not last_refined_query or not last_turn:
        return False
    t_lower = text.lower()
    # Short message with reference words
    ref_words = ("it", "that", "their", "them", "this", "there")
    has_ref = any(rf in t_lower for rf in ref_words)
    if not has_ref:
        return False
    # Last turn had substantive answer (not just clarification)
    ac = (last_turn.get("assistant_content") or "").strip()
    if len(ac) < 50:
        return False
    return len(text.split()) <= 12


def build_refined_query(
    base_query: str,
    jurisdiction: dict[str, Any] | None,
    *,
    strip_jurisdiction_first: bool = True,
) -> str:
    """Merge jurisdiction into base query. Strips jurisdiction phrases from base first, then recombines.

    E.g. 'how do I file an appeal for Sunshine Health' + Sunshine Health -> strip -> 'how do I file an appeal' -> recombine -> 'how do I file an appeal for Sunshine Health'
    """
    base = (base_query or "").strip()
    if not base:
        return base

    # Strip embedded jurisdiction from base so we recombine cleanly
    if strip_jurisdiction_first:
        base = strip_jurisdiction_from_intent(base)

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
    *,
    last_turn: dict[str, Any] | None = None,
) -> str:
    """Compute the new refined_query value.

    - slot_fill: merge jurisdiction from state into last_refined_query
    - jurisdiction_change: strip jurisdiction from last_refined_query, merge with new jurisdiction from state
    - follow-up: when is_followup_continuation, use last_refined_query + jurisdiction (expand "it"/"their")
    - new_question: use plan_subquestion_text (or user_text if plan not yet available)
    """
    if classification == "slot_fill" and last_refined_query:
        j = get_jurisdiction_from_active((merged_state or {}).get("active"))
        return build_refined_query(last_refined_query, j)

    if classification == "jurisdiction_change" and last_refined_query:
        j = get_jurisdiction_from_active((merged_state or {}).get("active"))
        return build_refined_query(last_refined_query, j)

    # Follow-up that references prior turn: use last_refined_query + jurisdiction
    if (
        last_refined_query
        and last_turn
        and is_followup_continuation(user_text, last_turn, last_refined_query)
    ):
        j = get_jurisdiction_from_active((merged_state or {}).get("active"))
        return build_refined_query(last_refined_query, j)

    if plan_subquestion_text and (plan_subquestion_text or "").strip():
        return (plan_subquestion_text or "").strip()

    return (user_text or "").strip()
