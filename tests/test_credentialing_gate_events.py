"""Gate events and orchestrator state serde (credentialing co-pilot)."""

from __future__ import annotations

from app.services.credentialing_gate_event import append_gate_event, build_user_validated_event
from app.services.credentialing_state_serde import orchestrator_state_from_dict, orchestrator_state_to_dict
from app.services.roster_credentialing_orchestrator import ROSTER_CREDENTIALING_PLAN, OrchestratorState, StepState


def test_gate_events_round_trip_serde():
    st = OrchestratorState(
        steps=[StepState(id=s["id"], label=s["label"]) for s in ROSTER_CREDENTIALING_PLAN],
        org_npis=[],
        org_name="Acme",
    )
    append_gate_event(st, build_user_validated_event("identify_org", org_name="Acme"))
    d = orchestrator_state_to_dict(st)
    st2 = orchestrator_state_from_dict(d)
    assert len(st2.gate_events) == 1
    assert st2.gate_events[0].get("reason_code") == "copilot_user_validated"
    assert st2.gate_events[0].get("step_id") == "identify_org"


def test_gate_events_truncation_in_serde():
    st = OrchestratorState(
        steps=[StepState(id=s["id"], label=s["label"]) for s in ROSTER_CREDENTIALING_PLAN],
        org_npis=[],
    )
    for i in range(3):
        append_gate_event(st, {"kind": "step_end", "step_id": f"s{i}", "reason_code": "test", "detail": str(i)})
    d = orchestrator_state_to_dict(st)
    st2 = orchestrator_state_from_dict(d)
    assert len(st2.gate_events) == 3
    assert st2.gate_events[-1]["detail"] == "2"
