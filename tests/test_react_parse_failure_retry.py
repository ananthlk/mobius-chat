"""Parse-failure retry + raw-JSON-dump guard (2026-08-07, Chat Master,
live-query finding, Ananth's screenshot, cid 50fda638).

Live incident: round 7 wrote a genuinely good, complete prose answer
that wasn't JSON-wrapped. The prose-trust fallback only fires on
guidance rounds (round 7 of 10 wasn't one yet), so the loop fell through
to "use the last tool output as the answer" -- which picked
appeals_validate_claim's raw {"raw_text": "...", "rules_validated": 4}
JSON blob and shipped it as the user-facing answer (score 0.50,
degraded card).

Two fixes: (1) retry the LLM once with an explicit format-correction
instruction before any fallback logic runs at all; (2) even in the
worst case (retry also fails), never let a tool's raw JSON-shaped
result become the final answer -- a result_summary (human prose) is
still safe to use, but bare JSON never is.
"""
from __future__ import annotations

from unittest.mock import patch

from app.pipeline.context import PipelineContext
from app.pipeline.react_loop import run_react


def _make_ctx(message="I have a COB denial from Sunshine Health"):
    ctx = PipelineContext(correlation_id="parse-retry-test", thread_id=None, message=message)
    ctx.merged_state = {}
    ctx.last_turns = []
    ctx.effective_message = ctx.message
    return ctx


class TestParseFailureRetry:
    def test_retry_fires_on_unparseable_response_and_succeeds(self):
        """Round 1 returns valid JSON (tool call). Round 2 returns
        unparseable prose -- the retry should fire, get a valid JSON
        response, and use IT instead of any fallback."""
        ctx = _make_ctx()
        calls = []

        def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
            calls.append(stage)
            if stage == "react_1":
                return '{"thought": "look it up", "tool": "rag", "inputs": {"query": "COB"}, "is_complete": false}'
            if stage == "react_2":
                return "Hey there! Here's what you need to know about your COB denial..."
            if stage == "react_2_retry":
                return '{"thought": "here it is", "tool": null, "inputs": {}, "is_complete": true, "answer": "CARC 22 applies.", "sources": [], "confidence": "high"}'
            raise AssertionError(f"unexpected stage {stage}")

        with patch("app.pipeline.react.critic.critic_enabled", return_value=False), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm):
            with patch("app.pipeline.react_loop._execute_tool") as mock_execute:
                mock_execute.return_value = {
                    "tool": "rag", "success": True, "result": "[1] Doc A\nCOB info.",
                    "signal": "corpus_only", "sources": [], "usage": None,
                }
                run_react(ctx, emitter=None)

        assert "react_2_retry" in calls
        assert ctx.final_message == "CARC 22 applies."

    def test_retry_also_fails_falls_through_to_prose_or_tool_fallback(self):
        """When the retry ALSO fails to parse, the loop must not crash --
        it falls through to whatever existing fallback applies (prose
        trust on guidance rounds, or the last-tool-output path)."""
        ctx = _make_ctx()

        def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
            if stage == "react_1":
                return '{"thought": "look it up", "tool": "rag", "inputs": {"query": "COB"}, "is_complete": false}'
            # Both the original and the retry return unparseable prose.
            return "Sorry, still thinking about this one, let me check further..."

        with patch("app.pipeline.react.critic.critic_enabled", return_value=False), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm):
            with patch("app.pipeline.react_loop._execute_tool") as mock_execute:
                mock_execute.return_value = {
                    "tool": "rag", "success": True, "result": "[1] Doc A\nCOB info, some real detail here.",
                    "signal": "corpus_only", "sources": [], "usage": None,
                }
                run_react(ctx, emitter=None)

        # Must not crash, and must produce SOME final message (the
        # last-tool-output fallback, since round 2/2 isn't a guidance round
        # for a 3-round copilot turn... actually round 2 of 3 IS guidance --
        # either fallback path is acceptable here, just must not be empty
        # and must not be raw JSON.
        assert ctx.final_message
        assert not ctx.final_message.strip().startswith("{")


class TestRawJsonNeverShipsAsFinalAnswer:
    def test_raw_json_tool_result_skipped_falls_through_to_escalate(self):
        """The exact live regression: the only usable tool result is a
        raw JSON blob (appeals_validate_claim-shaped) with no
        result_summary. Even after both parse attempts fail, this must
        NOT become ctx.final_message verbatim."""
        ctx = _make_ctx()

        def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
            if stage == "react_1":
                return '{"thought": "validate", "tool": "appeals_validate_claim", "inputs": {"carc": 22}, "is_complete": false}'
            # Unparseable on both the original and the retry -- no guidance-round prose to trust.
            return "not valid json and not a real answer either, just noise{"

        with patch("app.pipeline.react.critic.critic_enabled", return_value=False), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm):
            with patch("app.pipeline.react_loop._execute_tool") as mock_execute:
                mock_execute.return_value = {
                    "tool": "appeals_validate_claim", "success": True,
                    "result": '{"raw_text": "COB.R001: Medicaid Payor of Last Resort\\nHave: No direct evidence", "rules_validated": 4}',
                    "signal": None, "sources": [], "usage": None,
                }
                run_react(ctx, emitter=None)

        assert ctx.final_message is not None
        assert not ctx.final_message.strip().startswith("{")
        assert "raw_text" not in ctx.final_message

    def test_result_summary_still_usable_even_when_result_is_json(self):
        """The guard only blocks bare JSON with no human-readable
        summary -- a tool that sets BOTH (JSON result + prose summary)
        must still get to use its summary."""
        ctx = _make_ctx()

        def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
            if stage == "react_1":
                return '{"thought": "look up", "tool": "healthcare_query", "inputs": {}, "is_complete": false}'
            return "still not json, still not json on retry either"

        with patch("app.pipeline.react.critic.critic_enabled", return_value=False), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm):
            with patch("app.pipeline.react_loop._execute_tool") as mock_execute:
                mock_execute.return_value = {
                    "tool": "healthcare_query", "success": True,
                    "result": '{"code": "H0036", "description": "Community psychiatric supportive treatment"}',
                    "result_summary": "H0036: Community psychiatric supportive treatment, prior auth required.",
                    "signal": None, "sources": [], "usage": None,
                }
                run_react(ctx, emitter=None)

        assert ctx.final_message
        assert "prior auth required" in ctx.final_message or "H0036" in ctx.final_message
