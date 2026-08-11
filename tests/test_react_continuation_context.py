"""Task #83 (2026-08-11, Chat Master): ctx.is_continuation -> full-context
path in build_reasoning_context.

A continuation resubmit (think-mode escalation, or the "Continue
gathering" chip, Task #84) needs the model building on what it already
found -- e.g. a table it already assembled -- not the 400-char compact
preview normal follow-ups get, and not a keyword-guessed transform intent
either. Same ~12k-char budget transform_previous_answer already uses for
the same underlying reason (full prior substance, bounded so it doesn't
blow the prompt budget).
"""
from __future__ import annotations

from app.pipeline.context import PipelineContext
from app.pipeline.react.prompts import build_reasoning_context
from app.skills.builtin.transform_previous import _PREVIOUS_ANSWER_CHAR_BUDGET


def _make_ctx(is_continuation: bool = False, message: str = "does it matter") -> PipelineContext:
    ctx = PipelineContext(correlation_id="cont-test", thread_id=None, message=message)
    ctx.effective_message = ctx.message
    ctx.merged_state = {}
    ctx.chat_mode = "agentic"
    ctx.thinking_chunks = []
    ctx.is_continuation = is_continuation
    return ctx


def _prior_turn(assistant_len: int) -> dict:
    return {
        "user_content": "What are the timely filing deadlines?",
        "assistant_content": "X" * assistant_len,
    }


class TestIsContinuationDefaultFalse:
    def test_field_defaults_false(self):
        ctx = PipelineContext(correlation_id="c", thread_id=None, message="hi")
        assert ctx.is_continuation is False

    def test_default_compact_preview_unaffected(self):
        """Regression guard: normal follow-ups (is_continuation not set)
        must keep getting the 400-char compact preview, not the new
        full-context path."""
        long_answer = "X" * 5000
        ctx = _make_ctx(is_continuation=False)
        ctx.last_turns = [_prior_turn(5000)]
        out = build_reasoning_context(ctx, [], 2)
        assert long_answer[:400] in out
        assert long_answer[:1000] not in out


class TestIsContinuationFullContext:
    def test_full_prior_answer_included(self):
        """The core fix: is_continuation=True gets the full ~12k budget,
        not the 400-char compact preview."""
        marker_near_end = "TABLE-DATA-ROW-42"
        long_answer = ("A" * 6000) + marker_near_end + ("B" * 500)
        ctx = _make_ctx(is_continuation=True)
        ctx.last_turns = [_prior_turn(0)]
        ctx.last_turns[0]["assistant_content"] = long_answer
        out = build_reasoning_context(ctx, [], 2)
        assert marker_near_end in out

    def test_budget_matches_transform_previous_answer_skill(self):
        """Same cap transform_previous_answer uses -- single source of
        truth, not a duplicated magic number that can drift."""
        long_answer = "Z" * (_PREVIOUS_ANSWER_CHAR_BUDGET + 500)
        ctx = _make_ctx(is_continuation=True)
        ctx.last_turns = [_prior_turn(0)]
        ctx.last_turns[0]["assistant_content"] = long_answer
        out = build_reasoning_context(ctx, [], 2)
        assert long_answer[:_PREVIOUS_ANSWER_CHAR_BUDGET] in out
        assert long_answer[:_PREVIOUS_ANSWER_CHAR_BUDGET + 100] not in out

    def test_takes_priority_over_transform_keyword_detection(self):
        """is_continuation is an explicit, authoritative signal -- it must
        win over the keyword-guessed transform-intent branch (which uses a
        different, smaller 3000-char budget), not stack with or lose to it."""
        marker = "FULL-CONTEXT-MARKER"
        long_answer = ("A" * 3500) + marker
        ctx = _make_ctx(is_continuation=True, message="please rewrite this shorter")
        ctx.last_turns = [_prior_turn(0)]
        ctx.last_turns[0]["assistant_content"] = long_answer
        out = build_reasoning_context(ctx, [], 2)
        assert marker in out

    def test_continuation_preamble_signals_build_on_prior_work(self):
        ctx = _make_ctx(is_continuation=True)
        ctx.last_turns = [_prior_turn(100)]
        out = build_reasoning_context(ctx, [], 2)
        assert "CONTINUATION" in out
