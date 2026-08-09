"""Critic/groundedness-floor call resilience (2026-08-08, Chat Master
ruling, Chat FE-reported P0).

Incident: a provider-side latency spike on the "critique" LLM call hit
the global LLM_TIMEOUT_SECONDS cap (60s) and raised an uncaught exception
mid-round in react_loop.py -- no graceful path to a terminal event, the
turn just died silently until the frontend's own 90s "no progress"
watchdog fired. Traced to two call sites with zero exception handling
around ``_call_llm_json(..., stage="critique", ...)``:

  1. The Product Promise governor's MANDATORY groundedness floor
     (confidence_bar in medium/high) -- live whenever
     MOBIUS_PRODUCT_PROMISE_ENABLED is on.
  2. The classic, optional critic_enabled()-gated path -- currently
     dormant (MOBIUS_REACT_CRITIC default off) but gets the same fix.

Ruling: the mandatory floor stays mandatory for NORMAL operation, but a
provider infrastructure failure (timeout or any other exception) is a
SEPARATE failure mode that must never kill a turn -- degrade to the same
behavior as the floor/critic simply not running (skip it, ship the
model's self-reported/unaudited answer) rather than propagate.

These tests lock: an exception on the critique call never escapes
run_react, and the turn still finalizes with a real answer.
"""

from __future__ import annotations

from unittest.mock import patch

from app.pipeline.context import PipelineContext
from app.pipeline.react_loop import run_react

_SEARCH_RESULT = {
    "tool": "search_corpus",
    "success": True,
    "result": "a reasonably long snippet of retrieved policy text",
    "signal": "corpus_only",
    "sources": [],
    "usage": None,
}

_ANSWER_TEXT = "a real, sufficiently long, grounded answer here for the resilience test"


def _make_ctx(chat_mode: str = "copilot") -> PipelineContext:
    ctx = PipelineContext(correlation_id="critic-resilience-test", thread_id=None, message="What is the policy?")
    ctx.effective_message = ctx.message
    ctx.merged_state = {}
    ctx.last_turns = []
    ctx.chat_mode = chat_mode
    ctx.thinking_chunks = []
    return ctx


def _planner_llm(critique_side_effect):
    """fake_llm that plays a normal 2-round search->answer script and
    raises on the critique stage."""
    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        if stage == "react_1":
            return '{"thought": "search", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'
        if stage == "critique":
            raise critique_side_effect
        return (
            '{"thought": "done", "tool": null, "inputs": {}, "is_complete": true, '
            f'"answer": "{_ANSWER_TEXT}", "confidence": "high"}}'
        )
    return fake_llm


class TestMandatoryFloorDegradesOnException:
    def test_timeout_on_mandatory_floor_does_not_crash_the_turn(self):
        fake_llm = _planner_llm(TimeoutError("simulated provider timeout"))
        ctx = _make_ctx("copilot")

        with patch.dict("os.environ", {"MOBIUS_PRODUCT_PROMISE_ENABLED": "1"}), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
             patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
            run_react(ctx, emitter=None)  # must not raise

        assert ctx.final_message, "turn must still finalize with a real answer, not die silently"
        assert _ANSWER_TEXT in ctx.final_message

    def test_generic_exception_on_mandatory_floor_also_degrades(self):
        """Not just timeouts -- any provider/parsing failure on this call
        must degrade the same way, per the ruling ('any uncaught
        exception')."""
        fake_llm = _planner_llm(RuntimeError("simulated 500 from provider"))
        ctx = _make_ctx("copilot")

        with patch.dict("os.environ", {"MOBIUS_PRODUCT_PROMISE_ENABLED": "1"}), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
             patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
            run_react(ctx, emitter=None)

        assert ctx.final_message
        assert _ANSWER_TEXT in ctx.final_message

    def test_floor_exception_marks_floor_as_not_ran(self):
        """ctx.react_groundedness_floor_ran must reflect reality (it
        didn't complete), not silently stay unset/misleading for
        downstream diagnostics (react_trace)."""
        fake_llm = _planner_llm(TimeoutError("simulated provider timeout"))
        ctx = _make_ctx("copilot")

        with patch.dict("os.environ", {"MOBIUS_PRODUCT_PROMISE_ENABLED": "1"}), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
             patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
            run_react(ctx, emitter=None)

        assert ctx.react_groundedness_floor_ran is False

    def test_floor_exception_does_not_append_groundedness_warning(self):
        """A timeout is not the same as the critic actually flagging
        issues -- must not fabricate a 'Groundedness notice' the critic
        never produced."""
        fake_llm = _planner_llm(TimeoutError("simulated provider timeout"))
        ctx = _make_ctx("copilot")

        with patch.dict("os.environ", {"MOBIUS_PRODUCT_PROMISE_ENABLED": "1"}), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
             patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
            run_react(ctx, emitter=None)

        assert "Groundedness notice" not in (ctx.final_message or "")


class TestOptionalCriticPathDegradesOnException:
    def test_timeout_on_optional_critic_does_not_crash_the_turn(self):
        """Same defensive fix on the dormant critic_enabled()-gated path,
        for whenever MOBIUS_REACT_CRITIC is turned on."""
        fake_llm = _planner_llm(TimeoutError("simulated provider timeout"))
        ctx = _make_ctx("copilot")

        with patch.dict("os.environ", {"MOBIUS_REACT_CRITIC": "1", "MOBIUS_PRODUCT_PROMISE_ENABLED": ""}), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
             patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
            run_react(ctx, emitter=None)

        assert ctx.final_message
        assert _ANSWER_TEXT in ctx.final_message
        # Ships unaudited, exactly as-is -- no fabricated warning.
        assert "Groundedness notice" not in ctx.final_message
