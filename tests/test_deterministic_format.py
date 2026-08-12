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

    def test_five_short_pairs_promoted_to_table_not_stats(self):
        """FE's stats-tile cap is 4 -- 5 short pairs must route to table,
        not overflow a stats section past its render cap."""
        draft = "\n".join(f"Level {i}: {i * 10} days" for i in range(1, 6))
        result = deterministic_format(draft)
        assert len(result["sections"]) == 1
        sec = result["sections"][0]
        assert sec["format"] == "table"
        assert len(sec["data"]["rows"]) == 5


class TestMarkdownTable:
    def test_pipe_table_detected(self):
        draft = (
            "Here is the fee schedule:\n\n"
            "| Code | Description | Rate |\n"
            "| --- | --- | --- |\n"
            "| 90834 | Individual therapy | $85.00 |\n"
            "| 90837 | Extended therapy | $120.00 |\n"
        )
        result = deterministic_format(draft)
        assert len(result["sections"]) == 1
        sec = result["sections"][0]
        assert sec["format"] == "table"
        assert sec["data"]["headers"] == ["Code", "Description", "Rate"]
        assert sec["data"]["rows"] == [
            ["90834", "Individual therapy", "$85.00"],
            ["90837", "Extended therapy", "$120.00"],
        ]

    def test_no_table_no_section(self):
        draft = "This has a | pipe | character but no real table structure."
        result = deterministic_format(draft)
        assert result["sections"] == []

    def test_table_takes_priority_over_label_value_pairs(self):
        """A draft with both shapes present should not double-emit --
        table (most structurally specific) wins."""
        draft = (
            "Deadline: 180 days\n\n"
            "| Code | Rate |\n"
            "| --- | --- |\n"
            "| 90834 | $85.00 |\n"
        )
        result = deterministic_format(draft)
        assert len(result["sections"]) == 1
        assert result["sections"][0]["format"] == "table"
        assert result["sections"][0]["data"]["headers"] == ["Code", "Rate"]


class TestBullets:
    def test_three_bullets_detected(self):
        draft = (
            "You'll need the following:\n"
            "- Completed claim form\n"
            "- Proof of timely filing\n"
            "- Cover letter explaining the delay\n"
        )
        result = deterministic_format(draft)
        assert len(result["sections"]) == 1
        sec = result["sections"][0]
        assert sec["format"] == "bullets"
        assert sec["bullets"] == [
            "Completed claim form",
            "Proof of timely filing",
            "Cover letter explaining the delay",
        ]
        assert "data" not in sec

    def test_asterisk_bullets_detected(self):
        draft = "* First item\n* Second item\n* Third item\n"
        result = deterministic_format(draft)
        assert result["sections"][0]["format"] == "bullets"

    def test_two_bullets_not_promoted(self):
        """Fewer than 3 isn't confidently a list -- could be a stray dash
        in prose."""
        draft = "- Only one\n- Also this\n"
        result = deterministic_format(draft)
        assert result["sections"] == []

    def test_bullets_take_priority_over_label_value_pairs(self):
        draft = (
            "Deadline: 180 days\n"
            "- First requirement\n"
            "- Second requirement\n"
            "- Third requirement\n"
        )
        result = deterministic_format(draft)
        assert len(result["sections"]) == 1
        assert result["sections"][0]["format"] == "bullets"


class TestSteps:
    def test_step_prefixed_lines_detected(self):
        draft = (
            "Step 1: Gather your documentation\n"
            "Step 2: Complete the appeal form\n"
            "Step 3: Submit via the provider portal\n"
        )
        result = deterministic_format(draft)
        assert len(result["sections"]) == 1
        sec = result["sections"][0]
        assert sec["format"] == "steps"
        assert sec["data"]["items"] == [
            {"label": "Gather your documentation"},
            {"label": "Complete the appeal form"},
            {"label": "Submit via the provider portal"},
        ]

    def test_numbered_lines_detected(self):
        draft = "1. Gather documentation\n2. Complete the form\n3. Submit it\n"
        result = deterministic_format(draft)
        assert result["sections"][0]["format"] == "steps"

    def test_single_step_not_promoted(self):
        draft = "Step 1: Just do this one thing."
        result = deterministic_format(draft)
        assert result["sections"] == []

    def test_steps_take_priority_over_label_value_pairs_but_not_bullets(self):
        draft = (
            "Deadline: 180 days\n"
            "Step 1: Gather documentation\n"
            "Step 2: Submit the form\n"
        )
        result = deterministic_format(draft)
        assert len(result["sections"]) == 1
        assert result["sections"][0]["format"] == "steps"


class TestRawExcerptGuard:
    """react's thin-evidence fast-mode hedge (_build_fast_mode_hedge) ships a
    literal retrieved-chunk excerpt verbatim -- deliberately NOT synthesized,
    to avoid the appearance of confident synthesis when evidence is thin.
    Real policy text inside that excerpt can have genuine Label: Value
    lines; promoting those into a "Key Facts" stats card would misrepresent
    an unvetted excerpt as something structured with confidence. Same
    defensive posture as react_loop.py's _looks_like_raw_structured_blob
    guarding the JSON case (2026-08-10, ReAct agent's real-sample audit)."""

    def test_excerpt_with_label_value_shape_not_promoted(self):
        draft = (
            "[1] Sunshine Provider Manual [authority=authoritative]\n"
            "Initial filing: 180 days\n"
            "Resubmission: 90 days\n"
            "Copay: $25\n"
        )
        result = deterministic_format(draft)
        assert result["sections"] == []
        # Still bolds key facts -- only structural promotion is suppressed.
        assert "**180 days**" in result["direct_answer"]

    def test_excerpt_with_bullet_shape_not_promoted(self):
        draft = (
            "[2] Sunshine Appeals Guide [authority=authoritative]\n"
            "- Submit within 90 days\n"
            "- Include cover letter\n"
            "- Attach medical records\n"
        )
        result = deterministic_format(draft)
        assert result["sections"] == []

    def test_normal_prose_with_bracket_not_excerpt(self):
        """The guard only fires on a citation marker at the very START of
        the draft -- an inline [N] citation elsewhere in normal synthesized
        prose must not suppress structuring."""
        draft = "Initial filing: 180 days\nResubmission: 90 days [1]\nCopay: $25\n"
        result = deterministic_format(draft)
        assert len(result["sections"]) == 1
        assert result["sections"][0]["format"] == "stats"


class TestRealSampleShapes:
    """Real react_draft samples from dev (ReAct agent's 2026-08-10 audit,
    400 unique turns over 14 days) -- regression coverage for the exact
    shapes seen in production, not just synthetic fixtures."""

    def test_real_bullet_sample_with_three_space_indent(self):
        """Real sample cid=438f3da7 -- bullets used '*   text' (3 spaces
        after the marker), not just '* text'. \\s+ already covers this but
        pin it down with the literal real-world formatting."""
        draft = (
            "Hey Genius! For a timely filing denial from Sunshine Health, "
            "the most likely CARC is 29 ('The time limit for filing has expired').\n\n"
            "Your appeal playbook is now available in the structured card above, "
            "detailing the submission method, deadlines, required documents, and escalation steps.\n\n"
            "To help you prepare, here are some key questions to consider:\n"
            "*   What is the Sunshine Health timely filing limit for this specific service type and date of service?\n"
            "*   What was the original submission date of this claim to Sunshine Health?\n"
            "*   Are there any documented circumstances that would extend the timely filing limit for this claim?\n"
        )
        result = deterministic_format(draft)
        assert len(result["sections"]) == 1
        sec = result["sections"][0]
        assert sec["format"] == "bullets"
        assert len(sec["bullets"]) == 3

    def test_real_pipe_table_sample_with_br_tags_in_cells(self):
        """Real sample cid=65ed12e2 -- markdown table with <br>-joined
        sub-bullets inside cells, the one real pipe-table instance ReAct
        found in 400 sampled turns.

        2026-08-12 update (live finding, Chat Master/Ananth, cid=c60f9981):
        raw <br> tags ship unrendered to the user -- the cell is a plain
        string in the typed table block, not HTML. Must be replaced with
        a plain-text separator, not preserved verbatim as this test
        originally asserted."""
        draft = (
            "| Topic | Requirement / Process | Deadline | Details & Contact Information |\n"
            "| :--- | :--- | :--- | :--- |\n"
            "| Filing COB Claims | Submit the claim after receiving the primary payer's EOP. "
            "Ensure all COB data is correct. | Within 90 days from the date of the primary payer's EOP. | "
            "Electronic Claims (837s):<br>Institutional (837I): COB data must be in loop 2300.<br>more detail here |\n"
        )
        result = deterministic_format(draft)
        assert len(result["sections"]) == 1
        sec = result["sections"][0]
        assert sec["format"] == "table"
        assert sec["data"]["headers"] == ["Topic", "Requirement / Process", "Deadline", "Details & Contact Information"]
        assert len(sec["data"]["rows"]) == 1
        assert "<br>" not in sec["data"]["rows"][0][3]
        assert "Electronic Claims (837s):; Institutional (837I): COB data must be in loop 2300.; more detail here" == sec["data"]["rows"][0][3]

    def test_real_transposed_table_sample_br_stripped(self):
        """Real sample cid=c60f9981 -- the exact live-reported bug: a
        multi-value cell using <br> to separate Participating/Non-
        Participating values. Confirmed via llm_calls this went through
        the deterministic fast path (no integrator_a call), so this
        parser is the correct fix site."""
        draft = (
            "| Scenario | Sunshine Health | Aetna |\n"
            "| :--- | :--- | :--- |\n"
            "| Initial Claims | 180 days | **Participating:** 180 days <br> **Non-Participating:** 365 days |\n"
        )
        result = deterministic_format(draft)
        sec = result["sections"][0]
        cell = sec["data"]["rows"][0][2]
        assert "<br>" not in cell
        assert "**Participating:** 180 days; **Non-Participating:** 365 days" == cell

    def test_self_closing_br_variant_also_stripped(self):
        draft = (
            "| A | B |\n"
            "| :--- | :--- |\n"
            "| x | one<br/>two<br />three |\n"
        )
        result = deterministic_format(draft)
        cell = result["sections"][0]["data"]["rows"][0][1]
        assert "<br" not in cell
        assert cell == "one; two; three"
