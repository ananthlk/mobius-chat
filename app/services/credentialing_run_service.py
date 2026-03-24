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
Phase = Literal["awaiting_validation", "complete", "error"]

_run_lock = threading.Lock()
_runs: dict[str, dict[str, Any]] = {}


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


def _fresh_state(org_name: str) -> OrchestratorState:
    return OrchestratorState(
        steps=[StepState(id=s["id"], label=s["label"]) for s in ROSTER_CREDENTIALING_PLAN],
        org_npis=[],
        org_name=(org_name or "").strip(),
    )


def extract_draft_for_step(step_id: str, state: OrchestratorState) -> dict[str, Any]:
    """Structured draft the UI can show for user validation (subset/add rows)."""
    st = state.step_by_id(step_id)
    status = st.status if st else "unknown"
    summary = st.result_summary if st else ""

    if step_id == "ensure_benchmarks":
        return {"step_id": step_id, "status": status, "result_summary": summary}
    if step_id == "identify_org":
        return {
            "step_id": step_id,
            "status": status,
            "result_summary": summary,
            "org_npis": list(state.org_npis),
        }
    if step_id == "find_locations":
        return {
            "step_id": step_id,
            "status": status,
            "result_summary": summary,
            "locations": copy.deepcopy(state.locations) if isinstance(state.locations, list) else [],
        }
    if step_id == "find_associated_providers":
        return {
            "step_id": step_id,
            "status": status,
            "result_summary": summary,
            "associated_providers": copy.deepcopy(state.associated_providers),
            "active_roster": copy.deepcopy(state.active_roster),
        }
    if step_id == "build_report":
        return {
            "step_id": step_id,
            "status": status,
            "result_summary": summary,
            "report_run_id": state.report_run_id,
            "report_final_md_preview": (state.report_final_md or "")[:4000],
            "has_pdf": bool(state.report_pdf_base64),
        }
    return {
        "step_id": step_id,
        "status": status,
        "result_summary": summary,
    }


def apply_validated_output(state: OrchestratorState, step_id: str, validated: dict[str, Any]) -> None:
    """Merge user-validated payload into orchestrator state before the next step runs."""
    v = validated or {}

    if "org_npis" in v and isinstance(v["org_npis"], list):
        state.org_npis = [str(x).strip() for x in v["org_npis"] if str(x).strip()]

    if "locations" in v and isinstance(v["locations"], list):
        state.locations = copy.deepcopy(v["locations"])
        state.locations_count = len(state.locations)

    if "associated_providers" in v and isinstance(v["associated_providers"], dict):
        state.associated_providers = copy.deepcopy(v["associated_providers"])
    if "active_roster" in v and isinstance(v["active_roster"], dict):
        state.active_roster = copy.deepcopy(v["active_roster"])
    elif "associated_providers" in v and isinstance(v["associated_providers"], dict):
        state.active_roster = copy.deepcopy(state.associated_providers)

    if "org_benchmark" in v and isinstance(v["org_benchmark"], dict):
        state.org_benchmark = copy.deepcopy(v["org_benchmark"])
    if "pml_validated" in v and isinstance(v["pml_validated"], list):
        state.pml_validated = copy.deepcopy(v["pml_validated"])
    if "pml_flagged" in v and isinstance(v["pml_flagged"], list):
        state.pml_flagged = copy.deepcopy(v["pml_flagged"])
    if "missing_enrollment" in v and isinstance(v["missing_enrollment"], list):
        state.missing_enrollment = copy.deepcopy(v["missing_enrollment"])

    logger.debug("Applied validated output for step %s (keys=%s)", step_id, list(v.keys()))


def create_credentialing_run(
    org_name: str,
    mode: Mode,
    thread_id: str | None = None,
    emitter: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Start a run. Autopilot completes in one shot. Copilot runs first step then waits for validate.

    ``emitter`` streams orchestrator progress (same strings as chat tool) when provided.
    """
    org_name = (org_name or "").strip()
    if not org_name:
        raise ValueError("org_name is required")

    run_id = str(uuid.uuid4())
    tid = (thread_id or "").strip() or None

    if mode == "autopilot":
        try:
            final_text, state = run_orchestrator(org_name, emitter=emitter)
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
        return _public_view(rec)

    # copilot: run first step only
    state = _fresh_state(org_name)
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
    if rec.get("phase") != "awaiting_validation":
        raise ValueError(f"run is not awaiting validation (phase={rec.get('phase')})")
    if rec.get("pending_step_id") != sid:
        raise ValueError(f"pending step is {rec.get('pending_step_id')!r}, not {sid!r}")

    state = orchestrator_state_from_dict(rec["orchestrator_state_dict"])
    org_name = rec["org_name"]
    apply_validated_output(state, sid, validated_output)
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

    if next_sid == "build_report":
        rec["final_report_text"] = out

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
    if include_state:
        out["orchestrator_state"] = rec.get("orchestrator_state_dict")
    return out


def list_runs_for_tests() -> int:
    with _run_lock:
        return len(_runs)


def clear_runs_for_tests() -> None:
    with _run_lock:
        _runs.clear()
