"""Roster reconcile + roster truth + org dismissals endpoints (Phase 1d).

Biggest slice of the Phase 1 main-split refactor: 26 endpoints moved to a
single router module. Covers three URL prefixes:

    /chat/roster-reconcile/*   — skill-server proxy surface (progress SSE,
                                  status, report, llm-clean, NPI search,
                                  provider CRUD + audit log)
    /chat/roster-truth/*       — validated truth roster (per-org provider
                                  CRUD + org summary + AI summaries)
    /chat/roster-org/*         — org-level dismissals

Extracted from ``app/main.py`` as Phase 1d. External URLs preserved via
``app.include_router`` in main.py.

Like Phase 1c, this router stages the roster surface for eventual
extraction into its own package (Phase 3 or 3b). Also reuses the
``_skill_base`` and ``_task_manager_base`` helpers — both duplicated here
rather than imported from main.py so the router stays self-contained and
main.py can eventually drop them too.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

import httpx
from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["roster"])


def _task_manager_base() -> str:
    """Base URL of the task-manager skill server.

    Duplicated from main.py / app.api.credentialing. Consolidation into a
    shared ``app.api._common`` helper is a Phase 1e finishing task.
    """
    return (
        os.environ.get("CHAT_SKILLS_TASK_MANAGER_URL") or "http://localhost:8015"
    ).rstrip("/")


@router.get("/chat/roster-reconcile/{upload_id}/progress")
async def roster_reconcile_progress_proxy(upload_id: str):
    """SSE proxy: stream TurboTax-style validation progress from the skill server.

    Each event from the skill SSE is forwarded directly to the browser.
    Falls back to a single 'complete' event if the skill server is unavailable.
    """
    from fastapi.responses import StreamingResponse as _SR
    import asyncio

    base = (os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL") or "").rstrip("/").split("/report")[0]
    if not base:
        async def _unavailable():
            yield 'event: error\ndata: {"message":"Skill server not configured"}\n\n'
        return _SR(_unavailable(), media_type="text/event-stream",
                   headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    skill_url = f"{base}/roster/reconcile/{upload_id}/progress"

    async def _proxy_stream():
        import httpx
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", skill_url, timeout=300) as resp:
                    async for line in resp.aiter_lines():
                        if line:
                            yield line + "\n"
                        else:
                            yield "\n"
        except Exception as e:
            import json as _j
            yield f"event: error\ndata: {_j.dumps({'message': str(e)})}\n\n"

    return _SR(
        _proxy_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/chat/roster-reconcile/{upload_id}/status")
def roster_reconcile_status_proxy(upload_id: str):
    """Proxy: poll reconciliation status from the skill server."""
    base = (os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL") or "").rstrip("/").split("/report")[0]
    if not base:
        return {"upload_id": upload_id, "status": "unavailable", "progress": {}}
    try:
        import httpx
        with httpx.Client(timeout=10.0) as c:
            r = c.get(f"{base}/roster/reconcile/{upload_id}/status")
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return {"upload_id": upload_id, "status": "error", "error": str(e), "progress": {}}


@router.get("/chat/roster-reconcile/{upload_id}/report")
def roster_reconcile_report_proxy(upload_id: str, quick: bool = False):
    """Proxy: fetch full reconciliation report (providers list) from the skill server.

    ?quick=true is forwarded to the skill server to skip validation_history,
    reducing latency for preload/streaming scenarios.
    """
    base = (os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL") or "").rstrip("/").split("/report")[0]
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")
    try:
        import httpx
        params = {"quick": "true"} if quick else {}
        with httpx.Client(timeout=30.0) as c:
            r = c.get(f"{base}/roster/reconcile/{upload_id}/report", params=params)
            # Pass 4xx responses through as-is so the frontend can distinguish
            # "upload not found / deleted" (404) from a real server error (5xx).
            if r.status_code == 404:
                raise HTTPException(status_code=404, detail=f"Upload {upload_id} not found")
            r.raise_for_status()
            return r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/chat/roster-reconcile/{upload_id}/llm-clean-cache")
def roster_llm_clean_cache_proxy(upload_id: str):
    """Proxy: return cached LLM-clean result if available. 404 = not yet cached (run POST first)."""
    import httpx
    base = (os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL") or "").rstrip("/").split("/report")[0]
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")
    try:
        with httpx.Client(timeout=8.0) as c:
            r = c.get(f"{base}/roster/reconcile/{upload_id}/llm-clean-cache")
            if r.status_code == 404:
                raise HTTPException(status_code=404, detail="Not cached yet")
            r.raise_for_status()
            return r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/chat/roster-reconcile/{upload_id}/llm-clean")
def roster_llm_clean(upload_id: str, force: bool = False):
    """
    Fetch parsed roster rows and run a quick LLM pass to identify junk entries.
    Returns { clean: [...], excluded: [...] } where excluded rows have an exclude_reason.

    The LLM result is cached in the ReconciliationReport after the first run.
    Subsequent calls return the cached result immediately unless ?force=true.
    Caching means page reloads are instant — no LLM re-invocation.
    """
    import httpx, json as _json

    base = (os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL") or "").rstrip("/").split("/report")[0]
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")

    # ── Cache check: return cached result if available and not forcing refresh ──
    if not force:
        try:
            with httpx.Client(timeout=10.0) as c:
                cr = c.get(f"{base}/roster/reconcile/{upload_id}/llm-clean-cache")
                if cr.status_code == 200:
                    cached = cr.json()
                    if cached.get("clean") is not None:
                        return cached
        except Exception:
            pass  # cache miss or skill server unavailable — fall through to LLM

    # Fetch parsed providers from skill (?quick=true skips validation_history for speed)
    try:
        with httpx.Client(timeout=30.0) as c:
            r = c.get(f"{base}/roster/reconcile/{upload_id}/report", params={"quick": "true"})
            r.raise_for_status()
            raw = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch report: {e}")

    providers = raw.get("providers") or []
    if not providers:
        return {"clean": [], "excluded": [], "summary": raw.get("summary") or {}}

    # Separate already-flagged parse errors
    parse_errors = [p for p in providers if p.get("status") == "parse_error"]
    candidates   = [p for p in providers if p.get("status") != "parse_error"]

    # Build name list for LLM (cap at 300 for prompt size)
    sample = candidates[:300]
    name_list = "\n".join(
        f'{i+1}. {(p.get("provider_name") or "").strip() or "(blank)"}'
        for i, p in enumerate(sample)
    )

    prompt = f"""You are cleaning a healthcare provider roster uploaded from an Excel/CSV file.
For each numbered row below, decide KEEP or EXCLUDE.

EXCLUDE any row that is NOT a real individual provider name, including:
- Status labels: "Pending", "Effective", "Not Eligible", "Needs Medicaid First", "Active", "Inactive", "N/A", "TBD"
- Notes or instructions (e.g. "If not credentialed...", "Please note:", "Notes")
- Job titles or role descriptions: "Medical Director", "Registered Interns", "Registerd Interns", "Nursing Staff"
- Organization names or payer names (e.g. "BCBS", "Lucet", anything ending in "ONLY" or containing acronyms like "REGI.")
- Column headers, totals, or metadata rows
- Blank or single-word non-name entries
- Any text that is clearly a footnote, instruction, or category label

KEEP only rows that look like a real person's full name (first + last name, with optional credentials or suffix).

For EXCLUDE rows, give a reason in ≤6 words.

Return ONLY a JSON array — no other text:
[{{"n":1,"action":"KEEP"}},{{"n":2,"action":"EXCLUDE","reason":"status label"}}]

Rows:
{name_list}"""

    from app.services.llm_manager import generate_sync
    clean_rows = list(candidates)  # default: keep all if LLM fails
    excluded_rows = []

    import logging as _logging
    _llm_log = _logging.getLogger(__name__)

    try:
        llm_resp, _usage = generate_sync(
            prompt,
            stage="roster_clean",   # fast models only via Thompson sampling
            max_tokens=2000,
        )
        _llm_log.info("roster_clean LLM used model=%s", _usage.get("model", "?"))

        # Parse JSON from LLM response
        import re
        json_match = re.search(r'\[.*?\]', llm_resp, re.DOTALL)
        if not json_match:
            # Try stripping markdown code fences
            stripped = re.sub(r'```[a-z]*', '', llm_resp).strip()
            json_match = re.search(r'\[.*?\]', stripped, re.DOTALL)

        if json_match:
            decisions = _json.loads(json_match.group(0))
            exclude_set = {
                d["n"] - 1: d.get("reason", "auto-excluded")
                for d in decisions
                if isinstance(d, dict) and str(d.get("action", "")).upper() == "EXCLUDE"
            }
            _llm_log.info("roster_clean: %d EXCLUDE decisions out of %d rows", len(exclude_set), len(sample))
            clean_rows = []
            excluded_rows = []
            for i, p in enumerate(sample):
                if i in exclude_set:
                    excluded_rows.append({**p, "exclude_reason": exclude_set[i]})
                else:
                    clean_rows.append(p)
            # Any providers beyond the 300 sample are kept
            if len(candidates) > 300:
                clean_rows.extend(candidates[300:])
        else:
            _llm_log.warning("roster_clean: could not parse JSON from LLM response, using fallback. Response: %s", llm_resp[:300])
    except Exception as llm_err:
        _llm_log.warning("roster_clean LLM call failed, using fallback: %s", llm_err)
        # Fallback already set above (parse_error only)

    # Merge parse_errors into excluded
    excluded_rows.extend([{**p, "exclude_reason": p.get("parse_notes") or "parse error"} for p in parse_errors])

    # ── Enrich providers with backend-computed display fields ────────────────
    # This moves all business logic out of the frontend JS.
    # Any caller (API, agent, export job) gets the same pre-computed fields.
    try:
        import sys, os as _os
        _skill_path = _os.path.join(_os.path.dirname(__file__), "..", "..", "mobius-skills", "provider-roster-credentialing")
        if _skill_path not in sys.path:
            sys.path.insert(0, _skill_path)
        from app.provider_enrichment import enrich_provider, compute_roster_score, build_recon_tasks
        for p in clean_rows:
            enrich_provider(p)
        roster_score = compute_roster_score(clean_rows)
        recon_tasks  = build_recon_tasks(clean_rows)

        # Mirror recon_tasks into unified task-manager (best-effort)
        try:
            _tm_base = _task_manager_base()
            if _tm_base and recon_tasks:
                import httpx as _httpx
                _org = (raw.get("org_name") or "").strip()
                _enriched_tasks = [
                    {**t, "org_name": _org, "source_module": "roster_recon"}
                    for t in recon_tasks
                ]
                with _httpx.Client(timeout=5.0) as _c:
                    _c.post(f"{_tm_base}/tasks/bulk-import", json={"tasks": _enriched_tasks})
        except Exception:
            pass

    except Exception as _enrich_err:
        import logging as _logging
        _logging.getLogger(__name__).warning("provider enrichment failed (non-fatal): %s", _enrich_err)
        roster_score = None
        recon_tasks  = []

    result = {
        "clean": clean_rows,
        "excluded": excluded_rows,
        "summary": raw.get("summary") or {},
        "roster_score": roster_score,
        "recon_tasks": recon_tasks,
    }

    # ── Persist cache so future page loads are instant ────────────────────────
    try:
        with httpx.Client(timeout=8.0) as c:
            c.post(
                f"{base}/roster/reconcile/{upload_id}/llm-clean-cache",
                json=result,
            )
    except Exception:
        pass  # best-effort — non-fatal if cache write fails

    return result


@router.get("/chat/roster-reconcile/lookup-npi")
def roster_lookup_npi(npi: str = ""):
    """Direct NPPES NPI lookup by number. Returns provider info or 404."""
    n = (npi or "").strip().replace("-", "")
    if not n.isdigit() or len(n) != 10:
        raise HTTPException(status_code=400, detail="NPI must be exactly 10 digits")
    base = _skill_base()
    # Try skill server first
    if base:
        try:
            import httpx
            with httpx.Client(timeout=10.0) as c:
                r = c.get(f"{base}/find-npi-by-number", params={"npi": n})
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
    # Fallback: call NPPES public API directly
    try:
        import urllib.request, urllib.parse, json as _json
        qs = urllib.parse.urlencode({"version": "2.1", "number": n})
        url = f"https://npiregistry.cms.hhs.gov/api/?{qs}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())
        results = data.get("results") or []
        if not results:
            raise HTTPException(status_code=404, detail="NPI not found in NPPES")
        r0 = results[0]
        basic = r0.get("basic") or {}
        first = (basic.get("first_name") or "").strip()
        last  = (basic.get("last_name")  or "").strip()
        name_str = f"{first} {last}".strip() or (basic.get("organization_name") or "").strip()
        taxonomies = r0.get("taxonomies") or []
        specialty = next((t.get("desc","") for t in taxonomies if t.get("primary")), taxonomies[0].get("desc","") if taxonomies else "")
        taxonomy_code = next((t.get("code","") for t in taxonomies if t.get("primary")), taxonomies[0].get("code","") if taxonomies else "")
        addresses = r0.get("addresses") or []
        loc_addr = next((a for a in addresses if a.get("address_purpose") == "LOCATION"), addresses[0] if addresses else {})
        address = ", ".join(p for p in [
            (loc_addr.get("address_1") or "").strip(),
            (loc_addr.get("city") or "").strip(),
            (loc_addr.get("state") or "").strip(),
            (loc_addr.get("postal_code") or "")[:5].strip(),
        ] if p)
        return {
            "npi": r0.get("number"),
            "name": name_str,
            "status": basic.get("status"),
            "specialty": specialty,
            "taxonomy_code": taxonomy_code,
            "address": address,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/chat/roster-reconcile/latest-for-org")
def roster_latest_for_org(org_name: str = ""):
    """Return the latest roster upload_id for an org by name. Used to auto-load on pipeline page."""
    name = (org_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="org_name is required")
    base = _skill_base()
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")
    try:
        import httpx
        with httpx.Client(timeout=10.0) as c:
            r = c.get(f"{base}/roster-uploads/latest-for-org-name", params={"org_name": name})
            if r.status_code == 404:
                raise HTTPException(status_code=404, detail=f"No roster found for {name!r}")
            r.raise_for_status()
            return r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/chat/roster-reconcile/uploads")
def roster_reconcile_uploads_for_org(org_name: str = "", limit: int = 10):
    """List recent roster uploads for an org by name.

    Proxies to skill server GET /roster-uploads/latest-for-org-name and
    returns a paginated list of uploads with upload_id, org_name, status,
    total_providers, and validated_count.
    """
    name = (org_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="org_name is required")
    base = _skill_base()
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")
    try:
        import httpx
        with httpx.Client(timeout=10.0) as c:
            r = c.get(
                f"{base}/roster-uploads/latest-for-org-name",
                params={"org_name": name},
            )
            if r.status_code == 404:
                return {"uploads": [], "org_name": name}
            r.raise_for_status()
            data = r.json()
            # Normalise to list form — skill returns a single upload dict
            upload = data if isinstance(data, dict) else {}
            return {
                "uploads": [upload] if upload.get("upload_id") else [],
                "org_name": name,
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/chat/roster-reconcile/search-nppes")
def roster_search_nppes(name: str = ""):
    """Quick NPPES name search proxy — used by roster table 'no match' rows."""
    if not name.strip():
        return {"results": []}
    base = (os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL") or "").rstrip("/").split("/report")[0]
    if not base:
        return {"results": []}
    try:
        import httpx, urllib.parse
        with httpx.Client(timeout=15.0) as c:
            r = c.get(f"{base}/search/npi-by-name", params={"name": name.strip(), "limit": 5})
            if r.status_code == 200:
                return r.json()
            # Fallback: try NPPES public API directly
            q = urllib.parse.urlencode({"version": "2.1", "search_type": "NPI-1",
                                        "enumeration_type": "NPI-1", "first_name": name.split()[0] if name.split() else "",
                                        "last_name": name.split()[-1] if len(name.split()) > 1 else "", "limit": 5})
            nr = c.get(f"https://npiregistry.cms.hhs.gov/api/?{q}", timeout=10.0)
            if nr.status_code == 200:
                data = nr.json()
                results = []
                for entry in (data.get("results") or []):
                    basic = entry.get("basic") or {}
                    fname = basic.get("first_name", "")
                    lname = basic.get("last_name", "")
                    n = f"{fname} {lname}".strip() or basic.get("organization_name", "")
                    results.append({"npi": entry.get("number", ""), "name": n, "confidence": 0.5,
                                    "specialty": (((entry.get("taxonomies") or [{}])[0]).get("desc") or "")})
                return {"results": results}
    except Exception:
        pass
    return {"results": []}


def _skill_base() -> str:
    """Base URL of the provider-roster-credentialing skill server."""
    return (os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL") or "").rstrip("/").split("/report")[0]


@router.patch("/chat/roster-reconcile/provider/{provider_id}")
def roster_provider_save_decision(provider_id: int, body: dict = Body(...)):
    """Proxy: persist a user decision for a single roster provider.

    Forwards to skill server PATCH /roster/provider/{provider_id}.
    Body fields (all optional):
      name_corrected, npi_corrected, specialty_corrected,
      resolution_reason, correction_notes, correction_source
    """
    base = _skill_base()
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")
    try:
        import httpx
        with httpx.Client(timeout=15.0) as c:
            r = c.patch(f"{base}/roster/provider/{provider_id}", json=body)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.delete("/chat/roster-truth")
def dev_clear_roster_truth(org_name: str):
    """DEV / TEST ONLY — hard-delete all roster_truth rows for an org.

    Not exposed in production UI.  Protected only by obscurity — remove or
    gate behind auth before any public release.
    """
    try:
        from app.storage.roster_truth_pg import delete_roster_truth_for_org, ensure_schema
        ensure_schema()
        deleted = delete_roster_truth_for_org(org_name)
        return {"deleted": deleted, "org_name": org_name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/chat/roster-reconcile/provider/{provider_id}")
def roster_provider_delete(provider_id: int):
    """Proxy: soft-exclude a roster provider (audit trail preserved).

    Forwards to skill server DELETE /roster/provider/{provider_id}.
    """
    base = _skill_base()
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")
    try:
        import httpx
        with httpx.Client(timeout=15.0) as c:
            r = c.delete(f"{base}/roster/provider/{provider_id}")
            r.raise_for_status()
            return r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/chat/roster-reconcile/provider/{provider_id}/revalidate")
def roster_provider_revalidate(provider_id: int, body: dict = Body(default={})):
    """Proxy: re-validate a single provider, optionally with an NPI/name override."""
    base = _skill_base()
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")
    try:
        import httpx
        with httpx.Client(timeout=30.0) as c:
            r = c.post(f"{base}/roster/provider/{provider_id}/revalidate", json=body)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/chat/roster-reconcile/provider/{provider_id}/approve")
def roster_provider_approve(provider_id: int, body: dict = Body(default={})):
    """Proxy: approve provider and write to org roster truth (NPI Anchor model)."""
    base = _skill_base()
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")
    try:
        import httpx
        with httpx.Client(timeout=15.0) as c:
            r = c.post(f"{base}/roster/provider/{provider_id}/approve-to-truth", json=body)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/chat/roster-reconcile/provider/{provider_id}/audit-log")
def roster_write_audit_proxy(provider_id: int, body: dict = Body(default={})):
    """Proxy: write one audit event for a provider (user actions from frontend)."""
    base = _skill_base()
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")
    try:
        import httpx
        with httpx.Client(timeout=10.0) as c:
            r = c.post(f"{base}/roster/provider/{provider_id}/audit-log", json=body)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/chat/roster-reconcile/provider/{provider_id}/audit-log")
def roster_read_provider_audit_proxy(provider_id: int, npi: str = "", limit: int = 100):
    """Proxy: fetch audit trail for a single provider — passes npi so orchestrator events are included."""
    base = _skill_base()
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")
    try:
        import httpx
        params: dict = {"limit": limit}
        if npi:
            params["npi"] = npi
        with httpx.Client(timeout=10.0) as c:
            r = c.get(f"{base}/roster/provider/{provider_id}/audit-log", params=params)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/chat/roster-reconcile/run/{run_id}/audit-log")
def roster_read_run_audit_proxy(run_id: str, org_name: str = "", limit: int = 200):
    """Proxy: fetch macro audit log for a credentialing run."""
    base = _skill_base()
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")
    try:
        import httpx
        with httpx.Client(timeout=10.0) as c:
            r = c.get(f"{base}/roster/run/{run_id}/audit-log",
                      params={"org_name": org_name, "limit": limit})
            r.raise_for_status()
            return r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/chat/roster-truth/{org_name}/provider/{provider_id}/summary")
def roster_provider_summary_proxy(org_name: str, provider_id: int, force: bool = False):
    """Generate AI-written credentialing summary using llm_manager (Thompson sampling).

    If a pre-computed (non-stale) summary exists in roster_truth.ai_summary it is
    served immediately without an LLM call.  Pass ?force=true to regenerate.

    Architecture: this proxy fetches the structured profile from the skill server,
    then calls llm_manager here (in the chat process) so the request participates in
    the same Thompson-sampling bandit and usage tracking as all other LLM calls.
    """
    import time
    import re as _re

    base = _skill_base()
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")

    # 1. Fetch full provider profile from skill server
    try:
        import httpx
        with httpx.Client(timeout=20.0) as c:
            r = c.get(f"{base}/roster/truth/{org_name}/provider/{provider_id}")
            if r.status_code == 404:
                raise HTTPException(status_code=404, detail="Provider not found")
            r.raise_for_status()
            detail = r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch provider profile: {e}")

    # 2. Check for a pre-computed (non-stale) summary in the DB — serve it instantly.
    stored_summary = detail.get("ai_summary") or {}
    if not force and stored_summary.get("detailed") and not detail.get("ai_summary_stale", True):
        return {
            "provider_id":        provider_id,
            "provider_name":      detail.get("provider_name"),
            "org_name":           org_name,
            "summary":            stored_summary["detailed"],
            "summary_short":      stored_summary.get("one_liner", ""),
            "billability_status": detail.get("billability_status"),
            "billability_score":  detail.get("billability_score"),
            "model":              stored_summary.get("model", "cached"),
            "stage":              "integrator_roster",
            "input_tokens":       stored_summary.get("input_tokens", 0),
            "output_tokens":      stored_summary.get("output_tokens", 0),
            "latency_ms":         0,
            "from_cache":         True,
        }

    # 3. No stored summary or stale — generate via LLM and persist back to DB.
    from app.services.provider_summary import (
        build_detailed_prompt, build_oneliner_prompt,
        build_chat_profile, parse_oneliner, parse_brief_and_oneliner,
        is_clean_provider, CLEAN_SUMMARY_TEMPLATE,
    )

    # For clean providers: use a static template (no LLM cost)
    if is_clean_provider(detail):
        one_liner    = CLEAN_SUMMARY_TEMPLATE.format(name=detail.get("provider_name","Provider"))
        summary_text = f"## Credential Status\n{one_liner}\n\n## Key Risks\n- None\n\n## Recommended Actions\n1. No action required.\n"
        usage_meta   = {"model": "template", "input_tokens": 0, "output_tokens": 0, "latency_ms": 0}
    else:
        full_prompt = build_detailed_prompt(detail)
        try:
            from app.services.llm_manager import generate_sync as _llm_gen
            t0 = time.perf_counter()
            raw_text, usage_meta = _llm_gen(
                prompt=full_prompt,
                stage="integrator_roster",
                max_tokens=8192,
            )
            # Prompt already primed with "## Credential Status\n" so prepend it back
            summary_text = "## Credential Status\n" + raw_text
            usage_meta["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"LLM generation failed: {exc}")

        one_liner = parse_oneliner(summary_text)

    # Generate brief via a separate short LLM call
    brief = ""
    try:
        from app.services.llm_manager import generate_sync as _llm_gen2
        ol_raw, _ = _llm_gen2(
            prompt=build_oneliner_prompt(detail),
            stage="integrator_roster",
            max_tokens=256,
        )
        _, brief = parse_brief_and_oneliner(ol_raw)
    except Exception:
        brief = one_liner

    # Persist to DB (fire-and-forget via thread — don't block the HTTP response)
    import threading as _threading
    import datetime as _datetime
    _summary_payload = {
        "one_liner":     one_liner,
        "brief":         brief,
        "detailed":      summary_text,
        "chat_profile":  build_chat_profile(detail, run_id=detail.get("run_id")),
        "model":         usage_meta.get("model", ""),
        "input_tokens":  usage_meta.get("input_tokens", 0),
        "output_tokens": usage_meta.get("output_tokens", 0),
        "generated_at":  _datetime.datetime.utcnow().isoformat() + "Z",
        "run_id":        detail.get("run_id") or "",
    }
    _npi = detail.get("npi") or detail.get("npi_validated") or detail.get("npi_roster") or ""

    def _persist():
        try:
            from app.storage.roster_truth_pg import upsert_ai_summary
            upsert_ai_summary(org_name, _npi, _summary_payload)
        except Exception as _e:
            import logging; logging.getLogger(__name__).warning("summary persist failed: %s", _e)

    _threading.Thread(target=_persist, daemon=True).start()

    return {
        "provider_id":        provider_id,
        "provider_name":      detail.get("provider_name"),
        "org_name":           org_name,
        "summary":            summary_text,
        "summary_short":      one_liner,
        "billability_status": detail.get("billability_status"),
        "billability_score":  detail.get("billability_score"),
        "model":              usage_meta.get("model", ""),
        "stage":              "integrator_roster",
        "input_tokens":       usage_meta.get("input_tokens", 0),
        "output_tokens":      usage_meta.get("output_tokens", 0),
        "latency_ms":         usage_meta.get("latency_ms", 0),
        "from_cache":         False,
    }


@router.get("/chat/roster-truth/{org_name}/provider/{provider_id}")
def roster_provider_detail_proxy(org_name: str, provider_id: int):
    """Proxy: full provider profile — roster_truth + PML + audit log + version history."""
    base = _skill_base()
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")
    try:
        import httpx
        with httpx.Client(timeout=15.0) as c:
            r = c.get(f"{base}/roster/truth/{org_name}/provider/{provider_id}")
            if r.status_code == 404:
                raise HTTPException(status_code=404, detail="Provider not found")
            r.raise_for_status()
            return r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


class _AddProviderBody(BaseModel):
    npi:           str
    provider_name: str
    city:          str = ""
    state_cd:      str = ""
    specialty:     str = ""

@router.post("/chat/roster-truth/{org_name}/provider")
async def roster_provider_add_proxy(org_name: str, body: _AddProviderBody):
    """Proxy: manually add a single provider to the roster."""
    import httpx
    base = _skill_base()
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")
    try:
        with httpx.Client(timeout=15.0) as c:
            r = c.post(f"{base}/roster/truth/{org_name}/provider", json=body.dict())
            if r.status_code == 422:
                raise HTTPException(status_code=422, detail=r.json())
            r.raise_for_status()
            return r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


class _EditProviderBody(BaseModel):
    provider_name: str | None = None
    npi_validated: str | None = None
    city:          str | None = None
    state_cd:      str | None = None
    zip_code:      str | None = None
    phone:         str | None = None
    specialty:     str | None = None
    address_line1: str | None = None

@router.patch("/chat/roster-truth/{org_name}/provider/{provider_id}")
async def roster_provider_edit_proxy(org_name: str, provider_id: int, body: _EditProviderBody):
    """Proxy: edit provider fields (name, NPI, location) in roster_truth."""
    import httpx
    base = _skill_base()
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")
    try:
        with httpx.Client(timeout=15.0) as c:
            r = c.patch(f"{base}/roster/truth/{org_name}/provider/{provider_id}",
                        json={k: v for k, v in body.dict().items() if v is not None})
            if r.status_code == 404:
                raise HTTPException(status_code=404, detail="Provider not found")
            r.raise_for_status()
            return r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/chat/roster-org/{org_name}/dismissals")
def roster_org_dismissals_proxy(org_name: str):
    """Proxy: fetch map of npi → [dismissed dim] for all providers in an org."""
    base = _skill_base()
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")
    try:
        import httpx
        with httpx.Client(timeout=10.0) as c:
            r = c.get(f"{base}/roster/org/{org_name}/dismissals")
            r.raise_for_status()
            return r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/chat/roster-truth/{org_name}/org-summary")
def roster_org_summary_proxy(org_name: str):
    """Proxy: org-level credential health summary generated by Step 8."""
    base = _skill_base()
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")
    try:
        import httpx
        with httpx.Client(timeout=15.0) as c:
            r = c.get(f"{base}/roster/truth/{org_name}/org-summary")
            if r.status_code == 404:
                raise HTTPException(status_code=404, detail="No org summary found — run the pipeline first.")
            r.raise_for_status()
            return r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/chat/roster-truth/{org_name}")
def roster_truth_proxy(org_name: str, limit: int = 500):
    """Proxy: fetch canonical roster (approved providers) for an org."""
    base = _skill_base()
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")
    try:
        import httpx
        with httpx.Client(timeout=10.0) as c:
            r = c.get(f"{base}/roster/truth/{org_name}", params={"limit": limit})
            r.raise_for_status()
            return r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/chat/roster-reconcile/{upload_id}/mass-approve")
def roster_mass_approve_proxy(upload_id: str, body: dict = Body(default={})):
    """Proxy: bulk approve providers to roster_truth."""
    base = _skill_base()
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")
    try:
        import httpx
        with httpx.Client(timeout=60.0) as c:
            r = c.post(f"{base}/roster/reconcile/{upload_id}/mass-approve", json=body)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/chat/roster-reconcile/npi-search")
def roster_npi_search_proxy(name: str = "", state: str = "", npi: str = ""):
    """Proxy: NPPES search for inline re-match panel (name+state or direct NPI)."""
    base = _skill_base()
    if not base:
        raise HTTPException(status_code=503, detail="Skill server not configured")
    try:
        import httpx
        with httpx.Client(timeout=15.0) as c:
            r = c.get(f"{base}/roster/npi-search", params={"name": name, "state": state, "npi": npi})
            r.raise_for_status()
            return r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

