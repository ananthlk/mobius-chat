"""Jurisdiction clarification: decide when to ask user to clarify before answering."""
from typing import Any

from app.state.jurisdiction import get_jurisdiction_from_active, jurisdiction_to_summary


# Slots we can ask user to fill
JURISDICTION_SLOTS = ["jurisdiction.payor", "jurisdiction.state", "jurisdiction.program", "jurisdiction.perspective"]


def need_jurisdiction_clarification(
    plan_subquestions: list[Any],
    active: dict[str, Any] | None,
) -> tuple[bool, list[str], str | None]:
    """Check if we should ask for jurisdiction clarification before answering.

    Returns (needs_clarification, missing_slots, clarification_message).
    When needs_clarification is True, the worker should return the clarification_message
    and register open_slots with missing_slots.
    """
    if not plan_subquestions:
        return (False, [], None)

    # Only non_patient subquestions need jurisdiction; if all patient, no clarification
    non_patient = [sq for sq in plan_subquestions if getattr(sq, "kind", None) == "non_patient"]
    if not non_patient:
        return (False, [], None)

    j = get_jurisdiction_from_active(active)
    missing: list[str] = []
    payor = (j.get("payor") or "").strip()
    state = (j.get("state") or "").strip()
    program = (j.get("program") or "").strip()

    # Policy: ask when we have non_patient questions but no jurisdiction at all.
    # At least one of payor, state, or program should be present for RAG to scope well.
    if payor or state or program:
        return (False, [], None)
    missing.append("jurisdiction.payor")

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
