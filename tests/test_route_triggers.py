"""Tests for deterministic route triggers (web vs RAG)."""
import pytest

from app.planner.route_triggers import detect_route, TRIGGERS_WEB, TRIGGERS_RAG


def test_search_web_single_trigger():
    """'Search the web for X' → tool, 100% confidence."""
    agent, conf, choices = detect_route("Search the web for Florida Medicaid eligibility")
    assert agent == "tool"
    assert conf >= 1.0
    assert choices is None


def test_search_google():
    """'Search google for X' → tool."""
    agent, conf, _ = detect_route("Search google for prior auth process")
    assert agent == "tool"
    assert conf >= 1.0


def test_search_for():
    """'Search for X' → tool."""
    agent, conf, _ = detect_route("Search for Medicaid eligibility in florida")
    assert agent == "tool"
    assert conf >= 1.0


def test_look_up():
    """'Look up X' → tool."""
    agent, conf, _ = detect_route("Look up Sunshine Health contact info")
    assert agent == "tool"
    assert conf >= 1.0


def test_scrape_url():
    """'Scrape https://...' → tool."""
    agent, conf, _ = detect_route("Can you scrape https://www.sunshinehealth.com/providers/utilization-management/clinical-payment-policies.html and tell me key details")
    assert agent == "tool"
    assert conf >= 1.0


def test_what_can_you_do():
    """'What can you do?' → tool (capability question)."""
    agent, conf, _ = detect_route("What can you do?")
    assert agent == "tool"
    assert conf >= 1.0


def test_search_our_manual():
    """'Search our manual for X' → RAG."""
    agent, conf, _ = detect_route("Search our manual for appeal process")
    assert agent == "RAG"
    assert conf >= 1.0


def test_check_our_materials():
    """'Check our materials' → RAG."""
    agent, conf, _ = detect_route("Check our materials for grievance process")
    assert agent == "RAG"
    assert conf >= 1.0


def test_policy_lookup():
    """'Policy lookup' → RAG."""
    agent, conf, _ = detect_route("Policy lookup for prior authorization")
    assert agent == "RAG"
    assert conf >= 1.0


def test_clash_web_and_rag():
    """Both web and RAG triggers → clash, clarify choices."""
    agent, conf, choices = detect_route(
        "Search the web and search our manual for Medicaid eligibility"
    )
    assert agent is None
    assert conf < 1.0
    assert choices is not None
    assert len(choices) == 2
    labels = {c["label"] for c in choices}
    assert "Search web" in labels
    assert "Search our manual" in labels
    # Values should include the original query for re-submission
    for c in choices:
        assert "Medicaid eligibility" in c["value"]


def test_no_trigger():
    """No explicit trigger → no override."""
    agent, conf, choices = detect_route("How do I file an appeal?")
    assert agent is None
    assert conf == 0.0
    assert choices is None


def test_empty_message():
    """Empty message → no override."""
    agent, conf, choices = detect_route("")
    assert agent is None
    assert conf == 0.0
    assert choices is None


def test_button_value_triggers_web():
    """Clicking 'Search web' sends value that triggers tool."""
    value = "Search the web: Medicaid eligibility Florida"
    agent, conf, _ = detect_route(value)
    assert agent == "tool"
    assert conf >= 1.0


def test_button_value_triggers_rag():
    """Clicking 'Search our manual' sends value that triggers RAG."""
    value = "Search our manual: Medicaid eligibility Florida"
    agent, conf, _ = detect_route(value)
    assert agent == "RAG"
    assert conf >= 1.0


def test_blueprint_deterministic_override():
    """Blueprint overrides planner with deterministic route when 'Search for' present."""
    from app.planner.blueprint import build_blueprint
    from app.planner.schemas import Plan, SubQuestion

    # Plan that would normally route to RAG (non_patient, no capabilities_primary)
    plan = Plan(subquestions=[
        SubQuestion(
            id="sq1",
            text="Search for Florida Medicaid eligibility requirements",
            kind="non_patient",
            question_intent="factual",
            intent_score=0.7,
        ),
    ])
    retrieval_ctx = {"user_message": "Search for Florida Medicaid eligibility requirements"}
    blueprint = build_blueprint(plan, rag_default_k=10, retrieval_ctx=retrieval_ctx)
    assert len(blueprint) == 1
    assert blueprint[0]["agent"] == "tool"
