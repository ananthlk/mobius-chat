"""Credentialing runs + NPI lookup endpoints (Phase 1c).

Routes:
    GET    /chat/credentialing-runs                              — list recent runs
    POST   /chat/credentialing-runs                              — start autopilot or copilot run
    DELETE /chat/credentialing-runs/{run_id}                     — cascade delete run + roster data
    POST   /chat/credentialing-runs/{run_id}/seed-roster         — attach existing roster upload
    GET    /chat/credentialing-runs/{run_id}                     — fetch run (optional full state)
    GET    /chat/credentialing-runs/{run_id}/org-npis            — NPIs + NPPES details + prior assertion
    GET    /chat/credentialing-runs/{run_id}/roster-truth        — validated truth roster for org
    POST   /chat/credentialing-runs/{run_id}/roster-truth        — persist validated roster snapshot
    GET    /chat/credentialing-runs/{run_id}/roster-diff         — diff current roster vs truth
    POST   /chat/credentialing-runs/{run_id}/roster-snooze       — snooze a mismatch
    GET    /chat/credentialing-runs/{run_id}/roster-snoozes      — list active snoozes
    POST   /chat/credentialing-runs/{run_id}/validate            — commit validated step, advance
    PATCH  /chat/credentialing-runs/{run_id}/pml-tasks           — persist PML task state
    PATCH  /chat/credentialing-runs/{run_id}/taxonomy-tasks      — persist taxonomy task state
    GET    /chat/npi-lookup/{npi}                                — NPPES single-NPI lookup

Extracted from ``app/main.py`` as Phase 1c of the main-split refactor.
External URLs preserved via ``app.include_router``.

This router is also the staging ground for Phase 3 (credentialing → its
own package). Once all credentialing HTTP surface lives here, extracting
the package is a matter of moving this file + its service/storage
dependencies wholesale.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.parse
import urllib.request
import uuid
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api._common import task_manager_base_url as _task_manager_base
from app.storage.threads import ensure_thread, save_state

logger = logging.getLogger(__name__)

router = APIRouter(tags=["credentialing"])


# ── Request bodies ─────────────────────────────────────────────────────────


class CredentialingRunCreateBody(BaseModel):
    """Start credentialing report run: autopilot (full pipeline) or copilot (step-by-step validation)."""

    org_name: str = ""
    mode: Literal["autopilot", "copilot"] = "copilot"
    thread_id: str | None = None


class CredentialingValidateBody(BaseModel):
    """Commit user-validated output for the pending step; server runs the next step."""

    step_id: str = ""
    validated_output: dict[str, Any] = {}


class PmlTaskStateBody(BaseModel):
    done: list[str] = []
    notes: dict[str, str] = {}
    manual: list[dict] = []
    dismissed: list[str] = []
    providerLocations: dict[str, int] = {}   # npi-taxonomy key → confirmed location index


class TaxonomyTaskStateBody(BaseModel):
    done: list[str] = []
    notes: dict[str, str] = {}
    dismissed: list[str] = []


# ── Local helpers ──────────────────────────────────────────────────────────


# Phase 1e: _task_manager_base consolidated into app.api._common — imported
# above under its original name for call-site stability.


def _fetch_nppes_single(npi: str) -> dict[str, Any] | None:
    """Single-NPI NPPES lookup, returned as a compact dict. None if not found.

    Shared by /chat/npi-lookup and /chat/credentialing-runs/{run_id}/org-npis
    — they were inlining the same ~30 lines of NPPES unpacking logic.
    """
    qs = urllib.parse.urlencode({"version": "2.1", "number": npi})
    url = f"https://npiregistry.cms.hhs.gov/api/?{qs}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    results = data.get("results") or []
    if not results:
        return None
    r = results[0]
    basic = r.get("basic") or {}
    addrs = r.get("addresses") or []
    loc = next(
        (a for a in addrs if a.get("address_purpose") == "LOCATION"),
        addrs[0] if addrs else {},
    )
    taxonomies = r.get("taxonomies") or []
    primary_tax = next(
        (t for t in taxonomies if t.get("primary")),
        taxonomies[0] if taxonomies else {},
    )
    first = (basic.get("first_name") or "").strip()
    last = (basic.get("last_name") or "").strip()
    name = f"{first} {last}".strip() if (first or last) else (
        basic.get("organization_name") or ""
    ).strip()
    return {
        "npi": npi,
        "name": name or None,
        "status": basic.get("status"),
        "entity_type": r.get("enumeration_type"),
        "enumeration_date": basic.get("enumeration_date"),
        "last_updated": basic.get("last_updated"),
        "address": ", ".join(
            filter(
                None,
                [
                    loc.get("address_1"),
                    loc.get("city"),
                    loc.get("state"),
                    (loc.get("postal_code") or "")[:5],
                ],
            )
        ),
        "city": loc.get("city"),
        "state": loc.get("state"),
        "phone": loc.get("telephone_number"),
        "taxonomy": primary_tax.get("desc"),
        "taxonomy_code": primary_tax.get("code"),
    }


# ── Credentialing-runs CRUD ────────────────────────────────────────────────


@router.get("/chat/credentialing-runs")
def list_credentialing_runs_endpoint(limit: int = 30, offset: int = 0) -> list[dict[str, Any]]:
    """List recent credentialing runs (lightweight, no full state)."""
    try:
        from app.storage.credentialing_runs_pg import list_credentialing_runs
        return list_credentialing_runs(limit=limit, offset=offset)
    except Exception:
        return []


@router.post("/chat/credentialing-runs")
def post_credentialing_runs(body: CredentialingRunCreateBody) -> dict[str, Any]:
    """Create a credentialing pipeline run.

    - autopilot: seeds a run record immediately, runs full orchestrator in
      background thread.
    - copilot: runs the first step synchronously; use POST .../validate
      with validated_output, then repeat until phase=complete.
    """
    from app.services.credentialing_run_service import create_credentialing_run

    org = (body.org_name or "").strip()
    if not org:
        raise HTTPException(status_code=400, detail="org_name is required")
    tid = ensure_thread((body.thread_id or "").strip() or None)

    if body.mode == "autopilot":
        # Seed a run record immediately so the frontend can start polling,
        # then run the full orchestrator in a background thread.
        from app.services.credentialing_run_service import _public_view, _store_put

        run_id = str(uuid.uuid4())
        stub: dict[str, Any] = {
            "run_id": run_id,
            "thread_id": tid,
            "org_name": org,
            "mode": "autopilot",
            "phase": "running",
            "pending_step_id": None,
            "draft_output": None,
            "validated_outputs": {},
            "error": None,
            "final_report_text": None,
            "orchestrator_state_dict": None,
        }
        _store_put(run_id, stub)
        save_state(
            tid,
            {"active": {"credentialing_run_id": run_id, "credentialing_run_mode": "autopilot"}},
        )

        def _bg():
            try:
                create_credentialing_run(org, "autopilot", thread_id=tid, run_id=run_id)
            except Exception as _e:
                logger.warning("autopilot bg run failed: %s", _e)

        threading.Thread(target=_bg, daemon=True, name=f"autopilot-{run_id[:8]}").start()
        stub["thread_id"] = tid
        return _public_view(stub)

    try:
        result = create_credentialing_run(org, body.mode, thread_id=tid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    save_state(
        tid,
        {"active": {"credentialing_run_id": result.get("run_id"), "credentialing_run_mode": body.mode}},
    )
    result["thread_id"] = tid
    return result


@router.delete("/chat/credentialing-runs/{run_id}", status_code=200)
def delete_credentialing_run_endpoint(run_id: str) -> dict[str, Any]:
    """Permanently delete a credentialing run and all associated roster/reconciliation data.

    Cascade order:
    1. Extract step3_roster_upload_id from the run (before deletion).
    2. Call skill server DELETE /roster/reconcile/{upload_id} to wipe providers,
       validation_results, reconciliation_report (with llm_clean_cache),
       api_envelopes, and files on disk — so a new run for the same org always
       starts fresh.
    3. Delete the credentialing_runs row.
    """
    from app.services.credentialing_run_service import get_credentialing_run
    from app.storage.credentialing_runs_pg import delete_credentialing_run

    # Step 1: grab the upload_id before we delete the run
    run_rec = get_credentialing_run(run_id, include_state=True)
    if not run_rec:
        raise HTTPException(status_code=404, detail="run not found")

    upload_id: str | None = None
    try:
        ostate = run_rec.get("orchestrator_state") or {}
        upload_id = ostate.get("step3_roster_upload_id") or None
    except Exception:
        pass

    # Step 2: cascade-delete skill-server data for this upload
    if upload_id:
        skill_base = (
            os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL") or ""
        ).rstrip("/").split("/report")[0]
        if skill_base:
            try:
                with httpx.Client(timeout=15.0) as _c:
                    _resp = _c.delete(f"{skill_base}/roster/reconcile/{upload_id}")
                    logger.info(
                        "cascade delete upload_id=%s status=%s",
                        upload_id,
                        _resp.status_code,
                    )
            except Exception as _e:
                logger.warning(
                    "cascade delete for upload_id=%s failed (non-fatal): %s",
                    upload_id,
                    _e,
                )

    # Step 3: delete the run row itself
    deleted = delete_credentialing_run(run_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="run not found")
    return {"deleted": True, "run_id": run_id, "upload_id_purged": upload_id}


@router.post("/chat/credentialing-runs/{run_id}/seed-roster")
def seed_run_roster(run_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Persist a roster upload_id into the run's orchestrator state so it auto-loads next time."""
    upload_id = (body.get("roster_upload_id") or "").strip()
    if not upload_id:
        raise HTTPException(status_code=400, detail="roster_upload_id required")
    try:
        from app.storage.credentialing_runs_pg import patch_step3_upload_id
        ok = patch_step3_upload_id(run_id, upload_id)
        return {"ok": ok, "run_id": run_id, "roster_upload_id": upload_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chat/credentialing-runs/{run_id}")
def get_credentialing_run_endpoint(run_id: str, full: int = 0) -> dict[str, Any]:
    from app.services.credentialing_run_service import get_credentialing_run

    rec = get_credentialing_run(run_id, include_state=bool(full))
    if not rec:
        raise HTTPException(status_code=404, detail="run not found")
    return rec


@router.get("/chat/credentialing-runs/{run_id}/org-npis")
def get_credentialing_run_org_npis(run_id: str) -> dict[str, Any]:
    """Return org NPIs for this run with NPPES details + any previously persisted assertion."""
    from app.services.credentialing_run_service import get_credentialing_run

    rec = get_credentialing_run(run_id, include_state=True)
    if not rec:
        raise HTTPException(status_code=404, detail="run not found")

    # Current NPIs from orchestrator state
    state = rec.get("orchestrator_state") or {}
    current_npis: list[str] = state.get("org_npis") or []
    org_name: str = (rec.get("org_name") or "").strip()

    # Previously persisted assertion for this org (most recent run)
    prev_npis: list[dict] = []
    prev_validated_at: str | None = None
    try:
        import psycopg2

        from app.storage.credentialing_assertions_pg import _db_url

        url = _db_url()
        if url:
            with psycopg2.connect(url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT org_npi, validated_at, material
                        FROM credentialing_assertion
                        WHERE lower(org_name) = lower(%s)
                          AND fact_kind = 'org_npi'
                          AND valid_to IS NULL
                        ORDER BY validated_at DESC NULLS LAST
                        LIMIT 20
                        """,
                        (org_name,),
                    )
                    rows = cur.fetchall()
                    for npi, vat, mat in rows:
                        if npi:
                            prev_npis.append(
                                {
                                    "npi": str(npi),
                                    "validated_at": vat.isoformat() if vat else None,
                                    "detail": json.loads(mat) if isinstance(mat, str) else (mat or {}),
                                }
                            )
                    if rows:
                        prev_validated_at = rows[0][1].isoformat() if rows[0][1] else None
    except Exception:
        pass

    # Fetch NPPES details for each current NPI
    nppes_details: dict[str, dict] = {}
    try:
        for npi in current_npis[:10]:
            d = _fetch_nppes_single(npi)
            if d:
                # Keep the subset main.py used to return for this endpoint.
                nppes_details[npi] = {
                    "npi": d["npi"],
                    "name": d["name"],
                    "status": d["status"],
                    "entity_type": d["entity_type"],
                    "address": d["address"],
                    "city": d["city"],
                    "state": d["state"],
                    "phone": d["phone"],
                    "taxonomy": d["taxonomy"],
                    "taxonomy_code": d["taxonomy_code"],
                }
    except Exception:
        pass

    return {
        "run_id": run_id,
        "org_name": org_name,
        "current_npis": current_npis,
        "nppes_details": nppes_details,
        "previously_persisted": prev_npis,
        "prev_validated_at": prev_validated_at,
    }


# ── NPI lookup ─────────────────────────────────────────────────────────────


@router.get("/chat/npi-lookup/{npi}")
def npi_lookup(npi: str) -> dict[str, Any]:
    """Fetch a single NPI from the NPPES registry. Used for manual NPI entry in Step 1."""
    npi = npi.strip()
    if not npi.isdigit() or len(npi) != 10:
        raise HTTPException(status_code=400, detail="NPI must be exactly 10 digits")
    try:
        d = _fetch_nppes_single(npi)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"NPPES lookup failed: {e}") from e
    if not d:
        raise HTTPException(status_code=404, detail="NPI not found in NPPES registry")
    return d


# ── Roster truth + snooze ──────────────────────────────────────────────────


@router.get("/chat/credentialing-runs/{run_id}/roster-truth")
def get_roster_truth(run_id: str) -> dict[str, Any]:
    """Return the validated truth roster for this run's org."""
    from app.services.credentialing_run_service import get_credentialing_run
    from app.storage.roster_truth_pg import ensure_schema, get_truth_for_org

    rec = get_credentialing_run(run_id)
    if not rec:
        raise HTTPException(status_code=404, detail="run not found")
    ensure_schema()
    org = rec.get("org_name") or ""
    truth = get_truth_for_org(org)
    return {"org_name": org, "run_id": run_id, "providers": truth, "count": len(truth)}


@router.post("/chat/credentialing-runs/{run_id}/roster-truth")
def save_roster_truth(run_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Persist a validated roster snapshot.

    Body: ``{providers: [{provider_name, npi_roster, npi_validated, specialty,
    match_confidence, decision}]}``.
    """
    from app.services.credentialing_run_service import get_credentialing_run
    from app.storage.roster_truth_pg import ensure_schema, upsert_providers

    rec = get_credentialing_run(run_id)
    if not rec:
        raise HTTPException(status_code=404, detail="run not found")
    ensure_schema()
    org = rec.get("org_name") or ""
    providers = body.get("providers") or []
    count = upsert_providers(org, providers, run_id=run_id)
    return {"org_name": org, "run_id": run_id, "saved": count}


@router.get("/chat/credentialing-runs/{run_id}/roster-diff")
def get_roster_diff(run_id: str) -> dict[str, Any]:
    """Compute a diff of the current run's roster against the validated truth table.

    Returns providers tagged with ``change_type``: new | changed | unchanged | removed.
    """
    from app.services.credentialing_run_service import get_credentialing_run
    from app.storage.roster_truth_pg import (
        diff_roster_against_truth,
        ensure_schema,
        get_snoozes_for_org,
    )

    rec = get_credentialing_run(run_id, include_state=True)
    if not rec:
        raise HTTPException(status_code=404, detail="run not found")
    ensure_schema()
    org = rec.get("org_name") or ""

    # Extract current providers from orchestrator state
    state = rec.get("orchestrator_state") or {}
    providers: list[dict[str, Any]] = (
        state.get("providers") or state.get("roster_providers") or []
    )

    diffed = diff_roster_against_truth(org, providers)
    snoozes = get_snoozes_for_org(org)

    # Annotate rows with snooze status per mismatch dimension
    snooze_index: dict[tuple, dict] = {}
    for s in snoozes:
        snooze_index[(s["provider_key"], s["dimension"])] = s

    counts = {"new": 0, "changed": 0, "unchanged": 0, "removed": 0, "total": len(diffed)}
    for p in diffed:
        ct = p.get("change_type", "new")
        counts[ct] = counts.get(ct, 0) + 1
        for fc in p.get("field_changes") or []:
            key = (p.get("npi_validated") or p.get("npi_roster") or "", fc["field"])
            if key in snooze_index:
                s = snooze_index[key]
                fc["snoozed"] = True
                fc["snoozed_at"] = (
                    s["snoozed_at"].isoformat()
                    if hasattr(s["snoozed_at"], "isoformat")
                    else str(s["snoozed_at"])
                )
                fc["fingerprint_match"] = (
                    str(fc.get("roster_val", "")) == str(s["roster_val"] or "")
                    and str(fc.get("nppes_val", "")) == str(s["nppes_val"] or "")
                )

    delta = sum(1 for p in diffed if p["change_type"] in ("new", "changed"))
    return {
        "org_name": org,
        "run_id": run_id,
        "providers": diffed,
        "counts": counts,
        "delta": delta,
        "auto_pass": delta == 0,
    }


@router.post("/chat/credentialing-runs/{run_id}/roster-snooze")
def snooze_roster_mismatch(run_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Snooze a mismatch for a provider.

    Body: ``{provider_key, dimension, roster_val, nppes_val, expires_at?}``.
    """
    from app.services.credentialing_run_service import get_credentialing_run
    from app.storage.roster_truth_pg import ensure_schema, snooze_mismatch

    rec = get_credentialing_run(run_id)
    if not rec:
        raise HTTPException(status_code=404, detail="run not found")
    ensure_schema()
    org = rec.get("org_name") or ""
    ok = snooze_mismatch(
        org_name=org,
        provider_key=body.get("provider_key") or "",
        dimension=body.get("dimension") or "",
        roster_val=str(body.get("roster_val") or ""),
        nppes_val=str(body.get("nppes_val") or ""),
        expires_at=body.get("expires_at"),
    )
    return {"snoozed": ok, "org_name": org, "provider_key": body.get("provider_key")}


@router.get("/chat/credentialing-runs/{run_id}/roster-snoozes")
def list_roster_snoozes(run_id: str) -> dict[str, Any]:
    """Return all active snoozes for this run's org."""
    from app.services.credentialing_run_service import get_credentialing_run
    from app.storage.roster_truth_pg import ensure_schema, get_snoozes_for_org

    rec = get_credentialing_run(run_id)
    if not rec:
        raise HTTPException(status_code=404, detail="run not found")
    ensure_schema()
    org = rec.get("org_name") or ""
    snoozes = get_snoozes_for_org(org)
    return {"org_name": org, "snoozes": snoozes, "count": len(snoozes)}


# ── Validate + advance ─────────────────────────────────────────────────────


@router.post("/chat/credentialing-runs/{run_id}/validate")
def post_credentialing_run_validate(
    run_id: str, body: CredentialingValidateBody
) -> dict[str, Any]:
    from app.services.credentialing_run_service import (
        _public_view,
        _store_get,
        _store_put,
        rerun_step_for_run,
        validate_and_advance_credentialing_run,
    )

    sid = (body.step_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="step_id is required")

    # If the caller sets rerun=true, bypass the copilot phase-gate and re-execute
    # the step in-place (used by Refresh buttons for on-demand steps like PML).
    if body.validated_output.get("rerun"):
        try:
            return rerun_step_for_run(run_id, sid)
        except KeyError:
            raise HTTPException(status_code=404, detail="run not found") from None
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None

    # Verify run exists before going async
    rec = _store_get(run_id)
    if not rec:
        raise HTTPException(status_code=404, detail="run not found")

    validated_output = body.validated_output or {}

    # Mark the run as "running" in the DB immediately so the polling frontend
    # sees the transition right away (not after the heavy step finishes).
    rec["phase"] = "running"
    _store_put(run_id, rec)

    # Run the heavy orchestrator work in a background thread so the server
    # stays responsive for other requests (roster page, health checks, etc.).
    # The frontend polls GET /chat/credentialing-runs/{run_id} for progress.
    def _bg():
        try:
            validate_and_advance_credentialing_run(run_id, sid, validated_output)
        except Exception as _e:
            logger.warning("validate background task failed run=%s: %s", run_id, _e)

    t = threading.Thread(target=_bg, daemon=True, name=f"validate-{run_id[:8]}")
    t.start()

    view = _public_view(rec)
    view["phase"] = "running"
    view["pending_step_id"] = sid
    return view


# ── PML + taxonomy task state ──────────────────────────────────────────────


@router.patch("/chat/credentialing-runs/{run_id}/pml-tasks")
def patch_pml_tasks(run_id: str, body: PmlTaskStateBody) -> dict[str, Any]:
    """Persist PML task state (done flags, notes, manual tasks, dismissed rows,
    confirmed locations) for a run.
    """
    from app.storage.credentialing_runs_pg import patch_pml_task_state

    state = {
        "done": body.done,
        "notes": body.notes,
        "manual": body.manual,
        "dismissed": body.dismissed,
        "providerLocations": body.providerLocations,
    }
    ok = patch_pml_task_state(run_id, state)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to persist task state")

    # Mirror resolved/dismissed into task-manager (best-effort)
    try:
        base = _task_manager_base()
        if base:
            with httpx.Client(timeout=5.0) as _c:
                for tid in body.done or []:
                    _c.post(
                        f"{base}/tasks/{tid}/resolve",
                        json={"resolved_by": "pml_patch", "note": body.notes.get(tid)},
                    )
                for tid in body.dismissed or []:
                    _c.post(
                        f"{base}/tasks/{tid}/dismiss",
                        json={"dismissed_by": "pml_patch"},
                    )
    except Exception:
        pass

    return {"ok": True}


@router.patch("/chat/credentialing-runs/{run_id}/taxonomy-tasks")
def patch_taxonomy_tasks(run_id: str, body: TaxonomyTaskStateBody) -> dict[str, Any]:
    """Persist taxonomy task state (done flags, notes, dismissed) for a run."""
    from app.storage.credentialing_runs_pg import patch_taxonomy_task_state

    state = {
        "done": body.done,
        "notes": body.notes,
        "dismissed": body.dismissed,
    }
    ok = patch_taxonomy_task_state(run_id, state)
    if not ok:
        raise HTTPException(
            status_code=500, detail="Failed to persist taxonomy task state"
        )

    # Mirror resolved/dismissed into task-manager (best-effort)
    try:
        base = _task_manager_base()
        if base:
            with httpx.Client(timeout=5.0) as _c:
                for tid in body.done or []:
                    _c.post(
                        f"{base}/tasks/{tid}/resolve",
                        json={
                            "resolved_by": "taxonomy_patch",
                            "note": body.notes.get(tid),
                        },
                    )
                for tid in body.dismissed or []:
                    _c.post(
                        f"{base}/tasks/{tid}/dismiss",
                        json={"dismissed_by": "taxonomy_patch"},
                    )
    except Exception:
        pass

    return {"ok": True}
