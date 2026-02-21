"""Options for clarification slots. J tag lexicon as source of truth (policy_lexicon_entries)."""
from __future__ import annotations

from typing import Any

# Slot -> j_tag dimension (prefix in policy_lexicon_entries kind='j')
_SLOT_TO_DIMENSION: dict[str, str] = {
    "jurisdiction.payor": "payor",
    "jurisdiction.state": "state",
    "jurisdiction.program": "program",
    "jurisdiction.perspective": "perspective",
    "jurisdiction.regulatory_agency": "regulatory_authority",
}

_SLOT_LABELS: dict[str, str] = {
    "jurisdiction.payor": "Which health plan?",
    "jurisdiction.state": "Which state?",
    "jurisdiction.program": "Medicare or Medicaid?",
    "jurisdiction.perspective": "As a provider or patient?",
    "jurisdiction.regulatory_agency": "Which regulatory authority?",
}


def _get_rag_url() -> str:
    """RAG DB URL for lexicon (policy_lexicon_entries). Empty if not configured."""
    try:
        from app.chat_config import get_chat_config

        return (get_chat_config().rag.database_url or "").strip()
    except Exception:
        return ""


def get_options_for_slot(slot: str) -> list[dict[str, str]]:
    """Return options from lexicon j_tags. Choices use j_tag code as value, description as label."""
    rag_url = _get_rag_url()
    if not rag_url:
        return []

    try:
        from mobius_retriever.jpd_tagger import list_j_tag_options

        dimension = _SLOT_TO_DIMENSION.get(slot)
        raw = list_j_tag_options(rag_url, dimension=dimension)
        return [{"value": r["code"], "label": r["label"]} for r in raw]
    except Exception:
        return []


def build_clarification_options(missing_slots: list[str]) -> list[dict[str, Any]]:
    """Build option sets for each missing slot from lexicon. Returns list of {slot, label, selection_mode, choices}."""
    out: list[dict[str, Any]] = []
    for slot in missing_slots:
        choices = get_options_for_slot(slot)
        if not choices:
            continue
        out.append({
            "slot": slot,
            "label": _SLOT_LABELS.get(slot, slot.replace("jurisdiction.", "")),
            "selection_mode": "single",
            "choices": choices,
        })
    return out
