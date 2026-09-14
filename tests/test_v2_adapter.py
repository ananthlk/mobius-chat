"""v2 integration output -> formatter inputs (docs/v2-ux-contract.md).

The contract's rules restated as assertions on this side of the seam:
  §4  empty is not absent -- "v2 ran and found nothing" != "v2 did not run"
  §2  unobservable is never a pass
  §2  ran.failed is never "no issues found"
"""
from __future__ import annotations

from app.responder.deterministic_format import deterministic_format
from app.responder.v2_adapter import (
    budget_from_v2,
    coverage_payload,
    facts_payload,
    grounding_from_v2,
)

# Copied from the contract's own example payload (b2fcea4) -- a three-payer
# question where one payer is grounded and one is unobservable.
CONTRACT_SAMPLE = {
    "coverage": [
        {"part": "Molina care management philosophy", "status": "supported",
         "why": "1 grounded fact(s)",
         "evidence": ["molina_fl_provider_manual_2026.pdf p111"]},
        {"part": "Sunshine care management philosophy", "status": "unobservable",
         "why": "no grounded fact mentions this part", "evidence": []},
    ],
    "citations": ["molina_fl_provider_manual_2026.pdf p111"],
    "unsupported_claims": 1,
    "open_gaps": [],
    "critique": [], "critique_summary": "",
    "next_steps": [],
    "ran": {"assemble": "ok", "critique": "ok", "next_steps": "ok"},
    "prompt_sources": {"v2.integrator.critic": "fallback (missing)"},
    "problems": ["critique was not JSON"],
}


class TestEmptyIsNotAbsent:
    """§4 — the rule under all of it."""

    def test_v2_absent_returns_none(self):
        assert grounding_from_v2(None) is None
        assert grounding_from_v2({}) is None

    def test_v2_ran_and_found_nothing_is_a_finding(self):
        """A DECISIVE verdict of ungrounded. An all-unobservable turn is a
        different thing entirely -- see TestCouldNotCheckIsNotCheckedFalse."""
        grounding = grounding_from_v2({
            "coverage": [{"part": "X", "status": "unsupported", "evidence": []}],
            "citations": [], "ran": {"assemble": "ok"},
        })
        assert grounding is not None
        assert grounding.checked is True
        assert grounding.grounded_parts == 0
        assert grounding.conclusive is True
        assert grounding.thin is True

    def test_the_two_are_distinguishable_by_the_caller(self):
        absent = grounding_from_v2(None)
        empty = grounding_from_v2({"coverage": [], "citations": [], "ran": {"assemble": "ok"}})
        assert absent is None
        assert empty is not None and empty.grounded_parts == 0

    def test_a_turn_v2_never_touched_keeps_the_local_fallback(self):
        assert budget_from_v2(None, fallback_thin=True).is_thin_evidence is True
        assert budget_from_v2(None, fallback_thin=False).is_thin_evidence is False

    def test_v2s_verdict_overrides_the_local_fallback(self):
        """The whole point: one author. A caller that guessed 'thin' does not
        get to override the integrator that actually checked."""
        budget = budget_from_v2(CONTRACT_SAMPLE, fallback_thin=True)
        assert budget.is_thin_evidence is False


class TestUnobservableIsNotAPass:
    """§2 — the status the contract exists to protect."""

    def test_unobservable_never_counts_as_grounded(self):
        grounding = grounding_from_v2(CONTRACT_SAMPLE)
        assert grounding.grounded_parts == 1
        assert "Sunshine care management philosophy" in grounding.unverified_parts

    def test_partial_is_not_grounded_either(self):
        """partial is the critic's judgement, not provenance."""
        grounding = grounding_from_v2({
            "coverage": [{"part": "X", "status": "partial", "evidence": []}],
            "citations": [], "ran": {"assemble": "ok"},
        })
        assert grounding.grounded_parts == 0

    def test_not_attempted_is_reported_as_unverified(self):
        grounding = grounding_from_v2({
            "coverage": [{"part": "Y", "status": "not_attempted", "evidence": []}],
            "citations": [], "ran": {"assemble": "ok"},
        })
        assert grounding.unverified_parts == ("Y",)

    def test_a_partly_grounded_answer_is_not_thin(self):
        """Two payers grounded and one unobservable must not suppress the
        card -- that punishes the answer for the part it got right."""
        grounding = grounding_from_v2(CONTRACT_SAMPLE)
        assert grounding.thin is False
        assert grounding.partially_unverified is True


class TestRanFailedIsNotClean:
    """§2 — an absent check is not a passed one."""

    def test_failed_assemble_yields_no_verdict(self):
        assert grounding_from_v2({**CONTRACT_SAMPLE, "ran": {"assemble": "failed"}}) is None

    def test_skipped_assemble_yields_no_verdict(self):
        assert grounding_from_v2({**CONTRACT_SAMPLE, "ran": {"assemble": "skipped"}}) is None

    def test_a_failed_check_does_not_read_as_grounded(self):
        budget = budget_from_v2({**CONTRACT_SAMPLE, "ran": {"assemble": "failed"}},
                                fallback_thin=True)
        assert budget.is_thin_evidence is True


class TestPayloadViews:
    def test_coverage_becomes_a_table_keeping_every_part(self):
        payload = coverage_payload(CONTRACT_SAMPLE)
        assert payload.table.headers == ("Part", "Status", "Evidence")
        assert len(payload.table.rows) == 2
        assert payload.table.rows[1][1] == "unobservable"

    def test_a_part_with_no_evidence_says_so_rather_than_being_blank(self):
        payload = coverage_payload(CONTRACT_SAMPLE)
        assert payload.table.rows[1][2] == "—"

    def test_coverage_payload_absent_when_v2_absent(self):
        assert coverage_payload(None) is None

    def test_facts_carry_their_provenance(self):
        payload = facts_payload([
            {"fact": "Molina runs an ICM program", "document": "molina.pdf", "page": 111},
            {"fact": "UHC has a Care Model program", "document": "", "page": None},
        ])
        assert payload.items[0].note == "molina.pdf p111"
        assert payload.items[1].note == ""

    def test_a_bare_string_fact_is_kept(self):
        """The contract keeps ungrounded facts rather than dropping them --
        dropping would hide that react answered ungrounded."""
        payload = facts_payload(["something with no provenance"])
        assert payload.items[0].label == "something with no provenance"


class TestEndToEnd:
    DRAFT = "Initial filing: 180 days\nResubmission: 90 days\nCopay: $25"

    def test_a_grounded_turn_formats_normally(self):
        card = deterministic_format(self.DRAFT, budget=budget_from_v2(CONTRACT_SAMPLE))
        assert [s["format"] for s in card["sections"]] == ["stats"]
        assert "presentation" not in card

    def test_an_ungrounded_turn_abstains_and_says_why(self):
        """Decisively ungrounded: the critic looked and said the facts do not
        support it. That is what earns a suppressed card."""
        ungrounded = {**CONTRACT_SAMPLE, "coverage": [
            {"part": "Sunshine", "status": "unsupported", "evidence": []}],
            "citations": []}
        card = deterministic_format(self.DRAFT, budget=budget_from_v2(ungrounded))
        assert card["sections"] == []
        assert card["presentation"]["rule_id"] == "abstain.thin_evidence"
        assert "nothing in the sources grounds" in card["presentation"]["note"]

    def test_the_three_abstains_are_distinguishable(self):
        """The defect this closes: all three previously rendered as silence."""
        thin = deterministic_format(
            self.DRAFT,
            budget=budget_from_v2({
                "coverage": [{"part": "X", "status": "unsupported", "evidence": []}],
                "citations": [], "ran": {"assemble": "ok"},
            }),
        )
        excerpt = deterministic_format("[1] Provider Manual\nCopay: $25\nFiling: 180 days")
        prose = deterministic_format("Claims must be filed within 180 days.")

        assert thin["presentation"]["rule_id"] == "abstain.thin_evidence"
        assert excerpt["presentation"]["rule_id"] == "abstain.raw_excerpt"
        assert prose["presentation"]["rule_id"] == "abstain.no_match"
        assert len({thin["presentation"]["rule_id"],
                    excerpt["presentation"]["rule_id"],
                    prose["presentation"]["rule_id"]}) == 3

    def test_structureless_prose_gets_no_user_facing_note(self):
        """'There was nothing to format' is not worth telling anyone."""
        card = deterministic_format("Claims must be filed within 180 days.")
        assert "note" not in card["presentation"]


class TestCouldNotCheckIsNotCheckedFalse:
    """The live-traffic regression this class exists to prevent.

    Governor, 2026-09-12: react still returns v1-shaped responses on most
    turns, so `facts` is empty, so the integrator has nothing to check
    against and every part comes back `unobservable`. Reading that as "not
    grounded" suppressed formatting on essentially ALL live traffic -- and it
    is the same defect Governor is fixing on their own side, where a critic
    given no facts was marking grounded claims "unsupported".
    """

    V1_SHAPE_TURN = {
        "coverage": [
            {"part": "Molina care management", "status": "unobservable", "evidence": []},
            {"part": "Sunshine care management", "status": "unobservable", "evidence": []},
            {"part": "UHC care management", "status": "unobservable", "evidence": []},
        ],
        "citations": [], "unsupported_claims": 0, "open_gaps": [],
        "critique": [], "critique_summary": "", "next_steps": [],
        "ran": {"assemble": "ok", "critique": "ok", "next_steps": "ok"},
        "prompt_sources": {"v2.integrator.critic": "fallback (missing)"},
        "problems": ["critique was not JSON"],
    }
    DRAFT = "Initial filing: 180 days\nResubmission: 90 days\nCopay: $25"

    def test_all_unobservable_is_inconclusive_not_thin(self):
        grounding = grounding_from_v2(self.V1_SHAPE_TURN)
        assert grounding.grounded_parts == 0
        assert grounding.inconclusive is True
        assert grounding.thin is False

    def test_todays_traffic_still_formats(self):
        card = deterministic_format(self.DRAFT, budget=budget_from_v2(self.V1_SHAPE_TURN))
        assert [s["format"] for s in card["sections"]] == ["stats"]

    def test_ran_ok_with_an_empty_critique_does_not_count_as_a_check(self):
        """§2: an empty critique after ran=='ok' is an ABSENT check."""
        assert grounding_from_v2(self.V1_SHAPE_TURN).critic_ran is False

    def test_a_real_critic_verdict_makes_the_turn_conclusive(self):
        turn = {**self.V1_SHAPE_TURN,
                "critique": [{"claim": "x", "verdict": "unsupported"}]}
        grounding = grounding_from_v2(turn)
        assert grounding.critic_ran is True
        assert grounding.conclusive is True
        assert grounding.thin is True

    def test_a_decisive_status_makes_the_turn_conclusive(self):
        turn = {**self.V1_SHAPE_TURN, "coverage": [
            {"part": "A", "status": "unsupported", "evidence": []},
            {"part": "B", "status": "unobservable", "evidence": []},
        ]}
        grounding = grounding_from_v2(turn)
        assert grounding.conclusive is True
        assert grounding.thin is True

    def test_a_citation_alone_makes_the_turn_conclusive(self):
        turn = {**self.V1_SHAPE_TURN, "citations": ["molina.pdf p111"]}
        grounding = grounding_from_v2(turn)
        assert grounding.conclusive is True
        assert grounding.thin is False

    def test_unobservable_still_never_reads_as_grounded(self):
        """Fixing the suppression must not swing into calling it a pass."""
        grounding = grounding_from_v2(self.V1_SHAPE_TURN)
        assert grounding.grounded_parts == 0
        assert len(grounding.unverified_parts) == 3

    def test_the_failure_direction_is_deliberate(self):
        """Until the critic is reliable, thin rarely fires. Formatting an
        ungrounded answer is a smaller harm than silently stripping structure
        from a grounded one."""
        card = deterministic_format(self.DRAFT, budget=budget_from_v2(self.V1_SHAPE_TURN))
        assert card["sections"] != []
        assert "presentation" not in card


class TestRanValuesAreMatchedByMeaning:
    """The producer changed how it words "ok" and the gate died silently.

    `ran["critique"] == "ok"` became `"deterministic (verify_claims)"` when
    Governor replaced the LLM critic. The equality test then returned False on
    every turn, forever — and it failed in the SAFE direction (`thin` simply
    never fires), which is worse than it sounds: a gate that can never fire is
    dead code that still looks live, and nothing asserts on a negative.
    """

    BASE = {"coverage": [{"part": "X", "status": "unobservable", "evidence": []}],
            "citations": []}

    def _g(self, critique_ran, critique=()):
        return grounding_from_v2({**self.BASE,
                                  "ran": {"assemble": "ok", "critique": critique_ran},
                                  "critique": list(critique)})

    def test_the_new_deterministic_wording_counts_as_run(self):
        assert self._g("deterministic (verify_claims)", [{"c": 1}]).critic_ran is True

    def test_nothing_flagged_is_a_verdict_not_an_absence(self):
        """The deterministic critic compared every claim against its cited
        page and flagged none. Empty means clean here, unlike the LLM era."""
        g = self._g("deterministic (verify_claims) — nothing flagged")
        assert g.critic_ran is True
        assert g.conclusive is True

    def test_the_legacy_ok_still_needs_a_non_empty_critique(self):
        """Under the LLM critic, empty after "ok" meant the prompt blocks were
        missing and it fell back — an absent check, not a clean one."""
        assert self._g("ok", []).critic_ran is False
        assert self._g("ok", [{"c": 1}]).critic_ran is True

    def test_a_wording_nobody_predicted_defaults_to_RAN(self):
        """The closed set is on the FAILURE side. A producer is free to
        reword "ok"; it is not free to invent a new way of saying "did not
        run", so an unknown value means it ran rather than silence."""
        assert self._g("verify_claims v3 ran clean").critic_ran is True

    def test_skipped_and_failed_still_mean_not_run(self):
        for value in ("skipped", "failed", "", None):
            assert self._g(value).critic_ran is False, value


class TestPagesPerPart:
    """The field the answer-shape block wanted was already in the contract.
    coverage[] carries per-part evidence locators, so counting them needs
    nothing new from the producer."""

    COV = {"ran": {"assemble": "ok"}, "coverage": [
        {"part": "Molina", "evidence": ["molina_fl.pdf p111", "molina_fl.pdf p103"]},
        {"part": "UnitedHealthcare", "evidence": ["FL-Care.pdf p5", "FL-Care.pdf p5"]},
        {"part": "Sunshine", "evidence": []},
    ]}

    def test_distinct_pages_are_counted(self):
        from app.responder.v2_adapter import pages_per_part
        assert pages_per_part(self.COV) == {
            "Molina": 2, "UnitedHealthcare": 1, "Sunshine": 0}

    def test_the_same_page_cited_twice_is_one_page(self):
        """A live answer cited one payer twice and both were p5, which reads
        as two sources until the numbers are compared."""
        from app.responder.v2_adapter import pages_per_part
        assert pages_per_part(self.COV)["UnitedHealthcare"] == 1

    def test_a_locator_with_no_page_still_counts_as_one_source(self):
        from app.responder.v2_adapter import pages_per_part
        assert pages_per_part({"ran": {"assemble": "ok"}, "coverage": [
            {"part": "A", "evidence": ["some_manual.pdf"]}]}) == {"A": 1}

    def test_absent_v2_yields_nothing(self):
        from app.responder.v2_adapter import pages_per_part
        assert pages_per_part(None) == {}


class TestFactsMayFillAnEmptyCard:
    """Governor's fallback, moved into the formatter's lane and gated by
    construction rather than by the caller remembering.

    Their reasoning was right and is why this exists at all: classify_envelope
    refuses to infer structure out of PROSE, because guessing at structure is
    how a renderer invents emphasis the author never meant. A fact is not
    prose — it arrived typed, carrying its own document and page. Rendering it
    presents structure we were given.

    What their version could not express is WHICH empty card. "Zero sections"
    is four states, and two of them are deliberate refusals.
    """

    class _Fact:
        def __init__(self, fact, document="", page=None, grounded=True):
            self.fact, self.document, self.page, self.grounded = fact, document, page, grounded

    FACTS = [
        _Fact("Molina runs a comprehensive ICM program", "molina_fl.pdf", 111),
        _Fact("Sunshine uses an interdisciplinary approach", "sunshine.pdf", 52),
        _Fact("Molina runs a comprehensive ICM program", "molina_fl.pdf", 111),  # dup
        _Fact("UnitedHealthcare has a Care Model", grounded=False),              # ungrounded
    ]

    def _card(self, rule_id=None):
        card = {"direct_answer": "Some prose.", "sections": []}
        if rule_id:
            card["presentation"] = {"rule_id": rule_id, "why": "x"}
        return card

    def test_no_structure_in_the_draft_is_filled(self):
        from app.responder.v2_adapter import add_fact_sections
        out = add_fact_sections(self._card("abstain.no_match"), self.FACTS)
        assert out["sections"][0]["label"] == "What the documents say"

    def test_a_paragraph_list_is_filled(self):
        from app.responder.v2_adapter import add_fact_sections
        assert add_fact_sections(self._card("abstain.prose_list"), self.FACTS)["sections"]

    def test_a_RAW_EXCERPT_refusal_is_honoured(self):
        """The hedge exists to stop an unvetted excerpt looking like something
        we checked. A cited bullet list is exactly that."""
        from app.responder.v2_adapter import add_fact_sections
        assert add_fact_sections(self._card("abstain.raw_excerpt"), self.FACTS)["sections"] == []

    def test_a_THIN_EVIDENCE_refusal_is_honoured(self):
        """The worst direction: a bullet list carrying document and page, next
        to an answer nothing grounds, is the most confident-looking thing on
        the screen."""
        from app.responder.v2_adapter import add_fact_sections
        assert add_fact_sections(self._card("abstain.thin_evidence"), self.FACTS)["sections"] == []

    def test_a_card_that_already_has_sections_is_untouched(self):
        from app.responder.v2_adapter import add_fact_sections
        card = {"sections": [{"format": "table", "label": "Details"}]}
        assert add_fact_sections(card, self.FACTS)["sections"] == card["sections"]

    def test_only_grounded_facts_are_rendered(self):
        """A fact with no document is the ungrounded claim the contract exists
        to keep out. Promoting it would give it MORE prominence than the prose
        did."""
        from app.responder.v2_adapter import add_fact_sections
        bullets = add_fact_sections(self._card(), self.FACTS)["sections"][0]["bullets"]
        assert not any("Care Model" in b for b in bullets)

    def test_facts_are_deduplicated_and_cited(self):
        from app.responder.v2_adapter import add_fact_sections
        bullets = add_fact_sections(self._card(), self.FACTS)["sections"][0]["bullets"]
        assert len(bullets) == 2
        assert "[molina_fl.pdf p111]" in bullets[0]

    def test_one_fact_is_not_a_list(self):
        from app.responder.v2_adapter import add_fact_sections
        one = [self._Fact("Only this", "d.pdf", 1)]
        assert add_fact_sections(self._card(), one)["sections"] == []

    def test_the_abstain_note_is_removed_once_the_card_is_filled(self):
        """A note explaining why the card has no sections contradicts a card
        that now has one."""
        from app.responder.v2_adapter import add_fact_sections
        out = add_fact_sections(self._card("abstain.no_match"), self.FACTS)
        assert "presentation" not in out
