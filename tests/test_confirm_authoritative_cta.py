""""Confirm from authoritative sources" CTA (2026-08-07, Task #41(a)
follow-up, Chat Master directive).

Mirrors the Think Mode / suggest_escalate pattern exactly: a one-click
button that re-submits the same query with force_citable_required=True,
so react is guaranteed to use only citable (authoritative) sources
regardless of what the keyword heuristic would have picked. Suggested
whenever a turn's rag answer came from the broad "any" net
(citable_required=False) rather than citable-only sources.

Three layers tested: (1) the trigger condition itself
(_should_suggest_confirm_authoritative, orchestrator.py), (2) the
force_citable_required override in react_loop.py's citable_required
decision (including that it disables relax-eligibility -- silently
relaxing away from a forced authoritative-only guarantee would defeat
the button's whole point), (3) append_draft_answer's new param.
"""
from __future__ import annotations

from unittest.mock import patch

from app.pipeline.orchestrator import _should_suggest_confirm_authoritative
from app.storage import progress


class TestShouldSuggestConfirmAuthoritative:
    def test_suggests_when_last_rag_call_was_not_citable(self):
        history = [{"citable_required": False, "n_chunks": 5}]
        assert _should_suggest_confirm_authoritative(history, None, "a real answer with enough length to pass the floor") is True

    def test_does_not_suggest_when_already_citable(self):
        history = [{"citable_required": True, "n_chunks": 5}]
        assert _should_suggest_confirm_authoritative(history, None, "a real answer with enough length to pass the floor") is False

    def test_does_not_suggest_when_this_turn_was_already_a_forced_reconfirm(self):
        """No point suggesting the button that got us here."""
        history = [{"citable_required": True, "n_chunks": 5}]
        assert _should_suggest_confirm_authoritative(history, True, "a real answer with enough length to pass the floor") is False

    def test_does_not_suggest_when_no_rag_call_happened(self):
        assert _should_suggest_confirm_authoritative([], None, "a real answer with enough length to pass the floor") is False

    def test_does_not_suggest_on_the_all_rounds_failed_error_path(self):
        """Live false positive, caught on the first deployed test:
        react_loop.py's all-rounds-failed error refusal ("Every attempt
        to answer this hit an error...") is 71 chars (passes the length
        floor) and can have a citable_required=False entry from an
        earlier failed attempt -- but n_chunks=0, nothing was actually
        retrieved to confirm. Must not suggest the CTA on this path."""
        history = [{"citable_required": False, "n_chunks": 0}]
        error_text = "Every attempt to answer this hit an error. Please try again or rephrase."
        assert _should_suggest_confirm_authoritative(history, None, error_text) is False

    def test_does_not_suggest_below_the_length_floor(self):
        """Matches integrate.py's evidence_empty 50-char floor."""
        history = [{"citable_required": False, "n_chunks": 5}]
        assert _should_suggest_confirm_authoritative(history, None, "too short") is False

    def test_does_not_suggest_when_final_message_is_none(self):
        history = [{"citable_required": False, "n_chunks": 5}]
        assert _should_suggest_confirm_authoritative(history, None, None) is False

    def test_uses_the_last_call_not_an_earlier_one(self):
        history = [
            {"citable_required": True, "n_chunks": 0},
            {"citable_required": False, "n_chunks": 5},
        ]
        assert _should_suggest_confirm_authoritative(history, None, "a real answer with enough length to pass the floor") is True


class TestForceCitableRequiredOverride:
    def test_force_true_overrides_keyword_heuristic_for_non_matching_query(self):
        from app.pipeline.react_loop import _compute_baseline_citable_and_relax_eligible

        # "tell me about care programs" hits none of _CITABLE_TERMS --
        # the unforced heuristic would return False here.
        baseline, relax = _compute_baseline_citable_and_relax_eligible(
            rag_history=[], rag_call_number=1, force_citable_required=True,
            query="tell me about care programs",
        )
        assert baseline is True

    def test_unforced_query_not_matching_keywords_stays_false(self):
        from app.pipeline.react_loop import _compute_baseline_citable_and_relax_eligible

        baseline, relax = _compute_baseline_citable_and_relax_eligible(
            rag_history=[], rag_call_number=1, force_citable_required=None,
            query="tell me about care programs",
        )
        assert baseline is False

    def test_unforced_query_matching_keywords_stays_true(self):
        from app.pipeline.react_loop import _compute_baseline_citable_and_relax_eligible

        baseline, relax = _compute_baseline_citable_and_relax_eligible(
            rag_history=[], rag_call_number=1, force_citable_required=None,
            query="what is the prior authorization process",
        )
        assert baseline is True

    def test_force_true_disables_relax_eligibility(self):
        """A forced-authoritative turn must not silently relax away from
        citable_required on an empty result -- that would defeat the
        button's guarantee. Without the force, this exact history would
        make relax_eligible True (round 2, baseline citable, prior call
        was citable with 0 chunks) -- the force must override that."""
        from app.pipeline.react_loop import _compute_baseline_citable_and_relax_eligible

        history = [{"citable_required": True, "n_chunks": 0}]
        # Matches _CITABLE_TERMS ("coverage", "policy") so the unforced
        # baseline is True -- needed for relax_eligible to have a chance
        # to be True at all, so the comparison below is meaningful.
        query = "what does the coverage policy say"

        _, relax_forced = _compute_baseline_citable_and_relax_eligible(
            rag_history=history, rag_call_number=2, force_citable_required=True,
            query=query,
        )
        assert relax_forced is False

        _, relax_unforced = _compute_baseline_citable_and_relax_eligible(
            rag_history=history, rag_call_number=2, force_citable_required=None,
            query=query,
        )
        assert relax_unforced is True


class TestAppendDraftAnswerCtaParam:
    def test_cta_confirm_authoritative_included_when_true(self):
        progress.start_progress("cid-cta")
        try:
            with patch("app.storage.progress._publish_progress_event") as mock_publish:
                progress.append_draft_answer("cid-cta", "text", cta_confirm_authoritative=True)
            _, ev = mock_publish.call_args[0]
            assert ev["data"]["cta_confirm_authoritative"] is True
        finally:
            progress._progress.pop("cid-cta", None)

    def test_omitted_when_false(self):
        progress.start_progress("cid-cta-off")
        try:
            with patch("app.storage.progress._publish_progress_event") as mock_publish:
                progress.append_draft_answer("cid-cta-off", "text")
            _, ev = mock_publish.call_args[0]
            assert "cta_confirm_authoritative" not in ev["data"]
        finally:
            progress._progress.pop("cid-cta-off", None)

    def test_coexists_with_suggest_escalate(self):
        """The two CTAs are independent signals -- both can theoretically
        be present (though in practice suggest_escalate implies a
        stalled turn, unlikely to also trigger the authoritative CTA)."""
        progress.start_progress("cid-both-ctas")
        try:
            with patch("app.storage.progress._publish_progress_event") as mock_publish:
                progress.append_draft_answer(
                    "cid-both-ctas", "text",
                    suggest_escalate=True, cta_confirm_authoritative=True,
                )
            _, ev = mock_publish.call_args[0]
            assert ev["data"]["suggest_escalate"] is True
            assert ev["data"]["cta_confirm_authoritative"] is True
        finally:
            progress._progress.pop("cid-both-ctas", None)
