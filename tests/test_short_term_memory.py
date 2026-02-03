"""Unit tests for short-term memory: state extractor, context router, no patient data in state."""
import pytest

from app.state.state_extractor import extract_state_patch, answer_card_to_open_slots
from app.state.context_router import route_context


def test_payer_switch_resets_domain_and_slots():
    """Payer switch resets domain and slots: 'Sunshine prior auth' then 'UnitedHealthcare eligibility'."""
    state1 = {"active": {"payer": None, "domain": None, "jurisdiction": None, "user_role": None}, "open_slots": []}
    patch1, reset1 = extract_state_patch("Sunshine prior auth", state1, None, None)
    assert reset1 is None
    assert (patch1.get("active") or {}).get("payer") in ("Sunshine Health", "Sunshine")
    assert (patch1.get("active") or {}).get("domain") == "prior_auth"

    state2 = {
        "active": {"payer": "Sunshine Health", "domain": "prior_auth", "jurisdiction": None, "user_role": None},
        "open_slots": ["service_code"],
    }
    patch2, reset2 = extract_state_patch("UnitedHealthcare eligibility", state2, None, None)
    assert reset2 == "payer_change"
    assert patch2.get("open_slots") == []
    # Payer changed to UnitedHealthcare; old domain (prior_auth) is cleared. Domain in patch may be the new one from same message (eligibility).
    assert (patch2.get("active") or {}).get("payer") in ("United Healthcare", "UnitedHealthcare")
    assert (patch2.get("active") or {}).get("domain") in (None, "eligibility")


def test_missing_payer_uses_previous_payer_stateful():
    """Missing payer uses previous payer only when STATEFUL: state has payer=Sunshine; user says 'what about service code?'."""
    state = {
        "active": {"payer": "Sunshine Health", "domain": "prior_auth", "jurisdiction": None, "user_role": None},
        "open_slots": [],
    }
    route = route_context("what about service code?", state, [], reset_reason=None)
    assert route == "STATEFUL"


def test_open_slots_cleared_when_user_provides_service_code():
    """Open_slots cleared when user provides service code: state has open_slots=['service_code']; user says 'CPT 99213'."""
    state = {
        "active": {"payer": None, "domain": None, "jurisdiction": None, "user_role": None},
        "open_slots": ["service_code"],
    }
    patch, _ = extract_state_patch("CPT 99213", state, None, None)
    assert "open_slots" in patch
    assert "service_code" not in patch["open_slots"]


def test_no_patient_info_in_state():
    """No patient info in state: user text contains 'John Doe' or 'DOB 01/15/1990'; patch must not contain patient fields."""
    state = {"active": {}, "open_slots": []}
    patch1, _ = extract_state_patch("John Doe had a claim", state, None, None)
    patch2, _ = extract_state_patch("DOB 01/15/1990 for eligibility", state, None, None)
    disallowed = ("mrn", "member_id", "patient_name", "date_of_birth", "patient_dob", "name", "dob")
    for patch in (patch1, patch2):
        active = patch.get("active") or {}
        assert "patient" not in active
        for key in active:
            assert key not in disallowed


def test_website_for_united_healthcare_sets_payer():
    """User says 'Do you have the website for United Healthcare'; payer in patch must be United (canonical or fallback)."""
    state = {
        "active": {"payer": "Sunshine Health", "domain": None, "jurisdiction": None, "user_role": None},
        "open_slots": [],
    }
    patch, reset_reason = extract_state_patch("Do you have the website for United Healthcare", state, None, None)
    payer = (patch.get("active") or {}).get("payer")
    assert payer is not None, "Payer should be detected from 'United Healthcare' in message"
    assert payer in ("United Healthcare", "UnitedHealthcare"), f"Expected United canonical/fallback, got {payer!r}"
    assert reset_reason == "payer_change"


def test_answer_card_to_open_slots():
    """answer_card_to_open_slots maps required_variables and followups to slot types."""
    card = {
        "mode": "FACTUAL",
        "direct_answer": "Yes.",
        "sections": [],
        "required_variables": ["service code", "plan type"],
        "followups": [{"question": "What is the date range?", "reason": "", "field": ""}],
    }
    slots = answer_card_to_open_slots(card)
    assert "service_code" in slots
    assert "plan_type" in slots
    assert "date_range" in slots
