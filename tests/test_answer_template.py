"""The deterministic template proposer (app/responder/answer_template.py).

The move this module makes: the formatter downstream can only detect,
rearrange or refuse -- it cannot turn three paragraphs into a comparison,
because that is writing. But everything needed to know the answer SHOULD be a
comparison is available before a word is written. So the determinism moves
upstream into the instruction, and the model keeps the writing.
"""
from __future__ import annotations

from app.responder.answer_template import (
    EvidenceShape,
    QuestionShape,
    entities_from_subquestions,
    extract_axis,
    suggest_from_question,
    suggest_template,
)
from app.responder.deterministic_format import deterministic_format

LIVE_QUESTION = (
    "What is the care management philosophy for Molina health, "
    "sunshine health and united healthcare"
)
LIVE_EVIDENCE = EvidenceShape(
    pages_per_entity={"Molina health": 3, "sunshine health": 2, "united healthcare": 1},
    documents=3, total_chunks=16,
)


class TestAxisExtraction:
    """The column header is the user's own words, verbatim, or nothing."""

    def test_the_live_question(self):
        assert extract_axis(LIVE_QUESTION) == "care management philosophy"

    def test_compare_phrasing(self):
        assert extract_axis("Compare the appeal deadlines for Molina and Aetna") == "appeal deadlines"

    def test_a_whole_clause_is_not_a_header(self):
        """Six words is the width a header carries before it stops being
        scannable. Longer means the parser matched something that is not a
        noun phrase."""
        axis = extract_axis(
            "What is the single most important thing I should know before I file for Molina"
        )
        assert axis == ""

    def test_no_match_returns_empty_not_a_guess(self):
        """Returning '' is a real answer -- the caller falls back to a generic
        header rather than the parser putting words in the reader's mouth."""
        assert extract_axis("Molina denials help") == ""
        assert extract_axis("") == ""


class TestEntitiesFromThePlanner:
    """Entities come from the planner's own sub-question split, by set
    difference. The surface parser that read a conjunction list out of the
    question was deleted, not deprecated: it had three known defects and a
    fallback that is exercised only when the good path fails, and is known
    buggy, makes the failure worse and hides it (Governor, 2026-09-13)."""

    def test_three_payers(self):
        assert entities_from_subquestions((
            "What is Molina's care management philosophy?",
            "What is Sunshine Health's care management philosophy?",
            "What is UnitedHealthcare's care management philosophy?",
        )) == ("Molina", "Sunshine Health", "UnitedHealthcare")

    def test_possessives_are_stripped(self):
        assert entities_from_subquestions((
            "Molina's deadline", "Aetna's deadline",
        )) == ("Molina", "Aetna")

    def test_a_topic_split_works_the_same_way(self):
        """The planner splits on whatever the question varies. Two topics for
        one payer is still a comparison -- which is why the table's corner
        cell is blank rather than headed 'Entity'."""
        assert entities_from_subquestions((
            "What is the timely filing deadline?",
            "What is the appeal deadline?",
        )) == ("timely filing", "appeal")

    def test_one_subquestion_is_not_a_comparison(self):
        assert entities_from_subquestions(("What is Molina's deadline?",)) == ()

    def test_identical_subquestions_yield_nothing(self):
        """No token distinguishes them, so there is nothing to put in a row
        label. Returning () means 'not a comparison', not 'parse failed'."""
        assert entities_from_subquestions(("same question", "same question")) == ()

    def test_no_entities_means_no_block(self):
        assert suggest_from_question("What is Molina's deadline?", entities=()) is None


class TestComparisonTemplate:
    ENTITIES = ("Molina health", "sunshine health", "united healthcare")

    def _t(self, evidence=None):
        return suggest_from_question(
            LIVE_QUESTION, entities=self.ENTITIES, evidence=evidence or LIVE_EVIDENCE)

    def test_it_proposes_a_table_with_one_row_per_entity(self):
        template = self._t()
        assert "|  | Care management philosophy | Source |" in template
        assert template.count("| … | … |") == 3

    def test_the_header_is_the_users_own_words(self):
        assert "Care management philosophy" in self._t()

    def test_it_names_the_cell_length_cap(self):
        """react's own FORMAT RULES cap bullets at 25 words and the live
        answer shipped 61-69. The cap is restated where it applies."""
        assert "25 words" in self._t()

    def test_uneven_evidence_is_called_out(self):
        template = self._t()
        assert "Evidence is uneven" in template
        assert "3 page(s)" in template and "1 page(s)" in template

    def test_balanced_evidence_says_nothing_about_it(self):
        """Renders only when its fact exists -- no signal, no line."""
        even = EvidenceShape(pages_per_entity={
            "Molina health": 2, "sunshine health": 2, "united healthcare": 2})
        assert "Evidence is uneven" not in self._t(even)

    def test_an_entity_with_nothing_keeps_its_row(self):
        """A silently missing row reads as an entity we chose not to mention."""
        thin = EvidenceShape(pages_per_entity={"Molina health": 3, "sunshine health": 2})
        template = self._t(thin)
        assert "Retrieval returned nothing for: united healthcare" in template

    def test_it_permits_react_to_disagree(self):
        """A suggestion, not a mandate -- react has read the evidence and this
        has not."""
        template = self._t()
        assert "This is a suggestion" in template
        assert "follow the evidence" in template

    def test_non_comparability_is_offered_as_a_finding(self):
        assert "That is a finding, not a failure." in self._t()


class TestExplicitFormatOutranks:
    def test_a_named_format_wins_over_the_comparison_rule(self):
        template = suggest_from_question(
            "Show me the appeal levels for Molina and Aetna as bullet points",
            entities=("Molina", "Aetna"))
        assert "bulleted list" in template
        assert "| … | … |" not in template


class TestNoSignalNoBlock:
    """v2/blocks.py's rule: a block renders only when its fact exists. A
    template suggested from nothing is noise in an already-long prompt."""

    def test_a_single_entity_lookup_gets_no_template(self):
        assert suggest_from_question("What is the timely filing limit for Molina") is None

    def test_a_vague_question_gets_no_template(self):
        assert suggest_from_question("help with denials") is None

    def test_an_empty_question_gets_no_template(self):
        assert suggest_from_question("") is None


class TestProcedural:
    def test_a_how_do_i_question_proposes_steps(self):
        template = suggest_template(QuestionShape(axis="how do i file an appeal"))
        assert "Step 1:" in template

    def test_steps_are_exempt_from_the_bullet_cap(self):
        """An instruction is legitimately longer than a bullet."""
        template = suggest_template(QuestionShape(axis="how to submit a corrected claim"))
        assert "may run longer than a bullet" in template


class TestTheLoopCloses:
    """The point of the whole exercise: a template the model follows produces
    a draft the downstream classifier renders correctly, with no inference
    anywhere in the chain."""

    FOLLOWED = (
        "| Entity | Care management philosophy | Source |\n"
        "| --- | --- | --- |\n"
        "| Molina Healthcare | Member advocacy — ICM coordinates care across the journey | molina_fl p111 |\n"
        "| Sunshine Health | Interdisciplinary — managers, social workers, providers | Sunshine p52 |\n"
        "| UnitedHealthcare | Holistic — medical, behavioral and social concerns | FL-Care-Provider p5 |\n"
        "\nUnitedHealthcare rests on a single page where Molina draws on three.\n"
    )

    def test_a_followed_template_renders_as_a_table(self):
        card = deterministic_format(self.FOLLOWED)
        assert [s["format"] for s in card["sections"]] == ["table"]
        assert len(card["sections"][0]["data"]["rows"]) == 3

    def test_the_caveat_survives_as_the_answer_line(self):
        card = deterministic_format(self.FOLLOWED)
        assert "single page" in card["direct_answer"]
        assert "| Molina" not in card["direct_answer"]

    def test_an_ignored_template_is_still_caught_downstream(self):
        """react may ignore the suggestion. The bullet guard is the backstop,
        and it names the violation rather than rendering it."""
        ignored = "\n".join(
            "- " + " ".join(["word"] * 60) for _ in range(3)
        )
        card = deterministic_format(ignored)
        assert card["sections"] == []
        assert card["presentation"]["rule_id"] == "abstain.prose_list"


class TestRealTrafficQuestions:
    """Verbatim from docs/coverage/payor-question-bank.json — live traffic
    exported from rag_query_decisions. Entities are supplied as the planner
    would, since nothing parses them from the question any more."""

    def _kind(self, q: str, entities: tuple = ()) -> str:
        t = suggest_from_question(q, entities=entities)
        if not t:
            return "none"
        if "| … | … |" in t:
            return "comparison"
        if "Step 1:" in t:
            return "steps"
        return "explicit"

    def test_multi_payor_compound_fires(self):
        assert self._kind(
            "timely filing deadline for Sunshine Health, Aetna, and UnitedHealthcare",
            ("Sunshine Health", "Aetna", "UnitedHealthcare"),
        ) == "comparison"

    def test_single_payor_lookups_do_not_fire(self):
        for q in (
            "What is Sunshine Health's timely filing deadline for claims?",
            "timely filing deadline out of network Sunshine Health",
            "medical necessity criteria for Behavioral Health Overlay Services",
        ):
            assert self._kind(q) == "none", q

    def test_a_how_to_question_proposes_steps(self):
        assert self._kind("how to appeal CARC 197 denial for Sunshine Health") == "steps"

    def test_role_nouns_can_no_longer_reach_a_row_label(self):
        """The live false positive: 'appeal levels and process for providers
        and members' produced a two-row table of audiences. It cannot now --
        row labels come from the planner's split, and the planner does not
        split a single-payer question into 'providers' and 'members'."""
        q = ("Sunshine Health FL Medicaid MCO appeal levels and process "
             "for providers and members")
        assert self._kind(q) == "none"

    def test_the_axis_comes_out_of_bare_noun_phrase_queries(self):
        """Most live traffic has no interrogative to anchor on."""
        assert extract_axis(
            "timely filing deadline for Sunshine Health and Aetna"
        ) == "timely filing deadline"
