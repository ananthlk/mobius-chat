"""
Shared provider summary helpers.

Used by:
  - main.py::roster_provider_summary_proxy  (on-demand generation via HTTP POST)
  - roster_credentialing_orchestrator.py    (post-run batch generation)

Provides:
  build_detailed_prompt(detail)  → full prompt for the 3-section detailed summary
  build_oneliner_prompt(detail)  → short prompt for one_liner + brief
  build_chat_profile(detail)     → deterministic structured JSON (no LLM needed)
  parse_oneliner(summary_text)   → extract first sentence from ## Credential Status
"""
from __future__ import annotations

import re
from typing import Any


# ── System instructions ──────────────────────────────────────────────────────

SUMMARY_SYSTEM = (
    "FL Medicaid credentialing compliance rules:\n"
    "- FL NPI Initiative (Dec 2025): PML ZIP must match a confirmed service location. "
    "FLMMIS uses ZIP+4 → ZIP5 → Address Line 1 to route claims.\n"
    "- DENIAL-1120: Address Line 1 mismatch when multiple locations share ZIP5 → claim DENIAL.\n"
    "- PAY-1980: Multiple active enrollments without distinct ZIP+4 → defaults to oldest contract.\n"
    "- WARN-TAX-001: PML taxonomy not in NPPES → federal registry out of sync.\n"
    "- Billing restriction: taxonomy not TML-approved or not PML-enrolled → claims deny.\n\n"
    "Write a credentialing summary in three markdown sections:\n"
    "## Credential Status — one paragraph: NPPES status, PML enrollment, billability.\n"
    "## Key Risks — bullet list with specific edit codes, taxonomy codes, Medicaid IDs.\n"
    "## Recommended Actions — numbered steps, most urgent first.\n"
    "Limit: 300 words total. Be specific. Do not add preamble or acknowledgment."
)

ONELINER_SYSTEM = (
    "FL Medicaid credentialing compliance expert. Given a provider profile, produce exactly two lines:\n"
    "LINE1: one sentence (≤25 words) naming the single most critical credentialing issue "
    "or confirming they are fully credentialed. Include the billability status.\n"
    "LINE2: 2-3 sentences (≤60 words total) covering NPPES status, PML enrollment, "
    "and the most important taxonomy finding.\n"
    "Output ONLY the two lines with no headings, bullets, or preamble."
)


# ── Profile text builder (shared between detailed + one-liner prompts) ───────

def _safe_tax_codes(tax_profile: Any) -> list:
    if not tax_profile:
        return []
    if isinstance(tax_profile, dict):
        return tax_profile.get("codes") or []
    if isinstance(tax_profile, list):
        return tax_profile
    return []


def _build_profile_text(detail: dict) -> str:
    """Build a structured plain-text provider profile for LLM input."""
    snap         = detail.get("nppes_snapshot") or {}
    open_tasks_l = detail.get("open_tasks")     or []
    pml_rows     = detail.get("pml_rows")        or []
    tax_profile  = detail.get("taxonomy_profile")
    tax_codes    = _safe_tax_codes(tax_profile)
    tax_obj      = tax_profile if isinstance(tax_profile, dict) else {}
    drift_flags  = detail.get("drift_flags") or []

    lines = [
        f"Provider: {detail.get('provider_name', 'Unknown')}",
        f"NPI: {detail.get('npi') or detail.get('npi_validated') or detail.get('npi_roster') or 'N/A'}",
        f"NPPES Status: {snap.get('nppes_status') or 'Unknown'}",
        f"Billability: {detail.get('billability_status') or 'unknown'} "
        f"(score {detail.get('billability_score') or 0})",
        f"City/State: {detail.get('city','')}, {detail.get('state_cd','')}",
        "",
    ]

    if tax_codes:
        lines.append("Taxonomies:")
        for tc in tax_codes[:8]:
            code   = tc.get("code","") if isinstance(tc, dict) else str(tc)
            status = tc.get("status","") if isinstance(tc, dict) else ""
            lines.append(f"  - {code}  [{status or 'unknown'}]")
    if tax_obj.get("delta_billing_pct"):
        lines.append(f"Billing at risk from taxonomy gap: {tax_obj['delta_billing_pct']:.1f}%")
    if tax_obj.get("delta_hcpcs"):
        lines.append(f"At-risk HCPC codes: {', '.join(str(c) for c in tax_obj['delta_hcpcs'][:6])}")

    lines.append("")
    if pml_rows:
        lines.append("FL Medicaid PML Enrollment:")
        for r in pml_rows[:5]:
            mid    = r.get("medicaid_id") or "—"
            zip9   = r.get("zip9") or "—"
            status = r.get("enrollment_status") or "—"
            issues = r.get("issues") or []
            warns  = r.get("warnings") or []
            ecs    = [ec if isinstance(ec, str) else ec.get("code","") for ec in (r.get("edit_codes") or [])]
            parts  = [f"Medicaid ID: {mid}", f"ZIP+9: {zip9}", f"Status: {status}"]
            if issues: parts.append(f"Issues: {', '.join(issues[:3])}")
            if warns:  parts.append(f"Warnings: {len(warns)} (e.g. {warns[0][:60]})")
            if ecs:    parts.append(f"Edit codes: {', '.join(ecs)}")
            lines.append(f"  - {r.get('taxonomy_code','?')}: {' | '.join(parts)}")
    else:
        lines.append("FL Medicaid PML Enrollment: Not found or not run yet")

    lines.append("")
    if open_tasks_l:
        lines.append(f"Open Tasks ({len(open_tasks_l)}):")
        for t in open_tasks_l[:6]:
            sev    = t.get("severity","")
            dim    = t.get("dim","")
            reason = t.get("reason","")
            lines.append(f"  [{sev.upper() or 'OPEN'}] {dim}: {reason[:100]}")
    else:
        lines.append("Open Tasks: None")

    if drift_flags:
        lines.append(f"NPPES Alignment Flags: {', '.join(str(f) for f in drift_flags[:5])}")

    vh = detail.get("version_history") or []
    if vh:
        latest = vh[0] if isinstance(vh[0], dict) else {}
        lines.append(f"\nLatest NPPES record date: {latest.get('last_updated','unknown')}")

    return "\n".join(lines)


# ── Public prompt builders ────────────────────────────────────────────────────

def build_detailed_prompt(detail: dict) -> str:
    """Full prompt for the 3-section detailed summary (max_tokens=8192)."""
    profile_text = _build_profile_text(detail)
    return (
        f"{SUMMARY_SYSTEM}\n\n"
        f"Provider profile to analyse:\n\n{profile_text}\n\n"
        "---\n"
        "## Credential Status\n"
    )


def build_oneliner_prompt(detail: dict) -> str:
    """Short prompt for one_liner + brief (max_tokens=256)."""
    profile_text = _build_profile_text(detail)
    return (
        f"{ONELINER_SYSTEM}\n\n"
        f"Provider profile:\n\n{profile_text}\n\n"
    )


def build_chat_profile(detail: dict, run_id: str | None = None) -> dict:
    """Build a compact structured JSON profile for chat tool use — no LLM needed."""
    snap        = detail.get("nppes_snapshot") or {}
    tax_profile = detail.get("taxonomy_profile")
    tax_codes   = _safe_tax_codes(tax_profile)
    pml_rows    = detail.get("pml_rows") or []
    open_tasks  = detail.get("open_tasks") or []

    # Derive key risks deterministically
    key_risks: list[str] = []
    billability = (detail.get("billability_status") or "unknown").lower()
    nppes_st    = (snap.get("nppes_status") or "").upper()
    if nppes_st == "D":
        key_risks.append("NPPES deactivated")
    if billability in ("inactive", "blocked"):
        key_risks.append(f"Billing {billability}")
    for t in open_tasks[:5]:
        if t.get("severity") == "critical":
            key_risks.append(f"{t.get('dim','issue')}: {(t.get('reason') or '')[:60]}")
    for r in pml_rows[:3]:
        if r.get("issues"):
            key_risks.append(f"PML issue ({r.get('taxonomy_code','?')}): {r['issues'][0][:60]}")
    if not key_risks and billability == "billable":
        key_risks.append("No critical issues — fully credentialed")

    return {
        "npi":             detail.get("npi") or detail.get("npi_validated") or detail.get("npi_roster") or "",
        "name":            detail.get("provider_name") or "",
        "org":             detail.get("org_name") or "",
        "billability":     billability,
        "nppes_status":    nppes_st or "unknown",
        "specialty":       detail.get("specialty") or snap.get("specialty") or "",
        "taxonomies": [
            {
                "code":  tc.get("code","") if isinstance(tc, dict) else str(tc),
                "desc":  tc.get("desc","") if isinstance(tc, dict) else "",
                "tml":   tc.get("tml_approved", tc.get("status") not in ("not_tml",)) if isinstance(tc, dict) else None,
                "pml":   tc.get("pml_enrolled", False) if isinstance(tc, dict) else None,
                "status": tc.get("status","") if isinstance(tc, dict) else "",
            }
            for tc in tax_codes[:10]
        ],
        "pml_medicaid_ids": list({r.get("medicaid_id","") for r in pml_rows if r.get("medicaid_id")}),
        "pml_zip9s":        list({r.get("zip9","") for r in pml_rows if r.get("zip9")}),
        "open_tasks": [
            {"dim": t.get("dim",""), "severity": t.get("severity",""), "reason": (t.get("reason") or "")[:120]}
            for t in open_tasks[:10]
        ],
        "key_risks":     key_risks,
        "city":          detail.get("city") or "",
        "state":         detail.get("state_cd") or "",
        "last_run_id":   run_id or detail.get("run_id") or "",
    }


# ── Post-processing helpers ───────────────────────────────────────────────────

def parse_oneliner(summary_text: str) -> str:
    """Extract the first sentence from the ## Credential Status section."""
    cs_match = re.search(
        r"##\s*Credential Status\s*\n+(.+?)(?:\n\n|\n##|$)", summary_text, re.S
    )
    if cs_match:
        block      = cs_match.group(1).strip()
        first_sent = re.split(r"\.\s+|\.\n", block.replace("**", ""))[0].strip()
        if first_sent and not first_sent.endswith("."):
            first_sent += "."
        if first_sent:
            return first_sent[:200]
    # Fallback: first non-empty non-heading line
    for line in summary_text.splitlines():
        line = line.strip().lstrip("#").strip().replace("**", "")
        if line:
            return line[:200]
    return ""


def parse_brief_and_oneliner(raw: str) -> tuple[str, str]:
    """Parse raw one-liner LLM response into (one_liner, brief).

    The one-liner prompt asks for LINE1 then LINE2 with no headings.
    Returns (first_line, remaining_lines_joined).
    """
    lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
    if not lines:
        return ("", "")
    one_liner = lines[0]
    brief     = " ".join(lines[1:]) if len(lines) > 1 else one_liner
    return (one_liner[:200], brief[:400])


def is_clean_provider(detail: dict) -> bool:
    """Return True for providers that need no LLM summary — fully credentialed, no tasks."""
    billability = (detail.get("billability_status") or "unknown").lower()
    nppes_st    = ((detail.get("nppes_snapshot") or {}).get("nppes_status") or "").upper()
    open_tasks  = detail.get("open_tasks") or []
    return (
        billability == "billable"
        and nppes_st == "A"
        and len(open_tasks) == 0
    )


CLEAN_SUMMARY_TEMPLATE = (
    "{name} is fully credentialed — NPPES active, enrolled in FL Medicaid, no open tasks."
)
