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


# Plan steps: id, label (emitted to user). Execution order 1–8.
ROSTER_CREDENTIALING_PLAN = [
    {"id": "ensure_benchmarks",        "label": "Ensure revenue metrics are in place"},
    {"id": "identify_org",             "label": "Establish organization identity"},
    {"id": "find_locations",           "label": "Confirm approved service locations"},
    {"id": "nppes_alignment",          "label": "Confirm provider roster"},
    {"id": "find_associated_providers","label": "Load providers and identify compliance risks"},
    {"id": "pml_alignment",            "label": "Confirm Medicaid enrollment for each provider"},
    {"id": "taxonomy_optimization",    "label": "Ensure billing taxonomy codes are aligned"},
    {"id": "provider_summaries",       "label": "Generate AI provider credential summaries"},
    {"id": "org_summary",              "label": "Compile organization-wide credential health report"},
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
    extra_data: dict = field(default_factory=dict)


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
    # Step 3 compliance output: deduplicated, roster-filtered candidates for compliance review
    compliance_candidates: list = field(default_factory=list)
    compliance_rostered_excluded: int = 0
    # copilot: no algorithmic active panel until user validates; autopilot: full pipeline without per-step gate
    credentialing_run_mode: str = "copilot"
    last_active_roster_cutoff: int | None = None
    # Why we paused or advanced (copilot vs autopilot); capped list for API/UI
    gate_events: list[dict[str, Any]] = field(default_factory=list)
    # Per-step emit log: { step_id -> [msg, ...] } captured from _emit calls
    step_emit_log: dict[str, list[str]] = field(default_factory=dict)
    # Step 6 (taxonomy): TML-approved codes cached from PML step, per-provider inventory + analysis
    tml_codes: list[str] = field(default_factory=list)
    taxonomy_inventory: list[dict] = field(default_factory=list)   # raw NPPES per-provider taxonomy fetch
    taxonomy_analysis: list[dict] = field(default_factory=list)    # decision-tree results per provider
    # Unique run ID — set at pipeline start, used for audit event correlation
    run_id: str = ""
    # When True, the run service should skip the copilot gate and auto-advance to the next step.
    # Set by steps that complete without requiring user input (e.g. fresh-roster nppes_alignment).
    auto_advance: bool = False

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
    # Patch the running task card body so the feed shows live status
    if state is not None and step_id and getattr(state, "run_id", None):
        try:
            from app.sub_skills.task_management import patch_running_card_body
            patch_running_card_body(step_id, text, state.run_id)
        except Exception:
            pass


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


def _apply_pmr_to_reconcile(
    base: str,
    upload_id: str,
    org_name: str,
    orig_invalid: int,
    orig_review: int,
    orig_total: int,
) -> tuple[int, int, int, int, int]:
    """Apply Provider Match Rules + roster_truth to filter reconciliation results.

    Calls three skill-server endpoints to get:
      - Per-provider reconcile report (which rows are flagged)
      - Provider match rules for this org (dismiss / highlight / auto_fix)
      - Roster truth for this org (user-verified providers)

    Returns (adj_invalid, adj_review, adj_total, auto_resolved, dismissed):
      adj_*         — counts after removing dismissed/auto-resolved providers
      auto_resolved — providers found in roster_truth (already verified by user)
      dismissed     — providers matching a 'dismiss' rule (junk rows)

    Non-fatal: on any fetch/parse error returns original values unchanged.
    """
    if not base or not upload_id or not org_name:
        return orig_invalid, orig_review, orig_total, 0, 0

    def _get(url: str, timeout: int = 10) -> dict:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())

    # 1. Per-provider report
    try:
        report = _get(f"{base}/roster/reconcile/{upload_id}/report?quick=true", timeout=15)
    except Exception:
        return orig_invalid, orig_review, orig_total, 0, 0

    flagged = [
        p for p in (report.get("providers") or [])
        if p.get("status") in ("invalid", "needs_review", "parse_error")
    ]
    if not flagged:
        return orig_invalid, orig_review, orig_total, 0, 0

    # 2. Match rules
    dismiss_rules: list[tuple[str, str]] = []   # (pattern, match_mode)
    try:
        rules_data = _get(f"{base}/roster/match-rules/{urllib.parse.quote(org_name)}")
        for rule in (rules_data.get("rules") or []):
            if rule.get("rule_type") == "dismiss":
                dismiss_rules.append((rule.get("pattern", ""), rule.get("match_mode", "exact")))
    except Exception:
        pass

    # 3. Roster truth — collect verified NPIs and name keys
    truth_npis: set[str] = set()
    truth_name_keys: set[str] = set()
    try:
        truth_data = _get(f"{base}/roster/truth/{urllib.parse.quote(org_name)}")
        for t in (truth_data.get("providers") or []):
            if t.get("decision") in ("user_verified", "validated", "approved", "manual"):
                if t.get("npi_validated"):
                    truth_npis.add(t["npi_validated"].strip())
                if t.get("npi_roster"):
                    truth_npis.add(t["npi_roster"].strip())
                if t.get("provider_key"):
                    truth_name_keys.add(t["provider_key"].strip().lower())
                if t.get("provider_name"):
                    truth_name_keys.add(t["provider_name"].strip().lower())
    except Exception:
        pass

    def _matches(name: str, pattern: str, mode: str) -> bool:
        n = (name or "").lower().strip()
        p = (pattern or "").lower().strip()
        return (p in n) if mode == "contains" else (n == p)

    dismissed = 0
    auto_resolved = 0
    rem_invalid = 0
    rem_review = 0

    for p in flagged:
        name  = p.get("provider_name", "")
        lv    = p.get("latest_validation") or {}
        npi_v = (lv.get("npi_validated") or "").strip()
        npi_u = (p.get("npi_uploaded") or "").strip()
        nkey  = name.lower().strip()

        # Dismiss rule wins first
        if any(_matches(name, pat, mode) for pat, mode in dismiss_rules):
            dismissed += 1
            continue

        # Auto-resolve: provider already verified in roster_truth
        if (npi_v and npi_v in truth_npis) or \
           (npi_u and npi_u in truth_npis) or \
           (nkey and nkey in truth_name_keys):
            auto_resolved += 1
            continue

        # Still an open issue
        status = p.get("status", "")
        val_status = lv.get("validation_status", "")
        if status == "invalid" or val_status == "fail":
            rem_invalid += 1
        else:
            rem_review += 1

    # Total shrinks by dismissed (junk rows shouldn't count toward org roster size)
    adj_total = max(orig_total - dismissed, 0)
    return rem_invalid, rem_review, adj_total, auto_resolved, dismissed


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


def _task_signal(
    signal: str,
    *,
    step_id: str = "",
    state: "OrchestratorState | None" = None,
    data: dict | None = None,
    issue_code: str | None = None,
    provider_npi: str | None = None,
    provider_name: str | None = None,
    title: str | None = None,
    body: str | None = None,
    note: str | None = None,
    detail_payload: dict | None = None,
) -> None:
    """Non-fatal wrapper: emit a task-manager signal from any step."""
    try:
        from app.sub_skills.task_management import emit_signal
        emit_signal(
            signal,
            step_id=step_id,
            org=(state.org_name or "") if state else "",
            run_id=(state.run_id or None) if state else None,
            workflow="credentialing",
            source_module="credentialing",
            data=data or {},
            issue_code=issue_code,
            provider_npi=provider_npi,
            provider_name=provider_name,
            title=title,
            body=body,
            note=note,
            detail_payload=detail_payload,
        )
    except Exception as _te:
        logger.debug("_task_signal %s/%s failed (non-fatal): %s", signal, step_id, _te)


def _poll_reconciliation_status(
    base: str,
    upload_id: str,
    emitter: Callable[[str], None] | None,
    state: "OrchestratorState",
    step_id: str,
    max_wait_seconds: int = 300,
) -> dict | None:
    """Poll /roster/reconcile/{upload_id}/status until complete or timeout.

    Returns the progress dict on completion, None on timeout or error.
    """
    import time as _time
    status_url = f"{base}/roster/reconcile/{upload_id}/status"
    deadline = _time.time() + max_wait_seconds
    last_processed = -1

    while _time.time() < deadline:
        try:
            resp = urllib.request.urlopen(status_url, timeout=10)
            data = json.loads(resp.read().decode())
        except Exception as e:
            logger.debug("_poll_reconciliation_status error: %s", e)
            _time.sleep(5)
            continue

        status    = data.get("status", "")
        progress  = data.get("progress", {})
        processed = progress.get("processed", 0)
        total     = progress.get("total_providers", 0)

        if status == "not_started":
            # Trigger reconciliation if not yet started
            try:
                trigger_req = urllib.request.Request(
                    f"{base}/roster/reconcile/{upload_id}",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(trigger_req, timeout=5)
            except Exception:
                pass
            _task_signal("info", step_id=step_id, state=state,
                         title="NPPES validation started",
                         body=f"Validating {total or '?'} providers against the NPPES registry…")
            last_processed = processed
        elif processed != last_processed and total:
            # Card for each meaningful progress milestone (every ~25%)
            pct = int(processed / total * 100) if total else 0
            if pct % 25 == 0 or processed == total:
                _task_signal("info", step_id=step_id, state=state,
                             title=f"NPPES validation: {processed}/{total} checked",
                             body=f"{pct}% complete…")
            last_processed = processed

        if status in ("completed", "done", "complete"):
            return progress
        if status in ("not_found", "error", "failed"):
            logger.warning("Reconciliation status=%s for upload_id=%s", status, upload_id)
            return progress if progress.get("total_providers") else None

        _time.sleep(3)

    logger.warning("_poll_reconciliation_status timeout after %ds for %s", max_wait_seconds, upload_id)
    return None


def _run_step_0_ensure_benchmarks(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> None:
    """Step 1: Ensure taxonomy_utilization_benchmarks table is populated (utilization benchmarking)."""
    step_id = "ensure_benchmarks"
    state.mark_in_progress(step_id)
    _emit(emitter, "I am ensuring the revenue metrics are in place…", state, step_id)
    _task_signal("step_start", step_id=step_id, state=state)
    base = _provider_roster_base_url()
    if not base:
        state.mark_skipped(step_id, "Provider-roster API not configured.")
        _emit(emitter, "✓ Step 1 skipped. API not configured.", state, step_id)
        _task_signal("blocker", step_id=step_id, state=state,
                     title="Revenue benchmarks unavailable — API not configured",
                     body="The provider-roster API URL is not set. "
                          "Set CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL to enable this step. "
                          "Downstream steps that depend on benchmarks may be incomplete.",
                     issue_code="config_missing")
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
            _task_signal("step_done", step_id=step_id, state=state, detail_payload={
                "headers": ["Table", "Action", "Period", "State"],
                "rows": [[
                    (data.get("table") or "taxonomy_utilization_benchmarks").split(".")[-1].strip("`"),
                    "skipped (already exists)" if data.get("skipped") else "created",
                    "2024",
                    "FL",
                ]],
            })
        else:
            err = (data.get("error") or status or "unknown error").strip()
            state.mark_failed(step_id, f"Benchmarks not available: {err}")
            _emit(emitter, f"✗ Step 1 failed. {err}. Stopping pipeline.", state, step_id)
            _task_signal("step_failed", step_id=step_id, state=state, note=err)
    except Exception as e:
        logger.warning("ensure_benchmarks failed: %s", e)
        state.mark_failed(step_id, str(e))
        _emit(emitter, f"✗ Step 1 failed ({e}). Stopping pipeline.", state, step_id)
        _task_signal("step_failed", step_id=step_id, state=state, note=str(e))


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
    _task_signal("step_start", step_id=step_id, state=state)
    base = _provider_roster_base_url()
    if not base:
        state.mark_skipped(step_id, "Provider-roster API not configured.")
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Organization NPIs", csv_content="(API not configured)", row_count=0)
        )
        _emit(emitter, f"✓ Step {step_num} skipped. API not configured.", state, step_id)
        _task_signal("blocker", step_id=step_id, state=state,
                     title="Cannot identify organization — API not configured",
                     body="The provider-roster API URL is not set. "
                          "Set CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL. "
                          "All downstream steps (locations, providers, licenses) will also be skipped.",
                     issue_code="config_missing")
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
            _task_signal("step_failed", step_id=step_id, state=state, note=f"HTTP {e.code}")
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
        _task_signal("step_failed", step_id=step_id, state=state, note=reason)
        return reason

    results = data.get("results") or []
    npis = list(dict.fromkeys(str(r.get("npi", "")).strip() for r in results if r.get("npi")))
    state.org_npis = npis
    # Step output: rich CSV (npi, name, entity_type, source, taxonomy_code) for validation
    org_cols = ["npi", "name", "entity_type", "source", "taxonomy_code"]
    org_rows = []
    type1_npis = []
    for r in results:
        n = str(r.get("npi", "")).strip()
        if not n:
            continue
        etype = (r.get("entity_type") or "").strip()
        org_rows.append({
            "npi": n,
            "name": (r.get("name") or "").strip()[:80],
            "entity_type": etype,
            "source": (r.get("source") or "").strip(),
            "taxonomy_code": (r.get("taxonomy_code") or "").strip() or "",
        })
        if etype in ("1", "Type 1", "NPI-1"):
            type1_npis.append({"npi": n, "name": (r.get("name") or "").strip()[:80]})
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
        _task_signal("step_done", step_id=step_id, state=state, detail_payload={
            "headers": ["NPI", "Name", "Entity Type", "Source", "Taxonomy Code"],
            "rows": [[r.get("npi",""), r.get("name",""), r.get("entity_type",""), r.get("source",""), r.get("taxonomy_code","")] for r in org_rows[:50]],
        })
        type1_count = len(type1_npis)
        type2_count = len(npis) - type1_count

        # ── Search provenance — how we got here ──
        sources: dict[str, int] = {}
        for r in org_rows:
            src = (r.get("source") or "NPPES").strip()
            sources[src] = sources.get(src, 0) + 1
        src_parts = [f"{v} from {k}" for k, v in sorted(sources.items())]
        src_summary = ", ".join(src_parts) if src_parts else "NPPES registry"
        _task_signal(
            "insight",
            step_id=step_id,
            state=state,
            title=f"How we identified {org_name}",
            body=(
                f"Queried the provider-roster API with name <strong>'{org_name}'</strong>, "
                f"state=FL, limit=20. "
                f"Registry returned {len(results)} match(es): "
                f"{type2_count} Type 2 org NPI(s) and {type1_count} Type 1 individual NPI(s). "
                f"Sources: {src_summary}."
            ),
        )

        # ── Anomaly insights ──
        names = list({r.get("name","").strip() for r in org_rows if r.get("name","").strip()})
        if len(names) > 1:
            name_list = ", ".join(f'"{n}"' for n in names[:5])
            _task_signal(
                "insight",
                step_id=step_id,
                state=state,
                title=f"Multiple legal names found for {org_name}",
                body=f"NPIs returned {len(names)} distinct entity names: {name_list}. "
                     "These may be subsidiaries, DBAs, or legacy legal names. "
                     "Confirm only the NPIs that belong to this credentialing entity.",
            )
        for t1 in type1_npis:
            _task_signal(
                "insight",
                step_id=step_id,
                state=state,
                title=f"Individual NPI in results — verify ownership",
                body=f"NPI {t1['npi']} ({t1['name']}) is a Type 1 individual provider. "
                     "Org credentialing typically uses Type 2 NPIs only.",
                provider_npi=t1["npi"],
                provider_name=t1["name"],
                issue_code="type1_npi_in_org_results",
            )

        # ── Task feed: decision card (copilot) or autonomous (autopilot) ──
        is_copilot = (getattr(state, "credentialing_run_mode", "") or "").strip().lower() != "autopilot"
        if is_copilot:
            # Agent rationale — what the agent would decide and why
            facts: list[str] = []
            concerns: list[str] = []
            if type2_count > 0:
                facts.append(f"{type2_count} Type 2 (organization) NPI(s) found — correct type for org credentialing")
            if type1_count == 0:
                facts.append("No individual (Type 1) NPIs in results — clean set")
            else:
                concerns.append(
                    f"{type1_count} Type 1 individual NPI(s) detected — agent would exclude these; "
                    "uncheck any that aren't part of this org's billing identity"
                )
            if len(names) == 1:
                facts.append(f"Single legal name '{names[0]}' — no identity ambiguity")
            elif len(names) > 1:
                concerns.append(
                    f"{len(names)} distinct legal names — verify which NPIs belong to this credentialing entity"
                )
            confidence = "high" if not concerns else "medium"
            rec_text = (
                f"Approve {type2_count} Type 2 NPI(s)"
                + (f", exclude {type1_count} Type 1 individual NPI(s)" if type1_count else "")
            )
            _task_signal(
                "insight",
                step_id=step_id,
                state=state,
                title="Agent assessment — org identity",
                body="",
                issue_code="decision_rationale",
                detail_payload={
                    "issue_code":     "decision_rationale",
                    "recommendation": rec_text,
                    "confidence":     confidence,
                    "facts":          facts,
                    "concerns":       concerns,
                    "user_action":    (
                        "Uncheck any NPIs that don't belong to this org, then click Approve. "
                        "You can also add missing NPIs using '+ Add row' below."
                    ),
                },
            )
            _task_signal(
                "decision",
                step_id=step_id,
                state=state,
                title="Confirm org NPIs before proceeding",
                body=f"Found {len(npis)} NPI(s). Select which belong to this organization — "
                     "only confirmed NPIs will be used downstream.",
                detail_payload={
                    "rows": [{"npi": r["npi"], "name": r["name"], "entity_type": r["entity_type"], "source": r["source"]} for r in org_rows[:50]],
                },
            )
        else:
            _task_signal(
                "autonomous",
                step_id=step_id,
                state=state,
                title=f"Auto-selected {len(npis)} org NPI(s)",
                body=f"Autopilot selected {type2_count} Type 2 NPI(s)."
                     + (f" Excluded {type1_count} Type 1 individual NPI(s)." if type1_count else ""),
            )
    else:
        state.mark_failed(step_id, "No organization NPI matches from search/org-names.")
        _emit(emitter, f"✗ Step {step_num} failed. No registry matches — refine org name or check state. Stopping pipeline.", state, step_id)
        _task_signal(
            "blocker",
            step_id=step_id,
            state=state,
            title="No org NPIs found — action required",
            body=f"NPPES search returned no results for '{org_name}' in FL. "
                 "Try a different name or enter an NPI directly.",
            issue_code="no_org_npis_found",
        )
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
    _task_signal("step_start", step_id=step_id, state=state,
                 data={"total": len(state.org_npis or []), "unit": "org NPIs"})
    base = _provider_roster_base_url()
    if not base or not state.org_npis:
        if not base:
            skip_title = "Practice locations skipped — API not configured"
            skip_body  = ("The provider-roster API URL is not set. "
                          "Set CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL to enable location lookup. "
                          "Provider and license steps will also be skipped.")
        else:
            skip_title = "Practice locations skipped — no org NPIs from previous step"
            skip_body  = ("The organization identity step returned no NPIs, so there is nothing to look up locations for. "
                          "Check that the org name is spelled correctly or re-run the organization step.")
        state.mark_skipped(step_id, skip_title)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Practice locations", csv_content="(skipped)", row_count=0)
        )
        _emit(emitter, f"✓ Step {step_num} skipped. {skip_title}", state, step_id)
        _task_signal("blocker", step_id=step_id, state=state,
                     title=skip_title, body=skip_body,
                     issue_code="config_missing" if not base else "upstream_error")
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
        _task_signal("step_done", step_id=step_id, state=state, detail_payload={
            "headers": ["NPI", "Address", "City", "State", "ZIP"],
            "rows": [[r.get("npi",""), r.get("site_address",""), r.get("site_city",""), r.get("site_state",""), r.get("site_zip","")] for r in loc_rows[:50]],
        })
        # ── Task feed: insights only for genuine anomalies ──
        cities: dict[str, int] = {}
        states: set[str] = set()
        po_box_locs = []
        for loc in loc_rows:
            c = loc.get("site_city") or "Unknown"
            cities[c] = cities.get(c, 0) + 1
            if loc.get("site_state"):
                states.add(loc["site_state"].strip().upper())
            addr = (loc.get("site_address") or "").upper()
            if "PO BOX" in addr or "P.O. BOX" in addr or "P O BOX" in addr:
                po_box_locs.append(loc)

        # Multi-state: unusual — may indicate out-of-state sites or data error
        if len(states) > 1:
            state_list = ", ".join(sorted(states))
            _task_signal(
                "insight",
                step_id=step_id,
                state=state,
                title=f"Locations span multiple states: {state_list}",
                body=f"Practice sites were found across {len(states)} states ({state_list}). "
                     "Confirm that out-of-state sites belong to this credentialing entity "
                     "and are in scope for this run.",
            )

        # PO Box addresses: these are not valid clinical service locations
        if po_box_locs:
            po_npis = ", ".join(str(l.get("npi","?")) for l in po_box_locs[:3])
            _task_signal(
                "insight",
                step_id=step_id,
                state=state,
                title=f"{len(po_box_locs)} PO Box address(es) found — not valid service locations",
                body=f"NPI(s) {po_npis} have PO Box addresses registered in NPPES. "
                     "These cannot be used as clinical service locations. "
                     "Exclude them or update the NPPES record with a physical address.",
            )
        # ── Task feed: decision (copilot) or autonomous (autopilot) ──
        is_copilot = (getattr(state, "credentialing_run_mode", "") or "").strip().lower() != "autopilot"
        if is_copilot:
            # Agent rationale for location approval
            loc_facts: list[str] = []
            loc_concerns: list[str] = []
            loc_facts.append(
                f"{len(locations)} practice site(s) sourced from NPPES servicing address data"
            )
            if len(states) == 1:
                loc_facts.append(
                    f"All sites in {next(iter(states))} — consistent with org's registered state"
                )
            else:
                loc_concerns.append(
                    f"Sites span {len(states)} state(s) ({', '.join(sorted(states))}) — "
                    "confirm out-of-state sites are in scope for this credentialing run"
                )
            if po_box_locs:
                loc_concerns.append(
                    f"{len(po_box_locs)} PO Box address(es) detected — not valid clinical service "
                    "locations; agent would recommend removing these"
                )
            else:
                loc_facts.append("No PO Box addresses — all sites have physical addresses")

            loc_confidence = "high" if not loc_concerns else "medium"
            approvable = len(locations) - len(po_box_locs)
            loc_rec = (
                f"Approve {approvable} of {len(locations)} location(s)"
                + (f" (remove {len(po_box_locs)} PO Box site(s))" if po_box_locs else "")
            )
            _task_signal(
                "insight",
                step_id=step_id,
                state=state,
                title="Agent assessment — practice locations",
                body="",
                issue_code="decision_rationale",
                detail_payload={
                    "issue_code":     "decision_rationale",
                    "recommendation": loc_rec,
                    "confidence":     loc_confidence,
                    "facts":          loc_facts,
                    "concerns":       loc_concerns,
                    "user_action":    (
                        "Uncheck any closed, merged, or out-of-scope sites. "
                        "You can also edit addresses or add missing locations using '+ Add row'."
                    ),
                },
            )
            _task_signal(
                "decision",
                step_id=step_id,
                state=state,
                title="Confirm practice locations before proceeding",
                body=f"These {len(locations)} site(s) will anchor the provider association step. "
                     "Remove any that are closed, merged, or not part of this run.",
                detail_payload={
                    "rows": [
                        {
                            "location_id": r.get("location_id", ""),
                            "npi":         r.get("npi", ""),
                            "site_address": r.get("site_address", ""),
                            "site_city":   r.get("site_city", ""),
                            "site_state":  r.get("site_state", ""),
                            "site_zip":    r.get("site_zip", ""),
                            "why_listed":  r.get("why_listed", ""),
                        }
                        for r in loc_rows[:50]
                    ],
                },
            )
        else:
            _task_signal(
                "autonomous",
                step_id=step_id,
                state=state,
                title=f"Auto-confirmed {len(locations)} practice site(s)",
                body="All NPPES-sourced locations included automatically.",
            )
        return json.dumps({"locations": locations, "count": len(locations)})
    except urllib.error.HTTPError as e:
        body = e.fp.read().decode()[:300] if e.fp else str(e)
        logger.warning("find_locations HTTP %s %s", e.code, body)
        state.mark_done(step_id, f"API error: {e.code}")
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Practice locations", csv_content=f"(API error {e.code})", row_count=0)
        )
        _emit(emitter, f"✓ Step {step_num} done. API error ({e.code}). Continuing.", state, step_id)
        _task_signal("paused", step_id=step_id, state=state,
                     note=f"Location API error {e.code} — pipeline continuing without full location data.")
        return ""
    except Exception as e:
        logger.warning("find_locations failed: %s", e)
        state.mark_done(step_id, str(e))
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Practice locations", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, f"✓ Step {step_num} done. Failed. Continuing.", state, step_id)
        _task_signal("paused", step_id=step_id, state=state, note=str(e))
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
    _task_signal("step_start", step_id=step_id, state=state,
                 data={"total": len(state.locations or []), "unit": "locations"})
    base = _provider_roster_base_url()
    if not base or not state.org_npis or not state.locations:
        if not base:
            skip_title = "Provider lookup skipped — API not configured"
            skip_body  = ("The provider-roster API URL is not set. "
                          "Set CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL. "
                          "License validation and scoring will also be skipped.")
        elif not state.org_npis:
            skip_title = "Provider lookup skipped — no org NPIs available"
            skip_body  = ("The organization identity step produced no NPIs. "
                          "Re-run the organization step or verify the org name before retrying.")
        else:
            skip_title = "Provider lookup skipped — no practice locations available"
            skip_body  = ("The location step produced no sites. "
                          "Confirm that practice locations were approved correctly, then re-run.")
        state.mark_skipped(step_id, skip_title)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Associated providers", csv_content="(skipped)", row_count=0)
        )
        _emit(emitter, f"✓ Step {step_num} skipped. {skip_title}", state, step_id)
        _task_signal("blocker", step_id=step_id, state=state,
                     title=skip_title, body=skip_body,
                     issue_code="config_missing" if not base else "upstream_error")
        return ""
    # ── Poll NPPES reconciliation results before loading providers ────────────────
    uid = (state.step3_roster_upload_id or "").strip()
    # Hoisted — populated inside the recon block if a roster upload is present
    nppes_invalid: int = 0
    nppes_needs_review: int = 0
    nppes_total: int = 0
    if uid:
        _task_signal("info", step_id=step_id, state=state,
                     title="Running NPPES provider validation",
                     body=f"Checking roster against the NPPES registry (upload: {uid[:8]}…)")
        recon_status = _poll_reconciliation_status(base, uid, emitter, state, step_id)
        if recon_status:
            validated    = recon_status.get("validated", 0)
            invalid      = recon_status.get("invalid", 0)
            needs_review = recon_status.get("needs_review", 0)
            total        = recon_status.get("total_providers", 0)
            # Hoist to function scope for quality gate below
            nppes_invalid      = invalid
            nppes_needs_review = needs_review
            nppes_total        = total

            # ── PMR gate: apply match rules + roster_truth to filter known-good rows ──
            _org = (state.org_name or "").strip()
            if _org and (invalid > 0 or needs_review > 0):
                _adj_inv, _adj_rev, _adj_tot, _auto_res, _dismissed = _apply_pmr_to_reconcile(
                    base, uid, _org, invalid, needs_review, total
                )
                _pmr_filtered = _auto_res + _dismissed
                if _pmr_filtered > 0:
                    _task_signal("info", step_id=step_id, state=state,
                                 title=f"PMR gate — {_pmr_filtered} provider(s) resolved automatically",
                                 body=(
                                     (f"{_auto_res} resolved from your truth record · " if _auto_res else "") +
                                     (f"{_dismissed} dismissed by your rules" if _dismissed else "")
                                 ).strip(" ·"))
                    nppes_invalid      = _adj_inv
                    nppes_needs_review = _adj_rev
                    nppes_total        = _adj_tot

            _task_signal("insight", step_id=step_id, state=state,
                         title="NPPES validation complete",
                         body=(f"Checked your uploaded roster of <strong>{total}</strong> providers against NPPES: "
                               f"<strong>{validated}</strong> verified · "
                               f"<strong>{needs_review}</strong> need review · "
                               f"<strong>{invalid}</strong> not found."))
            if nppes_invalid or nppes_needs_review:
                _task_signal("insight", step_id=step_id, state=state,
                             title="NPPES issues flagged",
                             body=(f"From your uploaded file of {nppes_total} providers "
                                   f"(after applying your match rules): "
                                   f"{nppes_invalid} could not be found in NPPES, "
                                   f"{nppes_needs_review} have name mismatches or inactive status. "
                                   "Review the provider roster tab for details."))
        else:
            _task_signal("info", step_id=step_id, state=state,
                         title="NPPES validation still running",
                         body="Proceeding with available data — validation results will appear when complete.")

    url = f"{base}/find-associated-providers"
    rr = "autopilot" if (getattr(state, "credentialing_run_mode", "") or "").strip().lower() == "autopilot" else "copilot"
    n_locs = len(state.locations or [])
    _task_signal("info", step_id=step_id, state=state,
                 title="Searching NPPES by practice address",
                 body=(f"Querying NPPES for all individual providers associated with "
                       f"<strong>{n_locs}</strong> practice location(s) for {state.org_name or 'this org'}. "
                       f"This cross-references physical addresses to identify who is actively filing at each site."))
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
        _task_signal("info", step_id=step_id, state=state,
                     title="Matching location results to uploaded roster",
                     body=(f"Will cross-check location-discovered providers against your uploaded roster "
                           f"(upload ID: {uid[:8]}…) by NPI to identify who is on your roster vs external-only."))
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
        # Compliance candidates (deduped, roster-filtered) from the skill API
        state.compliance_candidates = data.get("compliance_candidates") or []
        state.compliance_rostered_excluded = int(data.get("compliance_rostered_excluded") or 0)
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
        _task_signal("step_done", step_id=step_id, state=state, detail_payload={
            "headers": ["NPI", "Name", "Specialty", "Location", "Roster Status"],
            "rows": [[r.get("npi",""), r.get("name",""), r.get("primary_taxonomy",""), r.get("location_id",""), r.get("roster_status","")] for r in prov_rows[:100]],
        })
        # ── Task feed: insight card (provider pool — permanent record) ──
        rostered = sum(
            1 for rows in associated.values() for p in rows
            if (p.get("roster_status") or "") not in ("external_only", "not_on_roster")
        )
        external_only = total - rostered
        loc_summary = [
            {"location": (location_details.get(lid) or {}).get("location_address", lid),
             "providers": len(prows),
             "external_only": sum(1 for p in prows if (p.get("roster_status") or "") in ("external_only", "not_on_roster"))}
            for lid, prows in associated.items()
        ]
        # ── Thinking log: what did we find and what does it mean ──
        if total > 0:
            _task_signal("info", step_id=step_id, state=state,
                         title=f"Address search complete — {total} provider(s) found at {len(associated)} location(s)",
                         body=(f"NPPES returned <strong>{total}</strong> individual providers associated with your practice addresses. "
                               f"<strong>{rostered}</strong> matched your uploaded roster by NPI "
                               f"({'exact match — these are confirmed roster members' if rostered > 0 else 'no overlap found — your uploaded file will be used directly'}). "
                               f"<strong>{external_only}</strong> appear at your locations but are not on your roster "
                               f"({'— flagged for ghost billing review' if external_only > 0 else ''})."))
        else:
            _task_signal("info", step_id=step_id, state=state,
                         title="No providers found at practice addresses via NPPES",
                         body=("The address-based NPPES search returned no results. "
                               "This can happen when practice addresses are not registered in NPPES or use a different format. "
                               f"{'Your uploaded roster will be used as the active panel.' if uid else 'No roster is available — downstream steps may be limited.'}"))
        if uid and rostered == 0 and total > 0:
            _task_signal("info", step_id=step_id, state=state,
                         title="Roster matching: no NPI overlap with location results",
                         body=("The providers found via address search share no NPIs with your uploaded roster. "
                               "This is common when providers list individual practice addresses separately from group affiliations, "
                               "or when the roster uses organizational NPIs. "
                               "Your uploaded roster file will be used as the definitive active panel."))
        _task_signal(
            "insight",
            step_id=step_id,
            state=state,
            title=f"{total} provider(s) found across {len(associated)} location(s)",
            body=(f"{rostered} on your roster · {external_only} external-only · "
                  f"{'uploaded roster will be used as active panel' if rostered == 0 and uid else f'{rostered} confirmed roster members set as active panel'}"),
            data={
                "detail_payload": {
                    "locations": loc_summary,
                    "summary": {
                        "total": total,
                        "rostered": rostered,
                        "external_only": external_only,
                    },
                },
                "count": total,
            },
        )
        # ── Task feed: ghost billing insight (if compliance candidates present) ──
        candidates = state.compliance_candidates or []
        if candidates:
            cand_rows = [
                {
                    "npi": c.get("npi", ""),
                    "name": (c.get("provider_name") or c.get("name") or ""),
                    "likelihood": c.get("score") or c.get("association_likelihood") or "",
                    "association_type": c.get("association_type", ""),
                }
                for c in candidates[:20]
            ]
            _task_signal(
                "insight",
                step_id=step_id,
                state=state,
                title=f"{len(candidates)} external provider(s) flagged for review",
                body="These providers appear at your locations in NPPES but are not on your roster. "
                     "They may represent ghost billing risk.",
                issue_code="ghost_billing_candidates",
                data={"detail_payload": {"rows": cand_rows}},
            )
        # ── Mobius confidence score ──────────────────────────────────────────
        HIGH_LIKELIHOOD = {"high", "very_high"}
        HIGH_MATCH_TYPES = {"address_match", "billing_match", "perfect_match", "address_billing_match"}
        recon_uid = (state.step3_roster_upload_id or "").strip()

        if rostered > 0:
            def _is_high_conf(p: dict) -> bool:
                """Handle both string ('high','very_high') and numeric (>=80) association_likelihood."""
                if (p.get("roster_status") or "") in ("external_only", "not_on_roster"):
                    return False
                likelihood = p.get("association_likelihood")
                if isinstance(likelihood, (int, float)):
                    likelihood_ok = likelihood >= 80
                else:
                    likelihood_ok = str(likelihood or "").lower() in HIGH_LIKELIHOOD
                match_type = str(p.get("match_type") or "").lower()
                return likelihood_ok or match_type in HIGH_MATCH_TYPES

            high_conf_count = sum(
                1 for rows in associated.values() for p in rows
                if _is_high_conf(p)
            )
            confidence_pct = round(high_conf_count / rostered * 100, 1)
            unconfirmed = rostered - high_conf_count
        elif recon_uid:
            # Uploaded roster is authoritative — 100% confidence by definition
            high_conf_count = 0
            confidence_pct = 100.0
            unconfirmed = 0
        else:
            high_conf_count = 0
            confidence_pct = 0.0
            unconfirmed = 0

        mobius_confident = confidence_pct >= 95.0

        # ── NPPES quality gate ─────────────────────────────────────────────────
        # Separate from panel-matching confidence: measures what % of the uploaded
        # roster actually validates cleanly in NPPES (not found + name mismatch counted as failures).
        NPPES_QUALITY_THRESHOLD = 95.0
        nppes_quality_pct: float | None = None
        if nppes_total > 0:
            _clean = nppes_total - nppes_invalid - nppes_needs_review
            nppes_quality_pct = round(_clean / nppes_total * 100, 1)
        quality_ok = nppes_quality_pct is None or nppes_quality_pct >= NPPES_QUALITY_THRESHOLD

        # Build Mobius recommendation to embed in the decision card
        if not quality_ok:
            assert nppes_quality_pct is not None
            _clean = nppes_total - nppes_invalid - nppes_needs_review
            rec_text = (f"Stop and fix roster — only {_clean} of {nppes_total} providers "
                        f"pass NPPES validation ({nppes_quality_pct:.0f}%)")
            rec_confidence = "high"
            rec_facts: list[str] = [f"{_clean} of {nppes_total} providers validated in NPPES"]
            rec_concerns: list[str] = []
            if nppes_invalid > 0:
                rec_concerns.append(
                    f"{nppes_invalid} providers not found in NPPES — cannot be credentialed")
            if nppes_needs_review > 0:
                rec_concerns.append(
                    f"{nppes_needs_review} have name mismatches or inactive status — need correction")
            rec_user_action = "Fix flagged providers in the roster tab, then re-run credentialing."
            rec_stop_and_fix = True
        else:
            rec_text = "Confirm active panel"
            rec_confidence = "high"
            rec_facts = []
            if nppes_quality_pct is not None:
                rec_facts.append(f"{nppes_quality_pct:.0f}% of providers pass NPPES validation")
            if rostered == 0 and recon_uid:
                rec_facts.append("Uploaded roster used as active panel — no NPI overlap with address search")
            elif mobius_confident:
                rec_facts.append(f"{confidence_pct:.0f}% matched with high confidence")
            rec_concerns = []
            rec_user_action = ""
            rec_stop_and_fix = False

        # ── Provider breakdown insight card ──────────────────────────────────
        med_low_count = rostered - high_conf_count if rostered > 0 else 0
        if rostered > 0 or external_only > 0:
            breakdown_parts = []
            if high_conf_count > 0:
                breakdown_parts.append(f"<strong>{high_conf_count}</strong> matched with high confidence (address or billing match)")
            if med_low_count > 0:
                breakdown_parts.append(f"<strong>{med_low_count}</strong> matched with medium/low confidence (weak signal)")
            if unconfirmed > 0:
                breakdown_parts.append(f"<strong>{unconfirmed}</strong> roster providers could <em>not</em> be confirmed at the listed locations")
            if external_only > 0:
                breakdown_parts.append(f"<strong>{external_only}</strong> appear at locations but are not on your roster (ghost billing risk)")
            breakdown_body = ". ".join(breakdown_parts) + "."
            _task_signal("insight", step_id=step_id, state=state,
                         title=f"Provider identification breakdown — {confidence_pct:.0f}% confidence",
                         body=breakdown_body,
                         data={"detail_payload": {
                             "confidence_pct": confidence_pct,
                             "high_conf": high_conf_count,
                             "med_low": med_low_count,
                             "unconfirmed": unconfirmed,
                             "external_only": external_only,
                         }})

        # ── Task feed: decision (copilot) or autonomous (autopilot) ──
        is_copilot = (getattr(state, "credentialing_run_mode", "") or "").strip().lower() != "autopilot"
        active_count = sum(len(v) for v in (state.active_roster or {}).values()) if isinstance(state.active_roster, dict) else 0
        if is_copilot:
            if rostered == 0 and recon_uid:
                panel_body = (
                    f"NPPES address search at your <strong>{len(associated)}</strong> location(s) "
                    f"found <strong>{total}</strong> provider(s), but none shared NPIs with your uploaded roster — "
                    "this is expected when your roster uses different NPI formats or site affiliations differ. "
                    "Your uploaded roster will be used as the active panel for all downstream checks."
                )
                decision_title = "Confirm active provider panel"
            elif mobius_confident:
                panel_body = (
                    f"<strong>{rostered}</strong> providers from your uploaded roster confirmed across "
                    f"<strong>{len(associated)}</strong> location(s) — "
                    f"<strong>{confidence_pct:.0f}%</strong> matched with high confidence. "
                    "Confirming sets the active panel for NPPES, Medicaid enrollment, and taxonomy checks."
                )
                if unconfirmed > 0:
                    panel_body += f" {unconfirmed} provider(s) could not be confirmed at the listed locations — review the breakdown above."
                decision_title = f"Confirm active provider panel — {confidence_pct:.0f}% matched"
            else:
                panel_body = (
                    f"⚠ Mobius could only confidently match <strong>{confidence_pct:.0f}%</strong> of your roster "
                    f"(<strong>{unconfirmed}</strong> of {rostered} providers could not be reliably identified "
                    f"at the listed locations). Proceeding may lead to misleading credential analysis. "
                    "Review the identification breakdown above before confirming."
                )
                decision_title = f"⚠ Review required — only {confidence_pct:.0f}% matched with confidence"
            _task_signal(
                "decision",
                step_id=step_id,
                state=state,
                title=decision_title,
                body=panel_body,
                detail_payload={
                    "roster_confirmation": True,
                    "total": total,
                    "rostered": rostered,
                    "external_only": external_only,
                    "locations": loc_summary,
                    "upload_id": recon_uid,
                    "confidence_pct": confidence_pct,
                    "mobius_confident": mobius_confident,
                    "unconfirmed": unconfirmed,
                    # Mobius recommendation — read by the decision card UI
                    "recommendation": rec_text,
                    "confidence": rec_confidence,
                    "facts": rec_facts,
                    "concerns": rec_concerns,
                    "user_action": rec_user_action,
                    "stop_and_fix": rec_stop_and_fix,
                    "nppes_quality_pct": nppes_quality_pct,
                    "nppes_invalid": nppes_invalid,
                    "nppes_needs_review": nppes_needs_review,
                    "nppes_total": nppes_total,
                },
            )
        else:
            _task_signal(
                "autonomous",
                step_id=step_id,
                state=state,
                title=f"Auto-selected {active_count or rostered} provider(s) for active roster",
                body=f"Autopilot used roster cutoff to select active panel. {external_only} external-only provider(s) excluded.",
            )
        return json.dumps({"associated_providers": associated, "providers_count": total})
    except urllib.error.HTTPError as e:
        body = e.fp.read().decode()[:300] if e.fp else str(e)
        logger.warning("find_associated_providers HTTP %s %s", e.code, body)
        state.mark_done(step_id, f"API error: {e.code}")
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Associated providers", csv_content=f"(API error {e.code})", row_count=0)
        )
        _emit(emitter, f"✓ Step {step_num} done. API error ({e.code}). Continuing.", state, step_id)
        _task_signal("paused", step_id=step_id, state=state,
                     note=f"Provider association API error {e.code} — pipeline continuing.")
        return ""
    except Exception as e:
        logger.warning("find_associated_providers failed: %s", e)
        state.mark_done(step_id, str(e))
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Associated providers", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, f"✓ Step {step_num} done. Failed. Continuing.", state, step_id)
        _task_signal("paused", step_id=step_id, state=state, note=str(e))
        return ""


def _run_step_nppes_alignment(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> None:
    """Step 4: Roster gate — confirm or upload provider roster before loading providers and PML validation."""
    step_id = "nppes_alignment"
    step_num = _step_num(step_id)
    state.mark_in_progress(step_id)
    _emit(emitter, "Checking provider roster…", state, step_id)
    _task_signal("step_start", step_id=step_id, state=state)

    existing_upload_id = (state.step3_roster_upload_id or "").strip()
    has_existing = bool(existing_upload_id)
    is_copilot = (getattr(state, "credentialing_run_mode", "") or "").strip().lower() != "autopilot"

    # Try to get metadata about the existing upload from the skill server
    upload_row_count: str | int = "?"
    upload_date: str = ""
    if has_existing:
        base = _provider_roster_base_url()
        if base:
            try:
                meta_url = f"{base}/roster-uploads/{existing_upload_id}"
                meta_resp = urllib.request.urlopen(meta_url, timeout=5)
                meta = json.loads(meta_resp.read().decode())
                upload_row_count = meta.get("row_count") or meta.get("provider_count") or 0
                raw_date = meta.get("created_at") or meta.get("uploaded_at") or ""
                upload_date = raw_date[:10] if raw_date else ""
            except Exception:
                pass
            # row_count is hardcoded 0 in the legacy endpoint — try reconciliation status for real count
            if not upload_row_count:
                try:
                    recon_url = f"{base}/roster/reconcile/{existing_upload_id}/status"
                    recon_resp = urllib.request.urlopen(recon_url, timeout=5)
                    recon = json.loads(recon_resp.read().decode())
                    upload_row_count = recon.get("progress", {}).get("total_providers") or "?"
                except Exception:
                    upload_row_count = "?"

    # ── Staleness check ──────────────────────────────────────────────────────
    upload_age_days: int | None = None
    staleness_level = "fresh"   # fresh | aging | stale
    if upload_date:
        try:
            from datetime import date as _date, datetime as _dt
            parsed = _dt.strptime(upload_date, "%Y-%m-%d").date()
            upload_age_days = (_date.today() - parsed).days
            if upload_age_days > 60:
                staleness_level = "stale"
            elif upload_age_days > 30:
                staleness_level = "aging"
        except Exception:
            pass

    if is_copilot:
        if has_existing and staleness_level == "fresh":
            # Fresh roster — auto-proceed, no decision needed, no copilot gate pause
            age_label = f"{upload_age_days} days old" if upload_age_days is not None else "recently uploaded"
            summary = f"Using current roster ({upload_row_count} providers, uploaded {upload_date or 'recently'}, {age_label})."
            state.step3_roster_upload_id = existing_upload_id
            state.mark_done(step_id, summary)
            state.auto_advance = True  # skip copilot gate — no user input needed for fresh roster
            state.step_outputs.append(
                StepOutput(step_id=step_id, label="Provider Roster", csv_content="", row_count=0,
                           markdown_content=summary)
            )
            _task_signal("step_done", step_id=step_id, state=state,
                         detail_payload={"summary": summary, "upload_id": existing_upload_id,
                                         "row_count": upload_row_count, "upload_date": upload_date})
            _emit(emitter, f"✓ Step {step_num} done. {summary}", state, step_id)

        elif has_existing:
            # Aging or stale — stop and require confirmation
            if staleness_level == "stale":
                freshness_body = (
                    f"Roster was uploaded <strong>{upload_age_days} days ago</strong>. "
                    "Mobius is not confident this reflects current staffing — a roster this old may produce "
                    "inaccurate credential and billing analysis. Re-uploading is strongly recommended."
                )
                title = "⚠ Roster is stale — re-upload recommended"
                body  = (f"Last roster: <strong>{upload_row_count} providers</strong>, uploaded {upload_date} "
                         f"({upload_age_days} days ago). Mobius recommends uploading a current file. "
                         "You may proceed with the existing roster, but analysis confidence will be reduced.")
            else:
                freshness_body = (
                    f"Roster was uploaded <strong>{upload_age_days} days ago</strong>. "
                    "Consider re-uploading if your staffing has changed since then."
                )
                title = "Confirm provider roster (aging)"
                body  = (f"Last roster: <strong>{upload_row_count} providers</strong>, uploaded {upload_date} "
                         f"({upload_age_days} days ago). Continue with this roster or upload a new file.")
            _task_signal("insight", step_id=step_id, state=state,
                         title=f"Roster freshness — {upload_age_days} days old",
                         body=freshness_body,
                         issue_code="stale_roster" if staleness_level == "stale" else None)
            _task_signal("decision", step_id=step_id, state=state,
                         title=title, body=body,
                         detail_payload={
                             "allow_upload":        True,
                             "has_existing_roster": True,
                             "existing_upload_id":  existing_upload_id,
                             "existing_row_count":  upload_row_count,
                             "existing_date":       upload_date,
                             "upload_age_days":     upload_age_days,
                             "staleness_level":     staleness_level,
                         })
            _emit(emitter, f"⏸ Step {step_num} awaiting roster confirmation.", state, step_id)

        else:
            # No roster on file — upload required
            _task_signal("decision", step_id=step_id, state=state,
                         title="Upload provider roster",
                         body=("No roster on file. Upload a provider CSV or Excel file "
                               "(NPI, Name, Specialty) to identify individuals for NPPES "
                               "and Medicaid enrollment validation. This roster is the authoritative "
                               "active panel for all downstream PML and taxonomy checks."),
                         detail_payload={
                             "allow_upload":        True,
                             "has_existing_roster": False,
                         })
            _emit(emitter, f"⏸ Step {step_num} awaiting roster upload.", state, step_id)

    else:
        # Autopilot: always use last roster if available
        if has_existing:
            summary = f"Using existing roster ({upload_row_count} providers, {upload_date or 'previously uploaded'})."
            state.mark_done(step_id, summary)
            state.step_outputs.append(
                StepOutput(step_id=step_id, label="Provider Roster", csv_content="", row_count=0,
                           markdown_content=summary)
            )
            _task_signal("step_done", step_id=step_id, state=state, detail_payload={"summary": summary})
            _emit(emitter, f"✓ Step {step_num} done. {summary}", state, step_id)
        else:
            summary = "No roster on file — upload a roster before running in autopilot mode."
            state.mark_done(step_id, summary)
            state.step_outputs.append(
                StepOutput(step_id=step_id, label="Provider Roster", csv_content="(no roster)", row_count=0)
            )
            _task_signal("blocker", step_id=step_id, state=state,
                         title="No provider roster — upload required",
                         body=summary, issue_code="config_missing")
            _emit(emitter, f"⏸ Step {step_num}: {summary}", state, step_id)


def _check_and_refresh_medicaid_tables(
    base: str,
    emitter: Callable[[str], None] | None,
    state: OrchestratorState,
    step_id: str,
) -> bool:
    """Check PML/PPL freshness and auto-trigger refresh if tables are not current to today.

    - Queries GET /medicaid/freshness.
    - If needs_refresh, streams POST /medicaid/refresh/stream, emitting feed cards per stage.
    - Retries up to 2 times on stream error before giving up.
    - Returns True  — data is current (or refresh succeeded).
    - Returns False — refresh failed after all retries; caller should emit blocker and halt.
    """
    import urllib.request as _ur

    _task_signal("info", step_id=step_id, state=state,
                 title="Checking enrollment data freshness",
                 body="Verifying PML / TML / PPL tables are current before running validation…")

    try:
        freshness_resp = _ur.urlopen(f"{base}/medicaid/freshness", timeout=8)
        freshness = json.loads(freshness_resp.read().decode())
    except Exception as e:
        logger.debug("Medicaid freshness check failed (skipping auto-refresh): %s", e)
        _task_signal("info", step_id=step_id, state=state,
                     title="Freshness check unavailable — proceeding with existing data",
                     body=f"Could not reach the freshness endpoint ({e}). Validation will proceed with current tables.")
        return True  # tolerate — stale table is better than a blocked pipeline

    if not freshness.get("needs_refresh"):
        src = freshness.get("source", "")
        pml_date = (freshness.get("pml") or {}).get("last_loaded", "unknown")
        tml_date = (freshness.get("tml") or {}).get("last_loaded", "unknown")
        ppl_date = (freshness.get("ppl") or {}).get("last_loaded", "unknown")
        sentinel_note = " (sentinel)" if src == "local_sentinel" else ""
        _task_signal("info", step_id=step_id, state=state,
                     title=f"Enrollment data is current{sentinel_note}",
                     body=(f"PML: <strong>{pml_date}</strong> · "
                           f"TML: <strong>{tml_date}</strong> · "
                           f"PPL: <strong>{ppl_date}</strong> — all sources verified, proceeding with validation."))
        return True

    pml_date = (freshness.get("pml") or {}).get("last_loaded") or "unknown"
    ppl_date = (freshness.get("ppl") or {}).get("last_loaded") or "unknown"
    _task_signal("info", step_id=step_id, state=state,
                 title="Enrollment data is stale — auto-refresh triggered",
                 body=(f"PML last loaded: <strong>{pml_date}</strong> · PPL: <strong>{ppl_date}</strong>. "
                       "Downloading latest enrollment data from AHCA — this may take a minute."))

    refresh_url = f"{base}/medicaid/refresh/stream"
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            _task_signal("info", step_id=step_id, state=state,
                         title=f"Refresh attempt {attempt} of {max_attempts}",
                         body="Retrying enrollment data refresh…")
        try:
            req = _ur.Request(refresh_url, method="POST",
                              headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
            with _ur.urlopen(req, timeout=480) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        evt = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    msg = evt.get("message") or ""
                    event_type = evt.get("event", "progress")
                    if event_type == "error":
                        _task_signal("info", step_id=step_id, state=state,
                                     title=f"Refresh stream error (attempt {attempt})",
                                     body=f"{msg or 'Unknown error'} — {'retrying…' if attempt < max_attempts else 'no more retries.'}")
                        break  # break inner loop → retry outer loop
                    if event_type == "complete":
                        r = evt.get("result") or {}
                        pml_rows = (r.get("pml") or {}).get("loaded_rows")
                        ppl_rows = (r.get("ppl") or {}).get("loaded_rows")
                        _task_signal("info", step_id=step_id, state=state,
                                     title="Enrollment data refresh complete",
                                     body=(f"PML: <strong>{f'{pml_rows:,} rows' if pml_rows else 'skipped'}</strong> · "
                                           f"PPL: <strong>{f'{ppl_rows:,} rows' if ppl_rows else 'skipped'}</strong> · "
                                           "All sources current as of today — ready to use."))
                        return True
                    if msg:
                        _task_signal("info", step_id=step_id, state=state,
                                     title="Refreshing enrollment data…", body=msg)
                else:
                    # Exhausted stream without complete/error event
                    continue
                # error event broke inner loop — retry
                continue
        except Exception as e:
            logger.warning("Medicaid auto-refresh stream attempt %d failed: %s", attempt, e)
            _task_signal("info", step_id=step_id, state=state,
                         title=f"Refresh attempt {attempt} failed",
                         body=f"{e} — {'retrying…' if attempt < max_attempts else 'no more retries.'}")

    # All attempts exhausted
    logger.error("Medicaid auto-refresh failed after %d attempts", max_attempts)
    return False


def _log_pml_events_to_audit(
    *,
    base: str,
    state: OrchestratorState,
    validated: list,
    flagged: list,
    missing: list,
) -> None:
    """Fire-and-forget: POST per-provider pml_checked events to the skill-server audit log.

    Groups all rows by NPI so each provider gets one summary event that captures
    every taxonomy code that was checked, its status, and any issues found.
    """
    try:
        by_npi: dict[str, dict] = {}

        def _row_detail(r: dict) -> dict:
            """Compact per-row dict stored in audit so the roster can reconstruct pml_rows."""
            return {
                "taxonomy_code":     (r.get("taxonomy_code") or "").strip(),
                "medicaid_id":       r.get("medicaid_provider_id") or r.get("medicaid_id") or "",
                "zip9":              r.get("zip9") or r.get("zip_9") or "",
                "enrollment_status": r.get("enrollment_status") or r.get("status") or "",
                "effective_date":    r.get("contract_effective_date") or r.get("effective_date") or "",
                "termination_date":  r.get("contract_end_date") or r.get("termination_date") or "",
                "enrollment_type":   r.get("enrollment_type") or "",
                "issues":            r.get("issues") or [],
                "warnings":          r.get("warnings") or [],
                "edit_codes":        r.get("edit_codes") or [],
                "valid":             bool(r.get("valid", True)),
            }

        for r in validated:
            npi = str(r.get("npi") or "").zfill(10)
            if not npi or npi == "0000000000":
                continue
            row_warnings = r.get("warnings") or []
            entry = by_npi.setdefault(npi, {
                "npi": npi,
                "provider_name": r.get("provider_name") or "",
                "result": "enrolled",
                "taxonomy_codes": [],
                "issues": [],
                "warnings": [],
                "edit_codes": [],
                "pml_rows": [],
            })
            entry["pml_rows"].append(_row_detail(r))
            code = (r.get("taxonomy_code") or "").strip()
            if code and code not in entry["taxonomy_codes"]:
                entry["taxonomy_codes"].append(code)
            # Promote warnings (DENIAL-1120, PAY-1980 etc.) into the audit entry
            for w in row_warnings:
                if w not in entry["warnings"]:
                    entry["warnings"].append(w)
            for ec in (r.get("edit_codes") or []):
                ec_code = ec.get("code") if isinstance(ec, dict) else str(ec)
                if ec_code and ec_code not in [x.get("code") if isinstance(x, dict) else x for x in entry["edit_codes"]]:
                    entry["edit_codes"].append(ec)
            # Demote to "enrolled_with_warnings" if warnings exist
            if row_warnings and entry["result"] == "enrolled":
                entry["result"] = "enrolled_with_warnings"

        for r in flagged:
            npi = str(r.get("npi") or "").zfill(10)
            if not npi or npi == "0000000000":
                continue
            row_warnings = r.get("warnings") or []
            entry = by_npi.setdefault(npi, {
                "npi": npi,
                "provider_name": r.get("provider_name") or "",
                "result": "flagged",
                "taxonomy_codes": [],
                "issues": [],
                "warnings": [],
                "edit_codes": [],
                "pml_rows": [],
            })
            entry["pml_rows"].append(_row_detail(r))
            # If already enrolled, demote to partial
            if entry["result"] in ("enrolled", "enrolled_with_warnings"):
                entry["result"] = "partial"
            code = (r.get("taxonomy_code") or "").strip()
            if code and code not in entry["taxonomy_codes"]:
                entry["taxonomy_codes"].append(code)
            for issue in (r.get("issues") or []):
                if issue not in entry["issues"]:
                    entry["issues"].append(issue)
            for w in row_warnings:
                if w not in entry["warnings"]:
                    entry["warnings"].append(w)
            for ec in (r.get("edit_codes") or []):
                ec_code = ec.get("code") if isinstance(ec, dict) else str(ec)
                if ec_code and ec_code not in [x.get("code") if isinstance(x, dict) else x for x in entry["edit_codes"]]:
                    entry["edit_codes"].append(ec)

        for r in missing:
            npi = str(r.get("npi") or "").zfill(10)
            if not npi or npi == "0000000000":
                continue
            by_npi.setdefault(npi, {
                "npi": npi,
                "provider_name": r.get("provider_name") or "",
                "result": "not_enrolled",
                "taxonomy_codes": [],
                "issues": ["Not found in FL Medicaid PML"],
            })

        if not by_npi:
            return

        events = []
        for npi, d in by_npi.items():
            events.append({
                "org_name":     state.org_name or "unknown",
                "event_type":   "pml_checked",
                "npi":          npi,
                "provider_name": d["provider_name"],
                "run_id":       state.run_id,
                "actor":        "mobius",
                "actor_label":  "Mobius PML Step",
                "event_data": {
                    "result":         d["result"],
                    "taxonomy_codes": d["taxonomy_codes"],
                    "issues":         d["issues"],
                    "warnings":       d.get("warnings") or [],
                    "edit_codes":     d.get("edit_codes") or [],
                    "pml_rows":       d.get("pml_rows") or [],
                    "pml_freshness":  (state.pml_source_freshness or {}).get("pml") or {},
                },
            })

        payload = json.dumps(events).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/roster/log-events",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            logger.info("pml audit events written: %s", result.get("written", "?"))
    except Exception as exc:
        logger.warning("_log_pml_events_to_audit failed (non-fatal): %s", exc)


def _run_step_pml_alignment(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> None:
    """Step 4: PML alignment — validate individual roster providers against FL Medicaid enrollment lists."""
    step_id = "pml_alignment"
    state.mark_in_progress(step_id)
    _emit(emitter, "── PML Medicaid enrollment validation ──", state, step_id)
    _task_signal("step_start", step_id=step_id, state=state)

    base = _provider_roster_base_url()
    if not base:
        summary = "Provider-roster API not configured. Set CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL."
        state.mark_done(step_id, summary)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="PML Alignment", csv_content="(skipped — no API URL)", row_count=0,
                       markdown_content=summary)
        )
        _emit(emitter, f"✗ Skipped: {summary}", state, step_id)
        _task_signal("blocker", step_id=step_id, state=state,
                     title="PML validation unavailable — API not configured",
                     body=summary, issue_code="config_missing")
        return

    # ── Auto-refresh PML/PPL if tables are not current to today ──────────────
    refresh_ok = _check_and_refresh_medicaid_tables(base, emitter, state, step_id)
    if not refresh_ok:
        is_copilot = (getattr(state, "credentialing_run_mode", "") or "").strip().lower() != "autopilot"
        if is_copilot:
            # Don't hard-block — give the user the choice to proceed with existing data
            _task_signal("decision", step_id=step_id, state=state,
                         title="Enrollment data refresh failed — proceed with existing data?",
                         body=("The PML/PPL refresh service could not be reached after multiple attempts. "
                               "Existing enrollment tables may be up to date — if the data was loaded recently, "
                               "proceeding is likely safe. Check connectivity to the enrollment service if you want "
                               "to ensure the latest data before proceeding."),
                         detail_payload={
                             "pml_summary": False,
                             "refresh_failed": True,
                         })
            _emit(emitter, "⏸ Enrollment refresh failed — awaiting user decision to proceed or stop.", state, step_id)
            return
        else:
            # Autopilot: log and continue with existing data rather than hard-blocking
            _emit(emitter, "⚠ Enrollment refresh failed — proceeding with existing tables (autopilot).", state, step_id)
            logger.warning("pml_alignment: refresh failed in autopilot mode, continuing with existing data")

    # ── Source of truth: prefer validated individual providers from roster_truth ──
    roster_providers: list[dict] = []
    try:
        from app.storage.roster_truth_pg import get_truth_for_org
        if state.org_name:
            roster_providers = get_truth_for_org(state.org_name)
            if roster_providers:
                _task_signal("info", step_id=step_id, state=state,
                             title=f"Loaded {len(roster_providers)} providers from confirmed roster",
                             body="Using the panel approved in the previous step as the source of truth for enrollment validation.")
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
                _task_signal("info", step_id=step_id, state=state,
                             title=f"Using {len(roster_providers)} providers from associated panel",
                             body="Roster truth not yet populated — using the associated providers panel from the previous step.")

    if not roster_providers:
        summary = "No validated providers found. Complete NPPES alignment (Step 3) and approve providers to roster before running PML validation."
        state.mark_done(step_id, summary)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="PML Alignment", csv_content="(no providers)", row_count=0,
                       markdown_content=summary)
        )
        _task_signal("blocker", step_id=step_id, state=state,
                     title="No providers to validate",
                     body=summary, issue_code="upstream_error")
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

    _task_signal("info", step_id=step_id, state=state,
                 title=f"Validating {len(associated_from_truth)} providers against PML / TML / PPL",
                 body="Running each individual NPI against Florida Medicaid enrollment lists to check enrollment status, active flags, and taxonomy alignment.")

    # Also check locations — API returns empty if locations are missing
    locations = state.locations or []
    if not locations:
        _task_signal("info", step_id=step_id, state=state,
                     title="No service locations on file",
                     body="ZIP-9 address validation will be skipped for this run. Add practice locations to enable location-based enrollment checks.")

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
        state.tml_codes = data.get("tml_codes") or []

        enrolled  = len(validated)
        n_flagged = len(flagged)
        n_missing = len(missing)
        total_checked = enrolled + n_flagged + n_missing

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
        _task_signal("step_done", step_id=step_id, state=state, detail_payload={
            "headers": ["NPI", "Provider", "Taxonomy", "ZIP9", "Medicaid ID", "Valid", "Issues"],
            "rows": [
                {"NPI": r["npi"], "Provider": r["provider_name"], "Taxonomy": r["taxonomy_code"],
                 "ZIP9": r["zip9"], "Medicaid ID": r["medicaid_provider_id"],
                 "Valid": r["valid"], "Issues": r["issues"]}
                for r in rows
            ],
            "summary": sm,
        })

        # ── Data freshness insight card ───────────────────────────────────────
        freshness = state.pml_source_freshness or {}
        if freshness:
            from datetime import date as _date
            today_str = _date.today().isoformat()
            def _days_ago(d: str) -> str:
                try:
                    delta = (_date.today() - _date.fromisoformat(d[:10])).days
                    return f"{delta} day{'s' if delta != 1 else ''} ago"
                except Exception:
                    return d or "unknown"
            pml_d = (freshness.get("pml") or {}) if isinstance(freshness.get("pml"), dict) else {"last_loaded": freshness.get("pml", "")}
            tml_d = (freshness.get("tml") or {}) if isinstance(freshness.get("tml"), dict) else {"last_loaded": freshness.get("tml", "")}
            ppl_d = (freshness.get("ppl") or {}) if isinstance(freshness.get("ppl"), dict) else {"last_loaded": freshness.get("ppl", "")}
            pml_date_s = pml_d.get("last_loaded") or str(freshness.get("pml") or "")
            tml_date_s = tml_d.get("last_loaded") or str(freshness.get("tml") or "")
            ppl_date_s = ppl_d.get("last_loaded") or str(freshness.get("ppl") or "")
            freshness_body = (
                f"PML: <strong>{pml_date_s}</strong> ({_days_ago(pml_date_s)}) · "
                f"TML: <strong>{tml_date_s}</strong> ({_days_ago(tml_date_s)}) · "
                f"PPL: <strong>{ppl_date_s}</strong> ({_days_ago(ppl_date_s)})"
            )
            _task_signal("insight", step_id=step_id, state=state,
                         title="Enrollment data sources used for this validation",
                         body=freshness_body,
                         data={"detail_payload": {"pml": pml_date_s, "tml": tml_date_s, "ppl": ppl_date_s}})

        # ── Provider detail insight card — flagged and missing explicitly listed ──
        if n_flagged > 0 or n_missing > 0:
            detail_rows = []
            for r in flagged:
                detail_rows.append({
                    "provider": (r.get("provider_name") or r.get("name") or r.get("npi") or "")[:50],
                    "npi": r.get("npi", ""),
                    "status": "Flagged",
                    "issues": (";".join(r.get("issues") or []))[:120],
                    "recommendation": (r.get("recommendation") or "")[:120],
                })
            for r in missing:
                detail_rows.append({
                    "provider": (r.get("provider_name") or r.get("name") or r.get("npi") or "")[:50],
                    "npi": r.get("npi", ""),
                    "status": "Not in PML",
                    "issues": "Not found in Florida Medicaid enrollment lists",
                    "recommendation": (r.get("recommendation") or "Enroll provider before billing")[:120],
                })
            _task_signal("insight", step_id=step_id, state=state,
                         title=f"{n_flagged + n_missing} provider(s) require attention",
                         body=(f"<strong>{n_flagged}</strong> flagged (active enrollment issues) · "
                               f"<strong>{n_missing}</strong> not found in PML. "
                               "These providers cannot bill Medicaid until issues are resolved."),
                         issue_code="pml_attention_required",
                         data={"detail_payload": {"rows": detail_rows[:50]}})

        # ── Task feed: decision card (copilot) ────────────────────────────────
        is_copilot = (getattr(state, "credentialing_run_mode", "") or "").strip().lower() != "autopilot"
        if is_copilot:
            miss_rate = n_missing / total_checked if total_checked > 0 else 0.0
            high_miss = miss_rate > 0.30

            # Build freshness summary line for decision card
            pml_date_label = ""
            if freshness:
                pml_val = freshness.get("pml")
                pml_date_label = (pml_val.get("last_loaded") if isinstance(pml_val, dict) else str(pml_val or "")) or ""

            if high_miss:
                pml_body = (
                    f"⚠ <strong>{n_missing}</strong> of {total_checked} providers ({miss_rate:.0%}) were not found in PML — "
                    "above the expected threshold. Proceeding with unenrolled providers risks billing compliance issues. "
                    "Review the detail card above and resolve enrollment gaps before continuing."
                )
                decision_title = f"⚠ High miss rate — {n_missing} providers not in PML"
            else:
                pml_body = (
                    f"Checked <strong>{total_checked}</strong> providers against Florida Medicaid enrollment lists. "
                    f"<strong>{enrolled}</strong> enrolled and active · "
                    f"<strong>{n_flagged}</strong> flagged · "
                    f"<strong>{n_missing}</strong> not found in PML."
                )
                if n_flagged or n_missing:
                    pml_body += f" {n_flagged + n_missing} provider(s) require attention before billing."
                decision_title = "Medicaid enrollment check complete"

            if pml_date_label:
                pml_body += f" <em>PML as of {pml_date_label}.</em>"

            _task_signal("decision", step_id=step_id, state=state,
                         title=decision_title,
                         body=pml_body,
                         detail_payload={
                             "pml_summary": True,
                             "enrolled": enrolled,
                             "flagged": n_flagged,
                             "missing": n_missing,
                             "total": total_checked,
                             "miss_rate": round(miss_rate, 3),
                             "high_miss": high_miss,
                         })
            # awaiting_validation REMOVED — decision card is the gate

        # ── Persist per-provider PML audit events ────────────────────────────
        _log_pml_events_to_audit(
            base=base,
            state=state,
            validated=validated,
            flagged=flagged,
            missing=missing,
        )

        # ── Flush PML tasks to roster immediately ─────────────────────────────
        # This runs here (not only at full-run end) so that copilot-mode runs and
        # runs that error out in later steps still carry PML findings to the Roster.
        try:
            _flush_pipeline_tasks_to_roster_truth(state)
        except Exception as _fe:
            logger.warning("pml_alignment: task flush failed (non-fatal): %s", _fe)

    except urllib.error.HTTPError as e:
        body = e.fp.read().decode()[:300] if e.fp else str(e)
        logger.warning("pml_alignment HTTP %s %s", e.code, body)
        summary = f"PML API error {e.code}: {body[:120]}"
        state.mark_done(step_id, summary)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="PML Alignment", csv_content=f"(API error {e.code})", row_count=0)
        )
        _emit(emitter, f"✗ {summary}", state, step_id)
        _task_signal("paused", step_id=step_id, state=state, note=summary)
    except Exception as e:
        logger.warning("pml_alignment failed: %s", e)
        summary = f"PML validation failed: {e}"
        state.mark_done(step_id, summary)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="PML Alignment", csv_content=f"(failed: {e})", row_count=0)
        )
        _emit(emitter, f"✗ {summary}", state, step_id)
        _task_signal("paused", step_id=step_id, state=state, note=summary)


def _log_taxonomy_events_to_audit(
    *,
    base: str,
    state: OrchestratorState,
    analysis: list[dict],
) -> None:
    """Fire-and-forget: POST per-provider taxonomy_checked events to the skill-server audit log."""
    try:
        if not analysis:
            return
        events = []
        for prof in analysis:
            npi  = prof.get("npi") or ""
            name = prof.get("provider_name") or ""
            if not npi or npi == "0000000000":
                continue
            events.append({
                "org_name":     state.org_name or "unknown",
                "event_type":   "taxonomy_checked",
                "npi":          npi,
                "provider_name": name,
                "run_id":       state.run_id,
                "actor":        "mobius",
                "actor_label":  "Mobius Taxonomy Step",
                "event_data": {
                    "result_type":       prof.get("result_type") or "",
                    "taxonomy_count":    prof.get("taxonomy_count") or 0,
                    # Full taxonomy profile with sources, TML status, PML status, license
                    "taxonomy_profile": [
                        {
                            "code":         c.get("code"),
                            "desc":         c.get("desc"),
                            "primary":      c.get("primary", False),
                            "source":       c.get("source", "nppes_snapshot"),
                            "license":      c.get("license", ""),
                            "state":        c.get("state", ""),
                            "in_tml":       c.get("in_tml"),
                            "pml_enrolled": c.get("pml_enrolled", False),
                            "pml_status":   c.get("pml_status"),
                            "pml_issues":   c.get("pml_issues", []),
                            "status":       c.get("status"),  # approved_enrolled | approved_missing_pml | not_tml
                        }
                        for c in (prof.get("codes") or [])
                    ],
                    # Billing codes from HCPC heatmap (top procedure codes for each taxonomy)
                    "hcpc_coverage": [
                        {"hcpcs_code": h.get("hcpcs_code"), "billing_pct": h.get("billing_pct"),
                         "claim_count": h.get("claim_count")}
                        for h in (prof.get("heatmap_rows") or [])[:10]
                    ],
                    "delta_hcpcs":       [
                        {"code": x.get("hcpcs_code"), "billing_pct": x.get("billing_pct")}
                        for x in (prof.get("delta_hcpcs") or [])
                    ],
                    "delta_billing_pct": prof.get("delta_billing_pct") or 0.0,
                    "tml_loaded":        bool(state.tml_codes),
                    "tml_code_count":    len(state.tml_codes or []),
                },
            })
        if not events:
            return
        payload = json.dumps(events).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/roster/log-events",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            logger.info("taxonomy audit events written: %s", result.get("written", "?"))
    except Exception as exc:
        logger.warning("_log_taxonomy_events_to_audit failed (non-fatal): %s", exc)


def _run_step_taxonomy_optimization(
    state: OrchestratorState,
    emitter: Callable[[str], None] | None,
) -> None:
    """Step 6 (pipeline): Taxonomy optimization — decision tree per provider.

    Decision flow per provider (nodes S2 → S3B → S4B → S5B):
      S2:  Multiple NPPES taxonomy codes?
      S3B: Per code: in TML? has PML enrollment row?
           ✅ = TML-approved + PML-enrolled
           ⚠️ = TML-approved but missing PML enrollment
           ❌ = not TML-approved at all
      S4B: If any ⚠️/❌: compute HCPC procedure delta from taxonomy_hcpcs_volume_fl
      S5B: If delta non-empty: billing restriction alert
    """
    step_id = "taxonomy_optimization"
    state.mark_in_progress(step_id)
    _task_signal("step_start", step_id=step_id, state=state)
    _emit(emitter, "── Taxonomy optimization analysis ──", state, step_id)

    base = _provider_roster_base_url()
    if not base:
        summary = "Provider-roster API not configured. Set CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL."
        state.mark_done(step_id, summary)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Taxonomy Optimization", csv_content="(skipped — no API URL)", row_count=0,
                       markdown_content=summary)
        )
        _emit(emitter, f"✗ Skipped: {summary}", state, step_id)
        _task_signal("step_done", step_id=step_id, state=state, detail_payload={"summary": summary})
        return

    # ── S1: Fetch pristine roster NPIs (decision='validated', invalidated_at IS NULL) ──
    pristine_providers: list[dict] = []
    try:
        from app.storage.roster_truth_pg import get_truth_for_org
        if state.org_name:
            all_truth = get_truth_for_org(state.org_name)
            # Accept any decision that means "on the roster" — approved, validated, sealed, corrected
            _active_decisions = {"approved", "validated", "sealed", "corrected"}
            pristine_providers = [
                p for p in all_truth
                if (p.get("decision") or "approved") in _active_decisions
                   and not p.get("invalidated_at")
            ]
            _task_signal("info", step_id=step_id, state=state,
                         title=f"Loaded {len(pristine_providers)} active providers for taxonomy analysis",
                         body="Running taxonomy decision tree (TML approval, PML enrollment, HCPC billing delta) for each provider.")
            _emit(emitter, f"✓ {len(pristine_providers)} active roster providers for taxonomy analysis", state, step_id)
    except Exception as e:
        logger.warning("taxonomy_optimization: could not load roster truth: %s", e)

    if not pristine_providers:
        summary = "No approved providers in roster truth yet. Approve providers in Step 3 (NPPES alignment) first."
        state.mark_done(step_id, summary)
        state.step_outputs.append(
            StepOutput(step_id=step_id, label="Taxonomy Optimization", csv_content="(no providers)", row_count=0,
                       markdown_content=summary)
        )
        _emit(emitter, f"✗ {summary}", state, step_id)
        _task_signal("step_done", step_id=step_id, state=state, detail_payload={"summary": summary})
        return

    npi_list = [str(p.get("npi_validated") or p.get("npi_roster") or "").strip().zfill(10) for p in pristine_providers]
    npi_list = [n for n in npi_list if n and n != "0000000000"]
    provider_name_map = {
        str(p.get("npi_validated") or p.get("npi_roster") or "").strip().zfill(10): (p.get("provider_name") or "")
        for p in pristine_providers
    }

    # ── S1: Build taxonomy inventory from stored NPPES snapshots (primary source) ──
    # The approve-to-truth step already stored all_taxonomies in nppes_snapshot.
    # Use this as the ground truth. Only supplement with BigQuery if snapshot is empty.
    _emit(emitter, f"Building taxonomy inventory for {len(npi_list)} providers…", state, step_id)

    # Build from stored NPPES snapshots in roster_truth (fast, no BQ dependency)
    snapshot_inv: dict[str, list[dict]] = {}
    for p in pristine_providers:
        npi_key = str(p.get("npi_validated") or p.get("npi_roster") or "").strip().zfill(10)
        if not npi_key or npi_key == "0000000000":
            continue
        snap = p.get("nppes_snapshot") or {}
        if isinstance(snap, str):
            try:
                snap = json.loads(snap)
            except Exception:
                snap = {}
        all_tax = snap.get("all_taxonomies") or []
        if all_tax:
            snapshot_inv[npi_key] = [
                {
                    "code":    t.get("code", "").strip(),
                    "desc":    t.get("desc", t.get("code", "")),
                    "primary": t.get("primary", False),
                    "source":  "nppes_snapshot",
                    "license": t.get("license", ""),
                    "state":   t.get("state", ""),
                }
                for t in all_tax if t.get("code", "").strip()
            ]

    # BigQuery supplement: fetch ALL providers from BQ to catch secondary taxonomy codes
    # that may be missing from the snapshot (e.g. snapshots stored before multi-taxonomy
    # support was added, or when the NPPES API only returned the primary taxonomy).
    # BQ npi_raw has healthcare_provider_taxonomy_code_1..15 — it is the authoritative source
    # for the complete set of registered taxonomy codes for any NPI.
    bq_inv: dict[str, list[dict]] = {}
    try:
        url = f"{base}/taxonomy/provider-inventory"
        payload = json.dumps({"org_name": state.org_name or "", "npis": npi_list}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            inv_data = json.loads(resp.read().decode())
        for prov in (inv_data.get("providers") or []):
            n = str(prov.get("npi") or "").strip().zfill(10)
            txs = prov.get("all_taxonomies") or []
            if n and txs:
                bq_inv[n] = [{**t, "source": "nppes_bigquery"} for t in txs]
        _emit(emitter, f"✓ BQ taxonomy supplement: {len(bq_inv)} providers enriched", state, step_id)
    except Exception as e:
        logger.info("taxonomy BQ inventory supplemental fetch: %s", e)

    # Build PML taxonomy source per NPI (from pml_validated + pml_flagged)
    pml_tax_map: dict[str, list[dict]] = {}
    for r in (state.pml_validated or []):
        n = str(r.get("npi") or "").strip().zfill(10)
        code = str(r.get("taxonomy_code") or "").strip()
        if n and code:
            if n not in pml_tax_map:
                pml_tax_map[n] = []
            pml_tax_map[n].append({"code": code, "source": "pml_enrolled", "status": "enrolled"})
    for r in (state.pml_flagged or []):
        n = str(r.get("npi") or "").strip().zfill(10)
        code = str(r.get("taxonomy_code") or "").strip()
        if n and code:
            if n not in pml_tax_map:
                pml_tax_map[n] = []
            if code not in {t["code"] for t in pml_tax_map[n]}:
                pml_tax_map[n].append({"code": code, "source": "pml_flagged", "status": "flagged", "issues": r.get("issues", [])})

    # Merge: NPPES snapshot provides rich metadata (desc, license, primary flag);
    # BQ fills in secondary codes missing from snapshot; PML adds enrollment data.
    # Union snapshot + BQ so secondary taxonomy codes like 101YM0800X are always captured.
    merged_inv: list[dict] = []
    for npi_key in npi_list:
        snap_taxes = snapshot_inv.get(npi_key) or []
        bq_taxes   = bq_inv.get(npi_key) or []
        # Union: start with snapshot entries (richer metadata), then add BQ codes not in snapshot
        snap_codes = {t["code"] for t in snap_taxes if t.get("code")}
        extra_from_bq = [t for t in bq_taxes if t.get("code") and t["code"] not in snap_codes]
        nppes_taxes = snap_taxes + extra_from_bq
        # Fallback: if snapshot and BQ both empty, use BQ directly
        if not nppes_taxes:
            nppes_taxes = bq_taxes
        pml_taxes = pml_tax_map.get(npi_key) or []
        # Merge PML source info into NPPES entries
        pml_code_set = {t["code"] for t in pml_taxes}
        enriched = []
        for t in nppes_taxes:
            pml_entry = next((p for p in pml_taxes if p["code"] == t["code"]), None)
            enriched.append({
                **t,
                "pml_enrolled": pml_entry is not None and pml_entry.get("status") == "enrolled",
                "pml_status":   pml_entry.get("status") if pml_entry else None,
                "pml_issues":   pml_entry.get("issues", []) if pml_entry else [],
            })
        # Add PML-only codes (enrolled but not in NPPES snapshot — unusual but possible)
        nppes_code_set = {t["code"] for t in nppes_taxes}
        for p in pml_taxes:
            if p["code"] not in nppes_code_set:
                enriched.append({
                    "code":         p["code"],
                    "desc":         p["code"],
                    "primary":      False,
                    "source":       p["source"],
                    "pml_enrolled": p.get("status") == "enrolled",
                    "pml_status":   p.get("status"),
                    "pml_issues":   p.get("issues", []),
                })
        merged_inv.append({
            "npi":            npi_key,
            "provider_name":  provider_name_map.get(npi_key, ""),
            "all_taxonomies": enriched,
            "taxonomy_count": len(enriched),
        })

    state.taxonomy_inventory = merged_inv
    total_w_tax = sum(1 for p in merged_inv if p.get("taxonomy_count", 0) > 0)
    total_codes = sum(p.get("taxonomy_count", 0) for p in merged_inv)
    _emit(emitter, f"✓ Taxonomy inventory: {total_w_tax}/{len(merged_inv)} providers with data, {total_codes} total codes", state, step_id)

    # ── Build lookup sets for S3B decision ─────────────────────────────────────
    # PML-enrolled codes per NPI: (npi, taxonomy_code) → True
    pml_enrolled: set[tuple[str, str]] = set()
    for r in (state.pml_validated or []):
        npi = str(r.get("npi") or "").strip().zfill(10)
        tax = str(r.get("taxonomy_code") or "").strip()
        if npi and tax:
            pml_enrolled.add((npi, tax))
    # Also include flagged rows that have a taxonomy code (they are still enrolled, just with issues)
    for r in (state.pml_flagged or []):
        npi = str(r.get("npi") or "").strip().zfill(10)
        tax = str(r.get("taxonomy_code") or "").strip()
        if npi and tax:
            pml_enrolled.add((npi, tax))

    tml_set: set[str] = set(state.tml_codes or [])

    def _code_status(npi: str, code: str) -> str:
        """Return 'approved_enrolled' | 'approved_missing_pml' | 'not_tml'."""
        if code not in tml_set:
            return "not_tml"
        if (npi, code) in pml_enrolled:
            return "approved_enrolled"
        return "approved_missing_pml"

    # ── S2/S3B: Per-provider taxonomy profiling ────────────────────────────────
    # Collect all gap codes for a single batch heatmap call
    all_gap_codes: set[str] = set()
    provider_profiles: list[dict] = []

    for prov in state.taxonomy_inventory:
        npi  = str(prov.get("npi") or "").strip().zfill(10)
        name = (prov.get("provider_name") or provider_name_map.get(npi) or npi)
        taxonomies: list[dict] = prov.get("all_taxonomies") or []

        if not taxonomies:
            provider_profiles.append({
                "npi": npi, "provider_name": name,
                "taxonomy_count": 0, "codes": [],
                "has_gaps": False, "result_type": "no_nppes_taxonomies",
            })
            continue

        codes_with_status = []
        for t in taxonomies:
            code = (t.get("code") or "").strip()
            if not code:
                continue
            status = _code_status(npi, code)
            codes_with_status.append({
                "code":       code,
                "desc":       t.get("desc", code),
                "primary":    t.get("primary", False),
                "status":     status,  # approved_enrolled | approved_missing_pml | not_tml
                "source":     t.get("source", "nppes_snapshot"),
                "license":    t.get("license", ""),
                "state":      t.get("state", ""),
                "pml_enrolled": t.get("pml_enrolled", False),
                "pml_status": t.get("pml_status"),
                "pml_issues": t.get("pml_issues", []),
                "in_tml":     code in tml_set if tml_set else None,
            })

        has_gaps = any(c["status"] != "approved_enrolled" for c in codes_with_status)
        if has_gaps:
            for c in codes_with_status:
                if c["status"] != "approved_enrolled":
                    all_gap_codes.add(c["code"])
            # Also add approved codes so the heatmap has the full picture for delta calc
            for c in codes_with_status:
                all_gap_codes.add(c["code"])

        provider_profiles.append({
            "npi":            npi,
            "provider_name":  name,
            "taxonomy_count": len(codes_with_status),
            "codes":          codes_with_status,
            "has_gaps":       has_gaps,
            "result_type":    "pending_heatmap" if has_gaps else "clean",
        })

    gap_count = sum(1 for p in provider_profiles if p.get("has_gaps"))
    _emit(emitter, f"✓ Profile built: {len(provider_profiles) - gap_count} clean · {gap_count} with gaps", state, step_id)

    # ── S4B: Heatmap + delta (single batch call for all gap codes) ─────────────
    heatmap_coverage: dict[str, list[dict]] = {}
    top_hcpcs: list[str] = []

    if gap_count > 0 and all_gap_codes:
        _emit(emitter, f"Fetching HCPC procedure coverage for {len(all_gap_codes)} taxonomy codes…", state, step_id)
        try:
            url = f"{base}/taxonomy/hcpc-heatmap"
            payload = json.dumps({
                "taxonomy_codes": list(all_gap_codes),
                "top_n_hcpcs": 20,
            }).encode("utf-8")
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=90) as resp:
                hm_data = json.loads(resp.read().decode())
            heatmap_coverage = hm_data.get("coverage_by_taxonomy") or {}
            top_hcpcs        = hm_data.get("top_hcpcs") or []
            _emit(emitter, f"✓ Heatmap data loaded for {len(top_hcpcs)} HCPC procedure(s)", state, step_id)
        except Exception as e:
            logger.warning("taxonomy hcpc-heatmap failed: %s", e)
            _emit(emitter, f"△ HCPC heatmap unavailable: {e}", state, step_id)

    # ── S3B → S4B → S5B: Per-provider decision tree finalization ──────────────
    analysis: list[dict] = []

    for prof in provider_profiles:
        npi   = prof["npi"]
        codes = prof["codes"]

        if not prof.get("has_gaps"):
            analysis.append({**prof, "result_type": "clean", "delta_hcpcs": [], "delta_billing_pct": 0.0,
                              "heatmap_rows": [], "top_hcpcs": []})
            continue

        # Build heatmap rows: one row per HCPC code, columns per taxonomy
        approved_codes = {c["code"] for c in codes if c["status"] == "approved_enrolled"}
        gap_codes_this = {c["code"] for c in codes if c["status"] != "approved_enrolled"}

        # HCPC coverage sets: which codes can be billed under approved vs gap taxonomies
        approved_hcpcs: set[str] = set()
        gap_hcpcs_to_pct: dict[str, float] = {}

        for c in codes:
            rows_for_tax = heatmap_coverage.get(c["code"]) or []
            for row in rows_for_tax:
                hcpc = row["hcpcs_code"]
                pct  = row["billing_pct"]
                if c["status"] == "approved_enrolled":
                    approved_hcpcs.add(hcpc)
                else:
                    # Keep the max billing_pct across gap taxonomies for this HCPC
                    if hcpc not in gap_hcpcs_to_pct or pct > gap_hcpcs_to_pct[hcpc]:
                        gap_hcpcs_to_pct[hcpc] = pct

        # Delta = HCPC codes reachable ONLY via gap taxonomies (NOT via any approved taxonomy)
        delta_hcpcs_with_pct = [
            {"hcpcs_code": h, "billing_pct": p}
            for h, p in gap_hcpcs_to_pct.items()
            if h not in approved_hcpcs
        ]
        delta_hcpcs_with_pct.sort(key=lambda x: -x["billing_pct"])

        delta_billing_pct = sum(x["billing_pct"] for x in delta_hcpcs_with_pct)

        # Build heatmap matrix rows for the UI (one row per top HCPC code)
        heatmap_rows = []
        for hcpc in (top_hcpcs or list(gap_hcpcs_to_pct.keys())[:20]):
            cells = {}
            for c in codes:
                rows_for_tax = heatmap_coverage.get(c["code"]) or []
                covered = any(r["hcpcs_code"] == hcpc for r in rows_for_tax)
                cells[c["code"]] = covered
            total_vol = sum(
                r["claim_count"]
                for c in codes
                for r in (heatmap_coverage.get(c["code"]) or [])
                if r["hcpcs_code"] == hcpc
            )
            heatmap_rows.append({
                "hcpcs_code": hcpc,
                "cells": cells,
                "total_volume": total_vol,
                "is_delta": hcpc in {x["hcpcs_code"] for x in delta_hcpcs_with_pct},
            })

        # S5B: billing restriction? Only if delta is non-empty
        if delta_hcpcs_with_pct:
            result_type = "restriction"
            _emit(emitter, f"⚠ {prof['provider_name']}: {len(delta_hcpcs_with_pct)} at-risk HCPC code(s) ({delta_billing_pct:.1f}% of billing)", state, step_id)
        elif gap_codes_this:
            result_type = "gap_only"
        else:
            result_type = "clean"

        analysis.append({
            **prof,
            "result_type":       result_type,
            "delta_hcpcs":       delta_hcpcs_with_pct,
            "delta_billing_pct": round(delta_billing_pct, 1),
            "heatmap_rows":      heatmap_rows,
            "top_hcpcs":         top_hcpcs,
        })

    state.taxonomy_analysis = analysis

    n_restriction = sum(1 for a in analysis if a["result_type"] == "restriction")
    n_gap         = sum(1 for a in analysis if a["result_type"] == "gap_only")
    n_clean       = sum(1 for a in analysis if a["result_type"] == "clean")
    n_no_data     = sum(1 for a in analysis if a["result_type"] == "no_nppes_taxonomies")

    summary = f"{n_restriction} billing restrictions · {n_gap} enrollment gaps · {n_clean} clean · {n_no_data} no NPPES data"
    state.mark_done(step_id, summary)
    state.step_outputs.append(
        StepOutput(step_id=step_id, label="Taxonomy Optimization", csv_content="", row_count=len(analysis),
                   markdown_content=summary)
    )
    _emit(emitter, f"✓ Taxonomy optimization complete. {summary}", state, step_id)
    state.auto_advance = True  # no user decision needed — proceed to provider summaries
    _task_signal("step_done", step_id=step_id, state=state, detail_payload={
        "summary": summary,
        "n_restriction": n_restriction,
        "n_gap": n_gap,
        "n_clean": n_clean,
        "providers_analyzed": len(analysis),
    })

    # ── Persist per-provider taxonomy_checked audit events ───────────────────
    _log_taxonomy_events_to_audit(base=base, state=state, analysis=analysis)

    # ── Flush taxonomy tasks to roster immediately ───────────────────────────
    # Same reasoning as pml_alignment: flush now so copilot/partial runs persist.
    try:
        _flush_pipeline_tasks_to_roster_truth(state)
        _emit(emitter, "✓ Taxonomy findings saved to provider roster", state, step_id)
    except Exception as _fe:
        logger.warning("taxonomy_optimization: task flush failed (non-fatal): %s", _fe)


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
        if step_id == "ensure_benchmarks":
            _run_step_0_ensure_benchmarks(state, emitter)
            return None
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
        if step_id == "provider_summaries":
            _run_step_provider_summaries(state, emitter)
            return None
        if step_id == "org_summary":
            _run_step_org_summary(state, emitter)
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


def _flush_pipeline_tasks_to_roster_truth(state: OrchestratorState) -> None:
    """At end of a run, merge PML + taxonomy findings into roster_truth.open_tasks.

    This is the single place where pipeline-step results (PML gaps, taxonomy
    billing restrictions) get persisted as structured tasks that the Roster page
    and chat can surface even outside a live run context.

    Task deduplication is handled by merge_pipeline_tasks — a task with the
    same (dim, type) is never written twice, so re-running is safe.
    """
    from datetime import datetime, timezone
    from app.storage.roster_truth_pg import merge_pipeline_tasks

    org = state.org_name or ""
    now = datetime.now(timezone.utc).isoformat()
    run_id = state.run_id or ""

    # ── Build per-NPI task lists ─────────────────────────────────────────────
    tasks_by_npi: dict[str, list[dict]] = {}

    def _add(npi: str, task: dict) -> None:
        if npi and npi != "0000000000":
            tasks_by_npi.setdefault(npi, []).append(task)

    # PML: flagged rows (hard issues — zip_mismatch, npi_inactive, etc.)
    for r in (state.pml_flagged or []):
        npi    = str(r.get("npi") or "").zfill(10)
        code   = (r.get("taxonomy_code") or "").strip()
        issues = r.get("issues") or []
        if not issues:
            continue
        issue_str = "; ".join(issues[:3])
        reason = f"PML flagged — {code}: {issue_str}" if code else f"PML enrollment issue: {issue_str}"
        _add(npi, {
            "dim":           "pml",
            "type":          f"pml_flagged_{code}" if code else "pml_flagged",
            "reason":        reason,
            "created_at":    now,
            "source":        "pipeline",
            "step":          "pml_alignment",
            "severity":      "critical",
            "taxonomy_code": code,
            "zip9":          r.get("zip9") or "",
            "issues":        issues,
            "edit_codes":    [ec.get("code") if isinstance(ec, dict) else ec for ec in (r.get("edit_codes") or [])],
            "run_id":        run_id,
        })

    # PML: validated rows that have warnings (DENIAL-1120, PAY-1980 risks)
    # These are enrolled but have compliance warnings that could become denials
    _warned_npis: set[str] = set()
    for r in (state.pml_validated or []):
        npi      = str(r.get("npi") or "").zfill(10)
        code     = (r.get("taxonomy_code") or "").strip()
        warnings = r.get("warnings") or []
        edit_codes = r.get("edit_codes") or []
        if not warnings or npi in _warned_npis:
            continue
        _warned_npis.add(npi)
        # Extract FL AHCA edit codes for the reason line
        edit_labels = [ec.get("code") if isinstance(ec, dict) else str(ec) for ec in edit_codes]
        edit_str = " · ".join(edit_labels) if edit_labels else ""
        # Determine primary warning type for deduplication key
        warn_type = "pml_compliance_warning"
        if any("taxonomy_not_in_nppes" in w for w in warnings):
            warn_type = "pml_taxonomy_not_in_nppes"
        elif any("address_mismatch" in w or "address_missing" in w for w in warnings):
            warn_type = "pml_address_mismatch"
        elif any("multiple_enrollment" in w for w in warnings):
            warn_type = "pml_multiple_enrollments"
        reason = f"PML compliance warning{' (' + edit_str + ')' if edit_str else ''}: {warnings[0][:120]}"
        _add(npi, {
            "dim":           "pml",
            "type":          warn_type,
            "reason":        reason,
            "created_at":    now,
            "source":        "pipeline",
            "step":          "pml_alignment",
            "severity":      "warning",
            "taxonomy_code": code,
            "warnings":      warnings,
            "edit_codes":    edit_labels,
            "run_id":        run_id,
        })

    # PML: missing enrollment (provider not found in FL Medicaid PML at all)
    for r in (state.missing_enrollment or []):
        npi  = str(r.get("npi") or r.get("npi_validated") or "").zfill(10)
        name = r.get("provider_name") or r.get("name") or ""
        _add(npi, {
            "dim":        "pml",
            "type":       "not_enrolled",
            "reason":     f"{name} not found in FL Medicaid PML — enrollment required",
            "created_at": now,
            "source":     "pipeline",
            "step":       "pml_alignment",
            "run_id":     run_id,
        })

    # Taxonomy: billing restrictions and enrollment gaps from analysis
    for prof in (state.taxonomy_analysis or []):
        npi         = str(prof.get("npi") or "").zfill(10)
        result_type = prof.get("result_type") or ""
        delta_pct   = prof.get("delta_billing_pct") or 0.0
        delta_codes = [x.get("hcpcs_code") or x.get("code") for x in (prof.get("delta_hcpcs") or []) if x]
        codes_str   = prof.get("codes") or []
        unapproved  = [c.get("code") for c in codes_str if c.get("status") == "not_tml" and c.get("code")]
        gap_codes   = [c.get("code") for c in codes_str if c.get("status") == "approved_missing_pml" and c.get("code")]

        # result_type can be 'restriction', 'billing_restriction', 'gap_only', 'enrollment_gap'
        is_restriction = result_type in ("restriction", "billing_restriction")
        is_gap = result_type in ("gap_only", "enrollment_gap")
        if is_restriction and delta_pct > 0:
            severity = "critical" if delta_pct >= 20 else "warning"
            _add(npi, {
                "dim":               "taxonomy",
                "type":              "billing_restriction",
                "reason":            f"Taxonomy billing restriction — {delta_pct:.1f}% of procedure volume at risk",
                "created_at":        now,
                "source":            "pipeline",
                "step":              "taxonomy_optimization",
                "severity":          severity,
                "delta_billing_pct": delta_pct,
                "at_risk_codes":     delta_codes[:10],
                "run_id":            run_id,
            })
        elif is_gap and gap_codes:
            _add(npi, {
                "dim":        "taxonomy",
                "type":       "pml_enrollment_gap",
                "reason":     f"Taxonomy approved by TML but not enrolled in PML: {', '.join(gap_codes[:3])}",
                "created_at": now,
                "source":     "pipeline",
                "step":       "taxonomy_optimization",
                "severity":   "warning",
                "codes":      gap_codes,
                "run_id":     run_id,
            })

        if unapproved:
            _add(npi, {
                "dim":        "taxonomy",
                "type":       "not_tml_approved",
                "reason":     f"Taxonomy not TML-approved: {', '.join(unapproved[:3])}",
                "created_at": now,
                "source":     "pipeline",
                "step":       "taxonomy_optimization",
                "severity":   "warning",
                "codes":      unapproved,
                "run_id":     run_id,
            })

        if gap_codes:
            _add(npi, {
                "dim":        "taxonomy",
                "type":       "pml_enrollment_gap",
                "reason":     f"Approved taxonomy not enrolled in PML: {', '.join(gap_codes[:3])}",
                "created_at": now,
                "source":     "pipeline",
                "step":       "taxonomy_optimization",
                "severity":   "warning",
                "codes":      gap_codes,
                "run_id":     run_id,
            })

    if not tasks_by_npi:
        logger.info("_flush_pipeline_tasks_to_roster_truth: no tasks to flush for org=%s", org)
        return

    flushed = 0
    skipped = 0
    for npi, tasks in tasks_by_npi.items():
        ok = merge_pipeline_tasks(org, npi, tasks)
        if ok:
            flushed += 1
        else:
            skipped += 1
    logger.info(
        "_flush_pipeline_tasks_to_roster_truth: org=%s flushed=%d skipped=%d total_tasks=%d",
        org, flushed, skipped, sum(len(v) for v in tasks_by_npi.values()),
    )


def _log_run_event(
    org_name: str,
    event_type: str,
    state: OrchestratorState,
    extra: dict | None = None,
) -> None:
    """Fire-and-forget: log a run-level macro event (no provider_id/npi) to the audit log.

    Used for run_started / run_completed / run_failed so the chat can answer
    "when was the last run?" and "what did it cover?" without querying credentialing_runs.
    """
    try:
        base = _provider_roster_base_url()
        if not base:
            return
        event = {
            "org_name":   org_name,
            "event_type": event_type,
            "run_id":     state.run_id,
            "actor":      "mobius",
            "actor_label": "Mobius Pipeline",
            "event_data": {
                "run_id":  state.run_id,
                "org":     org_name,
                **(extra or {}),
            },
        }
        payload = json.dumps([event]).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/roster/log-events",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            pass
    except Exception as exc:
        logger.warning("_log_run_event failed (non-fatal): %s", exc)


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
    import uuid as _uuid_run
    state.run_id = str(_uuid_run.uuid4())
    _log_run_event(org_name, "run_started", state, {"roster_upload_id": roster_upload_id})
    report_text: str | None = None
    for sid in ROSTER_CREDENTIALING_STEP_IDS:
        out = run_credentialing_step(org_name, state, sid, emitter)
        st_done = state.step_by_id(sid)
        if st_done and st_done.status == "failed":
            detail = (st_done.result_summary or "").strip() or "unknown error"
            _log_run_event(org_name, "run_failed", state, {"step": sid, "reason": detail})
            _emit(
                emitter,
                f"**Pipeline stopped** — step `{sid}` **failed**: {detail}",
            )
            return (
                f"Credentialing stopped at step `{sid}`: {detail}",
                state,
            )

    summary_lines = ["**Credentialing pipeline complete.**"]
    step_summary = {}
    for s in state.steps:
        icon = "✓" if s.status == "done" else ("—" if s.status == "skipped" else "✗")
        summary_lines.append(f"{icon} {s.label}: {s.result_summary or s.status}")
        step_summary[s.id] = {"status": s.status, "summary": s.result_summary or ""}
    report_text = "\n".join(summary_lines)

    # Flush PML + taxonomy findings as persistent tasks into roster_truth
    _emit(emitter, "Persisting pipeline tasks to provider roster…", state)
    try:
        _flush_pipeline_tasks_to_roster_truth(state)
    except Exception as _flush_exc:
        logger.warning("_flush_pipeline_tasks_to_roster_truth failed (non-fatal): %s", _flush_exc)

    _log_run_event(org_name, "run_completed", state, {
        "roster_upload_id": roster_upload_id,
        "steps_done":    sum(1 for s in state.steps if s.status == "done"),
        "steps_skipped": sum(1 for s in state.steps if s.status == "skipped"),
        "step_summary":  step_summary,
        "providers_checked": len(state.taxonomy_analysis or []) or len(state.pml_validated or []),
    })
    return report_text, state


def _flat_active_roster(state: OrchestratorState) -> list[dict]:
    """Return a flat list of provider dicts from state.active_roster.

    active_roster can be:
      • dict[npi, dict]           — from the autopilot/active_roster API field
      • dict[loc_key, list[dict]] — when falling back to associated_providers
      • list[dict]                — rare but handled
    """
    ar = state.active_roster
    if isinstance(ar, dict):
        result: list[dict] = []
        for v in ar.values():
            if isinstance(v, list):
                result.extend(p for p in v if isinstance(p, dict))
            elif isinstance(v, dict):
                result.append(v)
        return result
    if isinstance(ar, list):
        result = []
        for v in ar:
            if isinstance(v, list):
                result.extend(p for p in v if isinstance(p, dict))
            elif isinstance(v, dict):
                result.append(v)
        return result
    return []


def _run_step_provider_summaries(
    state: OrchestratorState,
    emitter: Any,
) -> None:
    """Step 7 — Generate and persist AI credential summaries for every provider in the run.

    Produces three levels per provider:
      • one_liner   — single sentence, displayed inline in the roster list
      • brief       — 2-3 sentences, shown as the collapsed drawer header
      • detailed    — full 3-section markdown, rendered in the provider drawer
      • chat_profile — compact JSON used by the chat tool registry

    Results are stored in roster_truth.ai_summary and served instantly on next
    roster page load — no per-request LLM calls required.
    """
    step_id = "provider_summaries"
    step = state.step_by_id(step_id)
    if step:
        step.status = "in_progress"
    _task_signal("step_start", step_id=step_id, state=state)
    _task_signal("info", step_id=step_id, state=state,
                 title="Generating AI credential summaries",
                 body="Building one-liner, brief, and detailed credential profiles for each provider using AI — billable/at-risk status, open compliance tasks, and taxonomy alignment.")

    _emit(emitter, "Step 7 — Generating AI credential summaries for all providers…", state)

    skill_base = _provider_roster_base_url()
    # Fire-and-forget — AI summary flush is slow (per-provider LLM calls) and non-blocking.
    # The enrichment read below uses whatever summaries are already stored; new ones will be
    # available on the next page load after the background thread finishes.
    import threading as _threading
    _bg = _threading.Thread(
        target=_flush_provider_summaries, args=(state, None, skill_base), daemon=True
    )
    _bg.start()

    # Build a structured summary object for the step output so the UI can render a card list.
    # Enrich with per-provider AI one-liner, specialty, NPPES/PML status from the skill server.
    _roster_providers: list[dict] = _flat_active_roster(state)

    # Try to pull stored AI one-liners + rich detail from skill server
    _enriched: dict[str, dict] = {}
    try:
        import urllib.request as _ureq2
        _list_url2 = f"{skill_base}/roster/truth/{urllib.parse.quote(state.org_name or '')}"
        with _ureq2.urlopen(_list_url2, timeout=10) as _r2:
            _list2 = json.loads(_r2.read().decode())
        for _p2 in (_list2.get("providers") or []):
            _n2 = str(_p2.get("npi_validated") or _p2.get("npi_roster") or "").zfill(10)
            if _n2:
                _enriched[_n2] = _p2
    except Exception:
        pass

    providers_total = len(_roster_providers) or len(state.taxonomy_analysis or [])
    clean_count     = 0
    risk_count      = 0
    summaries_list  = []
    for p in _roster_providers:
        snap     = p.get("nppes_snapshot") or {}
        npi      = str(p.get("npi_validated") or p.get("npi_roster") or p.get("npi") or "").zfill(10)
        name     = p.get("provider_name") or p.get("name") or ""
        tasks    = p.get("open_tasks") or []
        enriched = _enriched.get(npi, {})
        bill     = (enriched.get("billability_status") or snap.get("billability_status") or "unknown").lower()
        specialty = (enriched.get("specialty") or p.get("specialty") or snap.get("specialty") or "")
        nppes_status = (snap.get("nppes_status") or snap.get("status") or (
            "active" if snap.get("is_active") else "unknown")).lower()
        pml_enrolled = enriched.get("pml_enrolled") if "pml_enrolled" in enriched else (
            not bool((enriched.get("nppes_snapshot") or {}).get("pml_gap")))
        pml_status   = "enrolled" if pml_enrolled else "not enrolled"
        one_liner    = (enriched.get("ai_summary_short") or enriched.get("one_liner") or "")
        open_task_ct = len(tasks) if isinstance(tasks, list) else (tasks or 0)
        if bill == "billable" and open_task_ct == 0:
            clean_count += 1
        elif bill in ("risk", "at_risk", "inactive", "blocked"):
            risk_count += 1
        summaries_list.append({
            "npi":          npi,
            "name":         name,
            "specialty":    specialty,
            "billability":  bill,
            "nppes_status": nppes_status,
            "pml_status":   pml_status,
            "open_tasks":   open_task_ct,
            "one_liner":    one_liner,
        })

    step_csv = "\n".join(
        f"{s['npi']},{s['name']},{s['specialty']},{s['billability']},{s['nppes_status']},{s['pml_status']},{s['open_tasks']} tasks"
        for s in summaries_list[:50]
    )

    if step:
        step.status = "done"
        step.result_summary = (
            f"{providers_total} provider summaries generated — "
            f"{clean_count} fully credentialed, {risk_count} flagged for review"
        )

    state.step_outputs.append(StepOutput(
        step_id   = step_id,
        label     = "Provider AI Summaries",
        csv_content = step_csv,
        row_count = providers_total,
        extra_data  = {
            "summaries":    summaries_list,
            "total":        providers_total,
            "clean_count":  clean_count,
            "risk_count":   risk_count,
        },
    ))
    _emit(
        emitter,
        f"✓ Step 7 done — {providers_total} provider summaries stored ({clean_count} clean, {risk_count} at-risk)",
        state,
    )
    state.auto_advance = True  # no user decision needed — proceed to org summary
    _task_signal("step_done", step_id=step_id, state=state, detail_payload={
        "summary": f"{providers_total} provider summaries generated — {clean_count} fully credentialed, {risk_count} require attention",
        "total": providers_total,
        "clean_count": clean_count,
        "risk_count": risk_count,
        "headers": ["NPI", "Provider", "Specialty", "NPPES", "PML", "Billability", "Open Tasks", "Summary"],
        "rows": [{
            "NPI":        s["npi"],
            "Provider":   s["name"],
            "Specialty":  s["specialty"],
            "NPPES":      s["nppes_status"],
            "PML":        s["pml_status"],
            "Billability": s["billability"],
            "Open Tasks": s["open_tasks"],
            "Summary":    s["one_liner"],
        } for s in summaries_list[:75]],
    })


def _run_step_org_summary(
    state: OrchestratorState,
    emitter: Any,
) -> None:
    """Step 8 — Compile an organization-wide credential health report.

    Aggregates individual provider summaries into a single org-level narrative +
    metrics (billable %, PML gap count, open task count, taxonomy risks).  Stores
    the result in roster_truth_pg.upsert_org_summary() and emits it to the UI.
    """
    import datetime
    import json as _json

    step_id = "org_summary"
    step = state.step_by_id(step_id)
    if step:
        step.status = "in_progress"
    _task_signal("step_start", step_id=step_id, state=state)
    _task_signal("info", step_id=step_id, state=state,
                 title="Compiling organization credential health report",
                 body="Aggregating provider summaries into an org-level narrative — billability rate, PML gaps, open compliance tasks, and taxonomy risks.")

    _emit(emitter, "Step 8 — Compiling organization credential health report…", state)

    org    = state.org_name or ""
    run_id = state.run_id or ""
    base   = _provider_roster_base_url()

    # Pull live roster list to aggregate accurate counts
    providers_list: list[dict] = []
    try:
        import urllib.request as _ureq
        list_url = f"{base}/roster/truth/{urllib.parse.quote(org)}"
        with _ureq.urlopen(list_url, timeout=15) as resp:
            list_data = _json.loads(resp.read().decode())
        providers_list = list_data.get("providers") or []
    except Exception as _e:
        logger.warning("org_summary: roster list fetch failed — %s", _e)

    # Aggregate metrics
    total       = len(providers_list)
    billable    = sum(1 for p in providers_list if (p.get("billability_status") or "") == "billable")
    at_risk     = sum(1 for p in providers_list if (p.get("billability_status") or "") in ("risk","at_risk"))
    blocked     = sum(1 for p in providers_list if (p.get("billability_status") or "") in ("blocked","inactive"))
    warning     = sum(1 for p in providers_list if (p.get("billability_status") or "") == "warning")
    pml_gaps    = sum(1 for p in providers_list if (p.get("nppes_snapshot") or {}).get("pml_gap"))
    open_tasks  = sum(len(p.get("open_tasks") or []) for p in providers_list)
    billable_pct = round(100 * billable / total, 1) if total else 0

    # Gather one-liners from stored ai_summary for the org narrative prompt
    oneliner_bullets = []
    for p in providers_list[:20]:
        ol = p.get("ai_summary_short") or ""
        if ol:
            oneliner_bullets.append(f"  - {p.get('provider_name','?')}: {ol}")

    # Build LLM prompt for org narrative — professional, consultative tone
    _ORG_SYSTEM = (
        "You are a Florida Medicaid credentialing compliance consultant preparing an executive briefing.\n"
        "Write a concise (≤220 word) organization-level credentialing assessment in two sections — "
        "do NOT use markdown headings (##) or bullet symbols in the output, write in plain prose paragraphs:\n\n"
        "CREDENTIALING ASSESSMENT: Summarize the organization's current credentialing posture — "
        "billability rate, enrollment status patterns, and the primary compliance opportunity areas. "
        "Frame findings as observations, not judgments. Use professional, neutral language — avoid words "
        "like 'critically', 'staggering', 'catastrophic', 'severe', 'failure', or 'crisis'.\n\n"
        "RECOMMENDED ACTIONS: Three specific, prioritized next steps for the compliance team, "
        "written as action items (e.g. 'Initiate PML enrollment review for the 35 providers with identified gaps...'). "
        "Be specific about which segment to address first and why.\n\n"
        "No preamble. Start directly with the assessment paragraph."
    )
    metrics_text = (
        f"Organization: {org}\n"
        f"Assessment date: {datetime.datetime.utcnow().strftime('%Y-%m-%d')}\n"
        f"Total active providers: {total}\n"
        f"Fully credentialed and billable: {billable} ({billable_pct}%)\n"
        f"Providers requiring attention (at-risk): {at_risk}\n"
        f"Inactive or blocked: {blocked}\n"
        f"Elevated review status (warning): {warning}\n"
        f"Providers with PML enrollment gaps: {pml_gaps}\n"
        f"Open compliance tasks across roster: {open_tasks}\n\n"
        "Selected provider highlights:\n" + "\n".join(oneliner_bullets[:15])
    )
    full_org_prompt = f"{_ORG_SYSTEM}\n\n{metrics_text}\n\nCREDENTIALING ASSESSMENT:"

    org_narrative = ""
    model_used    = "template"
    try:
        from app.services.llm_manager import generate_sync as _llm_gen
        raw, usage = _llm_gen(
            prompt    = full_org_prompt,
            stage     = "integrator_roster",
            max_tokens = 4096,
        )
        org_narrative = "CREDENTIALING ASSESSMENT:" + raw
        model_used    = usage.get("model","")
    except Exception as _e:
        logger.warning("org_summary: LLM failed — %s", _e)
        org_narrative = (
            f"CREDENTIALING ASSESSMENT: {org} has {total} active providers on roster. "
            f"{billable} ({billable_pct}%) are fully credentialed and billable. "
            f"{at_risk} providers have been identified as requiring attention, and {pml_gaps} have PML enrollment gaps. "
            f"There are currently {open_tasks} open compliance tasks across the roster.\n\n"
            f"RECOMMENDED ACTIONS: "
            f"1. Prioritize PML enrollment review for the {pml_gaps} providers with identified gaps. "
            f"2. Schedule credentialing reviews for the {at_risk} at-risk providers to restore billing eligibility. "
            f"3. Assign and schedule resolution of the {open_tasks} open compliance tasks."
        )

    # Persist org summary
    org_summary_payload = {
        "narrative":     org_narrative,
        "metrics": {
            "total":       total,
            "billable":    billable,
            "billable_pct": billable_pct,
            "at_risk":     at_risk,
            "blocked":     blocked,
            "warning":     warning,
            "pml_gaps":    pml_gaps,
            "open_tasks":  open_tasks,
        },
        "model":        model_used,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "run_id":       run_id,
    }
    try:
        from app.storage.roster_truth_pg import upsert_org_summary
        upsert_org_summary(org, run_id, org_summary_payload)
    except Exception as _pe:
        logger.warning("org_summary: upsert_org_summary failed — %s", _pe)

    if step:
        step.status = "done"
        step.result_summary = (
            f"{billable_pct}% billable — {billable}/{total} providers fully credentialed, "
            f"{at_risk} at-risk, {blocked} blocked, {pml_gaps} PML gaps, {open_tasks} open tasks"
        )

    state.step_outputs.append(StepOutput(
        step_id     = step_id,
        label       = "Organization Summary",
        csv_content = "",
        row_count   = total,
        extra_data  = {
            "org_summary":  org_summary_payload,
            "metrics":      org_summary_payload["metrics"],
            "narrative":    org_narrative,
        },
    ))
    _emit(
        emitter,
        f"✓ Step 8 done — org health: {billable_pct}% billable, {open_tasks} open tasks",
        state,
    )
    org_sm = (
        f"{billable_pct}% billable — {billable}/{total} providers fully credentialed, "
        f"{at_risk} at-risk, {blocked} blocked, {pml_gaps} PML gaps, {open_tasks} open tasks"
    )
    # Strip markdown from the LLM narrative before displaying in the feed card
    import re as _re
    _narrative_plain = (org_narrative or org_sm)
    _narrative_plain = _re.sub(r"#{1,6}\s*", "", _narrative_plain)          # remove headings
    _narrative_plain = _re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", _narrative_plain)  # bold/italic
    _narrative_plain = _re.sub(r"^\s*[-*]\s+", "", _narrative_plain, flags=_re.M)   # bullets
    _narrative_plain = _re.sub(r"\n{2,}", " ", _narrative_plain).strip()
    _task_signal("insight", step_id=step_id, state=state,
                 title="Organization credential health summary",
                 body=_narrative_plain[:800],
                 data={"detail_payload": org_summary_payload.get("metrics", {})})
    # Gather provider rows from step 7 output so the org_summary card can display them
    _prov_rows: list[dict] = []
    for _so in state.step_outputs:
        if _so.step_id == "provider_summaries" and isinstance((_so.extra_data or {}).get("summaries"), list):
            _prov_rows = [
                {
                    "NPI":        s.get("npi", ""),
                    "Provider":   s.get("name", ""),
                    "Specialty":  s.get("specialty", ""),
                    "NPPES":      s.get("nppes_status", ""),
                    "PML":        s.get("pml_status", ""),
                    "Billability": s.get("billability", ""),
                    "Open Tasks": s.get("open_tasks", 0),
                    "Summary":    s.get("one_liner", ""),
                }
                for s in _so.extra_data["summaries"][:75]
            ]
            break

    _task_signal("step_done", step_id=step_id, state=state, detail_payload={
        "summary": org_sm,
        "billable_pct": billable_pct,
        "billable": billable,
        "total": total,
        "at_risk": at_risk,
        "blocked": blocked,
        "warning": warning,
        "pml_gaps": pml_gaps,
        "open_tasks": open_tasks,
        "narrative": _narrative_plain,
        "headers": ["NPI", "Provider", "Specialty", "NPPES", "PML", "Billability", "Open Tasks", "Summary"],
        "rows": _prov_rows,
    })


def _flush_provider_summaries(
    state: "OrchestratorState",
    emitter: Any,
    skill_base: str,
) -> None:
    """After a completed run, generate and persist AI summaries for all providers.

    Generates 3 levels (one_liner, brief, detailed) + a structured chat_profile JSON
    for each provider.  Clean/billable providers with no tasks get a static template
    (no LLM cost).  All others are processed in parallel with up to 4 worker threads.

    Results are stored in roster_truth.ai_summary via upsert_ai_summary().
    """
    import datetime
    import json as _json
    import concurrent.futures

    from app.storage.roster_truth_pg import upsert_ai_summary
    from app.services.provider_summary import (
        build_detailed_prompt, build_oneliner_prompt, build_chat_profile,
        parse_oneliner, parse_brief_and_oneliner,
        is_clean_provider, CLEAN_SUMMARY_TEMPLATE,
    )
    try:
        from app.services.llm_manager import generate_sync as _llm_gen
    except Exception as _e:
        logger.warning("_flush_provider_summaries: llm_manager unavailable — %s", _e)
        return

    org = state.org_name or ""
    run_id = state.run_id or ""

    # Collect providers from the run with their detail data
    # We build a minimal "detail" dict from the orchestrator state for each provider.
    # For richer data (PML rows, taxonomy profile), we fetch from the skill server.
    providers_to_summarize: list[dict] = []

    # Build a lookup of per-NPI data from state.active_roster (dict or list)
    npi_to_state: dict[str, dict] = {}
    for p in _flat_active_roster(state):
        npi = str(p.get("npi_validated") or p.get("npi_roster") or p.get("npi") or "").strip().zfill(10)
        if npi and npi != "0000000000":
            npi_to_state[npi] = p

    if not npi_to_state:
        logger.info("_flush_provider_summaries: no providers in state, skipping")
        return

    # Fetch full provider details from skill server (needed for PML rows, taxonomy profile)
    fetched_details: dict[str, dict] = {}  # npi → detail
    try:
        import urllib.request
        # Use the roster truth list to get provider IDs, then fetch details
        list_url = f"{skill_base}/roster/truth/{urllib.parse.quote(org)}"
        with urllib.request.urlopen(list_url, timeout=15) as resp:
            list_data = _json.loads(resp.read().decode())
        for prov in (list_data.get("providers") or []):
            prov_id = prov.get("id")
            npi     = str(prov.get("npi_validated") or prov.get("npi_roster") or "").zfill(10)
            if not prov_id or not npi:
                continue
            try:
                detail_url = f"{skill_base}/roster/truth/{urllib.parse.quote(org)}/provider/{prov_id}"
                with urllib.request.urlopen(detail_url, timeout=15) as dr:
                    detail = _json.loads(dr.read().decode())
                detail["run_id"] = run_id
                fetched_details[npi] = detail
            except Exception as _det_err:
                logger.debug("summary flush: detail fetch failed for npi=%s: %s", npi, _det_err)
    except Exception as _list_err:
        logger.warning("_flush_provider_summaries: roster list fetch failed — %s", _list_err)

    # Supplement with state data for any NPI we couldn't fetch from the skill server
    for npi, p in npi_to_state.items():
        if npi not in fetched_details:
            snap = p.get("nppes_snapshot") or {}
            _name = p.get("provider_name") or p.get("name") or ""
            fetched_details[npi] = {
                "npi":               npi,
                "npi_validated":     npi,
                "provider_name":     _name,
                "org_name":          org,
                "specialty":         p.get("specialty") or snap.get("specialty") or "",
                "billability_status": snap.get("billability_status") or "unknown",
                "nppes_snapshot":    snap,
                "open_tasks":        p.get("open_tasks") or [],
                "pml_rows":          [],
                "taxonomy_profile":  {},
                "drift_flags":       [],
                "run_id":            run_id,
            }

    providers_to_summarize = list(fetched_details.values())
    total = len(providers_to_summarize)
    step_id = "provider_summaries"
    _emit(emitter, f"Generating AI credential summaries for {total} providers…", state, step_id)

    generated = 0
    errors    = 0

    # Periodic state-save so the polling frontend can show live progress
    # without waiting for all summaries to complete.
    def _save_state_progress(run_record: dict | None = None) -> None:
        """Best-effort: persist current emit log to DB mid-loop so poller sees it."""
        try:
            if run_record is None:
                return
            from app.services.credentialing_state_serde import orchestrator_state_to_dict
            run_record["orchestrator_state_dict"] = orchestrator_state_to_dict(state)
            # Keep the frontend showing Step 7 while it's in progress
            run_record["pending_step_id"] = "provider_summaries"
            run_record["phase"] = "running"
            from app.storage.credentialing_runs_pg import save_credentialing_run_record
            save_credentialing_run_record(run_id, run_record)
        except Exception as _se:
            logger.debug("_flush_provider_summaries: mid-loop state save failed (non-fatal): %s", _se)

    # Pull the current run record once so we can update it periodically
    _run_rec: dict | None = None
    try:
        from app.services.credentialing_run_service import _store_get
        _run_rec = _store_get(run_id)
    except Exception:
        pass

    def _generate_one(detail: dict) -> tuple[str, str, dict | None]:
        """Generate and return (npi, name, summary_payload). Returns (npi, name, None) on failure."""
        npi  = str(detail.get("npi") or detail.get("npi_validated") or detail.get("npi_roster") or "").zfill(10)
        name = (detail.get("provider_name") or "Provider").strip().title()
        try:
            if is_clean_provider(detail):
                one_liner    = CLEAN_SUMMARY_TEMPLATE.format(name=name)
                detailed     = f"## Credential Status\n{one_liner}\n\n## Key Risks\n- None\n\n## Recommended Actions\n1. No action required.\n"
                brief        = one_liner
                model_used   = "template"
                in_tok = out_tok = 0
            else:
                # Detailed summary
                raw_detailed, usage = _llm_gen(
                    prompt=build_detailed_prompt(detail),
                    stage="integrator_roster",
                    max_tokens=8192,
                )
                detailed   = "## Credential Status\n" + raw_detailed
                one_liner  = parse_oneliner(detailed)
                model_used = usage.get("model","")
                in_tok     = usage.get("input_tokens", 0)
                out_tok    = usage.get("output_tokens", 0)

                # Brief (short call)
                try:
                    raw_brief, _ = _llm_gen(
                        prompt=build_oneliner_prompt(detail),
                        stage="integrator_roster",
                        max_tokens=256,
                    )
                    _, brief = parse_brief_and_oneliner(raw_brief)
                except Exception:
                    brief = one_liner

            payload = {
                "one_liner":     one_liner,
                "brief":         brief,
                "detailed":      detailed,
                "chat_profile":  build_chat_profile(detail, run_id=run_id),
                "model":         model_used,
                "input_tokens":  in_tok,
                "output_tokens": out_tok,
                "generated_at":  datetime.datetime.utcnow().isoformat() + "Z",
                "run_id":        run_id,
            }
            return (npi, name, payload)
        except Exception as _e:
            logger.warning("summary flush: LLM failed for npi=%s: %s", npi, _e)
            return (npi, name, None)

    max_workers = min(4, total)
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_generate_one, detail): detail for detail in providers_to_summarize}
        for future in concurrent.futures.as_completed(futures):
            npi, name, payload = future.result()
            done += 1
            if payload:
                upsert_ai_summary(org, npi, payload)
                generated += 1
                bill = (fetched_details.get(npi, {}).get("billability_status") or "unknown").lower()
                status_icon = "✓" if bill == "billable" else "⚠" if bill == "warning" else "🚨" if bill in ("risk","blocked","inactive") else "◎"
                _emit(emitter, f"{status_icon} {done}/{total} — {name} ({bill})", state, step_id)
            else:
                errors += 1
                _emit(emitter, f"△ {done}/{total} — {name} (summary skipped)", state, step_id)

            # Save state to DB every 3 providers so poller sees live progress
            if done % 3 == 0:
                _save_state_progress(_run_rec)

    _emit(
        emitter,
        f"✓ AI summaries complete — {generated} generated, {errors} skipped",
        state,
        step_id,
    )
    _save_state_progress(_run_rec)  # final save
    logger.info(
        "_flush_provider_summaries: org=%s run=%s generated=%d errors=%d",
        org, run_id, generated, errors,
    )


def _step_num(step_id: str) -> int:
    """Map step_id to display number 1–12."""
    order = {
        "ensure_benchmarks":         1,
        "identify_org":              2,
        "find_locations":            3,
        "nppes_alignment":           4,
        "find_associated_providers": 5,
        "pml_alignment":             6,
        "taxonomy_optimization":     7,
        "provider_summaries":        8,
        "org_summary":               9,
        # Legacy
        "org_benchmark": 10,
        "find_services_by_location": 11,
        "historic_billing_patterns": 12,
        "step_6": 13,
        "step_7": 14,
        "opportunity_sizing": 15,
        "build_report": 16,
        "npi_profile": 17,
    }
    return order.get(step_id, 0)
