"""Tests for conversational continuity + active skill context (message_resolver spec)."""
import pytest

from app.pipeline.message_resolver import (
    resolve_pronouns,
    detect_skill_reference,
    build_skill_context_summary,
    extract_roster_skill_data,
)


# ─── Test 1: PML question after credentialing report ───────────────────────


def test_pml_question_after_report_detected_as_skill_reference():
    """Turn 2: 'How many NPIs have issues with PML?' after report → is_skill_ref, roster_report."""
    active_skill = {
        "skill": "roster_report",
        "org": "David Lawrence Center",
        "data": {
            "section_b_count": 3,
            "section_c_count": 45,
            "readiness_score": 72,
        },
        "turn": "tid-1",
    }
    msg = "How many NPIs have issues with PML?"
    is_ref, name = detect_skill_reference(msg, active_skill)
    assert is_ref is True
    assert name == "roster_report"


def test_pml_question_skill_summary_contains_total_and_sections():
    """Skill summary for roster_report includes total PML issues (B+C) and section breakdown."""
    active_skill = {
        "skill": "roster_report",
        "org": "David Lawrence Center",
        "data": {
            "section_a_count": 100,
            "section_b_count": 3,
            "section_c_count": 45,
            "section_d_count": 12,
            "readiness_score": 72,
        },
        "turn": "tid-1",
    }
    summary = build_skill_context_summary(active_skill)
    assert "ACTIVE SKILL OUTPUT" in summary
    assert "David Lawrence Center" in summary
    assert "48" in summary  # 3 + 45
    assert "Section B" in summary
    assert "Section C" in summary
    assert "at-risk" in summary or "At-risk" in summary
    assert "missing enrollment" in summary or "Missing PML" in summary


# ─── Test 2: Section question after report ──────────────────────────────────


def test_section_question_after_report_detected_as_skill_reference():
    """Turn 2: 'Tell me more about Section C' → is_skill_ref."""
    active_skill = {"skill": "roster_report", "org": "Acme", "data": {}, "turn": "t1"}
    is_ref, name = detect_skill_reference("Tell me more about Section C", active_skill)
    assert is_ref is True
    assert name == "roster_report"


# ─── Test 3: Revenue question after report ──────────────────────────────────


def test_revenue_question_after_report_detected_and_summary_has_revenue():
    """Turn 2: 'What is the total revenue opportunity?' → skill ref; summary can include revenue."""
    active_skill = {
        "skill": "roster_report",
        "org": "Acme",
        "data": {
            "total_opportunity": 1969457.66,
            "section_b_revenue": 100000.0,
            "section_c_revenue": 500000.0,
            "section_d_revenue": 1369457.66,
        },
        "turn": "t1",
    }
    is_ref, _ = detect_skill_reference("What is the total revenue opportunity?", active_skill)
    assert is_ref is True
    summary = build_skill_context_summary(active_skill)
    assert "1,969,457.66" in summary or "1969457" in summary


# ─── Test 4: Pronoun after failed query ────────────────────────────────────


def test_pronoun_search_web_for_it_resolved_from_prior_failed():
    """Turn 1: failed query. Turn 2: 'Can you search the web for it?' → resolved to prior topic."""
    last_turns = [
        {
            "user_content": "What is Sunshine Health's PA requirement for H0036?",
            "assistant_content": "I couldn't find that in our materials.",
            "retrieval_signal": "no_sources",
            "layer_used": 5,
        },
    ]
    msg = "Can you search the web for it?"
    resolved, was_enriched = resolve_pronouns(msg, last_turns)
    assert was_enriched is True
    assert "Search the web for" in resolved
    assert "Sunshine" in resolved or "PA" in resolved or "H0036" in resolved


def test_pronoun_search_web_for_it_using_state_prior_failed():
    """When last_turns lack retrieval_signal, prior_failed_question from state is used."""
    resolved, was_enriched = resolve_pronouns(
        "Can you search the web for it?",
        [],  # no turns
        prior_failed_question="What is Sunshine Health's PA requirement for H0036?",
    )
    assert was_enriched is True
    assert "Search the web for" in resolved
    assert "Sunshine" in resolved or "PA" in resolved or "H0036" in resolved


def test_pronoun_emit_understood_prefix():
    """Resolved message is suitable for emit '↺ Understood: ...' (first 100 chars)."""
    resolved, _ = resolve_pronouns(
        "search the web for it",
        [],
        prior_failed_question="What is Molina's timely filing deadline?",
    )
    assert resolved.startswith("Search the web for")
    assert "Molina" in resolved or "timely" in resolved


# ─── Test 5: NPI follow-up ──────────────────────────────────────────────────


def test_npi_follow_up_which_one_detected_as_skill_reference():
    """Turn 2: 'Which one is the Orlando location?' after NPI lookup → is_skill_ref, npi_lookup."""
    active_skill = {
        "skill": "npi_lookup",
        "org": "Aspire Health",
        "data": {
            "results": [
                {"name": "Aspire Health Orlando", "npi": "1234567890", "match_type": "match"},
                {"name": "Aspire Health Tampa", "npi": "0987654321", "match_type": "match"},
            ]
        },
        "turn": "t1",
    }
    is_ref, name = detect_skill_reference("Which one is the Orlando location?", active_skill)
    assert is_ref is True
    assert name == "npi_lookup"


def test_npi_lookup_summary_lists_results():
    """build_skill_context_summary for npi_lookup includes result names and NPIs."""
    active_skill = {
        "skill": "npi_lookup",
        "org": "Aspire Health",
        "data": {
            "results": [
                {"name": "Aspire Orlando", "npi": "1234567890", "match_type": "match"},
            ]
        },
        "turn": "t1",
    }
    summary = build_skill_context_summary(active_skill)
    assert "ACTIVE SKILL OUTPUT" in summary
    assert "NPI lookup" in summary
    assert "1 NPIs" in summary or "1 NPI" in summary
    assert "Aspire Orlando" in summary
    assert "1234567890" in summary


# ─── Test 6: Unrelated question after report (should NOT match) ──────────────


def test_unrelated_question_after_report_not_skill_reference():
    """Turn 2: 'What is Sunshine Health's timely filing deadline?' after report → NOT skill ref."""
    active_skill = {
        "skill": "roster_report",
        "org": "David Lawrence Center",
        "data": {"section_b_count": 3, "section_c_count": 45},
        "turn": "t1",
    }
    msg = "What is Sunshine Health's timely filing deadline?"
    is_ref, name = detect_skill_reference(msg, active_skill)
    assert is_ref is False
    assert name is None


def test_no_pronoun_resolution_without_reference_signals():
    """Message with no 'it'/'that'/'search for it' etc. is unchanged."""
    last_turns = [{"user_content": "What is PA for H0036?", "assistant_content": "I don't know."}]
    msg = "What is Sunshine Health's timely filing deadline?"
    resolved, was_enriched = resolve_pronouns(msg, last_turns)
    assert was_enriched is False
    assert resolved == msg


# ─── extract_roster_skill_data ──────────────────────────────────────────────


def test_extract_roster_skill_data_from_answer_set_and_md():
    """extract_roster_skill_data parses section counts and revenue from ctx.answer_set and md."""
    class Ctx:
        roster_report_final_md = (
            "Section B (at-risk): $100,000.00. Section C (enrollment gap): $500,000.00. "
            "Total opportunity: $1,969,457.66"
        )
        answer_set = {
            "sq1": {
                "answer": "Section B: 3 providers. Section C: 45 providers. Readiness score: 72%",
                "source": "tool",
            },
        }
        roster_step_outputs = []

    ctx = Ctx()
    data = extract_roster_skill_data(ctx)
    assert data.get("section_b_count") == 3
    assert data.get("section_c_count") == 45
    assert data.get("readiness_score") == 72.0
    assert data.get("total_opportunity") == 1969457.66
    assert data.get("section_b_revenue") == 100000.0
    assert data.get("section_c_revenue") == 500000.0


def test_extract_roster_skill_data_empty_ctx():
    """extract_roster_skill_data on minimal ctx returns empty dict (no crash)."""
    class Ctx:
        answer_set = {}
        roster_report_final_md = ""

    data = extract_roster_skill_data(Ctx())
    assert isinstance(data, dict)
    assert "section_b_count" not in data or data.get("section_b_count") is None
