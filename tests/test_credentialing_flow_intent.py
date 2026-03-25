"""credentialing_flow_intent: rule-based data path (not autopilot/copilot)."""
from __future__ import annotations

from app.planner.blueprint import build_blueprint
from app.planner.credentialing_flow_intent import parse_credentialing_flow_intent
from app.planner.schemas import Plan, SubQuestion


def test_outside_in_phrase() -> None:
    i = parse_credentialing_flow_intent("Outside-in credentialing report for Acme")
    assert i.data_path == "outside_in"
    assert i.request_upload_inventory is False


def test_reconciliation_phrase() -> None:
    i = parse_credentialing_flow_intent("Run a reconciliation report for David Lawrence Center")
    assert i.data_path == "reconciliation"


def test_upload_inventory() -> None:
    i = parse_credentialing_flow_intent("What rosters did I upload on this thread?")
    assert i.request_upload_inventory is True


def test_default_credentialing_message_unspecified_path() -> None:
    i = parse_credentialing_flow_intent("Create a credentialing report for Test Org")
    assert i.data_path == "unspecified"


def test_blueprint_steers_reconciliation_tool_hint() -> None:
    plan = Plan(
        subquestions=[
            SubQuestion(
                id="sq1",
                text="Create reconciliation report for Org",
                kind="non_patient",
                question_intent="factual",
                intent_score=0.8,
            )
        ],
        credentialing_flow_intent=parse_credentialing_flow_intent(
            "Create reconciliation report for Org"
        ),
    )
    bp = build_blueprint(plan, retrieval_ctx={"user_message": plan.subquestions[0].text})
    assert bp[0]["agent"] == "tool"
    assert bp[0]["tool_hint"] == "roster_reconciliation"


def test_blueprint_upload_inventory_overrides() -> None:
    plan = Plan(
        subquestions=[
            SubQuestion(
                id="sq1",
                text="List my previous roster uploads",
                kind="non_patient",
                question_intent="factual",
                intent_score=0.8,
            )
        ],
        credentialing_flow_intent=parse_credentialing_flow_intent(
            "List my previous roster uploads on this chat"
        ),
    )
    bp = build_blueprint(plan, retrieval_ctx={"user_message": plan.subquestions[0].text})
    assert bp[0]["agent"] == "tool"
    assert bp[0]["tool_hint"] == "list_thread_document_uploads"
