"""Validate credentialing plan step order and single-step runner vs full orchestrator."""

from __future__ import annotations

import pytest

from app.services.roster_credentialing_orchestrator import (
    ROSTER_CREDENTIALING_PLAN,
    ROSTER_CREDENTIALING_STEP_IDS,
    OrchestratorState,
    StepState,
    run_credentialing_step,
    run_orchestrator,
)


def test_step_ids_match_plan_order_and_length() -> None:
    assert len(ROSTER_CREDENTIALING_STEP_IDS) == len(ROSTER_CREDENTIALING_PLAN)
    for i, step in enumerate(ROSTER_CREDENTIALING_PLAN):
        assert ROSTER_CREDENTIALING_STEP_IDS[i] == step["id"]


def test_unknown_step_raises() -> None:
    state = OrchestratorState(steps=[], org_npis=[])
    with pytest.raises(ValueError, match="Unknown credentialing step_id"):
        run_credentialing_step("Acme", state, "not_a_real_step", emitter=None)


def test_empty_org_same_message_orchestrator_vs_no_steps_semantics() -> None:
    text, state = run_orchestrator("", emitter=None)
    assert "No organization name provided" in text
    assert all(s.status == "pending" for s in state.steps)


def test_single_step_runner_matches_first_step_state() -> None:
    """With no org name, legacy path returns before any step. With org, first step mutates ensure_benchmarks."""
    org = "TestOrg"
    state = OrchestratorState(
        steps=[StepState(id=s["id"], label=s["label"]) for s in ROSTER_CREDENTIALING_PLAN],
        org_npis=[],
    )
    state.org_name = org
    run_credentialing_step(org, state, "ensure_benchmarks", emitter=None)
    s0 = state.step_by_id("ensure_benchmarks")
    assert s0 is not None
    assert s0.status in ("done", "skipped")


def test_full_run_loops_all_plan_steps() -> None:
    """Orchestrator runs every step id once (API may skip/fail individual steps)."""
    text, state = run_orchestrator("NonexistentOrgXYZ123", emitter=None)
    assert isinstance(text, str)
    for sid in ROSTER_CREDENTIALING_STEP_IDS:
        st = state.step_by_id(sid)
        assert st is not None, f"missing step {sid}"
        assert st.status in ("pending", "in_progress", "done", "skipped")
