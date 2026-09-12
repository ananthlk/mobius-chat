"""Tests for the envelope classifier (ENVELOPE_CLASSIFIER_SPEC.md Phase 1).

The acceptance criteria these cover, by requirement:

  R1  purity, rule_id always set, no I/O
  R2  ladder order, rule 1 beats everything, rule 2 never re-classified
  R3  gates return abstain, abstain never degrades to bullets
  R5  no threshold literals -- every bound comes from mobius_contracts
  R6  render budget: cap, degradation, duplicate suppression
  R8  cross-path agreement: the fast path and a typed payload carrying the
      same content produce the same format AND the same rule_id

The cross-path class at the bottom is the one that matters most. It is the
assertion that did not exist before and could not have existed before, since
there was no shared vocabulary in which to state it.
"""
from __future__ import annotations

import pytest

from mobius_contracts.taxonomies.envelope_thresholds import (
    BULLETS_MIN_ITEMS,
    MAX_RICH_BLOCKS_PER_TURN,
    PAIRS_MAX_ITEMS,
    STATS_MAX_ITEMS,
    STATS_MAX_VALUE_CHARS,
    STEPS_MIN_ITEMS,
)
from app.responder.deterministic_format import deterministic_format, extract_payload
from app.responder.envelope_classifier import (
    ContentPayload,
    IntentSignals,
    Item,
    RenderBudget,
    TableData,
    apply_render_budget,
    build_section,
    classify_envelope,
    detect_explicit_format,
)


def _pairs(n: int, value: str = "180 days") -> tuple[tuple[str, str], ...]:
    return tuple((f"Label {i}", value) for i in range(n))


class TestPurity:
    """R1."""

    def test_identical_inputs_give_identical_verdicts(self):
        payload = ContentPayload(pairs=_pairs(3))
        first = classify_envelope(payload)
        second = classify_envelope(payload)
        assert first == second

    def test_every_path_sets_a_rule_id(self):
        cases = [
            ContentPayload(),
            ContentPayload(pairs=_pairs(3)),
            ContentPayload(pairs=_pairs(PAIRS_MAX_ITEMS)),
            ContentPayload(items=tuple(Item(label=f"x{i}") for i in range(3)), explicit_list=True),
            ContentPayload(items=(Item(label="a"), Item(label="b")), ordered=True),
            ContentPayload(table=TableData(headers=("A",), rows=(("1",),))),
            ContentPayload(items=(Item(label="a", weight=0.5),)),
            ContentPayload(items=(Item(condition="if x", result="then y"),)),
            ContentPayload(text="[1] excerpt", pairs=_pairs(3)),
        ]
        for payload in cases:
            verdict = classify_envelope(payload)
            assert verdict.rule_id, f"empty rule_id for {payload}"
            assert verdict.reason

    def test_classifier_does_not_mutate_its_input(self):
        payload = ContentPayload(pairs=_pairs(3))
        before = (payload.pairs, payload.items, payload.table)
        classify_envelope(payload)
        assert (payload.pairs, payload.items, payload.table) == before


class TestRuleOrder:
    """R2."""

    def test_explicit_intent_beats_every_shape(self):
        payload = ContentPayload(
            table=TableData(headers=("A", "B"), rows=(("1", "2"),)),
            items=tuple(Item(label=f"x{i}") for i in range(3)),
            explicit_list=True,
        )
        verdict = classify_envelope(payload, IntentSignals(explicit_format="bullets"))
        assert verdict.format == "bullets"
        assert verdict.rule_id == "intent.explicit"

    def test_explicit_intent_beats_a_typed_hint(self):
        verdict = classify_envelope(
            ContentPayload(),
            IntentSignals(explicit_format="table", typed_hint_format="stats"),
        )
        assert verdict.format == "table"
        assert verdict.rule_id == "intent.explicit"

    def test_explicit_intent_survives_the_gates(self):
        """A user who asked for a table gets a table even on a thin-evidence
        turn. The hedge belongs in the prose, not in silently ignoring what
        they asked for."""
        verdict = classify_envelope(
            ContentPayload(pairs=_pairs(3)),
            IntentSignals(explicit_format="table"),
            RenderBudget(is_thin_evidence=True),
        )
        assert verdict.rule_id == "intent.explicit"

    def test_typed_hint_is_never_reclassified(self):
        """The payload's own shape says bullets; the tool said stats. The
        tool wins -- it knows its output better than a predicate over that
        output's rendered text does."""
        payload = ContentPayload(
            items=tuple(Item(label=f"x{i}") for i in range(5)), explicit_list=True
        )
        verdict = classify_envelope(payload, IntentSignals(typed_hint_format="stats"))
        assert verdict.format == "stats"
        assert verdict.rule_id == "hint.typed"

    def test_custom_tool_formats_pass_through_unchecked(self):
        verdict = classify_envelope(
            ContentPayload(), IntentSignals(typed_hint_format="appeals_playbook")
        )
        assert verdict.format == "appeals_playbook"

    def test_table_beats_pairs(self):
        payload = ContentPayload(
            table=TableData(headers=("Code", "Rate"), rows=(("90834", "$85"),)),
            pairs=_pairs(3),
        )
        assert classify_envelope(payload).rule_id == "shape.table"

    def test_bullets_beat_stats(self):
        """Pinned precedence: a real list whose prose happens to contain a
        colon is a list, not a stats card."""
        payload = ContentPayload(
            items=tuple(Item(label=f"x{i}") for i in range(BULLETS_MIN_ITEMS)),
            explicit_list=True,
            pairs=_pairs(2),
        )
        assert classify_envelope(payload).rule_id == "shape.bullets"

    def test_steps_beat_stats(self):
        payload = ContentPayload(
            items=tuple(Item(label=f"x{i}") for i in range(STEPS_MIN_ITEMS)),
            ordered=True,
            pairs=_pairs(2),
        )
        assert classify_envelope(payload).rule_id == "shape.steps"

    def test_weights_beat_an_explicit_list(self):
        """bars/conditions sit above bullets: a weighted list rendered as
        plain bullets throws away the weights."""
        payload = ContentPayload(
            items=tuple(Item(label=f"x{i}", weight=0.5) for i in range(4)),
            explicit_list=True,
        )
        assert classify_envelope(payload).rule_id == "shape.bars"

    def test_conditions_beat_an_explicit_list(self):
        payload = ContentPayload(
            items=tuple(Item(condition=f"if {i}", result=f"then {i}") for i in range(4)),
            explicit_list=True,
        )
        assert classify_envelope(payload).rule_id == "shape.conditions"

    def test_only_one_rule_fires(self):
        payload = ContentPayload(
            table=TableData(headers=("A", "B"), rows=(("1", "2"),)),
            items=tuple(Item(label=f"x{i}") for i in range(3)),
            explicit_list=True,
            pairs=_pairs(3),
        )
        section = build_section(payload, classify_envelope(payload))
        assert section is not None
        assert section["format"] == "table"


class TestThresholds:
    """R2 + R5 -- bounds are read from the contract, never hardcoded here."""

    def test_stats_at_the_item_cap(self):
        payload = ContentPayload(pairs=_pairs(STATS_MAX_ITEMS))
        assert classify_envelope(payload).rule_id == "shape.stats"

    def test_one_past_the_item_cap_becomes_a_table(self):
        payload = ContentPayload(pairs=_pairs(STATS_MAX_ITEMS + 1))
        verdict = classify_envelope(payload)
        assert verdict.format == "table"
        assert verdict.rule_id == "shape.table.pairs"

    def test_long_values_become_a_table(self):
        payload = ContentPayload(pairs=_pairs(2, value="x" * (STATS_MAX_VALUE_CHARS + 1)))
        assert classify_envelope(payload).rule_id == "shape.table.pairs"

    def test_value_exactly_at_the_char_cap_stays_stats(self):
        payload = ContentPayload(pairs=_pairs(2, value="x" * STATS_MAX_VALUE_CHARS))
        assert classify_envelope(payload).rule_id == "shape.stats"

    def test_above_the_pairs_band_abstains(self):
        payload = ContentPayload(pairs=_pairs(PAIRS_MAX_ITEMS + 1))
        assert classify_envelope(payload).format is None

    def test_below_the_bullets_minimum_abstains(self):
        payload = ContentPayload(
            items=tuple(Item(label=f"x{i}") for i in range(BULLETS_MIN_ITEMS - 1)),
            explicit_list=True,
        )
        assert classify_envelope(payload).format is None


class TestGates:
    """R3."""

    def test_raw_excerpt_abstains(self):
        payload = ContentPayload(text="[1] Provider Manual\nCopay: $25", pairs=_pairs(3))
        verdict = classify_envelope(payload)
        assert verdict.format is None
        assert verdict.rule_id == "abstain.raw_excerpt"

    def test_thin_evidence_abstains(self):
        verdict = classify_envelope(
            ContentPayload(pairs=_pairs(3)), budget=RenderBudget(is_thin_evidence=True)
        )
        assert verdict.rule_id == "abstain.thin_evidence"

    def test_single_fact_abstains(self):
        verdict = classify_envelope(ContentPayload(pairs=_pairs(1)))
        assert verdict.rule_id == "abstain.single_fact"

    def test_abstain_never_degrades_to_bullets(self):
        """An abstain is a decision to render prose. Falling through to a
        consolation bullets list would defeat the gate entirely."""
        payload = ContentPayload(text="[1] excerpt", pairs=_pairs(3),
                                 items=tuple(Item(label=f"x{i}") for i in range(5)),
                                 explicit_list=True)
        verdict = classify_envelope(payload)
        assert verdict.format is None
        assert build_section(payload, verdict) is None

    def test_a_gate_beats_every_shape_rule(self):
        payload = ContentPayload(
            text="[1] excerpt",
            table=TableData(headers=("A",), rows=(("1",),)),
        )
        assert classify_envelope(payload).rule_id == "abstain.raw_excerpt"


class TestExplicitFormatDetection:
    """R2 rule 1 -- the regex that replaced a prompt instruction."""

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("show me the deadlines as a table", "table"),
            ("can you put that in a table", "table"),
            ("give it to me in bullet points", "bullets"),
            ("as bullets please", "bullets"),
            ("list the requirements as a list", "bullets"),
            ("walk me through it as steps", "steps"),
            ("tabulate the rates", "table"),
        ],
    )
    def test_requests_are_detected(self, message, expected):
        assert detect_explicit_format(message) == expected

    @pytest.mark.parametrize(
        "message",
        [
            "what does the fee table say about 90834",
            "is there a list of covered services",
            "what are the steps in the appeal process",
            "",
            None,
        ],
    )
    def test_questions_about_a_format_are_not_requests_for_one(self, message):
        """'what does the fee table say' is a question about a table, not a
        request for one. The preposition anchor is what separates them."""
        assert detect_explicit_format(message) is None


class TestBuildSection:
    """Field placement -- bubble.ts reads sec['bullets'] at the top level and
    everything else under sec['data']. Getting this wrong renders an empty
    section instead of failing, so it is pinned."""

    def test_bullets_live_at_the_top_level(self):
        payload = ContentPayload(
            items=tuple(Item(label=f"x{i}") for i in range(3)), explicit_list=True
        )
        section = build_section(payload, classify_envelope(payload))
        assert section["bullets"] == ["x0", "x1", "x2"]
        assert "data" not in section

    def test_stats_live_under_data_items(self):
        payload = ContentPayload(pairs=(("Copay", "$25"), ("Filing", "180 days")))
        section = build_section(payload, classify_envelope(payload))
        assert section["data"]["items"] == [
            {"label": "Copay", "value": "$25"},
            {"label": "Filing", "value": "180 days"},
        ]

    def test_pairs_table_gets_item_detail_headers(self):
        payload = ContentPayload(pairs=_pairs(PAIRS_MAX_ITEMS - 1, value="x" * 40))
        section = build_section(payload, classify_envelope(payload))
        assert section["data"]["headers"] == ["Item", "Detail"]

    def test_bars_keep_their_weights(self):
        payload = ContentPayload(items=(Item(label="a", weight=0.8), Item(label="b", weight=0.2)))
        section = build_section(payload, classify_envelope(payload))
        assert section["data"]["items"][0]["weight"] == 0.8

    def test_abstain_builds_nothing(self):
        payload = ContentPayload()
        assert build_section(payload, classify_envelope(payload)) is None


class TestRenderBudget:
    """R6."""

    def _rich(self, label: str, value: str = "x"):
        return {"intent": "process", "label": label, "format": "stats",
                "data": {"items": [{"label": label, "value": value}]}}

    def test_under_the_cap_everything_survives(self):
        sections = [self._rich("A"), self._rich("B")]
        outcome = apply_render_budget(sections, max_rich_blocks=MAX_RICH_BLOCKS_PER_TURN)
        assert len(outcome.sections) == 2
        assert outcome.degraded == []

    def test_past_the_cap_degrades_to_bullets(self):
        sections = [self._rich("A"), self._rich("B"), self._rich("C")]
        outcome = apply_render_budget(sections, max_rich_blocks=2)
        assert [s["format"] for s in outcome.sections] == ["stats", "stats", "bullets"]
        assert outcome.degraded == ["C"]

    def test_degradation_keeps_the_content(self):
        sections = [self._rich("A"), self._rich("B"), self._rich("C", "180 days")]
        outcome = apply_render_budget(sections, max_rich_blocks=2)
        assert outcome.sections[-1]["bullets"] == ["C: 180 days"]

    def test_tool_sections_are_never_the_ones_degraded(self):
        sections = [self._rich("Inferred 1"), self._rich("Inferred 2"), self._rich("Appeal rules")]
        outcome = apply_render_budget(
            sections, max_rich_blocks=2, protected_labels={"Appeal rules"}
        )
        by_label = {s["label"]: s["format"] for s in outcome.sections}
        assert by_label["Appeal rules"] == "stats"
        assert "bullets" in by_label.values()

    def test_a_section_the_prose_already_said_is_dropped(self):
        prose = "Sunshine Health requires claims within 180 days of service."
        sections = [self._rich("Filing", "180 days")]
        outcome = apply_render_budget(sections, direct_answer=prose)
        assert outcome.sections == []
        assert outcome.dropped == ["Filing"]

    def test_one_novel_value_keeps_the_section(self):
        """Conservative by construction -- a card that adds anything at all
        earns its space."""
        prose = "Sunshine Health requires claims within 180 days of service."
        section = {
            "intent": "process", "label": "Filing", "format": "table",
            "data": {"headers": ["Item", "Detail"],
                     "rows": [["Initial", "180 days"], ["Appeal", "60 days"]]},
        }
        outcome = apply_render_budget([section], direct_answer=prose)
        assert outcome.sections == [section]


class TestCrossPathAgreement:
    """R8 -- the metric the whole spec exists to move.

    The fast path extracts a payload out of prose; a tool or the enricher
    supplies an equivalent payload directly. Identical content must produce
    an identical format AND an identical rule_id, or the inconsistency this
    work removes has grown back.
    """

    CASES = [
        (
            "Initial filing: 180 days\nResubmission: 90 days\nCopay: $25",
            "stats",
            "shape.stats",
        ),
        (
            "- Completed claim form\n- Proof of timely filing\n- Cover letter\n",
            "bullets",
            "shape.bullets",
        ),
        (
            "Step 1: Gather documentation\nStep 2: Submit the form\n",
            "steps",
            "shape.steps",
        ),
        (
            "| Code | Rate |\n| --- | --- |\n| 90834 | $85.00 |\n",
            "table",
            "shape.table",
        ),
        (
            "\n".join(f"Level {i}: {i * 10} days" for i in range(1, 6)),
            "table",
            "shape.table.pairs",
        ),
        (
            "Sunshine Health requires claims within 180 days of service.",
            None,
            "abstain.no_match",
        ),
    ]

    @pytest.mark.parametrize("draft,expected_format,expected_rule", CASES)
    def test_prose_path_verdict(self, draft, expected_format, expected_rule):
        verdict = classify_envelope(extract_payload(draft))
        assert verdict.format == expected_format
        assert verdict.rule_id == expected_rule

    @pytest.mark.parametrize("draft,expected_format,expected_rule", CASES)
    def test_typed_path_agrees_with_the_prose_path(self, draft, expected_format, expected_rule):
        """Re-classify the extracted payload as if a tool had handed it over
        typed rather than as prose. Same verdict, same rule."""
        payload = extract_payload(draft)
        retyped = ContentPayload(
            table=payload.table,
            pairs=payload.pairs,
            items=payload.items,
            explicit_list=payload.explicit_list,
            ordered=payload.ordered,
        )
        verdict = classify_envelope(retyped)
        assert verdict.format == expected_format
        assert verdict.rule_id == expected_rule

    @pytest.mark.parametrize("draft,expected_format,_rule", CASES)
    def test_end_to_end_card_matches_the_verdict(self, draft, expected_format, _rule):
        card = deterministic_format(draft)
        if expected_format is None:
            assert card["sections"] == []
        else:
            assert [s["format"] for s in card["sections"]] == [expected_format]


class TestContiguousPairs:
    """A ceiling that guards against regex over-match must not also cap how
    many real facts a block can hold. Found stress-testing long drafts:
    nine genuine appeal deadlines in a contiguous run abstained to prose."""

    def test_scattered_pairs_above_the_band_still_abstain(self):
        payload = ContentPayload(pairs=_pairs(PAIRS_MAX_ITEMS + 3), contiguous=False)
        assert classify_envelope(payload).rule_id == "abstain.no_match"

    def test_a_contiguous_run_above_the_band_becomes_a_table(self):
        payload = ContentPayload(pairs=_pairs(PAIRS_MAX_ITEMS + 3), contiguous=True)
        verdict = classify_envelope(payload)
        assert verdict.format == "table"
        assert verdict.rule_id == "shape.table.pairs"

    def test_contiguity_does_not_bypass_the_stats_caps(self):
        """Contiguity lifts the ceiling on the pairs BAND, not on stats tiles
        -- the frontend still only draws STATS_MAX_ITEMS of them."""
        payload = ContentPayload(pairs=_pairs(STATS_MAX_ITEMS + 1), contiguous=True)
        assert classify_envelope(payload).format == "table"

    def test_contiguity_does_not_rescue_a_single_pair(self):
        assert classify_envelope(
            ContentPayload(pairs=_pairs(1), contiguous=True)
        ).rule_id == "abstain.single_fact"


class TestMultiSection:
    """Long answers carry more than one shape; the single-section path keeps
    the winner and drops the rest. These pin the segmented path."""

    MIXED = (
        "Sunshine Health's 2026 timely filing changes affect three claim types.\n\n"
        "- Initial claims move to 180 days\n"
        "- COB claims remain at 90 days\n"
        "- Corrected claims are unchanged\n\n"
        "| Claim type | 2025 | 2026 |\n"
        "| --- | --- | --- |\n"
        "| Initial | 365 days | 180 days |\n\n"
        "Effective date: January 1, 2026\n"
        "Grace period: 60 days\n"
    )

    def test_single_section_path_keeps_only_the_winner(self):
        sections = deterministic_format(self.MIXED, multi_section=False)["sections"]
        assert [s["format"] for s in sections] == ["table"]

    def test_multi_section_path_keeps_every_block(self):
        sections = deterministic_format(self.MIXED, multi_section=True)["sections"]
        assert [s["format"] for s in sections] == ["bullets", "table", "stats"]

    def test_blocks_keep_their_source_order(self):
        sections = deterministic_format(self.MIXED, multi_section=True)["sections"]
        assert sections[0]["bullets"][0].startswith("Initial claims")
        assert sections[1]["data"]["headers"] == ["Claim type", "2025", "2026"]

    def test_prose_between_blocks_is_not_a_section(self):
        sections = deterministic_format(self.MIXED, multi_section=True)["sections"]
        assert all(s["format"] != "prose" for s in sections)
        assert len(sections) == 3

    def test_the_budget_caps_rich_blocks_on_long_drafts(self):
        """Table + steps + pairs is three rich blocks; the third degrades to
        bullets rather than being dropped."""
        draft = (
            "| Claim type | Deadline |\n| --- | --- |\n| Primary | 180 days |\n\n"
            "Step 1: Obtain the primary EOP\n"
            "Step 2: Populate loop 2320\n\n"
            "Payer ID: 68069\n"
            "Clearinghouse: Availity\n"
            "Escalation: provider.services@sunshinehealth.com\n"
        )
        formats = [s["format"] for s in deterministic_format(draft, multi_section=True)["sections"]]
        assert formats == ["table", "steps", "bullets"]

    def test_the_raw_excerpt_gate_still_covers_every_block(self):
        draft = (
            "[1] Sunshine Provider Manual\n"
            "- Submit within 90 days\n"
            "- Include a cover letter\n"
            "- Attach medical records\n"
        )
        assert deterministic_format(draft, multi_section=True)["sections"] == []

    def test_multi_section_is_on_by_default(self):
        """Flipped 2026-09-12 after measuring shape loss on long drafts. The
        single-section path stays reachable as an explicit fallback."""
        default = deterministic_format(self.MIXED)["sections"]
        explicit_multi = deterministic_format(self.MIXED, multi_section=True)["sections"]
        assert default == explicit_multi
        assert len(default) > 1

    def test_single_section_remains_reachable(self):
        sections = deterministic_format(self.MIXED, multi_section=False)["sections"]
        assert len(sections) == 1


class TestDirectAnswerSplit:
    """Sections have always been additive to direct_answer. Harmless when one
    section rendered; with every block becoming a section, the unsplit draft
    means the reader sees the same rows, steps and phone numbers twice."""

    LONG = (
        "Sunshine Health's appeal process runs in three levels, and the clock on each "
        "one starts from a different event.\n\n"
        "| Level | Deadline |\n| --- | --- |\n| Level 1 | 90 days |\n| Level 2 | 60 days |\n\n"
        "Expedited review is available where a delay would jeopardize the member's health.\n\n"
        "Step 1: Pull the original claim and the EOP\n"
        "Step 2: Complete the dispute form in full\n"
    )

    def test_block_content_leaves_the_answer_line(self):
        card = deterministic_format(self.LONG)
        answer = card["direct_answer"]
        assert "| Level |" not in answer
        assert "Step 1:" not in answer
        assert "90 days" not in answer

    def test_prose_between_blocks_survives(self):
        answer = deterministic_format(self.LONG)["direct_answer"]
        assert "runs in three levels" in answer
        assert "Expedited review is available" in answer

    def test_the_blocks_still_render_as_sections(self):
        """The content moved, it did not vanish."""
        sections = deterministic_format(self.LONG)["sections"]
        assert [s["format"] for s in sections] == ["table", "steps"]
        assert sections[0]["data"]["rows"][0] == ["Level 1", "90 days"]

    def test_the_gap_a_removed_block_leaves_is_collapsed(self):
        answer = deterministic_format(self.LONG)["direct_answer"]
        assert "\n\n\n" not in answer

    def test_facts_left_in_the_prose_are_still_bolded(self):
        draft = (
            "Claims must be filed within 180 days of service.\n\n"
            "- Original claim number\n- Denial CARC\n- Medical records\n"
        )
        card = deterministic_format(draft)
        assert "**180 days**" in card["direct_answer"]
        assert len(card["sections"]) == 1

    def test_an_all_structure_draft_keeps_its_full_answer_line(self):
        """direct_answer is the STREAMED anchor. Blanking it leaves the user
        watching an empty bubble until the card lands, so a draft with no
        prose at all falls back to the full text -- mild duplication beats a
        turn that looks broken while it loads."""
        draft = "Initial filing: 180 days\nResubmission: 90 days\nCopay: $25"
        card = deterministic_format(draft)
        assert card["sections"]
        assert "**180 days**" in card["direct_answer"]

    def test_a_draft_with_no_sections_is_untouched(self):
        draft = "Sunshine Health requires claims within 180 days of service."
        card = deterministic_format(draft)
        assert card["sections"] == []
        assert card["direct_answer"] == "Sunshine Health requires claims within **180 days** of service."

    def test_a_gated_draft_keeps_its_full_answer_line(self):
        """The raw-excerpt gate abstains on every block, so nothing rendered
        -- the excerpt must reach the user whole."""
        draft = (
            "[1] Sunshine Provider Manual\n"
            "- Submit within 90 days\n- Include cover letter\n- Attach records\n"
        )
        card = deterministic_format(draft)
        assert card["sections"] == []
        assert "Submit within" in card["direct_answer"]

    def test_single_section_mode_still_keeps_the_whole_draft(self):
        """The split belongs to the segmented path; the fallback path has no
        block boundaries to split on."""
        card = deterministic_format(self.LONG, multi_section=False)
        assert "| Level |" in card["direct_answer"]
