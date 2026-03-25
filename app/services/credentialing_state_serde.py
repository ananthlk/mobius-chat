"""JSON-safe serialization for OrchestratorState (credentialing co-pilot persistence)."""

from __future__ import annotations

from typing import Any

from app.services.roster_credentialing_orchestrator import (
    OrchestratorState,
    StepOutput,
    StepState,
    ROSTER_CREDENTIALING_PLAN,
)


def orchestrator_state_to_dict(state: OrchestratorState) -> dict[str, Any]:
    """Serialize orchestrator state for run store / API."""
    return {
        "org_name": state.org_name,
        "org_npis": list(state.org_npis),
        "locations_count": state.locations_count,
        "locations": list(state.locations) if isinstance(state.locations, list) else state.locations,
        "associated_providers": _stringify_keys(state.associated_providers),
        "active_roster": _stringify_keys(state.active_roster),
        "org_benchmark": dict(state.org_benchmark) if isinstance(state.org_benchmark, dict) else state.org_benchmark,
        "pml_validated": list(state.pml_validated),
        "pml_flagged": list(state.pml_flagged),
        "missing_enrollment": list(state.missing_enrollment),
        "report_final_md": state.report_final_md,
        "report_pdf_base64": state.report_pdf_base64,
        "report_run_id": state.report_run_id,
        "report_summary": dict(state.report_summary) if isinstance(state.report_summary, dict) else state.report_summary,
        "step3_roster_upload_id": getattr(state, "step3_roster_upload_id", "") or "",
        "step3_external_only": bool(getattr(state, "step3_external_only", False)),
        "step3_include_roster_members": bool(getattr(state, "step3_include_roster_members", True)),
        "steps": [
            {"id": s.id, "label": s.label, "status": s.status, "result_summary": s.result_summary}
            for s in state.steps
        ],
        "step_outputs": [
            {
                "step_id": o.step_id,
                "label": o.label,
                "csv_content": o.csv_content,
                "row_count": o.row_count,
                "markdown_content": o.markdown_content,
                "json_content": o.json_content,
            }
            for o in state.step_outputs
        ],
    }


def orchestrator_state_from_dict(data: dict[str, Any]) -> OrchestratorState:
    """Restore OrchestratorState from orchestrator_state_to_dict output."""
    steps_in = data.get("steps") or []
    if steps_in:
        steps = [
            StepState(
                id=str(s.get("id", "")),
                label=str(s.get("label", "")),
                status=str(s.get("status", "pending")),
                result_summary=str(s.get("result_summary", "")),
            )
            for s in steps_in
        ]
    else:
        steps = [StepState(id=s["id"], label=s["label"]) for s in ROSTER_CREDENTIALING_PLAN]

    outs_raw = data.get("step_outputs") or []
    step_outputs = [
        StepOutput(
            step_id=str(o.get("step_id", "")),
            label=str(o.get("label", "")),
            csv_content=str(o.get("csv_content", "")),
            row_count=int(o.get("row_count", 0)),
            markdown_content=str(o.get("markdown_content", "")),
            json_content=str(o.get("json_content", "")),
        )
        for o in outs_raw
    ]

    locs = data.get("locations")
    if not isinstance(locs, list):
        locs = []

    return OrchestratorState(
        steps=steps,
        org_npis=[str(x) for x in (data.get("org_npis") or [])],
        org_name=str(data.get("org_name", "") or ""),
        locations_count=int(data.get("locations_count", 0) or 0),
        locations=locs,
        step3_roster_upload_id=str(data.get("step3_roster_upload_id", "") or ""),
        step3_external_only=bool(data.get("step3_external_only", False)),
        step3_include_roster_members=bool(data.get("step3_include_roster_members", True)),
        associated_providers=_dict_maybe(data.get("associated_providers")),
        active_roster=_dict_maybe(data.get("active_roster")),
        org_benchmark=_dict_maybe(data.get("org_benchmark")),
        pml_validated=list(data.get("pml_validated") or []),
        pml_flagged=list(data.get("pml_flagged") or []),
        missing_enrollment=list(data.get("missing_enrollment") or []),
        step_outputs=step_outputs,
        report_final_md=str(data.get("report_final_md", "") or ""),
        report_pdf_base64=str(data.get("report_pdf_base64", "") or ""),
        report_run_id=str(data.get("report_run_id", "") or ""),
        report_summary=_dict_maybe(data.get("report_summary")),
    )


def _stringify_keys(obj: Any) -> Any:
    if not isinstance(obj, dict):
        return obj
    out: dict[str, Any] = {}
    for k, v in obj.items():
        out[str(k)] = v
    return out


def _dict_maybe(v: Any) -> dict:
    return dict(v) if isinstance(v, dict) else {}
