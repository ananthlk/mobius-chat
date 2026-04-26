"""
Standalone roster-upload handler — file_purpose='roster_reconciliation'.

Completely self-contained: proxies to the provider-roster-credentialing skill
via HTTP, optionally links the upload to an active pipeline run, and persists
metadata to thread state.  Does NOT import anything from the chat skill set.
"""
from __future__ import annotations

import io
import json as json_mod
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)


def _skill_base() -> str:
    """Return the base URL of the credentialing skill (no trailing slash)."""
    raw = os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL") or ""
    return raw.rstrip("/").split("/report")[0]


def handle_roster_upload(
    *,
    content: bytes,
    filename: str,
    ext: str,
    org_name: str,
    thread_id: str | None,
    run_id: str | None,
    file_purpose: str,
) -> dict[str, Any]:
    """
    Core handler for roster_reconciliation uploads.

    Steps:
      1. Validate extension + config
      2. Resolve org_id from pipeline run (fast path) — unknown = async backfill
      3. POST file to {skill}/roster-uploads
      4. POST file to {skill}/roster/upload (reconciliation pipeline, non-fatal)
      5. Kick off reconciliation (non-fatal)
      6. Patch step3_roster_upload_id on the pipeline run (background)
      7. Kick off legacy /process in background
      8. Persist to thread state (background)
      9. Fire audit-log event (background, non-fatal)
    10. Return upload metadata immediately
    """
    if ext not in ("csv", "xlsx", "xls"):
        raise HTTPException(
            status_code=400,
            detail="Roster files must be CSV or Excel (.csv, .xlsx, .xls)",
        )

    base = _skill_base()
    if not base:
        raise HTTPException(
            status_code=503,
            detail="Roster upload not configured. Set CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL.",
        )
    if not org_name:
        raise HTTPException(status_code=400, detail="org_name is required for roster uploads")

    # ── 1. Resolve org_id (fast path from pipeline run) ──────────────────
    org_id = ""
    matched_org_name = org_name
    matched_practice_address: str | None = None
    _run_id_val = (run_id or "").strip()

    if _run_id_val:
        try:
            from app.services.credentialing_run_service import _store_get  # lazy import
            rec = _store_get(_run_id_val)
            if rec:
                state_dict = rec.get("orchestrator_state_dict") or {}
                _npi = (
                    state_dict.get("org_npi")
                    or state_dict.get("billing_npi")
                    or (state_dict.get("selected_npis") or [None])[0]
                    or ""
                )
                if _npi:
                    org_id = str(_npi).strip().zfill(10)
                matched_org_name = state_dict.get("org_name") or org_name
        except Exception as _e:
            logger.debug("Could not get org_id from run state: %s", _e)

    _needs_bg_org_search = not org_id

    # ── 2. Upload roster to the credentialing skill ───────────────────────
    ct = (
        "text/csv"
        if ext == "csv"
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    try:
        with httpx.Client(timeout=60.0) as client:
            upload_resp = client.post(
                f"{base}/roster-uploads",
                files={"file": (filename, io.BytesIO(content), ct)},
                data={"org_name": org_name, "org_id": org_id},
            )
    except Exception as e:
        logger.warning("Roster upload request failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Roster upload failed: {e}") from e

    if upload_resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Roster upload rejected: {upload_resp.text[:500]}",
        )
    upload_data = upload_resp.json()
    upload_id: str = upload_data.get("upload_id") or ""
    if not upload_id:
        raise HTTPException(status_code=502, detail="No upload_id from roster upload")

    # ── 2b. Async backfill of org_id ─────────────────────────────────────
    if _needs_bg_org_search:
        _start_bg_org_search(base, org_name, upload_id)

    # ── 3. Register with the reconciliation pipeline ──────────────────────
    reconciliation_upload_id: str | None = None
    reconciliation_ui_url: str | None = None
    try:
        with httpx.Client(timeout=30.0) as rc_client:
            new_upload_resp = rc_client.post(
                f"{base}/roster/upload",
                files={"file": (filename, io.BytesIO(content), ct)},
                data={
                    "org_name": org_name,
                    "file_purpose": "roster_reconciliation",
                    "auto_reconcile": "false",
                    "uploaded_by": "chat",
                },
            )
            if new_upload_resp.status_code == 200:
                rc_data = new_upload_resp.json()
                reconciliation_upload_id = rc_data.get("upload_id") or None
                if reconciliation_upload_id:
                    reconciliation_ui_url = (
                        f"{base}/roster-ui/progress.html"
                        f"?upload_id={reconciliation_upload_id}"
                    )
                    _kick_reconcile(rc_client, base, reconciliation_upload_id, _run_id_val)
            else:
                logger.warning(
                    "New reconciliation upload returned %s", new_upload_resp.status_code
                )
    except Exception as exc:
        logger.warning("New reconciliation upload skipped: %s", exc)

    # ── 3c. Patch step3_roster_upload_id on the pipeline run ─────────────
    if reconciliation_upload_id and _run_id_val:
        threading.Thread(
            target=_patch_run_upload_id,
            args=(_run_id_val, reconciliation_upload_id),
            daemon=True,
        ).start()

    # ── 4. Kick legacy /process in background ────────────────────────────
    proc_payload = json_mod.dumps({"resolve_npi": True, "state": "FL"}).encode()
    threading.Thread(
        target=_run_legacy_process,
        args=(f"{base}/roster-uploads/{upload_id}/process", proc_payload),
        daemon=True,
    ).start()

    # ── 5. Persist to thread state ────────────────────────────────────────
    purpose = file_purpose or "roster_reconciliation"
    tid = (thread_id or "").strip() or str(uuid.uuid4())
    record: dict[str, Any] = {
        "upload_id": upload_id,
        "org_id": org_id,
        "org_name": org_name,
        "purpose": purpose,
        "filename": filename,
        "row_count": 0,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "reconciliation_upload_id": reconciliation_upload_id,
    }
    threading.Thread(
        target=_persist_thread_state,
        args=(tid, record),
        daemon=True,
    ).start()

    # ── 6. Audit log (fire-and-forget) ────────────────────────────────────
    if base:
        threading.Thread(
            target=_log_upload_audit,
            args=(base, org_name, filename, upload_id, reconciliation_upload_id),
            daemon=True,
        ).start()

    return {
        "upload_id": upload_id,
        "org_id": org_id,
        "org_name": org_name,
        "row_count": 0,
        "thread_id": tid,
        "file_purpose": purpose,
        "default_billing_npi": org_id,
        "filename": filename,
        "matched_organization_name": matched_org_name,
        "matched_practice_address": matched_practice_address,
        "reconciliation_upload_id": reconciliation_upload_id,
        "reconciliation_ui_url": reconciliation_ui_url,
    }


# ── Private helpers ────────────────────────────────────────────────────────────

def _kick_reconcile(
    client: httpx.Client,
    base: str,
    reconciliation_upload_id: str,
    run_id: str,
) -> None:
    """Kick off reconciliation (non-fatal). Optionally passes Step-2 org locations."""
    try:
        org_locations: list[dict] = []
        if run_id:
            try:
                from app.services.credentialing_run_service import _store_get
                rec = _store_get(run_id)
                if rec:
                    org_locations = (rec.get("orchestrator_state_dict") or {}).get(
                        "locations", []
                    )
            except Exception as _e:
                logger.debug("Could not load run locations for reconcile: %s", _e)
        client.post(
            f"{base}/roster/reconcile/{reconciliation_upload_id}",
            json={"org_locations": org_locations} if org_locations else None,
            timeout=5.0,
        )
    except Exception:
        pass


def _patch_run_upload_id(run_id: str, reconciliation_upload_id: str) -> None:
    try:
        from app.storage.credentialing_runs_pg import patch_step3_upload_id
        patch_step3_upload_id(run_id, reconciliation_upload_id)
    except Exception as _e:
        logger.debug("patch_step3_upload_id failed: %s", _e)


def _run_legacy_process(url: str, data: bytes) -> None:
    try:
        import urllib.request as _ur
        req = _ur.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with _ur.urlopen(req, timeout=120):
            pass
    except Exception as _e:
        logger.debug("Legacy roster process (background): %s", _e)


def _start_bg_org_search(base_url: str, name: str, upload_id: str) -> None:
    def _search() -> None:
        try:
            import urllib.request as _ur
            req = _ur.Request(
                f"{base_url}/search/org-names",
                data=json_mod.dumps({"name": name}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _ur.urlopen(req, timeout=60) as resp:
                top = (json_mod.loads(resp.read().decode()).get("results") or [{}])[0]
                oid = (top.get("org_id") or top.get("npi") or "").strip().zfill(10)
            if oid and upload_id:
                with httpx.Client(timeout=10) as c:
                    c.patch(f"{base_url}/roster-uploads/{upload_id}", json={"org_id": oid})
        except Exception as _e:
            logger.debug("Background org search: %s", _e)

    threading.Thread(target=_search, daemon=True).start()


def _persist_thread_state(tid: str, record: dict) -> None:
    try:
        from app.storage.threads import append_uploaded_file_record, ensure_thread
        real_tid = ensure_thread(tid)
        append_uploaded_file_record(real_tid, record)
    except Exception as _e:
        logger.debug("Background thread-state save skipped: %s", _e)


def _log_upload_audit(
    skill_base: str,
    org: str,
    fname: str,
    uid: str,
    rc_uid: str | None,
) -> None:
    try:
        import urllib.request as _ur
        evt = [
            {
                "org_name": org,
                "event_type": "uploaded",
                "upload_id": uid,
                "actor": "user",
                "actor_label": "Roster file upload",
                "event_data": {
                    "filename": fname,
                    "upload_id": uid,
                    "reconciliation_upload_id": rc_uid,
                },
            }
        ]
        req = _ur.Request(
            f"{skill_base}/roster/log-events",
            data=json_mod.dumps(evt).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _ur.urlopen(req, timeout=8):
            pass
    except Exception as _e:
        logger.debug("Upload audit log (non-fatal): %s", _e)
