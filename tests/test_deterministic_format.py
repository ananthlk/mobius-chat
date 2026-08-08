"""Tests for the deterministic (no-LLM) formatter, Task #76.

Chat Master's explicit ruling: NO LLM fallback on this path -- when react's
answer is already sufficient, structure it with regex only. Confidently
structured content (obvious Label: Value pairs) gets promoted to a stats/
table section; everything else passes through bolded, unchanged shape.
"""
from __future__ import annotations

from app.responder.deterministic_format import bold_key_facts, deterministic_format


class TestBoldKeyFacts:
    def test_bolds_duration(self):
        assert bold_key_facts("within 180 days of service") == "within **180 days** of service"

    def test_bolds_money(self):
        assert bold_key_facts("the fee is $50.25") == "the fee is **$50.25**"

    def test_bolds_percentage(self):
        assert bold_key_facts("a 20% coinsurance") == "a **20%** coinsurance"

    def test_bolds_date_slash_form(self):
        assert bold_key_facts("due by 01/15/2026") == "due by **01/15/2026**"

    def test_bolds_date_month_form(self):
        assert bold_key_facts("due by January 15, 2026") == "due by **January 15, 2026**"

    def test_bolds_multiple_facts_in_one_string(self):
        out = bold_key_facts("180 days and a $50 fee")
        assert out == "**180 days** and a **$50** fee"

    def test_no_facts_unchanged(self):
        text = "This is plain prose with no numbers at all."
        assert bold_key_facts(text) == text

    def test_empty_string(self):
        assert bold_key_facts("") == ""
        assert bold_key_facts(None) is None

    def test_does_not_double_bold(self):
        """A pre-bolded fact (e.g. from upstream text) must not get a second
        layer of ** markers."""
        out = bold_key_facts("**180 days**")
        assert out == "**180 days**"


class TestDeterministicFormat:
    def test_empty_react_draft(self):
        result = deterministic_format("")
        assert result == {"mode": "FACTUAL", "direct_answer": "", "sections": []}
        result_none = deterministic_format(None)
        assert result_none == {"mode": "FACTUAL", "direct_answer": "", "sections": []}

    def test_plain_prose_no_sections(self):
        draft = "Sunshine Health requires claims within 180 days of service."
        result = deterministic_format(draft)
        assert result["mode"] == "FACTUAL"
        assert "**180 days**" in result["direct_answer"]
        assert result["sections"] == []

    def test_label_value_pairs_promoted_to_stats(self):
        draft = "Initial filing: 180 days\nResubmission: 90 days\nCopay: $25"
        result = deterministic_format(draft)
        assert len(result["sections"]) == 1
        sec = result["sections"][0]
        assert sec["format"] == "stats"
        assert sec["label"] == "Key Facts"
        items = sec["data"]["items"]
        assert len(items) == 3
        assert {"label": "Initial filing", "value": "180 days"} in items

    def test_label_value_pairs_promoted_to_table_when_values_long(self):
        draft = (
            "Initial filing: must be submitted within 180 days of the original date of service\n"
            "Resubmission: must include all prior documentation and a cover letter explaining the delay\n"
            "Appeals: available for 60 days after a denial is issued in writing"
        )
        result = deterministic_format(draft)
        assert len(result["sections"]) == 1
        sec = result["sections"][0]
        assert sec["format"] == "table"
        assert sec["data"]["headers"] == ["Item", "Detail"]
        assert len(sec["data"]["rows"]) == 3

    def test_single_pair_not_promoted(self):
        """Only one Label: Value line is coincidence, not a real pattern --
        stays as plain bolded prose, no section."""
        draft = "Note: this is a single line with a colon in it."
        result = deterministic_format(draft)
        assert result["sections"] == []

    def test_too_many_pairs_not_promoted(self):
        """More than 6 colon-lines usually means the regex is over-matching
        prose, not real structured notes -- stay conservative."""
        lines = [f"Item {i}: value {i}" for i in range(8)]
        draft = "\n".join(lines)
        result = deterministic_format(draft)
        assert result["sections"] == []

    def test_direct_answer_still_has_bolded_facts_when_sections_present(self):
        draft = "Initial filing: 180 days\nResubmission: 90 days"
        result = deterministic_format(draft)
        assert "**180 days**" in result["direct_answer"]
        assert "**90 days**" in result["direct_answer"]
