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
