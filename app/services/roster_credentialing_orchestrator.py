"""Orchestrator for Provider Roster / Credentialing (Medicaid NPI) report flow.

Runs a fixed plan: (1) Identify organization, (2) Find practice locations, (3) Build report.
After each step the plan is "checked off" and progress is emitted so chat can show it.
Can be invoked standalone or from the tool agent when the user asks for a Medicaid NPI
or credentialing report.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.client import IncompleteRead, RemoteDisconnected
from typing import Callable

logger = logging.getLogger(__name__)

# Plan steps: id, label (emitted to user). Execution order 1–11.
ROSTER_CREDENTIALING_PLAN = [
    {"id": "ensure_benchmarks", "label": "Ensure revenue metrics (utilization benchmarking)"},
    {"id": "identify_org", "label": "Identify organization"},
    {"id": "find_locations", "label": "Find practice locations"},
    {"id": "find_associated_providers", "label": "Find associated facilities and providers"},
    {"id": "org_benchmark", "label": "Org benchmark (utilization for active roster)"},
    {"id": "find_services_by_location", "label": "Find services and capabilities by location"},
    {"id": "historic_billing_patterns", "label": "Historic billing patterns (DOGE, HCPCS breakdown)"},
    {"id": "step_6", "label": "PML validation (NPI, taxonomy, ZIP, Medicaid ID)"},
    {"id": "step_7", "label": "Missing PML enrollment"},
    {"id": "opportunity_sizing", "label": "Opportunity sizing (revenue waterfall A–E)"},
    {"id": "build_report", "label": "Build credentialing report"},
]


@dataclass
class StepState:
    """State for a single step."""

    id: str
    label: str
    status: str = "pending"  # pending | in_progress | done | skipped
    result_summary: str = ""


@dataclass
class StepOutput:
    """CSV-style output for a step so users can validate."""

    step_id: str
    label: str
    csv_content: str
    row_count: int


@dataclass
class OrchestratorState:
    """State of the orchestrator run (plan check-off)."""

    steps: list[StepState]
    org_npis: list[str]
    org_name: str = ""
    locations_count: int = 0
    locations: list = field(default_factory=list)
    associated_providers: dict = field(default_factory=dict)
    active_roster: dict = field(default_factory=dict)
    org_benchmark: dict = field(default_factory=dict)
    pml_validated: list = field(default_factory=list)   # From Step 6
    pml_flagged: list = field(default_factory=list)     # From Step 6
    missing_enrollment: list = field(default_factory=list)  # From Step 7
    step_outputs: list[StepOutput] = field(default_factory=list)
    report_final_md: str = ""
    report_pdf_base64: str = ""

    def step_by_id(self, step_id: str) -> StepState | None:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def mark_in_progress(self, step_id: str) -> None:
        s = self.step_by_id(step_id)
        if s:
            s.status = "in_progress"

    def mark_done(self, step_id: str, result_summary: str = "") -> None:
        s = self.step_by_id(step_id)
        if s:
            s.status = "done"
            s.result_summary = result_summary

    def mark_skipped(self, step_id: str, reason: str = "") -> None:
        s = self.step_by_id(step_id)
        if s:
            s.status = "skipped"
            s.result_summary = reason


def _emit(emitter: Callable[[str], None] | None, msg: str) -> None:
    if emitter and msg and str(msg).strip():
        try:
            emitter(str(msg).strip())
        except Exception:
            pass


def _provider_roster_base_url() -> str:
    """Base URL for provider-roster-credentialing API (e.g. http://localhost:8010)."""
    url = (os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL") or "").strip()
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _parse_npis_from_org_search_result(text: str) -> list[str]:
    """Extract NPI numbers from search_org_names result text (e.g. 'NPI: 1234567890')."""
    if not text:
        return []
    pattern = re.compile(r"NPI:\s*(\d{10})\b")
    return list(dict.fromkeys(pattern.findall(text)))


def _to_csv(rows: list[dict], columns: list[str]) -> str:
    """Turn list of dicts into CSV string."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: (r.get(k) if isinstance(r, dict) else "") for k in columns})
    return buf.getvalue().strip()


def _run_step_0_ensure_benchmarks(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> None:
    """Step 1: Ensure taxonomy_utilization_benchmarks table is populated (utilization benchmarking)."""
    step_id = "ensure_benchmarks"
    state.mark_in_progress(step_id)
    _emit(emitter, "I am ensuring the revenue metrics are in place…")
    base = _provider_roster_base_url()
    if not base:
        state.mark_skipped(step_id, "Provider-roster API not configured.")
        _emit(emitter, "✓ Step 1 skipped. API not configured.")
        return
    url = f"{base}/ensure-benchmarks"
    payload = json.dumps({"period": "2024", "state": "FL"}).encode("utf-8")
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        status = data.get("status", "")
        if status == "ok":
            state.mark_done(step_id, "Benchmarks table populated.")
            _emit(emitter, "✓ Step 1 done. Revenue metrics in place. Proceeding with the chain.")
        else:
            state.mark_done(step_id, data.get("error", status))
            _emit(emitter, f"✓ Step 1 done. ({data.get('error', status)})")
    except Exception as e:
        logger.warning("ensure_benchmarks failed: %s", e)
        state.mark_done(step_id, str(e))
        _emit(emitter, f"✓ Step 1 done. ({e})")


def _run_step_1_identify_org(
    org_input: str,
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> str:
    """Step 1: Search org by name via provider-roster API. Returns result text; updates state and org_npis."""
    step_id = "identify_org"
    org_name = (org_input or "").strip()
    state.mark_in_progress(step_id)
    _emit(emitter, f"Identifying organization ({org_name})…")
    base = _provider_roster_base_url()
    if not base:
        state.mark_done(step_id, "Provider-roster API not configured.")
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Organization NPIs", csv_content="(API not configured)", row_count=0)
        )
        _emit(emitter, "✓ Step 1 done. API not configured. Stopping.")
        return "Provider-roster API not configured. Set CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL."
    url = f"{base}/search/org-names"
    payload = json.dumps({"name": org_name, "state": "FL", "limit": 20}).encode("utf-8")
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        results = data.get("results") or []
        npis = list(dict.fromkeys(str(r.get("npi", "")).strip() for r in results if r.get("npi")))
        state.org_npis = npis
        # Step output: rich CSV (npi, name, entity_type, source, taxonomy_code) for validation
        org_cols = ["npi", "name", "entity_type", "source", "taxonomy_code"]
        org_rows = []
        for r in results:
            n = str(r.get("npi", "")).strip()
            if not n:
                continue
            org_rows.append({
                "npi": n,
                "name": (r.get("name") or "").strip()[:80],
                "entity_type": (r.get("entity_type") or "").strip(),
                "source": (r.get("source") or "").strip(),
                "taxonomy_code": (r.get("taxonomy_code") or "").strip() or "",
            })
        csv_content = _to_csv(org_rows, org_cols) if org_rows else "npi,name,entity_type,source,taxonomy_code\n(no matches)"
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Organization NPIs", csv_content=csv_content, row_count=len(org_rows))
        )
        result_text = "\n".join(
            f"  {i}. {r.get('name','')}  |  NPI: {r.get('npi','')}  |  {r.get('entity_type','')}  |  {r.get('source','')}"
            for i, r in enumerate(results[:20], 1)
        )
        if result_text:
            result_text = f"Found {len(results)} match(es):\n{result_text}"
        else:
            result_text = "No matches found."
        if results:
            summary = f"Found {len(npis)} org NPI(s)."
            state.mark_done(step_id, summary)
            _emit(emitter, f"✓ Step 1 done. {summary}")
        else:
            state.mark_done(step_id, "No matches found.")
            _emit(emitter, "✓ Step 1 done. No matches found.")
        return result_text
    except urllib.error.HTTPError as e:
        body = e.fp.read().decode()[:300] if e.fp else str(e)
        logger.warning("search_org_names HTTP %s %s", e.code, body)
        state.mark_done(step_id, f"API error {e.code}")
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Organization NPIs", csv_content=f"(API error {e.code})", row_count=0)
        )
        _emit(emitter, "✓ Step 1 done. API error. Stopping.")
        return f"Org search failed ({e.code}): {body}"
    except Exception as e:
        logger.warning("search_org_names failed: %s", e)
        state.mark_done(step_id, str(e))
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Organization NPIs", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, "✓ Step 1 done. Failed. Stopping.")
        return str(e)


def _run_step_2_find_locations(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> str:
    """Step 2: Find practice locations for org NPIs via provider-roster API."""
    step_id = "find_locations"
    state.mark_in_progress(step_id)
    _emit(emitter, "Finding practice locations…")
    base = _provider_roster_base_url()
    if not base or not state.org_npis:
        reason = "No provider-roster API or no org NPIs from Step 1."
        state.mark_skipped(step_id, reason)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Practice locations", csv_content="(skipped)", row_count=0)
        )
        _emit(emitter, f"✓ Step 2 skipped. {reason}")
        return ""
    url = f"{base}/find-locations"
    payload = json.dumps({"org_npis": state.org_npis[:50], "state": "FL"}).encode("utf-8")
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        locations = data.get("locations") or []
        state.locations_count = len(locations)
        state.locations = locations
        # Map site_source to human-readable why_listed for validation
        _WHY_LISTED = {
            "initial": "User-provided initial site",
            "org_nppes": "Org address (NPPES)",
            "org_pml": "Org address (PML)",
            "servicing_nppes": "Servicing facility (NPPES)",
            "servicing_pml": "Servicing facility (PML)",
        }
        # Step output: locations as CSV with why_listed for validation
        loc_cols = ["location_id", "npi", "site_address", "site_city", "site_state", "site_zip", "why_listed"]
        loc_rows = []
        for loc in locations:
            if isinstance(loc, dict):
                addr = loc.get("site_address_line_1") or loc.get("site_address", "")
                zip5 = loc.get("site_zip5") or loc.get("site_zip", "")
                src = loc.get("site_source", "")
                why = _WHY_LISTED.get(src, src or "Unknown")
                loc_rows.append(
                    {
                        "location_id": loc.get("location_id", ""),
                        "npi": loc.get("npi") or loc.get("org_npi", ""),
                        "site_address": addr,
                        "site_city": loc.get("site_city", ""),
                        "site_state": loc.get("site_state", ""),
                        "site_zip": zip5,
                        "why_listed": why,
                    }
                )
        csv_content = _to_csv(loc_rows, loc_cols) if loc_rows else "location_id,npi,site_address,site_city,site_state,site_zip,why_listed\n(no locations)"
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Practice locations", csv_content=csv_content, row_count=len(locations))
        )
        state.mark_done(step_id, f"Found {len(locations)} location(s).")
        _emit(emitter, f"✓ Step 2 done. Found {len(locations)} location(s).")
        return json.dumps({"locations": locations, "count": len(locations)})
    except urllib.error.HTTPError as e:
        body = e.fp.read().decode()[:300] if e.fp else str(e)
        logger.warning("find_locations HTTP %s %s", e.code, body)
        state.mark_done(step_id, f"API error: {e.code}")
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Practice locations", csv_content=f"(API error {e.code})", row_count=0)
        )
        _emit(emitter, f"✓ Step 2 done. API error ({e.code}). Continuing.")
        return ""
    except Exception as e:
        logger.warning("find_locations failed: %s", e)
        state.mark_done(step_id, str(e))
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Practice locations", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, f"✓ Step 2 done. Failed. Continuing.")
        return ""


def _run_step_3_find_associated_providers(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> str:
    """Step 3: Find associated facilities and providers per location."""
    step_id = "find_associated_providers"
    state.mark_in_progress(step_id)
    _emit(emitter, "Finding associated facilities and providers…")
    base = _provider_roster_base_url()
    if not base or not state.org_npis or not state.locations:
        reason = "No provider-roster API, org NPIs, or locations."
        state.mark_skipped(step_id, reason)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Associated providers", csv_content="(skipped)", row_count=0)
        )
        _emit(emitter, f"✓ Step 3 skipped. {reason}")
        return ""
    url = f"{base}/find-associated-providers"
    payload = json.dumps({
        "org_npis": state.org_npis[:50],
        "locations": state.locations,
        "org_name": state.org_name or "",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        associated = data.get("associated_providers") or {}
        active_roster = data.get("active_roster") or {}
        total = data.get("providers_count") or sum(len(v) for v in associated.values())
        state.associated_providers = associated
        state.active_roster = active_roster if active_roster else associated
        # Step output: associated providers as CSV with match_type, name_status, roster_status
        prov_cols = ["location_id", "npi", "name", "entity_type", "match_type", "name_status", "roster_status"]
        prov_rows = []
        for loc_id, providers in associated.items():
            for p in providers or []:
                name_val = p.get("name", p.get("provider_name", "")) or ""
                name_status = p.get("name_status") if not name_val else ""
                prov_rows.append(
                    {
                        "location_id": loc_id,
                        "npi": p.get("npi", ""),
                        "name": name_val or "",
                        "entity_type": "facility" if p.get("entity_type") == "2" else "individual",
                        "match_type": p.get("match_type", ""),
                        "name_status": name_status,
                        "roster_status": p.get("roster_status", ""),
                    }
                )
        csv_content = _to_csv(prov_rows, prov_cols) if prov_rows else "location_id,npi,name,entity_type,match_type,name_status\n(no providers)"
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Associated providers", csv_content=csv_content, row_count=total)
        )
        state.mark_done(step_id, f"Found {total} provider(s) across {len(associated)} location(s).")
        _emit(emitter, f"✓ Step 3 done. Found {total} provider(s) across {len(associated)} location(s).")
        return json.dumps({"associated_providers": associated, "providers_count": total})
    except urllib.error.HTTPError as e:
        body = e.fp.read().decode()[:300] if e.fp else str(e)
        logger.warning("find_associated_providers HTTP %s %s", e.code, body)
        state.mark_done(step_id, f"API error: {e.code}")
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Associated providers", csv_content=f"(API error {e.code})", row_count=0)
        )
        _emit(emitter, f"✓ Step 3 done. API error ({e.code}). Continuing.")
        return ""
    except Exception as e:
        logger.warning("find_associated_providers failed: %s", e)
        state.mark_done(step_id, str(e))
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Associated providers", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, f"✓ Step 3 done. Failed. Continuing.")
        return ""


def _run_step_4_find_services_by_location(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> None:
    """Step 4: Find services (taxonomies) by location with Medicaid approval."""
    step_id = "find_services_by_location"
    state.mark_in_progress(step_id)
    _emit(emitter, "Finding services and capabilities by location…")
    base = _provider_roster_base_url()
    downstream_providers = state.active_roster or state.associated_providers
    if not base or not state.org_npis or not state.locations or not downstream_providers:
        reason = "No provider-roster API, org NPIs, locations, or associated providers."
        state.mark_skipped(step_id, reason)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Services by location", csv_content="(skipped)", row_count=0)
        )
        _emit(emitter, f"✓ Step 4 skipped. {reason}")
        return
    url = f"{base}/find-services-by-location"
    payload = json.dumps({
        "org_npis": state.org_npis[:50],
        "locations": state.locations,
        "associated_providers": downstream_providers,
        "state": "FL",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode())
        services_by_loc = data.get("services_by_location") or {}
        total = sum(len(v) for v in services_by_loc.values())
        # Step output: location_id, taxonomy_code, taxonomy_description, medicaid_approved, location_address
        svc_cols = ["location_id", "location_address", "taxonomy_code", "taxonomy_description", "medicaid_approved"]
        svc_rows = []
        for loc_id, rows in services_by_loc.items():
            for r in rows or []:
                svc_rows.append({
                    "location_id": loc_id,
                    "location_address": r.get("location_address", "")[:80],
                    "taxonomy_code": r.get("taxonomy_code", ""),
                    "taxonomy_description": (r.get("taxonomy_description", "") or "")[:60],
                    "medicaid_approved": "yes" if r.get("medicaid_approved") else "no",
                })
        csv_content = _to_csv(svc_rows, svc_cols) if svc_rows else "location_id,location_address,taxonomy_code,taxonomy_description,medicaid_approved\n(no services)"
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Services by location", csv_content=csv_content, row_count=total)
        )
        state.mark_done(step_id, f"Found {total} service(s) across {len(services_by_loc)} location(s).")
        _emit(emitter, f"✓ Step 4 done. Found {total} service(s) across {len(services_by_loc)} location(s).")
    except urllib.error.HTTPError as e:
        body = e.fp.read().decode()[:300] if e.fp else str(e)
        logger.warning("find_services_by_location HTTP %s %s", e.code, body)
        state.mark_done(step_id, f"API error: {e.code}")
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Services by location", csv_content=f"(API error {e.code})", row_count=0)
        )
        _emit(emitter, f"✓ Step 4 done. API error ({e.code}). Continuing.")
    except Exception as e:
        logger.warning("find_services_by_location failed: %s", e)
        state.mark_done(step_id, str(e))
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Services by location", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, "✓ Step 4 done. Failed. Continuing.")


def _run_step_5_historic_billing_patterns(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> None:
    """Step 5: Historic billing patterns (DOGE, HCPCS breakdown by facility/professional)."""
    step_id = "historic_billing_patterns"
    state.mark_in_progress(step_id)
    _emit(emitter, "Fetching historic billing patterns…")
    base = _provider_roster_base_url()
    downstream_providers = state.active_roster or state.associated_providers
    if not base or not downstream_providers:
        reason = "No provider-roster API or no associated providers."
        state.mark_skipped(step_id, reason)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Historic billing patterns", csv_content="(skipped)", row_count=0)
        )
        _emit(emitter, f"✓ Step 5 skipped. {reason}")
        return
    url = f"{base}/historic-billing-patterns"
    payload = json.dumps({
        "associated_providers": downstream_providers,
        "period_start": "2024-01",
        "period_end": "2024-12",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        by_code = data.get("by_code") or []
        summary = data.get("summary") or {}
        cols = ["hcpcs_code", "description", "entity_type", "claim_count", "total_paid", "beneficiary_count"]
        rows = []
        for r in by_code:
            rows.append({
                "hcpcs_code": r.get("hcpcs_code", ""),
                "description": (r.get("description", "") or "")[:80],
                "entity_type": r.get("entity_type", ""),
                "claim_count": r.get("claim_count", 0),
                "total_paid": r.get("total_paid", 0),
                "beneficiary_count": r.get("beneficiary_count", 0),
            })
        csv_content = _to_csv(rows, cols) if rows else "hcpcs_code,description,entity_type,claim_count,total_paid,beneficiary_count\n(no billing data)"
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Historic billing patterns", csv_content=csv_content, row_count=len(rows))
        )
        sm = f"{summary.get('total_claims', 0)} claims, ${summary.get('total_paid', 0):,.0f} paid, {len(rows)} codes"
        state.mark_done(step_id, sm)
        _emit(emitter, f"✓ Step 5 done. {sm}")
    except urllib.error.HTTPError as e:
        body = e.fp.read().decode()[:300] if e.fp else str(e)
        logger.warning("historic_billing_patterns HTTP %s %s", e.code, body)
        state.mark_done(step_id, f"API error {e.code}")
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Historic billing patterns", csv_content=f"(API error {e.code})", row_count=0)
        )
        _emit(emitter, f"✓ Step 5 done. API error ({e.code}). Continuing.")
    except Exception as e:
        logger.warning("historic_billing_patterns failed: %s", e)
        state.mark_done(step_id, str(e))
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Historic billing patterns", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, "✓ Step 5 done. Failed. Continuing.")


def _run_step_6_pml_validation(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> None:
    """Step 6: PML validation (NPI, taxonomy, ZIP, Medicaid ID)."""
    step_id = "step_6"
    state.mark_in_progress(step_id)
    _emit(emitter, "Validating PML rows (NPI, taxonomy, ZIP, Medicaid ID)…")
    base = _provider_roster_base_url()
    downstream_providers = state.active_roster or state.associated_providers
    if not base or not state.org_npis or not state.locations or not downstream_providers:
        reason = "No provider-roster API, org NPIs, locations, or associated providers."
        state.mark_skipped(step_id, reason)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="PML validation", csv_content="(skipped)", row_count=0)
        )
        _emit(emitter, f"✓ Step 6 skipped. {reason}")
        return
    url = f"{base}/pml-validation"
    payload = json.dumps({
        "org_npis": state.org_npis[:50],
        "locations": state.locations,
        "associated_providers": downstream_providers,
        "program_state": "FL",
        "product": "medicaid",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode())
        validated = data.get("validated") or []
        flagged = data.get("flagged") or []
        state.pml_validated = validated
        state.pml_flagged = flagged
        summary = data.get("summary") or {}
        cols = ["npi", "provider_name", "taxonomy_code", "zip9", "medicaid_provider_id", "valid", "issues", "recommendation"]
        rows = []
        for r in validated + flagged:
            rows.append({
                "npi": r.get("npi", ""),
                "provider_name": (r.get("provider_name", "") or "")[:60],
                "taxonomy_code": r.get("taxonomy_code", ""),
                "zip9": r.get("zip9", ""),
                "medicaid_provider_id": r.get("medicaid_provider_id", ""),
                "valid": "yes" if r.get("valid") else "no",
                "issues": ";".join(r.get("issues") or []),
                "recommendation": (r.get("recommendation") or "")[:120],
            })
        csv_content = _to_csv(rows, cols) if rows else "npi,provider_name,taxonomy_code,zip9,medicaid_provider_id,valid,issues,recommendation\n(no PML rows)"
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="PML validation", csv_content=csv_content, row_count=len(rows))
        )
        sm = f"{summary.get('valid', 0)} valid, {summary.get('flagged', 0)} flagged"
        state.mark_done(step_id, sm)
        _emit(emitter, f"✓ Step 6 done. {sm}")
    except urllib.error.HTTPError as e:
        body = e.fp.read().decode()[:300] if e.fp else str(e)
        logger.warning("pml_validation HTTP %s %s", e.code, body)
        state.mark_done(step_id, f"API error: {e.code}")
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="PML validation", csv_content=f"(API error {e.code})", row_count=0)
        )
        _emit(emitter, f"✓ Step 6 done. API error ({e.code}). Continuing.")
    except Exception as e:
        logger.warning("pml_validation failed: %s", e)
        state.mark_done(step_id, str(e))
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="PML validation", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, "✓ Step 6 done. Failed. Continuing.")


def _run_step_7_missing_pml(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> None:
    """Step 7: Missing PML enrollment — active roster NPIs not in PML."""
    step_id = "step_7"
    state.mark_in_progress(step_id)
    _emit(emitter, "Finding active roster NPIs not enrolled in PML…")
    base = _provider_roster_base_url()
    downstream = state.active_roster or state.associated_providers
    if not base or not state.locations or not downstream:
        reason = "No API, locations, or associated providers."
        state.mark_skipped(step_id, reason)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Missing PML enrollment", csv_content="(skipped)", row_count=0)
        )
        _emit(emitter, f"✓ Step 7 skipped. {reason}")
        return
    url = f"{base}/missing-pml-enrollment"
    payload = json.dumps({"locations": state.locations, "active_roster": downstream}).encode("utf-8")
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        missing = data.get("missing") or []
        state.missing_enrollment = missing
        sm = data.get("summary") or {}
        total = sm.get("total", len(missing))
        cols = ["npi", "name", "location_id", "site_zip5", "suggested_taxonomy_code", "suggested_taxonomy_description", "tml_approved"]
        rows = [{"npi": r.get("npi"), "name": (r.get("name") or "")[:60], "location_id": r.get("location_id"), "site_zip5": r.get("site_zip5"), "suggested_taxonomy_code": r.get("suggested_taxonomy_code"), "suggested_taxonomy_description": (r.get("suggested_taxonomy_description") or "")[:80], "tml_approved": "yes" if r.get("tml_approved") else "no"} for r in missing]
        csv_content = _to_csv(rows, cols) if rows else "npi,name,location_id,site_zip5,suggested_taxonomy_code,tml_approved\n(no missing)"
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Missing PML enrollment", csv_content=csv_content, row_count=total)
        )
        state.mark_done(step_id, f"{total} provider(s) to enroll.")
        _emit(emitter, f"✓ Step 7 done. {total} provider(s) to enroll with suggested taxonomy + location.")
    except Exception as e:
        logger.warning("missing_pml_enrollment failed: %s", e)
        state.mark_done(step_id, str(e))
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Missing PML enrollment", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, f"✓ Step 7 done. Failed ({e}).")


def _run_step_org_benchmark(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> None:
    """Org benchmark: utilization metrics for active roster NPIs."""
    step_id = "org_benchmark"
    state.mark_in_progress(step_id)
    _emit(emitter, "Computing utilization metrics for this org's active roster…")
    base = _provider_roster_base_url()
    downstream = state.active_roster or state.associated_providers
    if not base or not downstream:
        state.mark_skipped(step_id, "No API or no associated providers.")
        _emit(emitter, "✓ Org benchmark skipped.")
        return
    url = f"{base}/org-benchmark"
    payload = json.dumps({"active_roster": downstream, "period": "2024"}).encode("utf-8")
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        if data and data.get("revenue_per_member") is not None:
            state.org_benchmark = data
            sm = f"${data.get('revenue_per_member', 0):,.0f}/member, {data.get('member_count', 0)} members"
            state.mark_done(step_id, sm)
            _emit(emitter, f"✓ Org benchmark done. {sm}")
        else:
            state.mark_done(step_id, "No DOGE data for active roster.")
            _emit(emitter, "✓ Org benchmark done. (No DOGE data for active roster.)")
    except Exception as e:
        logger.warning("org_benchmark failed: %s", e)
        state.mark_done(step_id, str(e))
        _emit(emitter, f"✓ Org benchmark done. ({e})")


def _run_step_opportunity_sizing(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> None:
    """Step 10: Revenue waterfall & opportunity sizing (A–E)."""
    step_id = "opportunity_sizing"
    state.mark_in_progress(step_id)
    _emit(emitter, "Computing opportunity sizing (revenue waterfall A–E)…")
    base = _provider_roster_base_url()
    if not base:
        state.mark_skipped(step_id, "Provider-roster API not configured.")
        _emit(emitter, "✓ Opportunity sizing skipped.")
        return
    # We need validated, flagged, missing from Step 6 and 7 — stored in state via step_outputs
    # The orchestrator doesn't store validated/flagged/missing explicitly; we get them from the last PML and missing runs.
    # We need to call PML validation and missing enrollment again, or store their results.
    # Actually the orchestrator runs steps sequentially but doesn't persist validated/flagged between steps.
    # We need to either: 1) Store validated, flagged in state, 2) Re-call the APIs.
    # The simplest is to add validated, flagged, missing to OrchestratorState and populate them in step 6 and 7.
    validated = state.pml_validated or []
    flagged = state.pml_flagged or []
    missing = state.missing_enrollment or []
    if not validated and not flagged and not missing:
        state.mark_skipped(step_id, "No PML validation or missing enrollment data.")
        _emit(emitter, "✓ Opportunity sizing skipped (no data).")
        return
    url = f"{base}/opportunity-sizing"
    payload = json.dumps({
        "validated": validated,
        "flagged": flagged,
        "missing_enrollment": missing,
        "org_benchmark": state.org_benchmark,
        "member_proxy": 100,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        g = data.get("guaranteed_revenue", 0)
        ar = data.get("at_risk_revenue", 0)
        m = data.get("missing_pml_revenue", 0)
        to = data.get("taxonomy_optimization_opportunity", 0)
        rg = data.get("org_vs_state_opportunity", 0)
        total = data.get("total_opportunity", 0)
        pc = data.get("provider_counts") or {}
        sm = f"Guaranteed ${g:,.0f}, At-risk ${ar:,.0f}, Missing ${m:,.0f}, Total opp ${total:,.0f}"
        state.mark_done(step_id, sm)
        _emit(emitter, f"✓ Opportunity sizing done. {sm}")
        opp_rows = [
            {"level": "A", "label": "Guaranteed revenue", "amount": g, "provider_count": pc.get("A")},
            {"level": "B", "label": "At-risk revenue", "amount": ar, "provider_count": pc.get("B")},
            {"level": "C", "label": "Missing PML revenue", "amount": m, "provider_count": pc.get("C")},
            {"level": "D", "label": "Taxonomy optimization opportunity", "amount": to, "provider_count": None},
            {"level": "E", "label": "Org vs state opportunity", "amount": rg, "provider_count": None},
            {"level": "Total", "label": "Total opportunity (B+C+D+E)", "amount": total, "provider_count": None},
        ]
        state.step_outputs.append(
            StepOutput(
                step_id=step_id,
                label="Opportunity sizing",
                csv_content=_to_csv(opp_rows, ["level", "label", "amount", "provider_count"]),
                row_count=len(opp_rows),
            )
        )
        npi_detail = data.get("npi_detail") or []
        npi_detail_cols = [
            "npi", "provider_name", "bucket", "pml_source_file", "pml_row_key", "taxonomy_code", "zip5", "state",
            "benchmark_source", "benchmark_file", "benchmark_geography_type", "benchmark_geography_value", "benchmark_row_key",
            "revenue_per_member", "revenue_per_claim", "member_proxy_used", "base_revenue",
            "taxonomy_opt_uplift", "taxonomy_opt_detail", "rate_gap_uplift", "rate_gap_detail",
        ]
        state.step_outputs.append(
            StepOutput(
                step_id="opportunity_sizing_detail",
                label="Opportunity sizing (NPI-level tick-and-tie)",
                csv_content=_to_csv(npi_detail, npi_detail_cols) if npi_detail else "(no detail)",
                row_count=len(npi_detail),
            )
        )
        # Benchmarks CSV (filtered to client taxonomies and ZIPs)
        try:
            taxonomy_codes = set()
            for r in validated + flagged:
                t = (r.get("taxonomy_code") or "").strip()
                if t:
                    taxonomy_codes.add(t)
            for r in missing:
                t = (r.get("suggested_taxonomy_code") or "").strip()
                if t:
                    taxonomy_codes.add(t)
            zip5_list = []
            for loc in (state.locations or []):
                z = (str(loc.get("site_zip5") or loc.get("site_zip") or "") if isinstance(loc, dict) else "").strip()[:5]
                if len(z) == 5 and z not in zip5_list:
                    zip5_list.append(z)
            bm_payload = json.dumps({
                "period": "2024",
                "taxonomy_codes": list(taxonomy_codes) if taxonomy_codes else None,
                "zip5_list": zip5_list if zip5_list else None,
            }).encode("utf-8")
            bm_req = urllib.request.Request(
                f"{base}/benchmarks-export",
                data=bm_payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(bm_req, timeout=120) as bm_resp:
                bm_data = json.loads(bm_resp.read().decode())
            bm_rows = bm_data.get("rows") or []
            bm_cols = ["taxonomy_code", "geography_type", "geography_value", "period", "claim_count", "total_revenue", "member_count", "claims_per_member", "revenue_per_member", "revenue_per_claim"]
            state.step_outputs.append(
                StepOutput(step_id="benchmarks", label="Utilization benchmarks (filtered)", csv_content=_to_csv(bm_rows, bm_cols) if bm_rows else "(no data)", row_count=len(bm_rows))
            )
        except Exception as bm_err:
            logger.warning("Benchmarks export failed: %s", bm_err)
    except Exception as e:
        logger.warning("opportunity_sizing failed: %s", e)
        state.mark_done(step_id, str(e))
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Opportunity sizing", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, f"✓ Opportunity sizing done. ({e})")


def _run_step_placeholder(step_id: str, label: str, state: OrchestratorState, emitter: Callable[[str], None] | None) -> None:
    """Placeholder for future steps 7, 8."""
    state.mark_in_progress(step_id)
    _emit(emitter, f"{label}…")
    state.mark_skipped(step_id, "Placeholder (not yet implemented)")
    state.step_outputs.append(StepOutput(step_id=step_id, label=label, csv_content="(placeholder)", row_count=0))
    _emit(emitter, f"✓ {label} skipped (placeholder).")


def _run_step_build_report(
    org_name: str,
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> str:
    """Step 11: Build credentialing report via granular API steps (draft → validate → compose → charts-pdf) with progress emission."""
    step_id = "build_report"
    state.mark_in_progress(step_id)
    base = _provider_roster_base_url()
    if not base:
        state.mark_done(step_id, "Provider-roster API not configured.")
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Credentialing report", csv_content="(API not configured)", row_count=0)
        )
        _emit(emitter, "✓ Step 11 done. API not configured.")
        return "Provider-roster API not configured. Set CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL."

    step_outputs_payload = [
        {"step_id": s.step_id, "label": s.label, "csv_content": s.csv_content, "row_count": s.row_count}
        for s in state.step_outputs
    ]
    timeout_per_step = 600  # seconds per LLM step (draft has 5–6 section calls; validate/compose also slow)

    def _post_report(path: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"{base}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_per_step) as resp:
            return json.loads(resp.read().decode())

    def _post_report_with_retry(path: str, body: dict, max_retries: int = 3) -> dict:
        """POST with retry on IncompleteRead / connection reset / RemoteDisconnected."""
        last_err: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                return _post_report(path, body)
            except (IncompleteRead, RemoteDisconnected, ConnectionResetError, BrokenPipeError, OSError) as e:
                last_err = e
                if attempt < max_retries:
                    delay = 8 * (attempt + 1)
                    logger.warning("Step 11 %s attempt %d/%d failed (%s), retrying in %ds", path, attempt + 1, max_retries + 1, e, delay)
                    time.sleep(delay)
                else:
                    raise

    try:
        # 11a–b: Draft + validate with retry on BLOCK (truncation, Section A inconsistency, etc.)
        draft_max_tries = 3
        draft_md = ""
        validation_report = ""
        critique_report = ""
        for draft_attempt in range(draft_max_tries):
            if draft_attempt == 0:
                _emit(emitter, "Building credentialing report…")
            else:
                _emit(emitter, f"Validation blocked. Retrying with fresh draft (attempt {draft_attempt + 1}/{draft_max_tries})…")
            draft_resp = _post_report_with_retry(
                "/report-from-steps/draft",
                {"org_name": org_name.strip(), "step_outputs": step_outputs_payload},
            )
            draft_md = draft_resp.get("draft_md") or ""
            _emit(emitter, "Draft ready. Validating…")
            validation_resp = _post_report_with_retry(
                "/report-from-steps/validate",
                {"org_name": org_name.strip(), "step_outputs": step_outputs_payload, "draft_md": draft_md},
            )
            validation_report = validation_resp.get("validation_report") or ""
            critique_report = validation_resp.get("critique_report") or ""

            if "Validation Status: BLOCK" not in (validation_report or ""):
                break
            if draft_attempt == draft_max_tries - 1:
                _emit(emitter, f"Validation blocked after {draft_max_tries} attempts (e.g. Section E truncation, data inconsistency). Report could not be generated.")
                state.mark_done(step_id, "Validation blocked")
                state.step_outputs.append(
                    StepOutput(step_id=step_id, label="Report validation", csv_content=validation_report or "(blocked)", row_count=0)
                )
                return (
                    f"Report validation blocked after {draft_max_tries} attempts. "
                    "Critical issues (e.g. Section E truncation, Section A data inconsistency) cannot be fixed by the composer. "
                    f"Details: {validation_report[:800] if validation_report else 'See validation output.'}…"
                )

        # Interim step outputs
        _draft_preview = (draft_md[:6000] + "...") if len(draft_md) > 6000 else draft_md
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Report draft", csv_content=_draft_preview or "(no draft)", row_count=1 if draft_md else 0)
        )
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Report validation (numbers)", csv_content=validation_report or "(no output)", row_count=1 if validation_report else 0)
        )
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Report validation (narrative)", csv_content=critique_report or "(no output)", row_count=1 if critique_report else 0)
        )
        _emit(emitter, "Validation complete. Building final report…")

        # 11c: Compose (incorporates both validations)
        compose_resp = _post_report_with_retry("/report-from-steps/compose", {
            "org_name": org_name.strip(),
            "step_outputs": step_outputs_payload,
            "draft_md": draft_md,
            "validation_report": validation_report,
            "critique_report": critique_report,
        })
        final_md = compose_resp.get("final_md") or ""
        _emit(emitter, "Final report ready. Generating charts and PDF…")

        # 11d: Charts + PDF
        charts_resp = _post_report_with_retry("/report-from-steps/charts-pdf", {"org_name": org_name.strip(), "step_outputs": step_outputs_payload, "final_md": final_md})
        final_md = charts_resp.get("final_md") or final_md
        pdf_base64 = charts_resp.get("pdf_base64") or ""

        state.report_final_md = final_md
        state.report_pdf_base64 = pdf_base64
        state.step_outputs.append(
            StepOutput(
                step_id=step_id,
                label="Final report",
                csv_content="(See main message above. Use the download button for PDF or Markdown.)",
                row_count=1 if final_md else 0,
            )
        )
        result_text = final_md or "Report generated (no markdown returned)."
        if final_md:
            state.mark_done(step_id, "Report generated.")
            _emit(emitter, "✓ Step 11 done. Report generated.")
        else:
            state.mark_done(step_id, "Report had issues.")
            _emit(emitter, "✓ Step 11 done. (Report had issues.)")
        return result_text
    except urllib.error.HTTPError as e:
        body = e.fp.read().decode()[:1000] if e.fp else str(e)
        logger.warning("report-from-steps HTTP %s %s", e.code, body, exc_info=(e.code >= 500))
        state.mark_done(step_id, f"API error {e.code}")
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Credentialing report", csv_content=f"(API error {e.code})", row_count=0)
        )
        _emit(emitter, f"✓ Step 11 done. API error ({e.code}).")
        return f"Report failed ({e.code}): {body}"
    except urllib.error.URLError as e:
        if "timed out" in str(e).lower():
            _emit(emitter, "Report step timed out. Try again or use a shorter org roster.")
        logger.warning("report-from-steps failed: %s", e)
        state.mark_done(step_id, str(e))
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Credentialing report", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, f"✓ Step 11 done. Failed ({e}).")
        return str(e)
    except Exception as e:
        logger.warning("report-from-steps failed: %s", e, exc_info=True)
        state.mark_done(step_id, str(e))
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Credentialing report", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, f"✓ Step 11 done. Failed ({e}).")
        return str(e)


def run_orchestrator(
    org_input: str,
    emitter: Callable[[str], None] | None = None,
) -> tuple[str, OrchestratorState]:
    """Run the Provider Roster / Credentialing plan and emit progress to chat.

    Args:
        org_input: Organization name or substring (e.g. "David Lawrence", "Aspire").
        emitter: Optional callback for progress messages (each message is one chunk for chat).

    Returns:
        (final_report_text, orchestrator_state). Report text is from Step 3; state has steps checked off.
    """
    state = OrchestratorState(
        steps=[StepState(id=s["id"], label=s["label"]) for s in ROSTER_CREDENTIALING_PLAN],
        org_npis=[],
    )
    # Emit 9-step plan
    plan_lines = ["Steps:"]
    for i, s in enumerate(ROSTER_CREDENTIALING_PLAN, 1):
        plan_lines.append(f"  {i}. {s['label']}")
    _emit(emitter, "\n".join(plan_lines))

    org_name = (org_input or "").strip()
    if not org_name:
        _emit(emitter, "No organization name provided; stopping.")
        return "No organization name provided. Try: 'Create a Medicaid NPI report for [org name]'.", state

    state.org_name = org_name
    _run_step_0_ensure_benchmarks(state, emitter)
    _run_step_1_identify_org(org_name, state, emitter)
    _run_step_2_find_locations(state, emitter)
    _run_step_3_find_associated_providers(state, emitter)
    _run_step_org_benchmark(state, emitter)
    _run_step_4_find_services_by_location(state, emitter)
    _run_step_5_historic_billing_patterns(state, emitter)
    _run_step_6_pml_validation(state, emitter)
    _run_step_7_missing_pml(state, emitter)
    _run_step_opportunity_sizing(state, emitter)
    report_text = _run_step_build_report(org_name, state, emitter)

    # Step outputs are passed via roster_step_outputs in the payload; frontend renders them as collapsible
    return (report_text or "Report could not be generated."), state


def _step_num(step_id: str) -> int:
    """Map step_id to display number 1–11."""
    order = {
        "ensure_benchmarks": 1,
        "identify_org": 2,
        "find_locations": 3,
        "find_associated_providers": 4,
        "org_benchmark": 5,
        "find_services_by_location": 6,
        "historic_billing_patterns": 7,
        "step_6": 8,
        "step_7": 9,
        "opportunity_sizing": 10,
        "build_report": 11,
    }
    return order.get(step_id, 0)
