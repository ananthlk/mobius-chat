"""Jurisdiction clarification: decide when to ask user to clarify before answering.

Uses JPD tag matcher on question text as primary source of truth (same as RAG retrieval).
If the tagger returns j_tags (state, payor, program, regulatory_authority, etc.), we have
sufficient scope and do not ask. Falls back to parsed state when tagger returns nothing.
"""
from typing import Any

from app.state.jurisdiction import get_jurisdiction_from_active


# Slots we can ask user to fill
JURISDICTION_SLOTS = ["jurisdiction.payor", "jurisdiction.state", "jurisdiction.program", "jurisdiction.perspective"]


def _question_has_j_tags(question_text: str, rag_url: str) -> bool:
    """Run JPD tag matcher on question; True if any j_tags matched (source of truth for scope)."""
    if not (question_text or "").strip() or not (rag_url or "").strip():
        return False
    try:
        from mobius_retriever.jpd_tagger import extract_tags_from_text

        result = extract_tags_from_text(question_text, rag_url, kinds=("j",))
        j_tags = result.get("j_tags") or {}
        return bool(j_tags)
    except Exception:
        return False


def need_jurisdiction_clarification(
    plan_subquestions: list[Any],
    active: dict[str, Any] | None,
    question_text: str = "",
    rag_url: str = "",
) -> tuple[bool, list[str], str | None]:
    """Check if we should ask for jurisdiction clarification before answering.

    Primary: JPD tag matcher on question_text. If it returns j_tags (state.*, payor.*,
    program.*, regulatory_authority.*, etc.), we have sufficient scope — no clarification.
    Fallback: parsed state (payor, state, program) when tagger returns nothing.

    Returns (needs_clarification, missing_slots, clarification_message).
    """
    if not plan_subquestions:
        return (False, [], None)

    # Only non_patient subquestions need jurisdiction; if all patient, no clarification
    non_patient = [sq for sq in plan_subquestions if getattr(sq, "kind", None) == "non_patient"]
    if not non_patient:
        return (False, [], None)

    # If planner emits requires_jurisdiction=False for a subquestion, exclude it (e.g. meta questions like "can you search google")
    non_patient_needing_j = [sq for sq in non_patient if getattr(sq, "requires_jurisdiction", None) is not False]
    if not non_patient_needing_j:
        return (False, [], None)

    # Primary: tag matcher on question — if j_tags found, we have scope (same as RAG)
    if _question_has_j_tags(question_text or "", rag_url or ""):
        return (False, [], None)

    # Fallback: parsed state (payor, state, program, regulatory_agency)
    j = get_jurisdiction_from_active(active)
    payor = (j.get("payor") or "").strip()
    state = (j.get("state") or "").strip()
    program = (j.get("program") or "").strip()
    regulatory_agency = (j.get("regulatory_agency") or "").strip()

    if payor or state or program or regulatory_agency:
        return (False, [], None)

    missing: list[str] = ["jurisdiction.payor"]

    # Build a friendly clarification message
    if "jurisdiction.payor" in missing and len(missing) == 1:
        msg = "Which health plan or payer are you asking about? (e.g., Sunshine Health, United Healthcare)"
    elif "jurisdiction.payor" in missing:
        parts = []
        if "jurisdiction.payor" in missing:
            parts.append("which health plan or payer")
        if "jurisdiction.state" in missing:
            parts.append("which state")
        if "jurisdiction.program" in missing:
            parts.append("Medicare or Medicaid")
        msg = f"To give you an accurate answer, could you please specify {', '.join(parts)}?"
    else:
        msg = "To scope this correctly, could you specify the payer, state, or program you're asking about?"

    return (True, missing, msg)
