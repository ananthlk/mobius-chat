"""Tests for master objective and continuity."""
import pytest

from app.state.master_objective import (
    MasterObjective,
    SubObjective,
    create_or_update_objective,
)
from app.state.objective_eval import (
    evaluate_sub_objective_status,
    update_objective_from_answers,
    update_objective_from_integrator,
)
from app.communication.user_leverage import format_user_ask
from app.planner.schemas import Plan, SubQuestion


def test_create_objective_from_plan() -> None:
    plan = Plan(subquestions=[
        SubQuestion(id="sq1", text="What is the prior auth requirement?", kind="non_patient"),
        SubQuestion(id="sq2", text="What is the ICD code for X?", kind="non_patient"),
    ])
    state = {}
    obj = create_or_update_objective(plan, state, is_new_question=True)
    assert obj is not None
    assert obj.status == "active"
    assert len(obj.sub_objectives) == 2
    assert all(so.status == "pending" for so in obj.sub_objectives)
    assert "prior auth" in obj.summary
    assert "ICD" in obj.summary


def test_evaluate_sub_objective_status() -> None:
    assert evaluate_sub_objective_status("The prior auth requirement is 5 days.") == "answered"
    assert evaluate_sub_objective_status(
        "The context does not contain information about this."
    ) == "failed"
    assert evaluate_sub_objective_status(
        "The specific ICD code is not specified in the provided context."
    ) == "failed"
    assert evaluate_sub_objective_status("", None) == "failed"
    assert evaluate_sub_objective_status("Peer support services are...", "planner_pre_resolved") == "answered"


def test_update_objective_from_answers() -> None:
    plan = Plan(subquestions=[
        SubQuestion(id="sq1", text="Q1?", kind="non_patient"),
        SubQuestion(id="sq2", text="Q2?", kind="non_patient"),
    ])
    obj = MasterObjective(
        id="o1",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        status="active",
        summary="Q1; Q2",
        sub_objectives=[
            SubObjective(id="sq1", text="Q1?", status="pending"),
            SubObjective(id="sq2", text="Q2?", status="pending"),
        ],
        attempts=0,
    )
    updated = update_objective_from_answers(
        obj,
        plan,
        answers=["The answer is 5 days.", "The context does not contain the answer."],
        retrieval_signals=["corpus_only", None],
    )
    assert updated is not None
    assert updated.sub_objectives[0].status == "answered"
    assert updated.sub_objectives[0].answer == "The answer is 5 days."
    assert updated.sub_objectives[1].status == "failed"
    assert updated.sub_objectives[1].answer is None
    assert updated.status == "active"
    assert updated.attempts == 1


def test_update_objective_from_integrator() -> None:
    obj = MasterObjective(
        id="o1",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        status="active",
        summary="Q1; Q2",
        sub_objectives=[
            SubObjective(id="sq1", text="Q1?", status="answered"),
            SubObjective(id="sq2", text="Q2?", status="failed"),
        ],
        attempts=1,
    )
    integrator_output = {
        "direct_answer": "...",
        "resolved_subquestions": ["sq2"],
        "resolutions": [{"sq_id": "sq2", "question": "Q2?", "resolution": "User provided: X is H0038.", "source": "user_input"}],
    }
    updated = update_objective_from_integrator(obj, integrator_output)
    assert updated is not None
    assert updated.sub_objectives[0].status == "answered"
    assert updated.sub_objectives[1].status == "answered"
    assert updated.sub_objectives[1].answer == "User provided: X is H0038."
    assert updated.status == "solved"


def test_format_user_ask() -> None:
    msg = format_user_ask("no_evidence", "prior auth for Sunshine Health")
    assert "prior auth" in msg
    assert "document" in msg or "link" in msg
    msg2 = format_user_ask("missing_code", "ICD code for X")
    assert "code" in msg2
    msg3 = format_user_ask("partial_answer", "part C", answered_parts=["part A", "part B"])
    assert "part A" in msg3 or "part B" in msg3
    assert "part C" in msg3
