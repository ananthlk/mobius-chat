"""Credentialing envelope: canonical message + routing helpers (shared with ReAct)."""

from __future__ import annotations

from typing import Any, Literal

OrgUploadClass = Literal["matched", "ambiguous", "no_files"]


def resolve_step3_roster_merge_context(
    active: dict[str, Any] | None,
    credentialing_options: dict[str, Any] | None,
) -> tuple[str | None, bool, bool]:
    """Step 3 roster merge flags from thread state + envelope.

    Returns:
        (roster_upload_id, external_only, include_roster_members).
        ``external_only`` is True when the user chose outside-in (``prefer_outside_in``).
        ``include_roster_members`` is False when external-only, or when no upload_id is available.
    """
    co = dict(credentialing_options or {})
    external_only = bool(co.get("prefer_outside_in"))
    ac = dict(active or {})
    rec_uid = (ac.get("reconciliation_upload_id") or "").strip() or None
    upload_id = rec_uid
    roster_files = roster_uploads_from_active(ac)
    if not upload_id and roster_files:
        latest = roster_files[0]
        upload_id = (latest.get("upload_id") or "").strip() or None
    include = (not external_only) and bool(upload_id)
    return upload_id, external_only, include


def roster_uploads_from_active(active: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for u in active.get("uploaded_files") or []:
        if isinstance(u, dict) and (u.get("purpose") or "").strip() == "roster_reconciliation":
            out.append(u)
    return out


def thread_has_roster_reconciliation_data(active: dict[str, Any]) -> bool:
    """True if this thread can run roster reconciliation (upload id + billing NPI on file)."""
    uid = (active.get("reconciliation_upload_id") or "").strip()
    oid = (active.get("reconciliation_org_id") or "").strip()
    if uid and oid:
        return True
    for u in roster_uploads_from_active(active):
        if (u.get("upload_id") or "").strip() and (u.get("org_id") or "").strip():
            return True
    return False


def message_prefers_outside_in_credentialing(message: str) -> bool:
    """Explicit ask for outside-in / no roster comparison (natural language)."""
    lower = (message or "").lower()
    needles = (
        "outside-in",
        "outside in",
        "ignore uploaded roster",
        "without uploaded roster",
        "without my roster",
        "no roster upload",
        "medicaid npi report only",
        "outside-in medicaid",
        "credentialing without reconciliation",
    )
    return any(n in lower for n in needles)


def envelope_routes_to_reconciliation(
    merged_state: dict[str, Any],
    credentialing_options: dict[str, Any],
    message: str,
) -> bool:
    """
    When the user confirmed the credentialing envelope, choose reconciliation vs outside-in credentialing:
    roster on thread → reconciliation unless prefer_outside_in, explicit credentialing, or message asks outside-in.
    """
    co = credentialing_options or {}
    if not co:
        return False
    rk = (co.get("report_kind") or "auto").strip().lower()
    if rk == "reconciliation":
        return True
    if rk == "credentialing":
        return False
    if co.get("prefer_outside_in") is True:
        return False
    if message_prefers_outside_in_credentialing(message):
        return False
    active = (merged_state or {}).get("active") or {}
    return thread_has_roster_reconciliation_data(active)


def classify_org_vs_uploads(org_hint: str, active: dict[str, Any]) -> OrgUploadClass:
    """Heuristic for UI copy: whether org matches any roster row."""
    hint = (org_hint or "").strip().lower()
    if not hint:
        return "ambiguous"
    files = roster_uploads_from_active(active or {})
    if not files and not (active.get("reconciliation_upload_id") or "").strip():
        return "no_files"
    matches = 0
    for u in files:
        on = (u.get("org_name") or "").strip().lower()
        if on and (hint in on or on in hint):
            matches += 1
    ron = (active.get("reconciliation_org_name") or "").strip().lower()
    if ron and (hint in ron or ron in hint):
        matches += 1
    if matches >= 1:
        return "matched"
    return "ambiguous" if files else "no_files"


def build_canonical_credentialing_message(
    raw_message: str,
    merged_state: dict[str, Any],
    credentialing_options: dict[str, Any],
) -> str:
    """Parser-friendly message for tool triggers, ReAct, and thread refined_query."""
    co = credentialing_options or {}
    org = (co.get("org_name") or "").strip()
    if not org:
        return (raw_message or "").strip() or ""
    if envelope_routes_to_reconciliation(merged_state or {}, co, raw_message or ""):
        return f"Run roster reconciliation report for {org}."
    return f"Create a credentialing report for {org}."
