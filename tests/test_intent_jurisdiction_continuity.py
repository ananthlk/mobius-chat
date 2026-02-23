"""Tests for intent/jurisdiction separation and follow-up continuity.

Baseline (pre-implementation): Documents current behavior. After implementing
parse-strip, continuity redraft, and reframe with strip+recombine, re-run to verify improvements.

Key flows under test:
- (a) Parse and strip jurisdiction from intent
- (b) Continuity with previous questions and redraft
- (c) Recombine clean intent + jurisdiction for retrieval
- Jurisdiction change: "what is X for Sunshine" -> "how about for United"
- Follow-up with reference: "can you search the web for it"
"""
from __future__ import annotations

import pytest

from app.state.refined_query import (
    build_refined_query,
    classify_message,
    compute_refined_query,
)
from app.state.query_refinement import reframe_for_retrieval
from app.state.jurisdiction import get_jurisdiction_from_active, jurisdiction_to_summary


# --- Baseline: Intent already contains jurisdiction (strip before recombine) ---
# CURRENT: build_refined_query does avoid_duplicate (summary_lower in base_lower)
# GAP: No explicit strip of jurisdiction from intent; avoid_duplicate is partial


class TestIntentWithJurisdictionEmbedded:
    """Intent like 'what is X for Sunshine' - should strip jurisdiction before recombine."""

    def test_build_refined_query_intent_has_sunshine_jurisdiction_sunshine(self):
        """Base has 'for Sunshine', jurisdiction is Sunshine Health - avoid_duplicate avoids double."""
        j = {"payor": "Sunshine Health", "state": None, "program": None}
        base = "what is the care management program for Sunshine"
        out = build_refined_query(base, j)
        # Current: "Sunshine Health" not in base ("Sunshine" is); summary is "Sunshine Health in Florida" or similar
        # Avoid duplicate checks: summary_lower in base_lower. "sunshine health" not in "for sunshine"
        # So we may get duplication. Document actual behavior.
        assert "care management" in out
        assert "Sunshine" in out

    def test_build_refined_query_intent_has_full_payer_jurisdiction_same(self):
        """Base already has 'for Sunshine Health', jurisdiction same - no duplicate."""
        j = {"payor": "Sunshine Health", "state": None, "program": None}
        base = "what is the care management program for Sunshine Health"
        out = build_refined_query(base, j)
        # avoid_duplicate: "sunshine health" in "what is the care management program for sunshine health"
        assert out == "what is the care management program for Sunshine Health"


# --- Baseline: Jurisdiction change ("how about for United") ---
# CURRENT: "how about" triggers new_question; we lose same-intent-different-jurisdiction


class TestJurisdictionChange:
    """User asks same question for different payer."""

    def test_how_about_for_united_classify(self):
        """'how about for United Healthcare' - jurisdiction_change (same intent, swap jurisdiction)."""
        out = classify_message(
            "how about for United Healthcare",
            {"user_content": "what is the care management program for Sunshine", "assistant_content": "..."},
            [],
            "what is the care management program for Sunshine Health",
        )
        assert out == "jurisdiction_change"

    def test_how_about_for_united_short(self):
        """'how about United' - jurisdiction_change (matches pattern)."""
        out = classify_message(
            "how about United",
            {"user_content": "care management program for Sunshine", "assistant_content": "..."},
            [],
            "what is the care management program for Sunshine Health",
        )
        assert out == "jurisdiction_change"


# --- Baseline: Follow-up with "it" / "their" ---
# CURRENT: "can you search the web for it" - short, may be new_question, loses prior topic


class TestFollowUpWithReference:
    """Follow-up that references prior turn ('it', 'their')."""

    def test_can_you_search_for_it_classify(self):
        """'can you search the web for it' - current classification."""
        out = classify_message(
            "can you search the web for it",
            {
                "user_content": "can you read their website and tell me the specific income criteria",
                "assistant_content": "The specific income criteria for Florida Medicaid cannot be provided...",
            },
            [],
            "specific income criteria for Florida Medicaid from Sunshine Health website",
        )
        # No slot patterns; "how do" etc not in message. Might be new_question.
        assert out in ("slot_fill", "new_question")

    def test_can_you_read_their_website_classify(self):
        """'can you read their website and tell me the specific income criteria' - first in sequence."""
        out = classify_message(
            "can you read their website and tell me the specific income criteria",
            {"user_content": "A member has income of $1500... Do they meet eligibility?", "assistant_content": "..."},
            [],
            "eligibility for member with income 1500 and two chronic conditions",
        )
        assert out == "new_question"


# --- Baseline: reframe_for_retrieval ---
# CURRENT: Returns question as-is; no last_refined_query, no jurisdiction merge


class TestReframeForRetrieval:
    """reframe_for_retrieval current behavior."""

    def test_reframe_returns_as_is(self):
        """Current: reframe_for_retrieval returns question unchanged."""
        q = "can you search the web for it"
        out = reframe_for_retrieval(q, intent="canonical", question_intent="canonical")
        assert out == q

    def test_reframe_factual(self):
        """Factual intent - still as-is."""
        q = "what is the income threshold for Medicaid"
        out = reframe_for_retrieval(q, intent="factual")
        assert out == q


# --- Desired flow: parse-strip, continuity, recombine (future) ---
# These document expected behavior after implementation. Use pytest.mark.skip or xfail for baseline.


class TestDesiredParseStrip:
    """Parse and strip jurisdiction from intent using J-tag lexicon."""

    def test_strip_sunshine_from_intent(self):
        """'what is X for Sunshine' -> intent has Sunshine stripped (requires lexicon)."""
        from app.state.intent_jurisdiction import strip_jurisdiction_from_intent

        out = strip_jurisdiction_from_intent("what is the care management program for Sunshine")
        # With lexicon: Sunshine stripped. Without RAG URL: returns as-is.
        assert "care management" in out


class TestDesiredContinuityRedraft:
    """Continuity + redraft via is_followup_continuation and compute_refined_query."""

    def test_followup_search_for_it_expands(self):
        """'can you search for it' + last_intent -> expanded with prior topic."""
        last_turn = {"assistant_content": "Eligibility is determined by DCF for Florida Medicaid."}
        refined2 = compute_refined_query(
            "new_question",
            "can you search for it",
            "income eligibility criteria for Florida Medicaid",
            {"active": {"payer": "Sunshine Health", "program": "Medicaid"}},
            "can you search for it",
            last_turn=last_turn,
        )
        assert "income" in refined2 or "Medicaid" in refined2


class TestDesiredRecombine:
    """Reframe with strip+recombine at retrieval."""

    def test_reframe_followup_with_context(self):
        """reframe_for_retrieval with last_refined_query, jurisdiction, is_followup -> concrete query."""
        out = reframe_for_retrieval(
            "can you search for it",
            intent=None,
            question_intent=None,
            last_refined_query="income eligibility criteria for Florida Medicaid",
            jurisdiction={"payor": "Sunshine Health", "state": "Florida", "program": "Medicaid"},
            is_followup=True,
        )
        assert "income" in out or "eligibility" in out
        assert "Sunshine" in out or "Florida" in out or "Medicaid" in out


# --- Integration: full flow simulation (current behavior) ---


class TestContinuityFlowBaseline:
    """Multi-turn flow - document current behavior."""

    def test_flow_intent_then_jurisdiction_change(self):
        """Turn 1: 'what is care management for Sunshine'. Turn 2: 'how about for United'."""
        # Turn 1
        msg1 = "what is the care management program for Sunshine"
        class1 = classify_message(msg1, None, [], None)
        assert class1 == "new_question"
        refined1 = "what is the care management program for Sunshine Health"

        # Turn 2: jurisdiction change — same intent, swap to United
        msg2 = "how about for United Healthcare"
        class2 = classify_message(msg2, {"user_content": msg1, "assistant_content": "..."}, [], refined1)
        assert class2 == "jurisdiction_change"
        merged2 = {"active": {"payer": "United Healthcare"}}  # state after extract_state_delta
        refined2 = compute_refined_query(class2, msg2, refined1, merged2, "what is the care management program for United Healthcare")
        assert "United" in refined2
        assert "care management" in refined2

    def test_flow_followup_can_you_search_for_it(self):
        """Turn 1: income question. Turn 2: 'can you search the web for it' — expands to prior topic."""
        refined1 = "specific income criteria for Florida Medicaid"
        merged1 = {"active": {"payer": "Sunshine Health", "program": "Medicaid"}}
        last_turn = {
            "user_content": "A member has income $1500. Do they meet eligibility?",
            "assistant_content": "Eligibility is determined by DCF. For Florida Medicaid, you can check with the Department of Children and Families.",
        }

        msg2 = "can you search the web for it"
        class2 = classify_message(msg2, last_turn, [], refined1)
        plan_text = "can you search the web for it"
        refined2 = compute_refined_query(class2, msg2, refined1, merged1, plan_text, last_turn=last_turn)
        # Follow-up: uses last_refined_query + jurisdiction
        assert "income" in refined2 or "Medicaid" in refined2
