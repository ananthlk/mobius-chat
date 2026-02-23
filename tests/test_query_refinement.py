"""Tests for need_query_refinement and concrete scenario detection."""
from __future__ import annotations

import pytest

from app.state.query_refinement import need_query_refinement, _has_concrete_scenario


def _plan(subquestions: list[dict]) -> object:
    """Build a minimal plan-like object with subquestions."""
    class Sq:
        def __init__(self, text, kind="non_patient", intent_score=None):
            self.text = text
            self.kind = kind
            self.intent_score = intent_score

    class Plan:
        def __init__(self, sqs):
            self.subquestions = [Sq(**s) for s in sqs]

    return Plan(subquestions)


class TestHasConcreteScenario:
    """Concrete scenario = age, income, location, patient+qualify -> skip refinement."""

    def test_has_age(self):
        assert _has_concrete_scenario("I have a patient who is 35 years old and needs Medicaid")

    def test_has_income(self):
        assert _has_concrete_scenario("She makes $1200 per month and lives in Tampa")

    def test_has_dependents(self):
        assert _has_concrete_scenario("A family with a 10 year old kid wants to enroll in Medicaid")

    def test_has_location(self):
        assert _has_concrete_scenario("Someone who lives in Tampa Florida asked about eligibility")

    def test_short_message_no_scenario(self):
        assert not _has_concrete_scenario("Medicaid eligibility")

    def test_long_abstract_question_no_scenario(self):
        assert not _has_concrete_scenario(
            "What are the general requirements for Medicaid eligibility in Florida?"
        )


class TestNeedQueryRefinement:
    """Refinement: when to ask user to narrow vs proceed."""

    def test_single_vague_short_still_refines(self):
        plan = _plan([{"text": "help"}])
        should, sugg = need_query_refinement(plan, user_message="help")
        assert should is True
        assert "help" in sugg

    def test_three_subquestions_no_scenario_still_refines(self):
        plan = _plan([
            {"text": "Medicaid eligibility"},
            {"text": "Health plans in Tampa"},
            {"text": "Enrollment process"},
        ])
        should, sugg = need_query_refinement(
            plan, user_message="What is Medicaid eligibility and how do I enroll?"
        )
        assert should is True
        assert len(sugg) >= 1

    def test_three_subquestions_with_concrete_scenario_skips_refinement(self):
        plan = _plan([
            {"text": "Does she qualify for Medicaid"},
            {"text": "What health plans serve Tampa"},
            {"text": "How to enroll"},
        ])
        user_msg = (
            "I have a patient, female who is 35 years old and makes $1200 per month "
            "with a 10 year old kid. She lives in Tampa Florida, I wanted to see if "
            "she may qualify for medicaid and if so what health plans serve that region "
            "and how to get her enrolled"
        )
        should, sugg = need_query_refinement(plan, user_message=user_msg)
        assert should is False
        assert sugg == []

    def test_two_subquestions_no_refinement(self):
        plan = _plan([
            {"text": "Prior auth for imaging"},
            {"text": "How to submit the claim"},
        ])
        should, sugg = need_query_refinement(plan, user_message="Prior auth and claim submission")
        assert should is False
        assert sugg == []
