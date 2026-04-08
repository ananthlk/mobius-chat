"""Credentialing runs: autopilot (full orchestrator) vs co-pilot (validate each step).

Records are keyed by run_id. When CHAT_RAG_DATABASE_URL is set and table credentialing_runs exists,
runs persist in Postgres so the chat worker and the FastAPI validate endpoint share state.
Otherwise falls back to in-process memory (single-process dev only).
"""

from __future__ import annotations

import copy
import logging
import threading
import uuid
from collections.abc import Callable
from typing import Any, Literal

from app.services.credentialing_gate_event import (
    append_gate_event,
    build_user_validated_event,
    emit_gate_event,
    get_credentialing_prerequisites_status,
)
from app.services.credentialing_workflow_followups import merge_user_follow_ups
from app.services.credentialing_state_serde import orchestrator_state_from_dict, orchestrator_state_to_dict
from app.services.roster_credentialing_orchestrator import (
    ROSTER_CREDENTIALING_PLAN,
    ROSTER_CREDENTIALING_STEP_IDS,
    OrchestratorState,
    StepState,
    run_credentialing_step,
    run_orchestrator,
)

logger = logging.getLogger(__name__)

Mode = Literal["autopilot", "copilot"]


def _active_roster_from_score_cutoff(associated: dict[str, Any], cutoff: int) -> dict[str, list[Any]]:
    """Build active_roster from associated using score only (copilot shortcut, matches autopilot cutoff)."""
    out: dict[str, list[Any]] = {}
    for loc_id, rows in (associated or {}).items():
        picked: list[Any] = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            if int(r.get("association_likelihood") or 0) >= cutoff:
                p = copy.deepcopy(r)
                p["roster_substatus"] = "promoted_by_cutoff"
                p["roster_status"] = "active"
                picked.append(p)
        out[str(loc_id)] = picked
    return out
Phase = Literal["awaiting_validation", "complete", "error"]


def _emit_credentialing_assertion_sync(
    emitter: Callable[[str], None] | None,
    sync: dict[str, Any] | None,
) -> None:
    """Stream human-readable assertion persistence outcomes (added / validated / revised / removed)."""
    if not emitter or not sync:
        return
    if sync.get("persisted"):
        em = sync.get("emit")
        if isinstance(em, list):
            for line in em:
                if line:
                    emitter(line)
        elif isinstance(em, str) and em:
            emitter(em)
        return
    if sync.get("reason") == "no_database_url":
        emitter("◌ credentialing_assertion: not persisted (database URL not configured).")
        return
    err = sync.get("error")
    if err:
        emitter(f"◌ credentialing_assertion: persist failed ({err}).")
        return
    emitter("◌ credentialing_assertion: not persisted.")


_run_lock = threading.Lock()
_runs: dict[str, dict[str, Any]] = {}


def _persist_compliance_findings(
    run_id: str,
    org_name: str,
    candidates: list[dict[str, Any]],
    agentic: bool = False,
    emitter: Callable[[str], None] | None = None,
) -> None:
    """POST compliance findings to the skill server for Postgres persistence (best-effort)."""
    if not candidates or not org_name:
        return
    import json as _json
    import os as _os
    import urllib.request as _ur

    skill_url = (
        _os.environ.get("CREDENTIALING_SKILL_URL", "http://localhost:8010").rstrip("/")
    )
    url = f"{skill_url}/compliance/{org_name}/findings/sync"
    payload = _json.dumps(
        {"run_id": run_id, "findings": candidates, "agentic": agentic}
    ).encode("utf-8")
    try:
        req = _ur.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _ur.urlopen(req, timeout=15) as resp:
            result = _json.loads(resp.read().decode())
        upserted = result.get("upserted", 0)
        alerted = result.get("auto_billing_alerts", 0)
        msg = f"◉ compliance: {upserted} finding(s) persisted"
        if alerted:
            msg += f", {alerted} billing alert(s) auto-created"
        logger.info("compliance sync run_id=%s %s", run_id, msg)
        if emitter:
            emitter(msg + ".")
    except Exception as e:
        logger.debug("compliance findings persist skipped: %s", e)
        if emitter:
            emitter(f"◌ compliance: not persisted ({e}).")


def _store_put(run_id: str, rec: dict[str, Any]) -> None:
    rid = (run_id or "").strip()
    if not rid:
        return
    with _run_lock:
        _runs[rid] = rec
    try:
        from app.storage.credentialing_runs_pg import save_credentialing_run_record

        save_credentialing_run_record(rid, rec)
    except Exception:
        pass


def _store_get(run_id: str) -> dict[str, Any] | None:
    rid = (run_id or "").strip()
    if not rid:
        return None
    try:
        from app.storage.credentialing_runs_pg import load_credentialing_run_record

        pg = load_credentialing_run_record(rid)
        if pg:
            with _run_lock:
                _runs[rid] = pg
            return pg
    except Exception:
        pass
    with _run_lock:
        return _runs.get(rid)


def _fresh_state(
    org_name: str,
    *,
    step3_upload_id: str | None = None,
    step3_external_only: bool = False,
    step3_include_roster_members: bool = True,
) -> OrchestratorState:
    return OrchestratorState(
        steps=[StepState(id=s["id"], label=s["label"]) for s in ROSTER_CREDENTIALING_PLAN],
        org_npis=[],
        org_name=(org_name or "").strip(),
        step3_roster_upload_id=(step3_upload_id or "").strip(),
        step3_external_only=bool(step3_external_only),
        step3_include_roster_members=bool(step3_include_roster_members),
    )


def _credentialing_assertion_envelope(step_id: str) -> dict[str, Any]:
    """Rings the same persistence + review contract as find_associated (credentialing_assertion table)."""
    fk = {
        "identify_org": "org_npi",
        "find_locations": "location",
        "find_associated_providers": "provider_link",
    }.get(step_id)
    if not fk:
        return {}
    return {
        "credentialing_assertion": {
            "table": "credentialing_assertion",
            "fact_kind": fk,
            "versioning": "valid_to_null_is_current",
            "validate_only_updates": "validated_at_same_material_hash",
        }
    }


def _draft_workflow_block(step_id: str, state: OrchestratorState) -> dict[str, Any]:
    st = state.step_by_id(step_id)
    wfu = copy.deepcopy(getattr(st, "workflow_follow_ups", None) or []) if st else []
    return {
        "workflow_follow_ups": wfu,
        "workflow_follow_ups_hint": (
            "Optional: one operational follow-up per line (e.g. ‘contact provider to fix NPPES’, "
            "‘add to roster’). Stored on this step when you continue."
        ),
    }


def _step_output_for(step_id: str, state: OrchestratorState) -> dict[str, Any] | None:
    """Return the most-recent StepOutput for step_id as a dict, or None."""
    for so in reversed(state.step_outputs):
        if so.step_id == step_id:
            return {
                "label":      so.label,
                "row_count":  so.row_count,
                "csv_preview": so.csv_content[:2000] if so.csv_content else None,
                "markdown":   so.markdown_content[:2000] if so.markdown_content else None,
                "extra_data": getattr(so, "extra_data", None) or {},
            }
    return None


def extract_draft_for_step(step_id: str, state: OrchestratorState) -> dict[str, Any]:
    """Structured draft the UI can show for user validation (subset/add rows)."""
    st = state.step_by_id(step_id)
    status = st.status if st else "unknown"
    summary = st.result_summary if st else ""
    wf = _draft_workflow_block(step_id, state)
    so = _step_output_for(step_id, state)

    def _emit_log(sid: str) -> list[str]:
        return list(state.step_emit_log.get(sid, []))

    if step_id == "ensure_benchmarks":
        return {"step_id": step_id, "status": status, "result_summary": summary,
                "step_output": so, "step_emit_log": _emit_log(step_id), **wf}
    if step_id == "identify_org":
        return {
            "step_id": step_id,
            "status": status,
            "result_summary": summary,
            "org_npis": list(state.org_npis),
            "step_output": so, "step_emit_log": _emit_log(step_id),
            **_credentialing_assertion_envelope(step_id),
            **wf,
        }
    if step_id == "find_locations":
        locs = copy.deepcopy(state.locations) if isinstance(state.locations, list) else []
        # Enrich with why_listed labels for display
        _WHY = {
            "initial": "User-provided initial site",
            "org_nppes": "From NPPES org address",
            "org_pml": "From PML org address",
            "servicing_nppes": "Servicing facility (NPPES)",
            "servicing_pml": "Servicing facility (PML)",
        }
        for loc in locs:
            if isinstance(loc, dict):
                src = loc.get("site_source", "")
                loc.setdefault("why_listed", _WHY.get(src, src or "Found via registry"))
        return {
            "step_id": step_id,
            "status": status,
            "result_summary": summary,
            "locations": locs,
            "step_emit_log": list(state.step_emit_log.get(step_id, [])),
            "step_output": so, "step_emit_log": _emit_log(step_id),
            **_credentialing_assertion_envelope(step_id),
            **wf,
        }
    if step_id == "find_associated_providers":
        # ── Build flat provider list with source tags and bucket classification ──
        assoc = state.associated_providers or {}
        flat: list[dict] = []
        seen_npi: set[str] = set()
        for loc_id, providers in assoc.items():
            for p in providers or []:
                npi = str(p.get("npi") or "").strip()
                prov = p.get("provenance") or {}
                mt   = (p.get("match_type") or "").lower()
                # Source flags
                on_roster  = "roster_upload_id" in prov or mt.startswith("roster")
                from_nppes = (not on_roster) or ("nppes_pml_zip_union" in str(prov.get("source", "")))
                from_doge  = prov.get("source") == "doge_servicing_npis" or "historic_billing" in mt
                # Anomaly flags
                nppes_info = p.get("nppes_status") or {}
                anom: list[str] = []
                if npi and npi in seen_npi:
                    anom.append("duplicate_npi")
                if nppes_info.get("active") is False:
                    anom.append("inactive_npi")
                if p.get("name_status") in ("no_match", "mismatch"):
                    anom.append("name_mismatch")
                if from_doge and not on_roster:
                    anom.append("bills_not_on_roster")
                if npi:
                    seen_npi.add(npi)
                # Bucket
                sources = (["roster"] if on_roster else []) + (["nppes"] if from_nppes or (not on_roster and not from_doge) else []) + (["doge"] if from_doge else [])
                if not sources:
                    sources = ["nppes"]
                n_sources = len(sources)
                if anom:
                    bucket = "anomaly"
                elif n_sources >= 2 and on_roster:
                    bucket = "aligned"
                elif not on_roster:
                    bucket = "external_only"
                else:
                    bucket = "needs_attention"
                flat.append({
                    "npi": npi,
                    "name": (p.get("name") or p.get("provider_name") or "").strip(),
                    "entity_type": "facility" if str(p.get("entity_type","")) == "2" else "individual",
                    "specialty": (p.get("specialty") or p.get("taxonomy_description") or ""),
                    "taxonomy_code": (p.get("taxonomy_code") or ""),
                    "location_id": loc_id,
                    "roster_status": p.get("roster_status", ""),
                    "association_likelihood": p.get("association_likelihood", 0),
                    "sources": sources,
                    "match_type": mt,
                    "inclusion_reasons": p.get("inclusion_reasons") or [],
                    "roster_rationale": p.get("roster_rationale") or "",
                    "anomalies": anom,
                    "bucket": bucket,
                })
        # Graph node counts
        buckets: dict[str, int] = {"aligned": 0, "needs_attention": 0, "anomaly": 0, "external_only": 0}
        source_counts: dict[str, int] = {"roster": 0, "nppes": 0, "doge": 0}
        for fp in flat:
            buckets[fp["bucket"]] = buckets.get(fp["bucket"], 0) + 1
            for s in fp["sources"]:
                source_counts[s] = source_counts.get(s, 0) + 1
        # ── Compliance candidates (deduped, roster-filtered) ──────────────────
        candidates = list(getattr(state, "compliance_candidates", None) or [])
        rostered_excluded = int(getattr(state, "compliance_rostered_excluded", 0) or 0)
        ghost_count = sum(1 for c in candidates if c.get("association_type") == "ghost_billing")
        unrostered_count = sum(1 for c in candidates if c.get("association_type") == "unrostered_associate")
        high_conf = sum(1 for c in candidates if int(c.get("score") or 0) >= 65)
        return {
            "step_id": step_id,
            "status": status,
            "result_summary": summary,
            "providers": flat[:200],           # flat list, capped for API size
            "provider_count": len(flat),
            "bucket_counts": buckets,
            "source_counts": source_counts,
            "associated_providers": copy.deepcopy(state.associated_providers),
            "active_roster": copy.deepcopy(state.active_roster),
            "active_roster_cutoff": getattr(state, "last_active_roster_cutoff", None),
            # ── Compliance data ────────────────────────────────────────────────
            "compliance_candidates": candidates[:200],
            "compliance_candidate_count": len(candidates),
            "compliance_rostered_excluded": rostered_excluded,
            "compliance_ghost_billing_count": ghost_count,
            "compliance_unrostered_count": unrostered_count,
            "compliance_high_confidence_count": high_conf,
            "compliance_methodology": {
                "score_threshold_agentic": 65,
                "score_threshold_display": 40,
                "description": (
                    "Providers found in DOGE/NPPES/PML with strong association to this org "
                    "who are NOT in the approved roster_truth. "
                    "Score ≥65 triggers agentic billing alerts."
                ),
            },
            "step_output": so, "step_emit_log": _emit_log(step_id),
            **_credentialing_assertion_envelope(step_id),
            **wf,
        }
    if step_id == "nppes_alignment":
        return {
            "step_id": step_id, "status": status, "result_summary": summary,
            "step_output": so, "step_emit_log": _emit_log(step_id), **wf,
        }
    if step_id == "pml_alignment":
        return {
            "step_id": step_id, "status": status, "result_summary": summary,
            "pml_validated_count": len(state.pml_validated),
            "pml_flagged_count": len(state.pml_flagged),
            "step_output": so, "step_emit_log": _emit_log(step_id), **wf,
        }
    if step_id == "taxonomy_optimization":
        analysis = list(getattr(state, "taxonomy_analysis", None) or [])
        n_restriction = sum(1 for a in analysis if a.get("result_type") == "restriction")
        n_gap         = sum(1 for a in analysis if a.get("result_type") == "gap_only")
        n_clean       = sum(1 for a in analysis if a.get("result_type") == "clean")
        n_no_data     = sum(1 for a in analysis if a.get("result_type") == "no_nppes_taxonomies")
        return {
            "step_id": step_id, "status": status, "result_summary": summary,
            "analyzed_count":     len(analysis),
            "restriction_count":  n_restriction,
            "gap_count":          n_gap,
            "clean_count":        n_clean,
            "no_data_count":      n_no_data,
            "taxonomy_analysis":  analysis,
            "step_output": so, "step_emit_log": _emit_log(step_id), **wf,
        }
    if step_id == "provider_summaries":
        extra = (so or {}).get("extra_data") or {}
        return {
            "step_id": step_id, "status": status, "result_summary": summary,
            "extra_data": extra,
            "step_output": so, "step_emit_log": _emit_log(step_id), **wf,
        }

    if step_id == "org_summary":
        extra = (so or {}).get("extra_data") or {}
        return {
            "step_id": step_id, "status": status, "result_summary": summary,
            "extra_data": extra,
            "step_output": so, "step_emit_log": _emit_log(step_id), **wf,
        }

    # Legacy removed steps — kept for backward compat with old stored runs
    if step_id in ("ensure_benchmarks", "org_benchmark", "find_services_by_location",
                   "historic_billing_patterns", "step_6", "step_7", "opportunity_sizing", "build_report"):
        return {"step_id": step_id, "status": status, "result_summary": summary,
                "step_output": so, "step_emit_log": _emit_log(step_id), **wf}
    return {
        "step_id": step_id,
        "status": status,
        "result_summary": summary,
        "step_output": so, "step_emit_log": _emit_log(step_id),
        **wf,
    }


def apply_validated_output(state: OrchestratorState, step_id: str, validated: dict[str, Any]) -> None:
    """Merge user-validated payload into orchestrator state before the next step runs."""
    v = validated or {}

    if "roster_upload_id" in v and v["roster_upload_id"]:
        state.step3_roster_upload_id = str(v["roster_upload_id"]).strip()

    if "org_npis" in v and isinstance(v["org_npis"], list):
        state.org_npis = [str(x).strip() for x in v["org_npis"] if str(x).strip()]

    if "locations" in v and isinstance(v["locations"], list):
        state.locations = copy.deepcopy(v["locations"])
        state.locations_count = len(state.locations)

    if "associated_providers" in v and isinstance(v["associated_providers"], dict):
        state.associated_providers = copy.deepcopy(v["associated_providers"])
    if "active_roster" in v and isinstance(v["active_roster"], dict):
        state.active_roster = copy.deepcopy(v["active_roster"])

    if "org_benchmark" in v and isinstance(v["org_benchmark"], dict):
        state.org_benchmark = copy.deepcopy(v["org_benchmark"])
    if "pml_validated" in v and isinstance(v["pml_validated"], list):
        state.pml_validated = copy.deepcopy(v["pml_validated"])
    if "pml_flagged" in v and isinstance(v["pml_flagged"], list):
        state.pml_flagged = copy.deepcopy(v["pml_flagged"])
    if "missing_enrollment" in v and isinstance(v["missing_enrollment"], list):
        state.missing_enrollment = copy.deepcopy(v["missing_enrollment"])

    st_ap = state.step_by_id(step_id)
    if st_ap is not None and "workflow_follow_ups" in v:
        merge_user_follow_ups(st_ap, v.get("workflow_follow_ups"))

    logger.debug("Applied validated output for step %s (keys=%s)", step_id, list(v.keys()))


def create_credentialing_run(
    org_name: str,
    mode: Mode,
    thread_id: str | None = None,
    emitter: Callable[[str], None] | None = None,
    credentialing_options: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Start a run. Autopilot completes in one shot. Copilot runs first step then waits for validate.

    ``emitter`` streams orchestrator progress (same strings as chat tool) when provided.
    ``run_id`` allows the caller to pre-seed a run record and pass its id so the orchestrator
    updates the same record rather than creating a new one.
    """
    org_name = (org_name or "").strip()
    if not org_name:
        raise ValueError("org_name is required")

    run_id = (run_id or "").strip() or str(uuid.uuid4())
    tid = (thread_id or "").strip() or None

    active: dict[str, Any] = {}
    if tid:
        try:
            from app.storage.threads import get_state

            st = get_state(tid)
            active = (st or {}).get("active") or {}
        except Exception:
            active = {}
    from app.pipeline.credentialing_envelope import resolve_step3_roster_merge_context

    uid3, ext3, incl3 = resolve_step3_roster_merge_context(active, credentialing_options)

    # Seed from the most recent previous run for this org so users don't start
    # from scratch.  The explicit credentialing_options / thread active state
    # always wins; the seed only fills in values that are still empty.
    prev_seed: dict[str, Any] = {}
    try:
        from app.storage.credentialing_runs_pg import get_latest_run_seed_for_org
        prev_seed = get_latest_run_seed_for_org(org_name)
    except Exception:
        pass
    if not uid3 and prev_seed.get("step3_roster_upload_id"):
        uid3 = prev_seed["step3_roster_upload_id"]
        logger.info(
            "new run for %r seeded step3_roster_upload_id=%s from %s",
            org_name, uid3, prev_seed.get("_seeded_from_run_id", "?"),
        )

    if mode == "autopilot":
        try:
            final_text, state = run_orchestrator(
                org_name,
                emitter=emitter,
                roster_upload_id=uid3,
                external_only=ext3,
                include_roster_members=incl3,
            )
        except Exception as e:
            logger.exception("autopilot credentialing run failed")
            rec = {
                "run_id": run_id,
                "thread_id": tid,
                "org_name": org_name,
                "mode": mode,
                "phase": "error",
                "pending_step_id": None,
                "draft_output": None,
                "validated_outputs": {},
                "error": str(e),
                "final_report_text": None,
                "orchestrator_state_dict": None,
            }
            _store_put(run_id, rec)
            return _public_view(rec)

        failed_st = state.first_failed_step()
        if failed_st is not None:
            err_msg = f"{failed_st.id}: {failed_st.result_summary or 'failed'}"
            rec = {
                "run_id": run_id,
                "thread_id": tid,
                "org_name": org_name,
                "mode": mode,
                "phase": "error",
                "pending_step_id": None,
                "draft_output": None,
                "validated_outputs": {},
                "error": err_msg,
                "final_report_text": None,
                "orchestrator_state_dict": orchestrator_state_to_dict(state),
            }
            _store_put(run_id, rec)
            return _public_view(rec)

        rec = {
            "run_id": run_id,
            "thread_id": tid,
            "org_name": org_name,
            "mode": mode,
            "phase": "complete",
            "pending_step_id": None,
            "draft_output": None,
            "validated_outputs": {sid: {"_autopilot": True} for sid in ROSTER_CREDENTIALING_STEP_IDS},
            "error": None,
            "final_report_text": final_text,
            "orchestrator_state_dict": orchestrator_state_to_dict(state),
        }
        _store_put(run_id, rec)
        assertion_sync: dict[str, Any] | None = None
        try:
            from app.storage.credentialing_assertions_pg import persist_autopilot_snapshot

            assertion_sync = persist_autopilot_snapshot(
                run_id,
                tid,
                org_name,
                org_npis=list(state.org_npis),
                locations=list(state.locations or []),
                associated_providers=dict(state.associated_providers or {}),
                active_roster=dict(state.active_roster or {}),
                policy_version=None,
                ruleset_hash=None,
            )
        except Exception as e:
            logger.debug("autopilot credentialing_assertion snapshot skipped", exc_info=True)
            assertion_sync = {"persisted": False, "error": str(e), "table": "credentialing_assertion"}
        if assertion_sync:
            rec["last_credentialing_assertion_sync"] = assertion_sync
            _store_put(run_id, rec)
            if assertion_sync.get("persisted"):
                logger.info(
                    "credentialing_assertion autopilot sync run_id=%s totals=%s",
                    run_id,
                    assertion_sync.get("totals"),
                )
            _emit_credentialing_assertion_sync(emitter, assertion_sync)

        # Persist compliance findings for autopilot runs (agentic=True → auto billing alerts)
        candidates = list(getattr(state, "compliance_candidates", None) or [])
        if candidates:
            _persist_compliance_findings(run_id, org_name, candidates, agentic=True, emitter=emitter)

        return _public_view(rec)

    # copilot: run first step only
    state = _fresh_state(
        org_name,
        step3_upload_id=uid3,
        step3_external_only=ext3,
        step3_include_roster_members=incl3,
    )
    state.run_id = run_id
    first_sid = ROSTER_CREDENTIALING_STEP_IDS[0]
    try:
        run_credentialing_step(org_name, state, first_sid, emitter=emitter)
    except Exception as e:
        logger.exception("copilot first step failed")
        rec = {
            "run_id": run_id,
            "thread_id": tid,
            "org_name": org_name,
            "mode": mode,
            "phase": "error",
            "pending_step_id": None,
            "draft_output": None,
            "validated_outputs": {},
            "error": str(e),
            "final_report_text": None,
            "orchestrator_state_dict": None,
        }
        _store_put(run_id, rec)
        return _public_view(rec)

    fst = state.step_by_id(first_sid)
    if fst and fst.status == "failed":
        rec = {
            "run_id": run_id,
            "thread_id": tid,
            "org_name": org_name,
            "mode": mode,
            "phase": "error",
            "pending_step_id": None,
            "draft_output": None,
            "validated_outputs": {},
            "error": fst.result_summary or f"{first_sid} failed",
            "final_report_text": None,
            "orchestrator_state_dict": orchestrator_state_to_dict(state),
        }
        _store_put(run_id, rec)
        return _public_view(rec)

    draft = extract_draft_for_step(first_sid, state)
    rec = {
        "run_id": run_id,
        "thread_id": tid,
        "org_name": org_name,
        "mode": mode,
        "phase": "awaiting_validation",
        "pending_step_id": first_sid,
        "draft_output": draft,
        "validated_outputs": {},
        "error": None,
        "final_report_text": None,
        "orchestrator_state_dict": orchestrator_state_to_dict(state),
    }
    _store_put(run_id, rec)
    return _public_view(rec)


def get_credentialing_run(run_id: str, include_state: bool = False) -> dict[str, Any] | None:
    rec = _store_get((run_id or "").strip())
    if not rec:
        return None
    return _public_view(rec, include_state=include_state)


def validate_and_advance_credentialing_run(
    run_id: str,
    step_id: str,
    validated_output: dict[str, Any],
    emitter: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Apply user validation for pending step, then run the next pipeline step (co-pilot).

    ``emitter`` is passed to the next ``run_credentialing_step`` when provided.
    """
    rid = (run_id or "").strip()
    sid = (step_id or "").strip()
    rec = _store_get(rid)
    if not rec:
        raise KeyError("run not found")
    if rec.get("mode") != "copilot":
        raise ValueError("validate only applies to copilot runs")
    if rec.get("phase") not in ("awaiting_validation", "running"):
        raise ValueError(f"run is not awaiting validation (phase={rec.get('phase')})")
    if rec.get("pending_step_id") != sid:
        raise ValueError(f"pending step is {rec.get('pending_step_id')!r}, not {sid!r}")

    state = orchestrator_state_from_dict(rec["orchestrator_state_dict"])
    state.run_id = run_id
    org_name = rec["org_name"]
    apply_validated_output(state, sid, validated_output)
    ev_val = build_user_validated_event(sid, org_name=org_name)
    append_gate_event(state, ev_val)
    emit_gate_event(emitter, ev_val)
    if sid == "find_associated_providers":
        from app.services.roster_credentialing_orchestrator import _roster_nonempty

        import os

        if validated_output.get("use_autopilot_active_cutoff"):
            try:
                co = int(os.environ.get("ACTIVE_ROSTER_CUTOFF", "50"))
            except ValueError:
                co = 50
            if not _roster_nonempty(state.active_roster):
                state.active_roster = _active_roster_from_score_cutoff(state.associated_providers, co)

        if not _roster_nonempty(state.active_roster):
            if not validated_output.get("allow_empty_active_roster"):
                # Only enforce when there are associated providers to classify;
                # if find_associated_providers was skipped (no API), both are empty — that's fine.
                if _roster_nonempty(state.associated_providers):
                    raise ValueError(
                        "active_roster has no providers: confirm the active panel, pass use_autopilot_active_cutoff, "
                        "or allow_empty_active_roster."
                    )
        try:
            from app.storage.roster_review_pg import persist_roster_review_from_validate

            persist_roster_review_from_validate(
                rid,
                rec.get("thread_id"),
                org_name,
                list(state.org_npis),
                sid,
                mode=str(rec.get("mode") or "copilot"),
                validated_output=validated_output,
                policy_version=(validated_output.get("policy_version") or None),
                ruleset_hash=(validated_output.get("ruleset_hash") or None),
            )
        except Exception:
            logger.debug("roster review persist skipped", exc_info=True)

        # Persist compliance findings (best-effort, non-blocking)
        candidates = list(getattr(state, "compliance_candidates", None) or [])
        if candidates:
            _persist_compliance_findings(
                rid,
                org_name,
                candidates,
                agentic=(rec.get("mode") == "autopilot"),
                emitter=emitter,
            )

    if sid in ("identify_org", "find_locations", "find_associated_providers"):
        assertion_sync: dict[str, Any] | None = None
        try:
            from app.storage.credentialing_assertions_pg import persist_assertions_after_validate

            assertion_sync = persist_assertions_after_validate(
                rid,
                rec.get("thread_id"),
                org_name,
                sid,
                str(rec.get("mode") or "copilot"),
                org_npis=list(state.org_npis),
                locations=list(state.locations or []),
                associated_providers=dict(state.associated_providers or {}),
                active_roster=dict(state.active_roster or {}),
                policy_version=(validated_output.get("policy_version") or None),
                ruleset_hash=(
                    validated_output.get("ruleset_hash")
                    or validated_output.get("policy_ruleset_hash")
                    or None
                ),
            )
        except Exception as e:
            logger.debug("credentialing_assertion persist skipped", exc_info=True)
            assertion_sync = {
                "persisted": False,
                "error": str(e),
                "table": "credentialing_assertion",
                "step_id": sid,
            }
        if assertion_sync is not None:
            rec["last_credentialing_assertion_sync"] = assertion_sync
            if assertion_sync.get("persisted"):
                logger.info(
                    "credentialing_assertion validate sync run_id=%s step=%s counts=%s",
                    rid,
                    sid,
                    assertion_sync.get("counts"),
                )
            _emit_credentialing_assertion_sync(emitter, assertion_sync)
    else:
        rec.pop("last_credentialing_assertion_sync", None)

    rec["validated_outputs"][sid] = copy.deepcopy(validated_output)

    idx = ROSTER_CREDENTIALING_STEP_IDS.index(sid)
    if idx + 1 >= len(ROSTER_CREDENTIALING_STEP_IDS):
        rec["phase"] = "complete"
        rec["pending_step_id"] = None
        rec["draft_output"] = None
        rec["orchestrator_state_dict"] = orchestrator_state_to_dict(state)
        _store_put(rid, rec)
        return _public_view(rec)

    next_sid = ROSTER_CREDENTIALING_STEP_IDS[idx + 1]

    # Pre-announce the next step in the DB so the polling frontend transitions
    # immediately rather than waiting for the (potentially long) step to finish.
    rec["pending_step_id"] = next_sid
    rec["phase"] = "running"
    rec["orchestrator_state_dict"] = orchestrator_state_to_dict(state)
    _store_put(rid, rec)

    try:
        out = run_credentialing_step(org_name, state, next_sid, emitter=emitter)
    except Exception as e:
        logger.exception("copilot step %s failed", next_sid)
        rec["phase"] = "error"
        rec["error"] = str(e)
        rec["pending_step_id"] = None
        rec["draft_output"] = None
        rec["orchestrator_state_dict"] = orchestrator_state_to_dict(state)
        _store_put(rid, rec)
        return _public_view(rec)

    nst = state.step_by_id(next_sid)
    if nst and nst.status == "failed":
        rec["phase"] = "error"
        rec["error"] = nst.result_summary or f"{next_sid} failed"
        rec["pending_step_id"] = None
        rec["draft_output"] = None
        rec["orchestrator_state_dict"] = orchestrator_state_to_dict(state)
        _store_put(rid, rec)
        return _public_view(rec)

    # If a step set auto_advance=True, skip the copilot gate and immediately run the next step.
    # Loop so that chained auto-advance (e.g. taxonomy→provider_summaries→org_summary) works.
    # NOTE: next_sid is the step that JUST RAN; we advance to the step after it each iteration.
    while getattr(state, "auto_advance", False) and next_sid is not None:
        state.auto_advance = False
        curr_idx = ROSTER_CREDENTIALING_STEP_IDS.index(next_sid)
        if curr_idx + 1 >= len(ROSTER_CREDENTIALING_STEP_IDS):
            # next_sid was the last step — mark complete and exit
            next_sid = None
            break
        auto_sid = ROSTER_CREDENTIALING_STEP_IDS[curr_idx + 1]
        rec["pending_step_id"] = auto_sid
        rec["phase"] = "running"
        rec["orchestrator_state_dict"] = orchestrator_state_to_dict(state)
        _store_put(rid, rec)
        try:
            out = run_credentialing_step(org_name, state, auto_sid, emitter=emitter)
        except Exception as e:
            logger.exception("auto-advance step %s failed", auto_sid)
            rec["phase"] = "error"
            rec["error"] = str(e)
            rec["pending_step_id"] = None
            rec["draft_output"] = None
            rec["orchestrator_state_dict"] = orchestrator_state_to_dict(state)
            _store_put(rid, rec)
            return _public_view(rec)
        nst2 = state.step_by_id(auto_sid)
        if nst2 and nst2.status == "failed":
            rec["phase"] = "error"
            rec["error"] = nst2.result_summary or f"{auto_sid} failed"
            rec["pending_step_id"] = None
            rec["draft_output"] = None
            rec["orchestrator_state_dict"] = orchestrator_state_to_dict(state)
            _store_put(rid, rec)
            return _public_view(rec)
        next_sid = auto_sid  # the step that just ran; loop checks if it set auto_advance again

    # If auto-advance ran through the last step, complete the run without a Continue banner
    _last_step = ROSTER_CREDENTIALING_STEP_IDS[-1]
    if next_sid is None or next_sid == _last_step:
        rec["final_report_text"] = out
        rec["pending_step_id"] = None
        rec["phase"] = "complete"
        rec["orchestrator_state_dict"] = orchestrator_state_to_dict(state)
        _store_put(rid, rec)
        return _public_view(rec)

    rec["pending_step_id"] = next_sid
    rec["draft_output"] = extract_draft_for_step(next_sid, state)
    rec["phase"] = "awaiting_validation"
    rec["orchestrator_state_dict"] = orchestrator_state_to_dict(state)
    _store_put(rid, rec)
    return _public_view(rec)


def _public_view(rec: dict[str, Any], include_state: bool = False) -> dict[str, Any]:
    out = {
        "run_id": rec["run_id"],
        "thread_id": rec.get("thread_id"),
        "org_name": rec.get("org_name"),
        "mode": rec.get("mode"),
        "phase": rec.get("phase"),
        "pending_step_id": rec.get("pending_step_id"),
        "draft_output": rec.get("draft_output"),
        "validated_step_ids": list((rec.get("validated_outputs") or {}).keys()),
        "error": rec.get("error"),
        "final_report_text": rec.get("final_report_text"),
    }
    od = rec.get("orchestrator_state_dict")
    if isinstance(od, dict):
        ge = od.get("gate_events")
        if isinstance(ge, list) and ge:
            out["gate_events"] = ge[-15:]
            out["last_gate_event"] = ge[-1]
        else:
            out["gate_events"] = []
            out["last_gate_event"] = None
    else:
        out["gate_events"] = []
        out["last_gate_event"] = None
    out["credentialing_prerequisites"] = get_credentialing_prerequisites_status()
    od_steps = od.get("steps") if isinstance(od, dict) else None
    wf_track: list[dict[str, Any]] = []
    if isinstance(od_steps, list):
        for s in od_steps:
            if isinstance(s, dict):
                wf_track.append(
                    {
                        "step_id": s.get("id"),
                        "workflow_follow_ups": s.get("workflow_follow_ups") or [],
                    }
                )
    out["workflow_follow_ups_by_step"] = wf_track
    if rec.get("last_credentialing_assertion_sync") is not None:
        out["credentialing_assertion_sync"] = rec["last_credentialing_assertion_sync"]
    if include_state:
        out["orchestrator_state"] = rec.get("orchestrator_state_dict")
        # Attach validated (confirmed) draft outputs and step_outputs so the UI can
        # show per-step data in the history accordion.
        validated = rec.get("validated_outputs") or {}
        # Also pull step_outputs from the orchestrator state for non-decision steps
        step_outputs_map: dict[str, Any] = {}
        od2 = rec.get("orchestrator_state_dict")
        if isinstance(od2, dict):
            for so in od2.get("step_outputs") or []:
                if isinstance(so, dict):
                    step_outputs_map.setdefault(so.get("step_id"), so)
        # Merge: for each known step, produce a minimal draft for the history panel
        enriched: dict[str, Any] = {}
        for sid in ROSTER_CREDENTIALING_STEP_IDS:
            v = validated.get(sid) or {}
            so = step_outputs_map.get(sid)
            merged = dict(v)
            if so and not merged.get("step_output"):
                merged["step_output"] = {
                    "label": so.get("label"),
                    "row_count": so.get("row_count", 0),
                    "csv_preview": (so.get("csv_content") or "")[:2000],
                    "markdown": (so.get("markdown_content") or "")[:2000],
                }
            enriched[sid] = merged
        out["step_drafts"] = enriched
    return out


def list_runs_for_tests() -> int:
    with _run_lock:
        return len(_runs)


def clear_runs_for_tests() -> None:
    with _run_lock:
        _runs.clear()


# ── On-demand step re-run (used by Refresh buttons) ────────────────────────────

# Steps that are safe to re-run at any pipeline phase without disrupting flow.
_RERUNNABLE_STEPS = {"pml_alignment", "nppes_alignment", "taxonomy_optimization"}


def rerun_step_for_run(
    run_id: str,
    step_id: str,
    emitter: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Re-execute a single pipeline step in-place, regardless of current phase.

    Only steps in ``_RERUNNABLE_STEPS`` may be triggered this way (PML refresh,
    etc.) — steps that fetch external data and don't change pipeline sequencing.
    The run record is updated and the public view is returned.

    Raises:
        KeyError:  run not found.
        ValueError: step not rerunnable or run is mid-flight.
    """
    rid = (run_id or "").strip()
    sid = (step_id or "").strip()
    if sid not in _RERUNNABLE_STEPS:
        raise ValueError(f"Step '{sid}' is not rerunnable on demand. Allowed: {sorted(_RERUNNABLE_STEPS)}")

    rec = _store_get(rid)
    if not rec:
        raise KeyError("run not found")

    # Don't allow re-run while the pipeline is actively executing a step
    if rec.get("phase") in ("running", "starting"):
        raise ValueError(f"Run is currently {rec.get('phase')} — wait until idle before refreshing.")

    state = orchestrator_state_from_dict(rec["orchestrator_state_dict"])
    org_name = rec.get("org_name", "")
    state.run_id = run_id

    # Patch org_name onto state if not present (older runs may omit it)
    if not getattr(state, "org_name", None):
        state.org_name = org_name

    # Re-run the step (mutates state in-place)
    run_credentialing_step(org_name, state, sid, emitter=emitter)

    # Persist updated state without changing phase or pending_step
    rec = dict(rec)
    rec["orchestrator_state_dict"] = orchestrator_state_to_dict(state)
    _store_put(rid, rec)

    return _public_view(rec)
