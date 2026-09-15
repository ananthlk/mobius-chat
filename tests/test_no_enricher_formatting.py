"""END-TO-END: a formatted answer card with NO model call anywhere.

This is the acceptance test for the whole line of work. The enricher made an
LLM call to structure an answer it could not check; the question this file
answers is whether the deterministic path produces a card good enough to
replace it.

Every draft below is a REAL react answer copied from a live trace, named by
correlation id. Nothing here is synthetic and nothing is paraphrased.

The assertions are deliberately about SHAPE, not wording: what envelope each
block became, that no fact appears twice, that nothing was silently dropped,
and that a draft with no honest structure is left alone rather than dressed
up. A test that asserted the prose would pass on a badly formatted answer and
fail on a re-worded good one.
"""
from __future__ import annotations

import json

import app.responder.envelope_classifier as ec
from app.responder.deterministic_format import deterministic_format

# ── cid 9c825ca5 — "tell me more about Molina's care management" ────────────
# Five labelled items averaging 31 words. Shipped as bullets; is a definition
# table.
MOLINA_FOLLOWUP = (
    "Of course, Genius! I'm happy to give you more detail on Molina's care "
    "management program.\n\n"
    "*   Dedicated Team: The Care Management team includes licensed nurses and "
    "clinicians with behavioral health experience to support members with mental "
    "health and substance use disorder needs. [1]\n"
    "*   Integrated Care Management (ICM): For members with high-risk psychiatric, "
    "medical, or psychosocial needs, there is a specialized ICM program. [1, 20]\n"
    "*   Provider Collaboration: Molina emphasizes partnership between Primary Care "
    "Providers and behavioral health specialists. [1]\n"
    "*   Provider Resources: Molina offers an online Behavioral Health Tool Kit for "
    "screening, assessment and diagnosis. [21]\n"
    "*   Member Support: Members have access to a 24/7 behavioral health crisis line "
    "staffed by clinicians. [3]\n\n"
    "Next step: To refer a member, contact Molina by phone at (855) 322-4076, by fax "
    "at (866) 440-9791, or by email at MFLCaseManagement@MolinaHealthcare.com."
)

# ── A long multi-shape answer: table + procedure + checklist + contacts ─────
APPEALS_LONG = (
    "Sunshine Health's appeal process runs in three levels, and the clock on each "
    "starts from a different event.\n\n"
    "| Level | Deadline | Clock starts |\n| --- | --- | --- |\n"
    "| Level 1 | 90 days | Denial date on the EOP |\n"
    "| Level 2 | 60 days | Level 1 determination letter |\n"
    "| Fair hearing | 120 days | Final internal determination |\n\n"
    "Note that the Level 2 clock starts from the determination letter date, not the "
    "date you received it.\n\n"
    "Step 1: Pull the original claim and the EOP showing the denial reason.\n"
    "Step 2: Complete the Provider Dispute Resolution Request form in full.\n"
    "Step 3: Submit through the provider portal and keep the confirmation number.\n\n"
    "You will need the following on hand:\n\n"
    "- The original claim number\n- The denial CARC and RARC codes\n"
    "- Medical records for the dates in dispute\n\n"
    "Provider services: 1-844-477-8313\nAppeals fax: 1-866-534-5978\n"
    "Payer ID: 68069\n"
)

# ── cid 65ed12e2 — a real pipe table with <br>-joined cells ─────────────────
COB_TABLE = (
    "| Topic | Requirement | Deadline |\n| :--- | :--- | :--- |\n"
    "| Filing COB Claims | Submit after the primary payer's EOP. | Within 90 days |\n"
)

# ── A genuinely unstructured policy narrative. The control. ─────────────────
NARRATIVE = (
    "Whether a prior authorization is required depends on the service, the place of "
    "service, and the product. For routine outpatient therapy the first twenty visits "
    "generally do not require authorization for Medicaid members, though the threshold "
    "is applied per provider rather than per member, which means a member who switches "
    "providers mid-year may trigger a new count."
)


def _formats(card):
    return [s["format"] for s in card["sections"]]


class TestNoModelIsInvolved:
    def test_the_formatter_imports_nothing_that_could_call_a_model(self):
        """Structural, not behavioural: the module graph has no client in it.
        A mocked-out model call would pass a behavioural test and still be a
        model call in production."""
        import ast

        for path in (
            "app/responder/envelope_classifier.py",
            "app/responder/deterministic_format.py",
            "app/responder/answer_template.py",
        ):
            tree = ast.parse(open(path).read())
            mods = {(n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            banned = [m for m in mods
                      if any(k in m.lower() for k in ("llm", "vertex", "openai", "genai", "anthropic"))]
            assert banned == [], (path, banned)

    def test_the_same_draft_always_produces_the_same_card(self):
        import json
        first = json.dumps(deterministic_format(MOLINA_FOLLOWUP), sort_keys=True)
        for _ in range(20):
            assert json.dumps(deterministic_format(MOLINA_FOLLOWUP), sort_keys=True) == first


class TestRealDraftsAreWellFormatted:
    def test_labelled_items_become_a_definition_table(self):
        card = deterministic_format(MOLINA_FOLLOWUP)
        assert _formats(card) == ["table"]
        assert len(card["sections"][0]["data"]["rows"]) == 5

    def test_a_long_answer_keeps_every_shape_it_had(self):
        """Table, procedure and checklist all survive. Before multi-section
        this rendered the table and dropped the rest into prose."""
        card = deterministic_format(APPEALS_LONG)
        assert _formats(card)[:3] == ["table", "steps", "bullets"]

    def test_the_render_budget_holds_on_a_long_answer(self):
        card = deterministic_format(APPEALS_LONG)
        rich = [f for f in _formats(card) if f in ("table", "steps", "stats", "bars", "conditions")]
        assert len(rich) <= ec.MAX_RICH_BLOCKS_PER_TURN

    def test_a_pipe_table_survives_verbatim(self):
        card = deterministic_format(COB_TABLE)
        assert _formats(card) == ["table"]
        assert card["sections"][0]["data"]["headers"] == ["Topic", "Requirement", "Deadline"]

    def test_unstructured_prose_is_left_alone(self):
        """The control. A formatter that manufactures a card here is worse
        than one that does nothing."""
        card = deterministic_format(NARRATIVE)
        assert card["sections"] == []
        assert "twenty visits" in card["direct_answer"]


class TestNothingIsSaidTwiceAndNothingIsLost:
    def test_a_rendered_block_leaves_the_answer_line(self):
        card = deterministic_format(APPEALS_LONG)
        assert "| Level 1 |" not in card["direct_answer"]
        assert "Step 1:" not in card["direct_answer"]

    def test_the_caveat_a_card_cannot_hold_survives(self):
        """The sentence that has no cell to live in is the one worth keeping
        prominent."""
        card = deterministic_format(APPEALS_LONG)
        assert "not the date you received it" in card["direct_answer"]

    def test_content_from_a_degraded_block_survives(self):
        card = deterministic_format(APPEALS_LONG)
        rendered = " ".join(str(s) for s in card["sections"])
        assert "68069" in rendered or "68069" in card["direct_answer"]

    def test_the_next_step_line_is_not_swallowed(self):
        card = deterministic_format(MOLINA_FOLLOWUP)
        assert "MFLCaseManagement@MolinaHealthcare.com" in card["direct_answer"]

    def test_no_row_is_duplicated_into_the_prose(self):
        card = deterministic_format(MOLINA_FOLLOWUP)
        assert "Dedicated Team:" not in card["direct_answer"]


class TestEveryDecisionIsExplainable:
    def test_every_section_came_from_a_named_rule(self):
        """The trace has to be able to answer "why is this a table". Each
        draft's verdict is checked through the same public entry points the
        pipeline uses."""
        from app.responder.deterministic_format import extract_payload, segment_draft

        for draft in (MOLINA_FOLLOWUP, APPEALS_LONG, COB_TABLE, NARRATIVE):
            for block in segment_draft(draft).blocks or (extract_payload(draft),):
                verdict = ec.classify_envelope(block)
                assert verdict.rule_id
                assert verdict.reason
                assert verdict.format is None or verdict.format in ec.RENDERABLE_FORMATS

    def test_an_abstain_says_which_kind_it_was(self):
        long_unlabelled = "\n".join(
            "- " + " ".join(["word"] * 40) for _ in range(3))
        card = deterministic_format(long_unlabelled)
        assert card["presentation"]["rule_id"] == "abstain.prose_list"


class TestTypeGuardsAtTheEntryPoints:
    """Guarded on TYPE, not truthiness.

    `(x or "").strip()` reads as a null check and is not one: it handles None
    and "" correctly, passes an EMPTY dict silently, and raises on a populated
    one. So the bug hides on exactly the payloads that are empty and stays
    live on the ones that matter.

    Governor hit this exact shape on 2026-09-13 (react_loop:6637 —
    `tool_results[-1].get("result") or ""` handed a parsed rag contract to a
    regex, and every v2 turn on dev failed). The formatter had the same hole.
    """

    def test_a_non_string_draft_renders_nothing_rather_than_raising(self):
        for bad in ({"matches": [1, 2]}, {}, ["a", "b"], [], 7, 0.0, object()):
            card = deterministic_format(bad)
            assert card["sections"] == []
            assert card["direct_answer"] == ""

    def test_the_populated_and_empty_cases_behave_the_SAME(self):
        """The asymmetry is the defect. An empty dict passing while a full one
        raises is what hides this class of bug until it reaches production."""
        assert deterministic_format({}) == deterministic_format({"matches": [1, 2]})

    def test_none_and_empty_string_are_unchanged(self):
        assert deterministic_format(None)["sections"] == []
        assert deterministic_format("")["direct_answer"] == ""

    def test_a_real_draft_is_untouched_by_the_guard(self):
        assert deterministic_format(MOLINA_FOLLOWUP)["sections"][0]["format"] == "table"


class TestTheAnswerLineNeverShowsRawMarkup:
    """Found by RENDERING, not by asserting.

    A draft that is nothing but a pipe table has no prose to fall back to, so
    the all-structure fallback handed the raw markdown to direct_answer — and
    the card showed

        | Topic | Requirement | Deadline |
        | :--- | :--- | :--- |

    above the same content as a real table, with a raw <br> in it that the
    table renderer had already cleaned. Duplicated AND uglier than nothing.
    Every unit test passed. It took putting the card on a screen (cid
    65ed12e2, 2026-09-13) to see it.
    """

    PURE_TABLE = (
        "| Topic | Requirement | Deadline |\n| :--- | :--- | :--- |\n"
        "| Filing COB Claims | Submit after the primary payer's EOP. | Within 90 days |\n"
        "| Electronic Claims | Institutional (837I): loop 2300.<br>Professional: 2320. | Same |\n"
    )

    def test_a_pure_table_draft_leaves_the_answer_line_empty(self):
        card = deterministic_format(self.PURE_TABLE)
        assert card["direct_answer"] == ""
        assert [s["format"] for s in card["sections"]] == ["table"]

    def test_the_table_itself_is_unaffected(self):
        rows = deterministic_format(self.PURE_TABLE)["sections"][0]["data"]["rows"]
        assert len(rows) == 2
        assert "<br>" not in rows[1][1]

    def test_a_readable_all_structure_draft_still_keeps_its_anchor(self):
        """The fallback exists because direct_answer is the STREAMED anchor.
        It is only the lesser evil when the draft READS as prose — which a
        label/value block does and a pipe table does not."""
        card = deterministic_format(
            "Initial filing: 180 days\nResubmission: 90 days\nCopay: $25")
        assert card["sections"]
        assert "180 days" in card["direct_answer"]

    def test_a_draft_with_prose_around_a_table_is_unchanged(self):
        card = deterministic_format(
            "Here are the deadlines.\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\nThat is all.")
        assert "Here are the deadlines." in card["direct_answer"]
        assert "| A |" not in card["direct_answer"]


class TestRawToolPayloadsBecomeTables:
    """A tool result shipped verbatim as the answer.

    react's fast path does this for non-rag tools, and live on 2026-09-15 a
    CMHC org lookup reached the user as its entire JSON payload:

        [{"org_entity_id": "9127...", "org_name": "DAVID LAWRENCE CENTER", ...}]

    v1 handled it by routing the turn to the LLM integrator to rewrite the
    JSON as prose. That works and it is backwards — it spends a model call to
    DESTROY structure that is already perfect. A list of records with shared
    keys is the single most table-shaped thing that can arrive here.
    """

    LIVE = (
        '[{"org_entity_id": "9127414240976b712796f4a912cb40e3", '
        '"org_name": "DAVID LAWRENCE CENTER", "org_type": "CMHC", '
        '"market_tier": "sparse", "orgs_in_market": 2, "billing_npi_count": 1, '
        '"billing_npis": ["1033883731"], "bene_count": 4199, '
        '"revenue": 1219170.73}]'
    )

    def test_the_live_payload_becomes_a_table(self):
        card = deterministic_format(self.LIVE)
        assert [s["format"] for s in card["sections"]] == ["table"]

    def test_the_raw_json_never_reaches_the_answer_line(self):
        """It is not prose in any sense, and it now renders in full above."""
        assert deterministic_format(self.LIVE)["direct_answer"] == ""

    def test_one_record_becomes_its_FIELDS_not_a_one_row_table(self):
        """Twelve columns and a single row is unreadable, and the fields ARE
        label/value pairs."""
        data = deterministic_format(self.LIVE)["sections"][0]["data"]
        assert data["headers"] == ["Item", "Detail"]
        assert ["org_name", "DAVID LAWRENCE CENTER"] in data["rows"]

    def test_several_records_become_a_real_table(self):
        draft = (
            '[{"org_name": "DAVID LAWRENCE CENTER", "org_type": "CMHC", "bene_count": 4199},'
            ' {"org_name": "PARK ROYAL", "org_type": "HOSPITAL", "bene_count": 812}]'
        )
        section = deterministic_format(draft)["sections"][0]
        assert section["data"]["headers"] == ["org_name", "org_type", "bene_count"]
        assert len(section["data"]["rows"]) == 2

    def test_column_order_is_the_TOOL_s_order(self):
        """First-seen across records, so the tool's own field order survives
        instead of being alphabetised into something it never chose."""
        draft = '[{"zeta": 1, "alpha": 2}, {"alpha": 3, "zeta": 4}]'
        assert deterministic_format(draft)["sections"][0]["data"]["headers"] == ["zeta", "alpha"]

    def test_nothing_is_dropped(self):
        """Every key reaches the card, ids and hashes included. Deciding which
        fields matter is an editorial judgement about the user's question, and
        this module does not have the question."""
        rows = deterministic_format(self.LIVE)["sections"][0]["data"]["rows"]
        assert ["org_entity_id", "9127414240976b712796f4a912cb40e3"] in rows

    def test_nested_values_are_flattened_readably(self):
        rows = deterministic_format(self.LIVE)["sections"][0]["data"]["rows"]
        assert ["billing_npis", "1033883731"] in rows

    def test_null_reads_as_a_dash_not_as_blank(self):
        """An empty cell and a null are different facts, and only one of them
        should look deliberate.

        Asserted on the VALUES rather than on a table, because two short pairs
        is a stats card — the ladder decides the envelope from the content and
        this is a test about flattening, not about shape."""
        card = deterministic_format('[{"a": null, "b": 1}]')
        # Read the structure, not a JSON dump of it — json.dumps escapes the
        # em-dash to \u2014 and the assertion silently tests the encoder.
        values = [i["value"] for i in card["sections"][0]["data"]["items"]]
        assert "—" in values

    def test_a_list_of_scalars_is_a_list(self):
        card = deterministic_format('["first item", "second item", "third item"]')
        assert card["sections"][0]["format"] == "bullets"

    def test_prose_is_never_mistaken_for_json(self):
        """Narrow by design: must START with a bracket AND parse."""
        for draft in (
            "Sunshine Health requires claims within 180 days of service. " * 5,
            "The answer is [see the table above] for the filing deadlines. " * 4,
            "{not actually json at all, just a brace} " * 6,
        ):
            assert deterministic_format(draft)["sections"] == [] or \
                   deterministic_format(draft)["sections"][0]["format"] != "table"
