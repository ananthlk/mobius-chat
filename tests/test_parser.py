"""Unit tests for planner parser and plan stage."""
from unittest.mock import patch

import pytest

from app.planner import parse
from app.planner.schemas import Plan, SubQuestion
from app.pipeline.context import PipelineContext
from app.stages.plan import run_plan, _minimal_plan


def test_parse_empty_message():
    """Empty message returns Plan with empty subquestions."""
    plan = parse("")
    assert isinstance(plan, Plan)
    assert plan.subquestions == []


def test_parse_whitespace_only():
    """Whitespace-only message returns Plan with empty subquestions."""
    plan = parse("   \n\t  ")
    assert isinstance(plan, Plan)
    assert plan.subquestions == []


def test_parse_rule_based_decompose_single():
    """Single question (no separators) goes to rule-based; one subquestion."""
    mock_parser = type("Cfg", (), {"use_mobius_planner": False, "patient_keywords": [], "decomposition_separators": [" and ", " also "]})()
    mock_config = type("Config", (), {"parser": mock_parser})()
    with patch("app.chat_config.get_chat_config", return_value=mock_config):
        with patch("app.planner.parser._llm_decompose", return_value=(None, None)):
            plan = parse("What is prior authorization?")
    assert isinstance(plan, Plan)
    assert len(plan.subquestions) >= 1
    assert plan.subquestions[0].text == "What is prior authorization?"


def test_parse_rule_based_decompose_multi():
    """Message with ' and ' splits into multiple subquestions."""
    mock_parser = type("Cfg", (), {"use_mobius_planner": False, "patient_keywords": [], "decomposition_separators": [" and ", " also "]})()
    mock_config = type("Config", (), {"parser": mock_parser})()
    with patch("app.chat_config.get_chat_config", return_value=mock_config):
        with patch("app.planner.parser._llm_decompose", return_value=(None, None)):
            plan = parse("What is PA and how do I file an appeal?")
    assert isinstance(plan, Plan)
    assert len(plan.subquestions) >= 2


def test_minimal_plan():
    """_minimal_plan returns Plan with single non_patient subquestion."""
    plan = _minimal_plan("Hello world")
    assert isinstance(plan, Plan)
    assert len(plan.subquestions) == 1
    assert plan.subquestions[0].id == "sq1"
    assert plan.subquestions[0].text == "Hello world"
    assert plan.subquestions[0].kind == "non_patient"


def test_minimal_plan_empty_message():
    """_minimal_plan with empty message uses fallback text."""
    plan = _minimal_plan("")
    assert plan.subquestions[0].text == "What can you help with?"


def test_run_plan_on_parse_exception():
    """When parse raises, run_plan sets minimal plan and does not re-raise."""
    ctx = PipelineContext(correlation_id="test", thread_id=None, message="Hello")
    ctx.effective_message = "Hello"
    ctx.merged_state = {}
    ctx.classification = "new_question"

    with patch("app.stages.plan.parse", side_effect=RuntimeError("parse failed")):
        run_plan(ctx)

    assert ctx.plan is not None
    assert len(ctx.plan.subquestions) == 1
    assert ctx.plan.subquestions[0].text == "Hello"
    assert ctx.plan.subquestions[0].kind == "non_patient"
    assert ctx.blueprint is not None
    assert len(ctx.blueprint) == 1
