"""Tests for the Task #76 dynamic-enrichment sufficiency signal.

Chat Master's approved formula (do not loosen without a new ruling):
sufficient = (chat_mode=="quick" AND rounds_used==1)
             OR (rounds_used<=3 AND gaps_open empty AND react_unfinished_reason
                 is None AND len(react_draft.strip())>=200)
Built entirely from ctx fields ReAct already sets -- no new ReAct changes.
"""
from __future__ import annotations

import json

from app.pipeline.context import PipelineContext
from app.pipeline.react_loop import (
    _is_sufficient_for_deterministic_pass,
    _looks_like_raw_structured_blob,
    _requests_explicit_presentation_format,
)

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


# ── Raw structured-data guard (2026-08-08, Chat Master live incident) ──────
# "tell me more about how best to appeal for COB denials?" -- quick mode,
# round 1 exit, react_draft was the appeals_find_carc tool's raw JSON
# (chunk_count==0 branch ships non-rag tool output unsynthesized). The
# deterministic pass has no way to reformat JSON into prose; it sailed
# through, the existing bleed-detector caught it downstream, and replaced
# the whole card with the generic "I had trouble formatting the answer"
# fallback -- losing the real answer entirely.

_RAW_JSON_DRAFT = json.dumps({
    "matches": [{"carc": 22, "title": "Coordination of Benefits", "rules": [
        {"rule_id": "COB.R001", "rule_statement": "Medicaid is always the payor of last resort."},
    ]}],
})


class TestRawStructuredBlobGuard:
    def test_raw_json_object_detected(self):
        assert _looks_like_raw_structured_blob(_RAW_JSON_DRAFT) is True

    def test_raw_json_array_detected(self):
        assert _looks_like_raw_structured_blob(json.dumps([{"a": 1}, {"b": 2}])) is True

    def test_prose_not_detected(self):
        assert _looks_like_raw_structured_blob(
            "Sunshine Health requires initial claims within 180 days of service."
        ) is False

    def test_prose_starting_with_brace_but_not_valid_json_not_detected(self):
        """A stray leading brace in real prose (rare, but not impossible)
        must not false-positive -- only text that actually PARSES as JSON
        counts."""
        assert _looks_like_raw_structured_blob("{this is not json, just a sentence}") is False

    def test_empty_and_none_not_detected(self):
        assert _looks_like_raw_structured_blob("") is False
        assert _looks_like_raw_structured_blob(None) is False

    def test_quick_mode_round_1_with_raw_json_draft_is_not_sufficient(self):
        """The exact failure mode: quick-mode round-1 exit (normally an
        unconditional True) must NOT be sufficient when react_draft is raw
        JSON -- needs full Call A to actually synthesize an answer."""
        ctx = _make_ctx(chat_mode="quick", react_rounds_used=1, react_draft=_RAW_JSON_DRAFT)
        assert _is_sufficient_for_deterministic_pass(ctx) is False

    def test_general_rule_with_raw_json_draft_is_not_sufficient(self):
        long_json_draft = json.dumps({"data": "x" * 250})
        ctx = _make_ctx(
            chat_mode="copilot", react_rounds_used=2, react_draft=long_json_draft,
            react_unfinished_reason=None,
        )
        assert _is_sufficient_for_deterministic_pass(ctx) is False

    def test_quick_mode_round_1_with_real_prose_still_sufficient(self):
        """Regression guard: the fix must not break the common, correct
        case -- quick-mode round-1 with genuine synthesized prose stays
        sufficient."""
        ctx = _make_ctx(chat_mode="quick", react_rounds_used=1, react_draft="A real synthesized answer.")
        assert _is_sufficient_for_deterministic_pass(ctx) is True


# ── Explicit user format request guard (2026-08-11, Chat FE QC audit) ──────
# "provided the correct data but failed to follow the user's explicit
# instruction to format it as a table" -- deterministic_format only regex-
# matches react_draft's already-written structure, it has zero visibility
# into what the user actually asked for, so it can never honor an explicit
# format request the draft's prose doesn't already happen to satisfy.
# Routes these turns to full Call A instead, where the integrator prompt is
# now explicitly instructed to honor the request.

class TestExplicitFormatRequestDetection:
    def test_as_a_table_detected(self):
        assert _requests_explicit_presentation_format("Can you show that as a table?") is True

    def test_in_table_format_detected(self):
        assert _requests_explicit_presentation_format("Give me the rates in table format.") is True

    def test_bullet_points_detected(self):
        assert _requests_explicit_presentation_format("List the requirements in bullet points.") is True

    def test_as_steps_detected(self):
        assert _requests_explicit_presentation_format("Walk me through this as steps.") is True

    def test_plain_question_not_detected(self):
        assert _requests_explicit_presentation_format("What is the timely filing deadline?") is False

    def test_empty_and_none_not_detected(self):
        assert _requests_explicit_presentation_format("") is False
        assert _requests_explicit_presentation_format(None) is False


class TestExplicitFormatRequestOverridesSufficiency:
    def test_quick_mode_round_1_with_table_request_is_not_sufficient(self):
        """The exact reported failure mode: quick-mode round-1 (normally
        an unconditional True) must NOT be sufficient when the user
        explicitly asked for a table -- needs full Call A to actually
        honor the format request."""
        ctx = _make_ctx(
            message="Show the appeal levels as a table.",
            chat_mode="quick", react_rounds_used=1, react_draft="A real synthesized answer.",
        )
        assert _is_sufficient_for_deterministic_pass(ctx) is False

    def test_general_rule_with_table_request_is_not_sufficient(self):
        ctx = _make_ctx(
            message="Can you put this in a table?",
            chat_mode="copilot", react_rounds_used=2, react_draft=_LONG_DRAFT,
            react_unfinished_reason=None,
        )
        assert _is_sufficient_for_deterministic_pass(ctx) is False

    def test_plain_question_still_sufficient(self):
        """Regression guard: the fix must not affect turns with no
        explicit format request."""
        ctx = _make_ctx(
            message="What is the timely filing deadline?",
            chat_mode="quick", react_rounds_used=1, react_draft="A real synthesized answer.",
        )
        assert _is_sufficient_for_deterministic_pass(ctx) is True
