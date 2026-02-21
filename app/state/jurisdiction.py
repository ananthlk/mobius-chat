"""Jurisdiction model: state, payor, program, perspective, regulatory_agency.
Resolves from state.active (legacy flat fields + jurisdiction_obj)."""
from dataclasses import dataclass
from typing import Any

from app.storage.threads import DEFAULT_JURISDICTION


@dataclass
class Jurisdiction:
    """Single source of truth for jurisdiction. Replaces dual legacy + jurisdiction_obj representation."""

    state: str | None = None
    payor: str | None = None
    program: str | None = None
    perspective: str | None = None
    regulatory_agency: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "payor": self.payor,
            "program": self.program,
            "perspective": self.perspective,
            "regulatory_agency": self.regulatory_agency,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Jurisdiction":
        d = d or {}
        return cls(
            state=(d.get("state") or "").strip() or None,
            payor=(d.get("payor") or "").strip() or None,
            program=(d.get("program") or "").strip() or None,
            perspective=(d.get("perspective") or "").strip() or None,
            regulatory_agency=(d.get("regulatory_agency") or "").strip() or None,
        )


def rag_filters_from_active(active: dict[str, Any] | None) -> dict[str, str]:
    """Build RAG filter overrides from jurisdiction (state.active). Returns {filter_payer, filter_state, filter_program}."""
    j = get_jurisdiction_obj(active)
    out: dict[str, str] = {}
    payor = (j.payor or "").strip()
    if payor:
        try:
            from app.payer_normalization import normalize_payer_for_rag
            canonical = normalize_payer_for_rag(payor)
            if canonical:
                out["filter_payer"] = canonical
        except Exception:
            out["filter_payer"] = payor
    state = (j.state or "").strip()
    if state:
        out["filter_state"] = state
    program = (j.program or "").strip()
    if program:
        out["filter_program"] = program
    return out


def get_jurisdiction_from_active(active: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve jurisdiction from active. Merges jurisdiction_obj with legacy flat fields.
    Returns {state, payor, program, perspective, regulatory_agency} (dict for backward compat)."""
    j = get_jurisdiction_obj(active)
    return j.to_dict()


def get_jurisdiction_obj(active: dict[str, Any] | None) -> Jurisdiction:
    """Resolve Jurisdiction from active. Single source of truth."""
    active = active if isinstance(active, dict) else {}
    base = dict(DEFAULT_JURISDICTION)
    obj = active.get("jurisdiction_obj")
    if isinstance(obj, dict):
        for k in base:
            if k in obj and obj[k] is not None:
                base[k] = obj[k]
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
    return Jurisdiction.from_dict(base)


def jurisdiction_to_summary(j: dict[str, Any] | Jurisdiction | None) -> str:
    """Format jurisdiction for display/caveat (e.g. 'For Sunshine Health in Florida (Medicaid)')."""
    if j is None:
        return ""
    if isinstance(j, Jurisdiction):
        j = j.to_dict()
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
