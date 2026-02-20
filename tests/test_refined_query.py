"""Extensive tests for refined query: classify_message, build_refined_query, compute_refined_query.

Flow under test:
- User: "how do I file an appeal" -> refined_query = "how do I file an appeal"
- System asks "for which payor?"
- User: "Sunshine Health" (slot fill) -> refined_query = "how do I file an appeal for Sunshine Health"
- User: "how do I check eligibility" (new question) -> refined_query = "how do I check eligibility"
"""
from __future__ import annotations

import pytest

from app.state.refined_query import (
    build_refined_query,
    classify_message,
    compute_refined_query,
)
from app.state.jurisdiction import get_jurisdiction_from_active, jurisdiction_to_summary
from app.storage.threads import DEFAULT_STATE


# --- classify_message tests ---


class TestClassifyMessageSlotFill:
    """User is filling a slot (same question + context)."""

    def test_payer_answer_with_open_slots(self):
        """'Sunshine Health' with open_slots -> slot_fill."""
        out = classify_message(
            "Sunshine Health",
            {"user_content": "how do I file an appeal", "assistant_content": "For which payor?"},
            ["jurisdiction.payor"],
            "how do I file an appeal",
        )
        assert out == "slot_fill", f"Expected slot_fill, got {out}"

    def test_payer_answer_slots_already_cleared_fallback(self):
        """'Sunshine Health' with no open_slots but existing_refined_query, short -> slot_fill."""
        out = classify_message(
            "Sunshine Health",
            {"user_content": "how do I file an appeal", "assistant_content": "For which payor?"},
            [],
            "how do I file an appeal",
        )
        assert out == "slot_fill", f"Expected slot_fill, got {out}"

    def test_state_answer_with_open_slots(self):
        """'Florida' with open_slots -> slot_fill."""
        out = classify_message(
            "Florida",
            {"user_content": "appeal process", "assistant_content": "Which state?"},
            ["jurisdiction.state"],
            "how do I file an appeal",
        )
        assert out == "slot_fill", f"Expected slot_fill, got {out}"

    def test_medicaid_answer_with_open_slots(self):
        """'Medicaid' with open_slots -> slot_fill."""
        out = classify_message(
            "Medicaid",
            {"user_content": "prior auth", "assistant_content": "Which program?"},
            ["jurisdiction.program"],
            "prior auth requirements",
        )
        assert out == "slot_fill", f"Expected slot_fill, got {out}"

    def test_as_a_provider_with_open_slots(self):
        """'as a provider' with open_slots -> slot_fill."""
        out = classify_message(
            "as a provider",
            {"user_content": "appeal process", "assistant_content": "Provider or member?"},
            ["jurisdiction.perspective"],
            "appeal process",
        )
        assert out == "slot_fill", f"Expected slot_fill, got {out}"

    def test_explicit_same(self):
        """'same' or 'that one' with open_slots -> slot_fill."""
        out = classify_message(
            "that one",
            {"user_content": "Sunshine", "assistant_content": "Use same payor?"},
            ["jurisdiction.payor"],
            "appeal for Sunshine",
        )
        assert out == "slot_fill", f"Expected slot_fill, got {out}"

    def test_united_healthcare_answer(self):
        """'United Healthcare' with open_slots -> slot_fill."""
        out = classify_message(
            "United Healthcare",
            {"user_content": "how do I file an appeal", "assistant_content": "Which payor?"},
            ["jurisdiction.payor"],
            "how do I file an appeal",
        )
        assert out == "slot_fill", f"Expected slot_fill, got {out}"

    def test_short_yes_with_open_slots(self):
        """'yes' with open_slots -> slot_fill."""
        out = classify_message(
            "yes",
            {"user_content": "appeal", "assistant_content": "Same payor?"},
            ["jurisdiction.payor"],
            "appeal process",
        )
        assert out == "slot_fill", f"Expected slot_fill, got {out}"


class TestClassifyMessageNewQuestion:
    """User is asking a different question."""

    def test_full_new_question_how_do_i(self):
        """'how do I check eligibility' -> new_question."""
        out = classify_message(
            "how do I check eligibility",
            {"user_content": "how do I file an appeal", "assistant_content": "..."},
            [],
            "how do I file an appeal",
        )
        assert out == "new_question", f"Expected new_question, got {out}"

    def test_full_new_question_what_is(self):
        """'what is the prior auth process' -> new_question."""
        out = classify_message(
            "what is the prior auth process for Sunshine",
            None,
            [],
            None,
        )
        assert out == "new_question", f"Expected new_question, got {out}"

    def test_what_about_indicates_new(self):
        """'what about eligibility' -> new_question (what about + 4+ words)."""
        out = classify_message(
            "what about eligibility for prior auth",
            {"user_content": "appeal", "assistant_content": "..."},
            [],
            "appeal process",
        )
        assert out == "new_question", f"Expected new_question, got {out}"

    def test_different_question_phrase(self):
        """'different question - how do I check status' -> new_question."""
        out = classify_message(
            "different question - how do I check status",
            {"user_content": "appeal", "assistant_content": "..."},
            [],
            "appeal",
        )
        assert out == "new_question", f"Expected new_question, got {out}"

    def test_first_message_no_context(self):
        """First message, no last_turn -> new_question."""
        out = classify_message("how do I file an appeal", None, [], None)
        assert out == "new_question", f"Expected new_question, got {out}"

    def test_empty_text(self):
        """Empty text -> new_question (fallback)."""
        out = classify_message("", {}, [], "something")
        assert out == "new_question", f"Expected new_question, got {out}"

    def test_whitespace_only(self):
        """Whitespace only -> new_question."""
        out = classify_message("   ", {}, [], "something")
        assert out == "new_question", f"Expected new_question, got {out}"

    def test_long_question_with_question_mark(self):
        """Long text ending in ? without new-question pattern -> new_question (fallback)."""
        out = classify_message(
            "is there anything else I need to know?",
            {"user_content": "appeal", "assistant_content": "..."},
            [],
            "appeal",
        )
        assert out == "new_question", f"Expected new_question, got {out}"


class TestClassifyMessageEdgeCases:
    """Edge cases and boundary conditions."""

    def test_sunshine_health_two_words_slot_fill(self):
        """'Sunshine Health' 2 words, existing query -> slot_fill."""
        out = classify_message(
            "Sunshine Health",
            {"user_content": "appeal", "assistant_content": "Which payor?"},
            [],
            "how do I file an appeal",
        )
        assert out == "slot_fill", f"Expected slot_fill, got {out}"

    def test_short_slot_like_without_existing_query(self):
        """'Florida' with no existing_refined_query -> new_question (can't merge)."""
        out = classify_message("Florida", {}, [], None)
        assert out == "new_question", f"Expected new_question, got {out}"


# --- build_refined_query tests ---


class TestBuildRefinedQuery:
    """Merge jurisdiction into base query."""

    def test_payor_only(self):
        """Base + payor -> 'X for Sunshine Health'."""
        j = {"payor": "Sunshine Health", "state": None, "program": None}
        out = build_refined_query("how do I file an appeal", j)
        assert "how do I file an appeal" in out
        assert "Sunshine Health" in out
        assert out == "how do I file an appeal for Sunshine Health"

    def test_payor_and_state(self):
        """Base + payor + state -> jurisdiction summary includes both."""
        j = {"payor": "Sunshine Health", "state": "Florida", "program": None}
        out = build_refined_query("how do I file an appeal", j)
        assert "Sunshine Health" in out
        assert "Florida" in out

    def test_full_jurisdiction(self):
        """Base + payor + state + program."""
        j = {"payor": "Sunshine Health", "state": "Florida", "program": "Medicaid"}
        out = build_refined_query("prior auth requirements", j)
        assert "prior auth" in out
        assert "Sunshine" in out or "Florida" in out or "Medicaid" in out

    def test_empty_jurisdiction(self):
        """Empty jurisdiction -> base unchanged."""
        out = build_refined_query("how do I file an appeal", {})
        assert out == "how do I file an appeal"

    def test_none_jurisdiction(self):
        """None jurisdiction -> base unchanged."""
        out = build_refined_query("how do I file an appeal", None)
        assert out == "how do I file an appeal"

    def test_avoid_duplicate(self):
        """Jurisdiction already in base -> no duplicate."""
        j = {"payor": "Sunshine Health", "state": None, "program": None}
        out = build_refined_query("how do I file an appeal for Sunshine Health", j)
        assert out == "how do I file an appeal for Sunshine Health"

    def test_empty_base(self):
        """Empty base -> returns empty."""
        out = build_refined_query("", {"payor": "Sunshine"})
        assert out == ""

    def test_none_base(self):
        """None base -> returns empty."""
        out = build_refined_query(None, {"payor": "Sunshine"})
        assert out == ""

    def test_whitespace_base(self):
        """Whitespace base -> stripped."""
        out = build_refined_query("  ", {"payor": "Sunshine"})
        assert out == ""


# --- compute_refined_query tests ---


class TestComputeRefinedQuery:
    """Compute refined_query from classification and state."""

    def test_slot_fill_merges_jurisdiction(self):
        """slot_fill + last_refined_query + state with payer -> merged query."""
        merged_state = {
            "active": {
                "payer": "Sunshine Health",
                "jurisdiction_obj": None,
                "domain": None,
                "jurisdiction": None,
                "user_role": None,
                "program": None,
            },
        }
        out = compute_refined_query(
            "slot_fill",
            "Sunshine Health",
            "how do I file an appeal",
            merged_state,
            None,
        )
        assert "how do I file an appeal" in out
        assert "Sunshine" in out

    def test_new_question_uses_plan_text(self):
        """new_question -> uses plan_subquestion_text."""
        out = compute_refined_query(
            "new_question",
            "how do I check eligibility",
            "how do I file an appeal",
            {},
            "how do I check eligibility for a member",
        )
        assert out == "how do I check eligibility for a member"

    def test_new_question_no_plan_fallback_user_text(self):
        """new_question with no plan text -> user_text."""
        out = compute_refined_query(
            "new_question",
            "how do I check eligibility",
            "appeal",
            {},
            None,
        )
        assert out == "how do I check eligibility"

    def test_slot_fill_no_last_refined_fallback(self):
        """slot_fill but no last_refined_query -> falls through to plan/user_text."""
        out = compute_refined_query(
            "slot_fill",
            "Sunshine Health",
            None,
            {"active": {"payer": "Sunshine Health"}},
            "appeal for Sunshine Health",
        )
        assert "Sunshine" in out or "appeal" in out

    def test_new_question_empty_plan_uses_user_text(self):
        """new_question, plan_text empty -> user_text."""
        out = compute_refined_query(
            "new_question",
            "what is eligibility",
            "appeal",
            {},
            "",
        )
        assert out == "what is eligibility"


# --- jurisdiction helpers (smoke) ---


class TestJurisdictionHelpers:
    """Smoke test jurisdiction helpers used by refined_query."""

    def test_get_jurisdiction_from_active_legacy_payer(self):
        """active.payer -> jurisdiction.payor."""
        active = {"payer": "Sunshine Health"}
        j = get_jurisdiction_from_active(active)
        assert j.get("payor") == "Sunshine Health"

    def test_jurisdiction_to_summary(self):
        """Format jurisdiction summary."""
        j = {"payor": "Sunshine Health", "state": "Florida", "program": "Medicaid"}
        s = jurisdiction_to_summary(j)
        assert "Sunshine" in s
        assert "Florida" in s
        assert "Medicaid" in s


# --- DEFAULT_STATE ---


class TestDefaultState:
    """State schema includes refined_query."""

    def test_refined_query_in_default_state(self):
        """DEFAULT_STATE has refined_query key."""
        assert "refined_query" in DEFAULT_STATE
        assert DEFAULT_STATE["refined_query"] is None


# --- Integration-style: full flow simulation (no DB/LLM) ---


class TestRefinedQueryFlowSimulation:
    """Simulate the worker flow for refined query logic."""

    def test_flow_turn1_new_question(self):
        """Turn 1: 'how do I file an appeal' -> new_question, refined = from plan."""
        message = "how do I file an appeal"
        last_turn = {}
        open_slots = []
        last_refined = None
        classification = classify_message(message, last_turn, open_slots, last_refined)
        assert classification == "new_question"
        plan_text = "how do I file an appeal"
        merged_state = {"active": {}}
        refined = compute_refined_query(
            classification, message, last_refined, merged_state, plan_text
        )
        assert refined == "how do I file an appeal"

    def test_flow_turn2_slot_fill_sunshine(self):
        """Turn 2: 'Sunshine Health' -> slot_fill, refined = '... for Sunshine Health'."""
        message = "Sunshine Health"
        last_turn = {"user_content": "how do I file an appeal", "assistant_content": "For which payor?"}
        open_slots = []
        last_refined = "how do I file an appeal"
        merged_state = {"active": {"payer": "Sunshine Health"}}
        classification = classify_message(message, last_turn, open_slots, last_refined)
        assert classification == "slot_fill"
        effective = build_refined_query(last_refined, get_jurisdiction_from_active(merged_state["active"]))
        assert "Sunshine" in effective
        assert "appeal" in effective
        refined = compute_refined_query(
            classification, message, last_refined, merged_state, None
        )
        assert "Sunshine" in refined
        assert "appeal" in refined

    def test_flow_turn3_new_question_replace(self):
        """Turn 3: 'how do I check eligibility' -> new_question, refined replaced."""
        message = "how do I check eligibility"
        last_turn = {"user_content": "how do I file an appeal for Sunshine Health", "assistant_content": "..."}
        open_slots = []
        last_refined = "how do I file an appeal for Sunshine Health"
        plan_text = "how do I check eligibility"
        classification = classify_message(message, last_turn, open_slots, last_refined)
        assert classification == "new_question"
        refined = compute_refined_query(
            classification, message, last_refined, {}, plan_text
        )
        assert refined == "how do I check eligibility"
        assert "appeal" not in refined
        assert "Sunshine" not in refined
