"""Jurisdiction model: state, payor, program, perspective, regulatory_agency.
Resolves from state.active (legacy flat fields + jurisdiction_obj)."""
from typing import Any

from app.storage.threads import DEFAULT_JURISDICTION


def rag_filters_from_active(active: dict[str, Any] | None) -> dict[str, str]:
    """Build RAG filter overrides from jurisdiction (state.active). Returns {filter_payer, filter_state, filter_program}.
    Payer is normalized via payer_normalization. Use these to override get_chat_config().rag defaults when state has jurisdiction."""
    j = get_jurisdiction_from_active(active)
    out: dict[str, str] = {}
    payor = (j.get("payor") or "").strip()
    if payor:
        try:
            from app.payer_normalization import normalize_payer_for_rag
            canonical = normalize_payer_for_rag(payor)
            if canonical:
                out["filter_payer"] = canonical
        except Exception:
            out["filter_payer"] = payor
    state = (j.get("state") or "").strip()
    if state:
        out["filter_state"] = state
    program = (j.get("program") or "").strip()
    if program:
        out["filter_program"] = program
    return out


def get_jurisdiction_from_active(active: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve jurisdiction dict from active. Merges jurisdiction_obj with legacy flat fields.
    Returns {state, payor, program, perspective, regulatory_agency}."""
    active = active if isinstance(active, dict) else {}
    base = dict(DEFAULT_JURISDICTION)
    obj = active.get("jurisdiction_obj")
    if isinstance(obj, dict):
        for k in base:
            if k in obj and obj[k] is not None:
                base[k] = obj[k]
    # Legacy: flat fields override when jurisdiction_obj missing or empty
    payer = (active.get("payer") or "").strip()
    if payer:
        base["payor"] = payer
    payers = active.get("payers")
    if payers and isinstance(payers, list) and len(payers) > 1:
        base["payor"] = ", ".join(str(p).strip() for p in payers if p) or base["payor"]
    program = (active.get("program") or "").strip()
    if program:
        base["program"] = program
    j = active.get("jurisdiction")
    if isinstance(j, str) and j.strip():
        base["state"] = j.strip()
    role = (active.get("user_role") or "").strip()
    if role in ("provider_office", "patient"):
        base["perspective"] = role
    return base


def jurisdiction_to_summary(j: dict[str, Any] | None) -> str:
    """Format jurisdiction for display/caveat (e.g. 'For Sunshine Health in Florida (Medicaid)')."""
    if not j:
        return ""
    parts = []
    payor = (j.get("payor") or "").strip()
    if payor:
        parts.append(payor)
    state = (j.get("state") or "").strip()
    if state:
        parts.append(f"in {state}")
    program = (j.get("program") or "").strip()
    if program:
        parts.append(f"({program})")
    if not parts:
        return ""
    return " ".join(parts)


def build_jurisdiction_patch(
    *,
    state: str | None = None,
    payor: str | None = None,
    program: str | None = None,
    perspective: str | None = None,
    regulatory_agency: str | None = None,
) -> dict[str, Any]:
    """Build jurisdiction_obj for state patch."""
    out: dict[str, Any] = {}
    if state is not None:
        out["state"] = state.strip() if state else None
    if payor is not None:
        out["payor"] = payor.strip() if payor else None
    if program is not None:
        out["program"] = program.strip() if program else None
    if perspective is not None:
        out["perspective"] = perspective.strip() if perspective else None
    if regulatory_agency is not None:
        out["regulatory_agency"] = regulatory_agency.strip() if regulatory_agency else None
    return {"jurisdiction_obj": out} if out else {}
