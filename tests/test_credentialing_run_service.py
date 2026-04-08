"""Credentialing co-pilot run store and validate-and-advance (no provider-roster API required)."""

from __future__ import annotations

import pytest

from app.services.credentialing_run_service import (
    clear_runs_for_tests,
    create_credentialing_run,
    get_credentialing_run,
    validate_and_advance_credentialing_run,
)
from app.services.roster_credentialing_orchestrator import ROSTER_CREDENTIALING_STEP_IDS


@pytest.fixture(autouse=True)
def _clear_runs():
    clear_runs_for_tests()
    yield
    clear_runs_for_tests()


def test_autopilot_run_completes_without_remote_api(monkeypatch) -> None:
    monkeypatch.delenv("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL", raising=False)
    out = create_credentialing_run("TestOrg", "autopilot", thread_id=None)
    assert out["mode"] == "autopilot"
    assert out["phase"] == "complete"
    assert out.get("error") is None
    assert isinstance(out.get("final_report_text"), str)
    full = get_credentialing_run(out["run_id"], include_state=True)
    assert full and full.get("orchestrator_state")


def test_copilot_advances_through_all_steps_with_empty_validation(monkeypatch) -> None:
    monkeypatch.delenv("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL", raising=False)
    out = create_credentialing_run("TestOrg", "copilot", thread_id=None)
    assert out["phase"] == "awaiting_validation"
    assert out["pending_step_id"] == ROSTER_CREDENTIALING_STEP_IDS[0]
    assert out["draft_output"] is not None

    run_id = out["run_id"]
    for _ in range(len(ROSTER_CREDENTIALING_STEP_IDS) - 1):
        pending = out["pending_step_id"]
        assert pending
        out = validate_and_advance_credentialing_run(run_id, pending, {})
        assert out["phase"] == "awaiting_validation"
        assert out["pending_step_id"] != pending

    last_pending = out["pending_step_id"]
    assert last_pending == ROSTER_CREDENTIALING_STEP_IDS[-1]
    out = validate_and_advance_credentialing_run(run_id, last_pending, {})
    assert out["phase"] == "complete"
    assert out["pending_step_id"] is None


def test_copilot_user_filters_locations_persisted(monkeypatch) -> None:
    monkeypatch.delenv("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL", raising=False)
    out = create_credentialing_run("TestOrg", "copilot", thread_id=None)
    run_id = out["run_id"]
    # advance through identify_org with synthetic NPI
    out = validate_and_advance_credentialing_run(run_id, ROSTER_CREDENTIALING_STEP_IDS[0], {})
    assert out["pending_step_id"] == "identify_org"
    out = validate_and_advance_credentialing_run(
        run_id,
        "identify_org",
        {"org_npis": ["1234567893", "0987654321"]},
    )
    assert out["pending_step_id"] == "find_locations"
    full = get_credentialing_run(run_id, include_state=True)
    st = full["orchestrator_state"]
    assert st["org_npis"] == ["1234567893", "0987654321"]

    fake_locs = [
        {"location_id": "a", "npi": "1234567893", "site_address_line_1": "1 Main"},
        {"location_id": "b", "npi": "1234567893", "site_address_line_1": "2 Oak"},
    ]
    out = validate_and_advance_credentialing_run(
        run_id,
        "find_locations",
        {"locations": [fake_locs[0]]},
    )
    assert out["pending_step_id"] == "nppes_alignment"
    full2 = get_credentialing_run(run_id, include_state=True)
    assert len(full2["orchestrator_state"]["locations"]) == 1
    assert full2["orchestrator_state"]["locations"][0]["location_id"] == "a"


def test_validate_wrong_pending_raises(monkeypatch) -> None:
    monkeypatch.delenv("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL", raising=False)
    out = create_credentialing_run("O", "copilot", thread_id=None)
    with pytest.raises(ValueError, match="pending step"):
        validate_and_advance_credentialing_run(out["run_id"], "build_report", {})
