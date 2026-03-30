"""Per-step workflow_follow_ups: serde and user merge."""

from __future__ import annotations

from app.services.credentialing_state_serde import orchestrator_state_from_dict, orchestrator_state_to_dict
from app.services.credentialing_workflow_followups import merge_user_follow_ups, replace_system_follow_ups
from app.services.roster_credentialing_orchestrator import ROSTER_CREDENTIALING_PLAN, OrchestratorState, StepState


def test_workflow_follow_ups_round_trip_serde():
    st = OrchestratorState(
        steps=[StepState(id=s["id"], label=s["label"]) for s in ROSTER_CREDENTIALING_PLAN],
        org_npis=[],
    )
    identify = st.step_by_id("identify_org")
    assert identify is not None
    merge_user_follow_ups(identify, ["Contact billing to confirm NPI"])
    d = orchestrator_state_to_dict(st)
    st2 = orchestrator_state_from_dict(d)
    s2 = st2.step_by_id("identify_org")
    assert s2 and len(s2.workflow_follow_ups) == 1
    assert s2.workflow_follow_ups[0].get("source") == "user"
    assert "NPI" in s2.workflow_follow_ups[0].get("text", "")


def test_replace_system_keeps_user_lines():
    st = StepState(id="find_locations", label="x", workflow_follow_ups=[{"text": "User note", "source": "user"}])
    replace_system_follow_ups(st, [{"text": "System hint", "kind": "test"}])
    assert len(st.workflow_follow_ups) == 2
    sources = {x.get("source") for x in st.workflow_follow_ups}
    assert sources == {"user", "system"}
