"""Tests for the Task #76 dynamic-enrichment sufficiency signal.

Chat Master's approved formula (do not loosen without a new ruling):
sufficient = (chat_mode=="quick" AND rounds_used==1)
             OR (rounds_used<=3 AND gaps_open empty AND react_unfinished_reason
                 is None AND len(react_draft.strip())>=200)
Built entirely from ctx fields ReAct already sets -- no new ReAct changes.
"""
from __future__ import annotations

from app.pipeline.context import PipelineContext
from app.pipeline.react_loop import _is_sufficient_for_deterministic_pass

_LONG_DRAFT = "x" * 200
_SHORT_DRAFT = "too short"


def _make_ctx(**extra) -> PipelineContext:
    ctx = PipelineContext(correlation_id="suff-test", thread_id=None, message="What is X?")
    for k, v in extra.items():
        setattr(ctx, k, v)
    return ctx


class TestQuickModeEarlyExit:
    def test_quick_mode_round_1_is_sufficient_regardless_of_draft_length(self):
        ctx = _make_ctx(chat_mode="quick", react_rounds_used=1, react_draft=_SHORT_DRAFT)
        assert _is_sufficient_for_deterministic_pass(ctx) is True

    def test_quick_mode_round_2_not_covered_by_this_branch(self):
        """Round 2 in quick mode isn't the early-exit case -- falls through
        to the general rule, which still needs a substantive draft."""
        ctx = _make_ctx(chat_mode="quick", react_rounds_used=2, react_draft=_SHORT_DRAFT)
        assert _is_sufficient_for_deterministic_pass(ctx) is False

    def test_non_quick_mode_round_1_not_covered_by_this_branch(self):
        ctx = _make_ctx(chat_mode="copilot", react_rounds_used=1, react_draft=_SHORT_DRAFT)
        assert _is_sufficient_for_deterministic_pass(ctx) is False


class TestGeneralRule:
    def test_short_clean_run_is_sufficient(self):
        ctx = _make_ctx(
            chat_mode="copilot", react_rounds_used=2, react_draft=_LONG_DRAFT,
            react_unfinished_reason=None, react_trace_rounds=[],
        )
        assert _is_sufficient_for_deterministic_pass(ctx) is True

    def test_unfinished_reason_set_is_not_sufficient(self):
        ctx = _make_ctx(
            chat_mode="copilot", react_rounds_used=2, react_draft=_LONG_DRAFT,
            react_unfinished_reason="no_path_forward",
        )
        assert _is_sufficient_for_deterministic_pass(ctx) is False

    def test_more_than_3_rounds_is_not_sufficient(self):
        ctx = _make_ctx(
            chat_mode="copilot", react_rounds_used=4, react_draft=_LONG_DRAFT,
            react_unfinished_reason=None,
        )
        assert _is_sufficient_for_deterministic_pass(ctx) is False

    def test_exactly_3_rounds_is_sufficient(self):
        ctx = _make_ctx(
            chat_mode="copilot", react_rounds_used=3, react_draft=_LONG_DRAFT,
            react_unfinished_reason=None,
        )
        assert _is_sufficient_for_deterministic_pass(ctx) is True

    def test_open_gaps_on_last_round_is_not_sufficient(self):
        ctx = _make_ctx(
            chat_mode="copilot", react_rounds_used=2, react_draft=_LONG_DRAFT,
            react_unfinished_reason=None,
            react_trace_rounds=[{"round": 2, "enrichment": {"gaps_open": ["resubmission window"]}}],
        )
        assert _is_sufficient_for_deterministic_pass(ctx) is False

    def test_empty_gaps_open_on_last_round_is_sufficient(self):
        ctx = _make_ctx(
            chat_mode="copilot", react_rounds_used=2, react_draft=_LONG_DRAFT,
            react_unfinished_reason=None,
            react_trace_rounds=[{"round": 2, "enrichment": {"gaps_open": []}}],
        )
        assert _is_sufficient_for_deterministic_pass(ctx) is True

    def test_short_draft_is_not_sufficient(self):
        ctx = _make_ctx(
            chat_mode="copilot", react_rounds_used=2, react_draft=_SHORT_DRAFT,
            react_unfinished_reason=None,
        )
        assert _is_sufficient_for_deterministic_pass(ctx) is False

    def test_missing_react_draft_is_not_sufficient(self):
        ctx = _make_ctx(chat_mode="copilot", react_rounds_used=2, react_unfinished_reason=None)
        assert _is_sufficient_for_deterministic_pass(ctx) is False

    def test_round_without_enrichment_key_does_not_block(self):
        """A round with no enrichment content at all (e.g. is_complete on
        round 1) shouldn't be treated as having open gaps."""
        ctx = _make_ctx(
            chat_mode="copilot", react_rounds_used=1, react_draft=_LONG_DRAFT,
            react_unfinished_reason=None,
            react_trace_rounds=[{"round": 1, "tool": "search_corpus"}],
        )
        assert _is_sufficient_for_deterministic_pass(ctx) is True
