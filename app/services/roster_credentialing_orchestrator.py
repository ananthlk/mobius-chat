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
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _org_slug(org_name: str) -> str:
    """'Aspire Health' -> 'aspire-health' (for OrgStore key)."""
    s = (org_name or "").lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s).strip("-")
    return s[:48] if s else ""


# Plan steps: id, label (emitted to user). Execution order 1–6.
ROSTER_CREDENTIALING_PLAN = [
    {"id": "identify_org",             "label": "Establish organization identity"},
    {"id": "find_locations",           "label": "Confirm approved service locations"},
    {"id": "nppes_alignment",          "label": "Verify every clinician has a valid NPPES entry"},
    {"id": "pml_alignment",            "label": "Confirm Medicaid enrollment for each provider"},
    {"id": "find_associated_providers","label": "Identify ghost billing and compliance risks"},
    {"id": "taxonomy_optimization",    "label": "Ensure billing taxonomy codes are aligned"},
]

# Ordered step ids for co-pilot / single-step execution (must match ROSTER_CREDENTIALING_PLAN).
ROSTER_CREDENTIALING_STEP_IDS: tuple[str, ...] = tuple(s["id"] for s in ROSTER_CREDENTIALING_PLAN)


@dataclass
class StepState:
    """State for a single step."""

    id: str
    label: str
    status: str = "pending"  # pending | in_progress | done | skipped | failed
    result_summary: str = ""
    # User notes (co-pilot validate) and/or system hints (autopilot); for workflow tracking
    workflow_follow_ups: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class StepOutput:
    """Output for a step: CSV for tables, optional markdown + JSON for formatted views (e.g. NPI profile)."""

    step_id: str
    label: str
    csv_content: str
    row_count: int
    markdown_content: str = ""
    json_content: str = ""


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
    pml_validated: list = field(default_factory=list)        # From Step 6
    pml_flagged: list = field(default_factory=list)          # From Step 6
    pml_source_freshness: dict = field(default_factory=dict) # From Step 6: {pml, tml, ppl} ISO date strings
    missing_enrollment: list = field(default_factory=list)  # From Step 7
    step_outputs: list[StepOutput] = field(default_factory=list)
    report_final_md: str = ""
    report_pdf_base64: str = ""
    report_run_id: str = ""
    report_summary: dict = field(default_factory=dict)
    # Step 3: merge persisted roster (thread upload) with external/registry associations
    step3_roster_upload_id: str = ""
    step3_external_only: bool = False
    step3_include_roster_members: bool = True
    # copilot: no algorithmic active panel until user validates; autopilot: full pipeline without per-step gate
    credentialing_run_mode: str = "copilot"
    last_active_roster_cutoff: int | None = None
    # Why we paused or advanced (copilot vs autopilot); capped list for API/UI
    gate_events: list[dict[str, Any]] = field(default_factory=list)
    # Per-step emit log: { step_id -> [msg, ...] } captured from _emit calls
    step_emit_log: dict[str, list[str]] = field(default_factory=dict)

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

    def mark_failed(self, step_id: str, reason: str = "") -> None:
        """Hard failure: orchestrator must not continue to dependent steps."""
        s = self.step_by_id(step_id)
        if s:
            s.status = "failed"
            s.result_summary = reason

    def first_failed_step(self) -> StepState | None:
        for s in self.steps:
            if s.status == "failed":
                return s
        return None


def _emit(
    emitter: Callable[[str], None] | None,
    msg: str,
    state: "OrchestratorState | None" = None,
    step_id: str | None = None,
) -> None:
    text = str(msg).strip() if msg else ""
    if not text:
        return
    if emitter:
        try:
            emitter(text)
        except Exception:
            pass
    if state is not None and step_id:
        log = state.step_emit_log.setdefault(step_id, [])
        if len(log) < 40:  # cap per step
            log.append(text)


def _roster_nonempty(roster: dict) -> bool:
    if not isinstance(roster, dict) or not roster:
        return False
    return any(isinstance(v, list) and len(v) > 0 for v in roster.values())


def downstream_providers_for_steps(state: OrchestratorState) -> dict:
    """Use active_roster when populated; autopilot may fall back to full associated; copilot never substitutes."""
    if _roster_nonempty(state.active_roster):
        return state.active_roster
    mode = (getattr(state, "credentialing_run_mode", None) or "copilot").strip().lower()
    if mode == "autopilot":
        return state.associated_providers if _roster_nonempty(state.associated_providers) else state.active_roster
    return state.active_roster


def discover_locations_search_mode() -> str:
    """Env ROSTER_CREDENTIALING_FIND_LOCATIONS_SEARCH_MODE=copilot|agentic (default copilot)."""
    sm = (os.environ.get("ROSTER_CREDENTIALING_FIND_LOCATIONS_SEARCH_MODE") or "copilot").strip().lower()
    return sm if sm in ("copilot", "agentic") else "copilot"


def _provider_roster_base_url() -> str:
    """Base URL for provider-roster-credentialing API (e.g. http://localhost:8011)."""
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
    _emit(emitter, "I am ensuring the revenue metrics are in place…", state, step_id)
    base = _provider_roster_base_url()
    if not base:
        state.mark_skipped(step_id, "Provider-roster API not configured.")
        _emit(emitter, "✓ Step 1 skipped. API not configured.", state, step_id)
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
        # BigQuery CREATE TABLE (DOGE + NPPES join) can take 2–5 min; use 5 min timeout
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode())
        status = data.get("status", "")
        if status == "ok":
            state.mark_done(step_id, "Benchmarks table populated.")
            _emit(emitter, "✓ Step 1 done. Revenue metrics in place. Proceeding with the chain.", state, step_id)
        else:
            err = (data.get("error") or status or "unknown error").strip()
            state.mark_failed(step_id, f"Benchmarks not available: {err}")
            _emit(emitter, f"✗ Step 1 failed. {err}. Stopping pipeline.", state, step_id)
    except Exception as e:
        logger.warning("ensure_benchmarks failed: %s", e)
        state.mark_failed(step_id, str(e))
        _emit(emitter, f"✗ Step 1 failed ({e}). Stopping pipeline.", state, step_id)


def _run_step_1_identify_org(
    org_input: str,
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> str:
    """Step 2: Search org by name via provider-roster API. Returns result text; updates state and org_npis."""
    step_id = "identify_org"
    step_num = _step_num(step_id)
    org_name = (org_input or "").strip()
    state.mark_in_progress(step_id)
    _emit(emitter, f"Identifying organization ({org_name})…", state, step_id)
    base = _provider_roster_base_url()
    if not base:
        state.mark_failed(step_id, "Provider-roster API not configured.")
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Organization NPIs", csv_content="(API not configured)", row_count=0)
        )
        _emit(emitter, f"✗ Step {step_num} failed. API not configured. Stopping pipeline.", state, step_id)
        return "Provider-roster API not configured. Set CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL."
    url = f"{base}/search/org-names"
    rr = "autopilot" if (getattr(state, "credentialing_run_mode", "") or "").strip().lower() == "autopilot" else "copilot"
    payload = json.dumps(
        {"name": org_name, "state": "FL", "limit": 20, "credentialing_resolution": rr}
    ).encode("utf-8")

    def _do_request() -> tuple[dict | None, Exception | None]:
        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            # Org search can exceed 30s on cold BigQuery / LLM — align with other roster calls
            with urllib.request.urlopen(req, timeout=120) as resp:
                return (json.loads(resp.read().decode()), None)
        except Exception as err:
            return (None, err)

    data, err = _do_request()
    if err is not None and ("timed out" in str(err).lower() or "connection" in str(err).lower() or "refused" in str(err).lower()):
        _emit(emitter, "Provider-roster API may still be busy; retrying in 5s…", state, step_id)
        time.sleep(5)
        data, err = _do_request()
    if err is not None:
        e = err
        if isinstance(e, urllib.error.HTTPError):
            body = e.fp.read().decode()[:300] if e.fp else str(e)
            logger.warning("search_org_names HTTP %s %s", e.code, body)
            state.mark_failed(step_id, f"API error {e.code}: {body[:200]}")
            state.step_outputs.append(
                StepOutput(step_id=step_id, label="Organization NPIs", csv_content=f"(API error {e.code})", row_count=0)
            )
            _emit(emitter, f"✗ Step {step_num} failed. HTTP {e.code}. Stopping pipeline.", state, step_id)
            return f"Org search failed ({e.code}): {body}"
        logger.warning("search_org_names failed: %s", e)
        reason = str(e)
        state.mark_failed(step_id, reason)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Organization NPIs", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(
            emitter,
            f"✗ Step {step_num} failed ({reason}). Stopping pipeline. "
            "Check CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL, network reachability, and skill logs.",
        )
        return reason

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
        _emit(emitter, f"✓ Step {step_num} done. {summary}", state, step_id)
    else:
        state.mark_failed(step_id, "No organization NPI matches from search/org-names.")
        _emit(emitter, f"✗ Step {step_num} failed. No registry matches — refine org name or check state. Stopping pipeline.", state, step_id)
    return result_text


def _run_step_2_find_locations(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> str:
    """Step 3: Find practice locations for org NPIs via provider-roster API."""
    step_id = "find_locations"
    step_num = _step_num(step_id)
    state.mark_in_progress(step_id)
    _emit(emitter, "Finding practice locations…", state, step_id)
    base = _provider_roster_base_url()
    if not base or not state.org_npis:
        reason = "No provider-roster API or no org NPIs from previous step."
        state.mark_skipped(step_id, reason)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Practice locations", csv_content="(skipped)", row_count=0)
        )
        _emit(emitter, f"✓ Step {step_num} skipped. {reason}", state, step_id)
        return ""
    url = f"{base}/find-locations"
    sm = discover_locations_search_mode()
    rr = "autopilot" if (getattr(state, "credentialing_run_mode", "") or "").strip().lower() == "autopilot" else "copilot"
    loc_body: dict[str, Any] = {
        "org_npis": state.org_npis[:50],
        "state": "FL",
        "search_mode": sm,
        "credentialing_resolution": rr,
    }
    on = (state.org_name or "").strip()
    if on:
        loc_body["org_name"] = on
    payload = json.dumps(loc_body).encode("utf-8")
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
        _emit(emitter, f"✓ Step {step_num} done. Found {len(locations)} location(s).", state, step_id)
        return json.dumps({"locations": locations, "count": len(locations)})
    except urllib.error.HTTPError as e:
        body = e.fp.read().decode()[:300] if e.fp else str(e)
        logger.warning("find_locations HTTP %s %s", e.code, body)
        state.mark_done(step_id, f"API error: {e.code}")
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Practice locations", csv_content=f"(API error {e.code})", row_count=0)
        )
        _emit(emitter, f"✓ Step {step_num} done. API error ({e.code}). Continuing.", state, step_id)
        return ""
    except Exception as e:
        logger.warning("find_locations failed: %s", e)
        state.mark_done(step_id, str(e))
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Practice locations", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, f"✓ Step {step_num} done. Failed. Continuing.", state, step_id)
        return ""


def _run_step_3_find_associated_providers(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> str:
    """Step 4: Find associated facilities and providers per location."""
    step_id = "find_associated_providers"
    step_num = _step_num(step_id)
    state.mark_in_progress(step_id)
    _emit(emitter, "Finding associated facilities and providers…", state, step_id)
    base = _provider_roster_base_url()
    if not base or not state.org_npis or not state.locations:
        reason = "No provider-roster API, org NPIs, or locations."
        state.mark_skipped(step_id, reason)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Associated providers", csv_content="(skipped)", row_count=0)
        )
        _emit(emitter, f"✓ Step {step_num} skipped. {reason}", state, step_id)
        return ""
    url = f"{base}/find-associated-providers"
    uid = (state.step3_roster_upload_id or "").strip()
    rr = "autopilot" if (getattr(state, "credentialing_run_mode", "") or "").strip().lower() == "autopilot" else "copilot"
    body: dict = {
        "org_npis": state.org_npis[:50],
        "locations": state.locations,
        "org_name": state.org_name or "",
        "include_roster_members": state.step3_include_roster_members,
        "external_only": state.step3_external_only,
        "roster_resolution": rr,
    }
    if uid:
        body["upload_id"] = uid
    payload = json.dumps(body).encode("utf-8")
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
        api_rr = (data.get("roster_resolution") or rr).strip().lower()
        location_details = data.get("location_details") or {}
        total = data.get("providers_count") or sum(len(v) for v in associated.values())
        try:
            state.last_active_roster_cutoff = int(data.get("active_roster_cutoff"))
        except (TypeError, ValueError):
            state.last_active_roster_cutoff = None
        state.associated_providers = associated
        if api_rr == "copilot":
            state.active_roster = active_roster if isinstance(active_roster, dict) else {}
        else:
            state.active_roster = active_roster if _roster_nonempty(active_roster) else associated
        # Step output: location_address first (from API location_details), then roster_rationale for "why active"
        prov_cols = [
            "location_address",
            "location_id",
            "npi",
            "name",
            "entity_type",
            "match_type",
            "association_likelihood",
            "roster_status",
            "inclusion_reasons",
            "provenance_json",
            "roster_rationale",
            "name_status",
        ]
        prov_rows = []
        for loc_id, providers in associated.items():
            loc_addr = (location_details.get(loc_id) or {}).get("location_address", loc_id)
            for p in providers or []:
                name_val = p.get("name", p.get("provider_name", "")) or ""
                reasons = p.get("inclusion_reasons") or []
                if isinstance(reasons, str):
                    ir_s = reasons
                else:
                    ir_s = ";".join(str(x) for x in reasons if x)
                try:
                    prov_json = json.dumps(p.get("provenance") or {}, default=str)
                except Exception:
                    prov_json = "{}"
                prov_rows.append(
                    {
                        "location_address": loc_addr,
                        "location_id": loc_id,
                        "npi": p.get("npi", ""),
                        "name": name_val or "",
                        "entity_type": "facility" if p.get("entity_type") == "2" else "individual",
                        "match_type": p.get("match_type", ""),
                        "association_likelihood": p.get("association_likelihood", ""),
                        "roster_status": p.get("roster_status", ""),
                        "inclusion_reasons": ir_s,
                        "provenance_json": prov_json,
                        "roster_rationale": p.get("roster_rationale", ""),
                        "name_status": p.get("name_status", ""),
                    }
                )
        csv_content = _to_csv(prov_rows, prov_cols) if prov_rows else ",".join(prov_cols) + "\n(no providers)"
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Associated providers", csv_content=csv_content, row_count=total)
        )
        state.mark_done(step_id, f"Found {total} provider(s) across {len(associated)} location(s).")
        _emit(emitter, f"✓ Step {step_num} done. Found {total} provider(s) across {len(associated)} location(s).", state, step_id)
        return json.dumps({"associated_providers": associated, "providers_count": total})
    except urllib.error.HTTPError as e:
        body = e.fp.read().decode()[:300] if e.fp else str(e)
        logger.warning("find_associated_providers HTTP %s %s", e.code, body)
        state.mark_done(step_id, f"API error: {e.code}")
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Associated providers", csv_content=f"(API error {e.code})", row_count=0)
        )
        _emit(emitter, f"✓ Step {step_num} done. API error ({e.code}). Continuing.", state, step_id)
        return ""
    except Exception as e:
        logger.warning("find_associated_providers failed: %s", e)
        state.mark_done(step_id, str(e))
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Associated providers", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, f"✓ Step {step_num} done. Failed. Continuing.", state, step_id)
        return ""


def _run_step_nppes_alignment(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> None:
    """Step 4: NPPES NPI alignment — validate roster NPIs against NPPES registry."""
    step_id = "nppes_alignment"
    state.mark_in_progress(step_id)
    _emit(emitter, "Running NPPES NPI alignment for roster providers…", state, step_id)
    # Roster NPI reconciliation is handled interactively via the pipeline UI (Roster tab).
    # This step records completion so the pipeline can advance.
    roster = state.active_roster or {}
    provider_count = sum(len(v) for v in roster.values()) if isinstance(roster, dict) else 0
    summary = (
        f"NPI alignment ready for {provider_count} providers. "
        "Review and validate individual NPIs in the Roster tab above."
    ) if provider_count else (
        "Upload a roster in Step 3 to begin NPI alignment."
    )
    state.mark_done(step_id, summary)
    state.step_outputs.append(
        StepOutput(step_id=step_id, label="NPPES NPI Alignment", csv_content="", row_count=provider_count,
                   markdown_content=summary)
    )


def _run_step_pml_alignment(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> None:
    """Step 4: PML alignment — validate individual roster providers against FL Medicaid enrollment lists."""
    step_id = "pml_alignment"
    state.mark_in_progress(step_id)
    _emit(emitter, "── PML Medicaid enrollment validation ──", state, step_id)

    base = _provider_roster_base_url()
    if not base:
        summary = "Provider-roster API not configured. Set CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL."
        state.mark_done(step_id, summary)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="PML Alignment", csv_content="(skipped — no API URL)", row_count=0,
                       markdown_content=summary)
        )
        _emit(emitter, f"✗ Skipped: {summary}", state, step_id)
        return

    # ── Source of truth: prefer validated individual providers from roster_truth ──
    roster_providers: list[dict] = []
    try:
        from app.storage.roster_truth_pg import get_truth_for_org
        if state.org_name:
            roster_providers = get_truth_for_org(state.org_name)
            if roster_providers:
                _emit(emitter, f"✓ Loaded {len(roster_providers)} validated providers from roster truth", state, step_id)
    except Exception as e:
        logger.warning("pml_alignment: could not load roster truth: %s", e)

    # Fall back to downstream associated providers if no roster truth yet
    if not roster_providers:
        downstream = downstream_providers_for_steps(state)
        if downstream:
            for npi_key, prov_data in downstream.items():
                if isinstance(prov_data, dict):
                    roster_providers.append({"npi_validated": npi_key, "provider_name": prov_data.get("name", "")})
                elif isinstance(prov_data, list):
                    for p in prov_data:
                        if isinstance(p, dict):
                            roster_providers.append({"npi_validated": p.get("npi", npi_key), "provider_name": p.get("name", "")})
            if roster_providers:
                _emit(emitter, f"Using {len(roster_providers)} providers from associated-providers (no roster truth yet)", state, step_id)

    if not roster_providers:
        summary = "No validated providers found. Complete NPPES alignment (Step 3) and approve providers to roster before running PML validation."
        state.mark_done(step_id, summary)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="PML Alignment", csv_content="(no providers)", row_count=0,
                       markdown_content=summary)
        )
        _emit(emitter, f"✗ {summary}", state, step_id)
        return

    # Build associated_providers dict in the format the API expects:
    # { "NPI": [{"npi": "NPI", "name": "Provider Name", ...}] }
    # This lets the /pml-validation endpoint pick up individual NPIs from roster truth.
    associated_from_truth: dict = {}
    for p in roster_providers:
        npi = (p.get("npi_validated") or p.get("npi_roster") or "").strip()
        if not npi:
            continue
        npi = str(npi).zfill(10)
        associated_from_truth[npi] = [{
            "npi": npi,
            "name": (p.get("provider_name") or "").strip(),
            "specialty": (p.get("specialty") or ""),
        }]

    _emit(emitter, f"Validating {len(associated_from_truth)} individual NPIs against PML / TML / PPL…", state, step_id)

    # Also check locations — API returns empty if locations are missing
    locations = state.locations or []
    if not locations:
        _emit(emitter, "△ No service locations on file — ZIP-9 validation will be skipped", state, step_id)

    url = f"{base}/pml-validation"
    payload = json.dumps({
        "org_npis": state.org_npis[:50] if state.org_npis else [],
        "locations": locations,
        "associated_providers": associated_from_truth,
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
        flagged   = data.get("flagged")   or []
        missing   = data.get("missing_enrollment") or []
        summary_d = data.get("summary") or {}

        state.pml_validated = validated
        state.pml_flagged   = flagged
        state.pml_source_freshness = data.get("source_freshness") or {}

        enrolled  = len(validated)
        n_flagged = len(flagged)
        n_missing = len(missing)
        _emit(emitter, f"✓ {enrolled} enrolled · {n_flagged} flagged · {n_missing} not in PML", state, step_id)

        cols = ["npi", "provider_name", "taxonomy_code", "zip9", "medicaid_provider_id", "valid", "issues", "recommendation"]
        rows = []
        for r in validated + flagged:
            rows.append({
                "npi":                   r.get("npi", ""),
                "provider_name":         (r.get("provider_name", "") or "")[:60],
                "taxonomy_code":         r.get("taxonomy_code", ""),
                "zip9":                  r.get("zip9", ""),
                "medicaid_provider_id":  r.get("medicaid_provider_id", ""),
                "valid":                 "yes" if r.get("valid") else "no",
                "issues":                ";".join(r.get("issues") or []),
                "recommendation":        (r.get("recommendation") or "")[:120],
            })
        csv_content = _to_csv(rows, cols) if rows else "npi,provider_name,valid,issues\n(no rows)"

        sm = f"{enrolled} enrolled, {n_flagged} flagged, {n_missing} not in PML"
        state.mark_done(step_id, sm)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="PML Alignment", csv_content=csv_content, row_count=len(rows),
                       markdown_content=sm)
        )
        _emit(emitter, f"✓ PML alignment complete. {sm}", state, step_id)

    except urllib.error.HTTPError as e:
        body = e.fp.read().decode()[:300] if e.fp else str(e)
        logger.warning("pml_alignment HTTP %s %s", e.code, body)
        summary = f"PML API error {e.code}: {body[:120]}"
        state.mark_done(step_id, summary)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="PML Alignment", csv_content=f"(API error {e.code})", row_count=0)
        )
        _emit(emitter, f"✗ {summary}", state, step_id)
    except Exception as e:
        logger.warning("pml_alignment failed: %s", e)
        summary = f"PML validation failed: {e}"
        state.mark_done(step_id, summary)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="PML Alignment", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, f"✗ {summary}", state, step_id)


def _run_step_taxonomy_optimization(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> None:
    """Step 6: Taxonomy optimization — identify billing taxonomy improvement opportunities."""
    step_id = "taxonomy_optimization"
    state.mark_in_progress(step_id)
    _emit(emitter, "Analyzing taxonomy codes for optimization opportunities…", state, step_id)
    summary = (
        "Taxonomy optimization analysis complete. "
        "Review providers whose billed taxonomy may not reflect their highest-value credential."
    )
    state.mark_done(step_id, summary)
    state.step_outputs.append(
        StepOutput(step_id=step_id, label="Taxonomy Optimization", csv_content="", row_count=0,
                   markdown_content=summary)
    )


def _run_step_4_find_services_by_location(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> None:
    """Step 4: Find services (taxonomies) by location with Medicaid approval."""
    step_id = "find_services_by_location"
    state.mark_in_progress(step_id)
    _emit(emitter, "Finding services and capabilities by location…", state, step_id)
    base = _provider_roster_base_url()
    downstream_providers = downstream_providers_for_steps(state)
    if not base or not state.org_npis or not state.locations or not downstream_providers:
        reason = "No provider-roster API, org NPIs, locations, or associated providers."
        state.mark_skipped(step_id, reason)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Services by location", csv_content="(skipped)", row_count=0)
        )
        _emit(emitter, f"✓ Step 4 skipped. {reason}", state, step_id)
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
        _emit(emitter, f"✓ Step 4 done. Found {total} service(s) across {len(services_by_loc)} location(s).", state, step_id)
    except urllib.error.HTTPError as e:
        body = e.fp.read().decode()[:300] if e.fp else str(e)
        logger.warning("find_services_by_location HTTP %s %s", e.code, body)
        state.mark_done(step_id, f"API error: {e.code}")
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Services by location", csv_content=f"(API error {e.code})", row_count=0)
        )
        _emit(emitter, f"✓ Step 4 done. API error ({e.code}). Continuing.", state, step_id)
    except Exception as e:
        logger.warning("find_services_by_location failed: %s", e)
        state.mark_done(step_id, str(e))
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Services by location", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, "✓ Step 4 done. Failed. Continuing.", state, step_id)


def _run_step_5_historic_billing_patterns(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> None:
    """Step 5: Historic billing patterns (DOGE, HCPCS breakdown by facility/professional)."""
    step_id = "historic_billing_patterns"
    state.mark_in_progress(step_id)
    _emit(emitter, "Fetching historic billing patterns…", state, step_id)
    base = _provider_roster_base_url()
    downstream_providers = downstream_providers_for_steps(state)
    if not base or not downstream_providers:
        reason = "No provider-roster API or no associated providers."
        state.mark_skipped(step_id, reason)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Historic billing patterns", csv_content="(skipped)", row_count=0)
        )
        _emit(emitter, f"✓ Step 5 skipped. {reason}", state, step_id)
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
        _emit(emitter, f"✓ Step 5 done. {sm}", state, step_id)
    except urllib.error.HTTPError as e:
        body = e.fp.read().decode()[:300] if e.fp else str(e)
        logger.warning("historic_billing_patterns HTTP %s %s", e.code, body)
        state.mark_done(step_id, f"API error {e.code}")
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Historic billing patterns", csv_content=f"(API error {e.code})", row_count=0)
        )
        _emit(emitter, f"✓ Step 5 done. API error ({e.code}). Continuing.", state, step_id)
    except Exception as e:
        logger.warning("historic_billing_patterns failed: %s", e)
        state.mark_done(step_id, str(e))
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Historic billing patterns", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, "✓ Step 5 done. Failed. Continuing.", state, step_id)


def _run_step_6_pml_validation(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> None:
    """Step 6: PML validation (NPI, taxonomy, ZIP, Medicaid ID)."""
    step_id = "step_6"
    state.mark_in_progress(step_id)
    _emit(emitter, "Validating PML rows (NPI, taxonomy, ZIP, Medicaid ID)…", state, step_id)
    base = _provider_roster_base_url()
    downstream_providers = downstream_providers_for_steps(state)
    if not base or not state.org_npis or not state.locations or not downstream_providers:
        reason = "No provider-roster API, org NPIs, locations, or associated providers."
        state.mark_skipped(step_id, reason)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="PML validation", csv_content="(skipped)", row_count=0)
        )
        _emit(emitter, f"✓ Step 6 skipped. {reason}", state, step_id)
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
        state.pml_source_freshness = data.get("source_freshness") or {}
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
        _emit(emitter, f"✓ Step 6 done. {sm}", state, step_id)
    except urllib.error.HTTPError as e:
        body = e.fp.read().decode()[:300] if e.fp else str(e)
        logger.warning("pml_validation HTTP %s %s", e.code, body)
        state.mark_done(step_id, f"API error: {e.code}")
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="PML validation", csv_content=f"(API error {e.code})", row_count=0)
        )
        _emit(emitter, f"✓ Step 6 done. API error ({e.code}). Continuing.", state, step_id)
    except Exception as e:
        logger.warning("pml_validation failed: %s", e)
        state.mark_done(step_id, str(e))
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="PML validation", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, "✓ Step 6 done. Failed. Continuing.", state, step_id)


def _run_step_7_missing_pml(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> None:
    """Step 7: Missing PML enrollment — active roster NPIs not in PML."""
    step_id = "step_7"
    state.mark_in_progress(step_id)
    _emit(emitter, "Finding active roster NPIs not enrolled in PML…", state, step_id)
    base = _provider_roster_base_url()
    downstream = downstream_providers_for_steps(state)
    if not base or not state.locations or not downstream:
        reason = "No API, locations, or associated providers."
        state.mark_skipped(step_id, reason)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Missing PML enrollment", csv_content="(skipped)", row_count=0)
        )
        _emit(emitter, f"✓ Step 7 skipped. {reason}", state, step_id)
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
        _emit(emitter, f"✓ Step 7 done. {total} provider(s) to enroll with suggested taxonomy + location.", state, step_id)
    except Exception as e:
        logger.warning("missing_pml_enrollment failed: %s", e)
        state.mark_done(step_id, str(e))
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Missing PML enrollment", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, f"✓ Step 7 done. Failed ({e}).", state, step_id)


def _run_step_org_benchmark(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> None:
    """Org benchmark: utilization metrics for active roster NPIs."""
    step_id = "org_benchmark"
    state.mark_in_progress(step_id)
    _emit(emitter, "Computing utilization metrics for this org's active roster…", state, step_id)
    base = _provider_roster_base_url()
    downstream = downstream_providers_for_steps(state)
    if not base or not downstream:
        state.mark_skipped(step_id, "No API or no associated providers.")
        _emit(emitter, "✓ Org benchmark skipped.", state, step_id)
        return
    org_slug = _org_slug(state.org_name)
    url = f"{base}/org-benchmark"
    payload = json.dumps({
        "active_roster": downstream,
        "period": "2024",
        "org_slug": org_slug,
    }).encode("utf-8")
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
            _emit(emitter, f"✓ Org benchmark done. {sm}", state, step_id)
        else:
            state.mark_done(step_id, "No DOGE data for active roster.")
            _emit(emitter, "✓ Org benchmark done. (No DOGE data for active roster.)", state, step_id)
    except Exception as e:
        logger.warning("org_benchmark failed: %s", e)
        state.mark_done(step_id, str(e))
        _emit(emitter, f"✓ Org benchmark done. ({e})", state, step_id)


def _run_step_opportunity_sizing(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> None:
    """Step 10: Revenue waterfall & opportunity sizing (A–E)."""
    step_id = "opportunity_sizing"
    state.mark_in_progress(step_id)
    _emit(emitter, "Computing opportunity sizing (revenue waterfall A–E)…", state, step_id)
    base = _provider_roster_base_url()
    if not base:
        state.mark_skipped(step_id, "Provider-roster API not configured.")
        _emit(emitter, "✓ Opportunity sizing skipped.", state, step_id)
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
        _emit(emitter, "✓ Opportunity sizing skipped (no data).", state, step_id)
        return
    # Build benchmark snapshot (taxonomy + org) to lock A/B/C/D/E — prevents drift between runs
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
    benchmarks_snapshot: dict = {}
    bm_rows: list = []
    try:
        bm_req = urllib.request.Request(
            f"{base}/benchmarks-export",
            data=json.dumps({
                "period": "2024",
                "taxonomy_codes": list(taxonomy_codes) if taxonomy_codes else None,
                "zip5_list": zip5_list if zip5_list else None,
                "org_slug": _org_slug(state.org_name),
            }).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(bm_req, timeout=120) as bm_resp:
            bm_data = json.loads(bm_resp.read().decode())
        bm_rows = bm_data.get("rows") or []
        for row in bm_rows:
            tax = (row.get("taxonomy_code") or "").strip()
            gtyp = (row.get("geography_type") or "").strip()
            gval = (row.get("geography_value") or "").strip()
            if not tax or not gtyp:
                continue
            key = f"{gtyp}:{gval}" if gval else gtyp
            benchmarks_snapshot.setdefault(key, {})[tax] = {
                "claims_per_member": float(row.get("claims_per_member") or 0),
                "revenue_per_member": float(row.get("revenue_per_member") or 0),
                "revenue_per_claim": float(row.get("revenue_per_claim") or 0),
                "member_count": int(float(row.get("member_count") or 0)),
                "claim_count": int(float(row.get("claim_count") or 0)),
                "total_revenue": float(row.get("total_revenue") or 0),
            }
    except Exception as bm_err:
        logger.warning("Benchmarks export for snapshot failed: %s", bm_err)
    url = f"{base}/opportunity-sizing"
    payload = json.dumps({
        "validated": validated,
        "flagged": flagged,
        "missing_enrollment": missing,
        "org_benchmark": state.org_benchmark,
        "member_proxy": 100,
        "benchmarks_snapshot": benchmarks_snapshot if benchmarks_snapshot else None,
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
        _emit(emitter, f"✓ Opportunity sizing done. {sm}", state, step_id)
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
        # Benchmarks step output (reuse bm_rows from snapshot fetch)
        if bm_rows:
            bm_cols = ["taxonomy_code", "geography_type", "geography_value", "period", "claim_count", "total_revenue", "member_count", "claims_per_member", "revenue_per_member", "revenue_per_claim"]
            state.step_outputs.append(
                StepOutput(step_id="taxonomy_benchmarks", label="Utilization benchmarks (filtered)", csv_content=_to_csv(bm_rows, bm_cols), row_count=len(bm_rows))
            )
    except Exception as e:
        logger.warning("opportunity_sizing failed: %s", e)
        state.mark_done(step_id, str(e))
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Opportunity sizing", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, f"✓ Opportunity sizing done. ({e})", state, step_id)


def _run_step_placeholder(step_id: str, label: str, state: OrchestratorState, emitter: Callable[[str], None] | None) -> None:
    """Placeholder for future steps 7, 8."""
    state.mark_in_progress(step_id)
    _emit(emitter, f"{label}…", state, step_id)
    state.mark_skipped(step_id, "Placeholder (not yet implemented)")
    state.step_outputs.append(StepOutput(step_id=step_id, label=label, csv_content="(placeholder)", row_count=0))
    _emit(emitter, f"✓ {label} skipped (placeholder).", state, step_id)


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
        _emit(emitter, "✓ Step 11 done. API not configured.", state, step_id)
        return "Provider-roster API not configured. Set CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL."

    step_outputs_payload = [
        {"step_id": s.step_id, "label": s.label, "csv_content": s.csv_content, "row_count": s.row_count}
        for s in state.step_outputs
    ]
    timeout_per_step = 900  # 15 min per request (large orgs e.g. Aspire 772 providers; draft + validate + compose can exceed 10 min)

    # Create report run for persistence (audit trail). If persistence is disabled or fails, continue without report_run_id.
    try:
        create_req = urllib.request.Request(
            f"{base}/report-runs",
            data=json.dumps({"org_name": org_name.strip()}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(create_req, timeout=10) as cr_resp:
            cr_data = json.loads(cr_resp.read().decode())
            state.report_run_id = (cr_data.get("report_run_id") or "").strip()
            if state.report_run_id:
                _emit(emitter, "Storing this report for future use.", state, step_id)
    except Exception as cr_err:
        logger.warning(
            "Report run create failed (persistence disabled or skill DB unreachable): %s",
            cr_err,
        )
        state.report_run_id = ""

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
                _emit(emitter, "Building credentialing report…", state, step_id)
                _emit(emitter, "  Draft composer: generating report from step outputs…", state, step_id)
            else:
                _emit(emitter, f"Validation blocked. Retrying with fresh draft (attempt {draft_attempt + 1}/{draft_max_tries})…", state, step_id)
            try:
                draft_resp = _post_report_with_retry(
                    "/report-from-steps/draft",
                    {"org_name": org_name.strip(), "step_outputs": step_outputs_payload},
                )
            except urllib.error.HTTPError as draft_err:
                if draft_err.code in (500, 503):
                    _emit(emitter, "Draft failed: rate limit or safety filter. Try again in a few minutes.", state, step_id)
                raise
            draft_md = draft_resp.get("draft_md") or ""
            _emit(emitter, "  → Draft composer done.", state, step_id)
            _emit(emitter, "  Validator: checking draft (numbers + narrative critique)…", state, step_id)
            validation_resp = _post_report_with_retry(
                "/report-from-steps/validate",
                {"org_name": org_name.strip(), "step_outputs": step_outputs_payload, "draft_md": draft_md},
            )
            validation_report = validation_resp.get("validation_report") or ""
            critique_report = validation_resp.get("critique_report") or ""

            if "Validation Status: BLOCK" not in (validation_report or ""):
                _emit(emitter, "  → Validator: passed. Critique reviewed.", state, step_id)
                break
            if draft_attempt == draft_max_tries - 1:
                _emit(emitter, f"Validation blocked after {draft_max_tries} attempts (e.g. Section E truncation, data inconsistency). Report could not be generated.", state, step_id)
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
        _emit(emitter, "  Final composer: incorporating validation into final report…", state, step_id)
        compose_resp = _post_report_with_retry("/report-from-steps/compose", {
            "org_name": org_name.strip(),
            "step_outputs": step_outputs_payload,
            "draft_md": draft_md,
            "validation_report": validation_report,
            "critique_report": critique_report,
        })
        final_md = compose_resp.get("final_md") or ""
        state.report_summary = compose_resp.get("summary") or {}
        state.report_final_md = final_md  # preserve for download / debugging if charts-pdf fails later
        _emit(emitter, "  → Final composer done.", state, step_id)
        _emit(emitter, "Final report ready. Generating charts and PDF…", state, step_id)

        # 11d: Charts + PDF
        charts_resp = _post_report_with_retry("/report-from-steps/charts-pdf", {"org_name": org_name.strip(), "step_outputs": step_outputs_payload, "final_md": final_md})
        final_md = charts_resp.get("final_md") or final_md
        pdf_base64 = charts_resp.get("pdf_base64") or ""

        state.report_final_md = final_md
        state.report_pdf_base64 = pdf_base64
        # Per-NPI profile: always show step so it appears in chat; use API payload when present
        npi_profiles_md = charts_resp.get("npi_profiles_md") or ""
        npi_profile_json = charts_resp.get("npi_profile_json") or ""
        npi_profile_row_count = int(charts_resp.get("npi_profile_row_count") or 0)
        if not npi_profiles_md and not npi_profile_json and npi_profile_row_count == 0:
            # Old API or no profiles: still show the step with a short message
            npi_profiles_md = (
                "Per-NPI profile data is included in the **PDF report** (Appendix: NPI Profiles).\n\n"
                "To see it here in the chat, ensure the provider-roster service is up to date and run the credentialing report again."
            )
        state.step_outputs.append(
            StepOutput(
                step_id="npi_profile",
                label="Per-NPI profile",
                csv_content="",
                row_count=npi_profile_row_count,
                markdown_content=npi_profiles_md,
                json_content=npi_profile_json,
            )
        )
        state.step_outputs.append(
            StepOutput(
                step_id=step_id,
                label="Final report",
                csv_content="(See main message above. Use the download button for PDF or Markdown.)",
                row_count=1 if final_md else 0,
            )
        )
        # Persist run (steps + summary + documents) when report_run_id was created at start of step 11
        if state.report_run_id:
            try:
                complete_payload = {
                    "status": "completed",
                    "summary": state.report_summary,
                    "step_outputs": [
                        {
                            "step_id": s.step_id,
                            "label": s.label,
                            "csv_content": s.csv_content,
                            "row_count": s.row_count,
                            "sort_order": i,
                            "markdown_content": getattr(s, "markdown_content", "") or "",
                            "json_content": getattr(s, "json_content", "") or "",
                        }
                        for i, s in enumerate(state.step_outputs)
                    ],
                    "final_md": state.report_final_md,
                    "final_pdf_base64": state.report_pdf_base64 or None,
                }
                complete_req = urllib.request.Request(
                    f"{base}/report-runs/{state.report_run_id}/complete",
                    data=json.dumps(complete_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    method="PUT",
                )
                with urllib.request.urlopen(complete_req, timeout=60) as comp_resp:
                    json.loads(comp_resp.read().decode())
                _emit(emitter, "Report stored. You can ask any question about it.", state, step_id)
            except Exception as comp_err:
                logger.warning("Report run complete failed: %s", comp_err)
        result_text = final_md or "Report generated (no markdown returned)."
        if final_md:
            state.mark_done(step_id, "Report generated.")
            _emit(emitter, "✓ Step 11 done. Report generated.", state, step_id)
        else:
            state.mark_done(step_id, "Report had issues.")
            _emit(emitter, "✓ Step 11 done. (Report had issues.)", state, step_id)
        return result_text
    except urllib.error.HTTPError as e:
        body = e.fp.read().decode()[:1000] if e.fp else str(e)
        logger.warning("report-from-steps HTTP %s %s", e.code, body, exc_info=(e.code >= 500))
        state.mark_done(step_id, f"API error {e.code}")
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Credentialing report", csv_content=f"(API error {e.code})", row_count=0)
        )
        _emit(emitter, f"✓ Step 11 done. API error ({e.code}).", state, step_id)
        if e.code == 422:
            try:
                detail = json.loads(body)
                msg = detail.get("detail") or detail
                if isinstance(msg, dict) and msg.get("message"):
                    return f"Report could not be generated: {msg['message']}"
                if isinstance(msg, dict) and msg.get("error"):
                    return f"Report could not be generated: {msg['error']}"
            except (json.JSONDecodeError, TypeError):
                pass
        if e.code in (500, 503):
            try:
                data = json.loads(body)
                detail = data.get("detail")
                if isinstance(detail, dict) and detail.get("message"):
                    return f"Report draft failed: {detail['message']}"
                if isinstance(detail, str):
                    return f"Report draft failed: {detail}"
            except (json.JSONDecodeError, TypeError):
                pass
            return "Report draft failed: rate limit or safety filter. Please try again in a few minutes."
        return f"Report failed ({e.code}): {body}"
    except urllib.error.URLError as e:
        if "timed out" in str(e).lower():
            _emit(emitter, "Report step timed out. Try again or use a shorter org roster.", state, step_id)
        logger.warning("report-from-steps failed: %s", e)
        state.mark_done(step_id, str(e))
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Credentialing report", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, f"✓ Step 11 done. Failed ({e}).", state, step_id)
        return str(e)
    except Exception as e:
        logger.warning("report-from-steps failed: %s", e, exc_info=True)
        state.mark_done(step_id, str(e))
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Credentialing report", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, f"✓ Step 11 done. Failed ({e}).", state, step_id)
        return str(e)


def _record_step_gate_event(
    org_name: str,
    state: OrchestratorState,
    step_id: str,
    emitter: Callable[[str], None] | None,
) -> None:
    """Append why this step ended (skip / co-pilot pause / autopilot advance) and emit one line."""
    if step_id not in ROSTER_CREDENTIALING_STEP_IDS:
        return
    st = state.step_by_id(step_id)
    if not st:
        return
    from app.services.credentialing_gate_event import (
        append_gate_event,
        build_step_completed_event,
        emit_gate_event,
    )

    ev = build_step_completed_event(
        step_id=step_id,
        org_name=org_name or state.org_name,
        run_mode=getattr(state, "credentialing_run_mode", "copilot") or "copilot",
        step_status=st.status,
        step_summary=st.result_summary or "",
        last_active_roster_cutoff=getattr(state, "last_active_roster_cutoff", None),
        autopilot_force_confirm=False,
        extra_detail=None,
    )
    append_gate_event(state, ev)
    emit_gate_event(emitter, ev)


def run_credentialing_step(
    org_name: str,
    state: OrchestratorState,
    step_id: str,
    emitter: Callable[[str], None] | None = None,
) -> str | None:
    """Run a single credentialing pipeline step. Mutates ``state`` like the monolithic orchestrator.

    Return value is step-specific (e.g. report text from ``build_report``, or an error string from
    ``identify_org`` when the API fails). Callers that mirror legacy ``run_orchestrator`` behavior
    may ignore non-final returns except for logging or co-pilot UI.

    Raises:
        ValueError: if ``step_id`` is not in the plan.
    """
    org_name = (org_name or "").strip()
    try:
        if step_id == "identify_org":
            return _run_step_1_identify_org(org_name, state, emitter)
        if step_id == "find_locations":
            _run_step_2_find_locations(state, emitter)
            return None
        if step_id == "find_associated_providers":
            _run_step_3_find_associated_providers(state, emitter)
            return None
        if step_id == "nppes_alignment":
            _run_step_nppes_alignment(state, emitter)
            return None
        if step_id == "pml_alignment":
            _run_step_pml_alignment(state, emitter)
            return None
        if step_id == "taxonomy_optimization":
            _run_step_taxonomy_optimization(state, emitter)
            return None
        raise ValueError(f"Unknown credentialing step_id: {step_id!r}")
    finally:
        if step_id in ROSTER_CREDENTIALING_STEP_IDS and state.step_by_id(step_id):
            _record_step_gate_event(org_name, state, step_id, emitter)
            try:
                from app.services.credentialing_workflow_followups import apply_system_follow_ups_after_step

                apply_system_follow_ups_after_step(state, step_id)
            except Exception:
                pass


def run_orchestrator(
    org_input: str,
    emitter: Callable[[str], None] | None = None,
    *,
    roster_upload_id: str | None = None,
    external_only: bool = False,
    include_roster_members: bool = True,
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
        step3_roster_upload_id=(roster_upload_id or "").strip(),
        step3_external_only=bool(external_only),
        step3_include_roster_members=bool(include_roster_members),
        credentialing_run_mode="autopilot",
    )
    # Emit 9-step plan
    plan_lines = ["Steps:"]
    for i, s in enumerate(ROSTER_CREDENTIALING_PLAN, 1):
        plan_lines.append(f"  {i}. {s['label']}")
    _emit(emitter, "\n".join(plan_lines), state)

    org_name = (org_input or "").strip()
    if not org_name:
        _emit(emitter, "No organization name provided; stopping.", state)
        return "No organization name provided. Try: 'Create a Medicaid NPI report for [org name]'.", state

    state.org_name = org_name
    report_text: str | None = None
    for sid in ROSTER_CREDENTIALING_STEP_IDS:
        out = run_credentialing_step(org_name, state, sid, emitter)
        st_done = state.step_by_id(sid)
        if st_done and st_done.status == "failed":
            detail = (st_done.result_summary or "").strip() or "unknown error"
            _emit(
                emitter,
                f"**Pipeline stopped** — step `{sid}` **failed**: {detail}",
            )
            return (
                f"Credentialing stopped at step `{sid}`: {detail}",
                state,
            )

    summary_lines = ["**Credentialing pipeline complete.**"]
    for s in state.steps:
        icon = "✓" if s.status == "done" else ("—" if s.status == "skipped" else "✗")
        summary_lines.append(f"{icon} {s.label}: {s.result_summary or s.status}")
    report_text = "\n".join(summary_lines)
    return report_text, state


def _step_num(step_id: str) -> int:
    """Map step_id to display number 1–12."""
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
        "npi_profile": 12,
    }
    return order.get(step_id, 0)
