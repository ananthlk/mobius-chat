"""ReAct dynamic early exit — the structural-exhaustion offramp.

2026-08-04 (Ananth, watching a live agentic-mode trace: "why are we
forcing 3 rounds when we already exhausted... doesn't it have to be
dynamic"): round consumption was already dynamic in one direction (the
loop exits the moment is_complete=true fires, even at round 1) but not
the other — the "honest give up" self-report (_REACT_FINAL_ROUND_
INSTRUCTION, see test_react_unfinished_final_round.py) was only legal on
the MECHANICAL last round of a fixed per-mode budget. On a long-budget
mode (agentic, 10 rounds), a turn that becomes genuinely hopeless at
round 3 had no way to say so — it either had to keep going through the
motions for up to 7 more rounds it didn't believe would help, or bail out
in a form the loop didn't recognize.

This adds a data-driven early offramp: once ReactRetryGuard.
structurally_exhausted() is true (zero successes, 2+ genuinely different
tools have each failed — see test_react_retry_guard.py's
TestStructurallyExhausted), the SAME self-report format becomes legal
starting the very next round, not forced — the model can still keep
trying if it believes a different angle is worth it. The instruction text
differs from the final-round one (it must not claim "no round after this"
or forbid further tool calls, since on an early round that's false).

A real bug was caught building this (not by inspection): the existing
"skip default tool dispatch" line used `continue`, which is a no-op vs.
`break` on the TRUE final round (the loop's own bound check breaks on the
next pass either way) but silently keeps looping past an EARLY offramp
round instead of finalizing there — defeating the entire point. Fixed by
changing `continue` to `break`; the four tests below lock in the exact
round sequence, not just that a message eventually renders correctly.
"""

from __future__ import annotations

from unittest.mock import patch

from app.pipeline.context import PipelineContext
from app.pipeline.react_loop import run_react

_CORPUS_FAIL = {
    "tool": "search_corpus", "success": False, "result": "nothing found",
    "signal": "no_sources", "sources": [], "usage": None,
}
_WEB_FAIL = {
    "tool": "web_scrape", "success": False, "result": "HTTP 403",
    "error": {"error_code": "scrape_failed"}, "signal": "no_sources", "sources": [], "usage": None,
}
_SEARCH_SUCCESS = {
    "tool": "search_corpus", "success": True, "result": "a real, sufficiently long, useful snippet",
    "signal": "corpus_only", "sources": [], "usage": None,
}


def _make_ctx(chat_mode: str = "agentic") -> PipelineContext:
    ctx = PipelineContext(correlation_id="offramp-test", thread_id=None, message="q")
    ctx.effective_message = ctx.message
    ctx.merged_state = {}
    ctx.last_turns = []
    ctx.chat_mode = chat_mode
    ctx.thinking_chunks = []
    return ctx


def test_agentic_stops_at_round_3_not_round_10_when_structurally_exhausted():
    """The core regression: agentic mode (10 rounds available) must stop
    as soon as it's genuinely earned the right to, not burn through to
    the mechanical ceiling."""
    seen_stages = []

    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        seen_stages.append(stage)
        if stage == "react_1":
            return '{"thought": "try corpus", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'
        if stage == "react_2":
            return '{"thought": "try web", "tool": "web_scrape", "inputs": {"url": "https://x"}, "is_complete": false}'
        if stage == "react_3":
            return (
                '{"thought": "nothing left to try", "tool": null, "inputs": {}, "is_complete": false, '
                '"unfinished_reason": "no_path_forward", "unfinished_summary": "tried corpus and web, both dead ends"}'
            )
        raise AssertionError(f"should not reach {stage} — the offramp should have let the loop stop at round 3")

    def fake_exec_retry(tool, inputs, ctx, rn, emit, tool_emitter, skip_retry=False):
        return _CORPUS_FAIL if tool == "search_corpus" else _WEB_FAIL

    ctx = _make_ctx("agentic")
    with patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", side_effect=fake_exec_retry):
        run_react(ctx, emitter=None)

    assert seen_stages == ["react_1", "react_2", "react_3"], (
        f"expected exactly 3 rounds, got {seen_stages} — early exit did not take effect"
    )
    assert "tried corpus and web" in ctx.final_message
    assert "specific next step" in ctx.final_message.lower()


def test_offramp_text_absent_before_two_distinct_failures():
    """Round 1 has zero completed attempts, round 2 has exactly ONE
    (corpus) — neither should see the offramp; it only becomes legal once
    genuine breadth (2+ different tools) has failed."""
    seen = []

    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        seen.append((stage, "multiple different approaches" in user))
        if stage == "react_1":
            return '{"thought": "try corpus", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'
        return '{"thought": "try web", "tool": "web_scrape", "inputs": {"url": "https://x"}, "is_complete": false}'

    def fake_exec_retry(tool, inputs, ctx, rn, emit, tool_emitter, skip_retry=False):
        return _CORPUS_FAIL if tool == "search_corpus" else _WEB_FAIL

    ctx = _make_ctx("agentic")
    # Cap the scripted LLM at 2 calls by only patching what's needed —
    # let it run further, we only care about rounds 1 and 2's context.
    call_count = {"n": 0}

    def fake_llm_capped(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        call_count["n"] += 1
        if call_count["n"] > 3:
            return (
                '{"thought": "done", "tool": null, "inputs": {}, "is_complete": false, '
                '"unfinished_reason": "no_path_forward", "unfinished_summary": "stopping the test here"}'
            )
        return fake_llm(system, user, max_tokens, ctx, stage, **kwargs)

    with patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm_capped), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", side_effect=fake_exec_retry):
        run_react(ctx, emitter=None)

    round_1_2 = [(s, has) for s, has in seen if s in ("react_1", "react_2")]
    assert round_1_2, "expected round 1 and round 2 to have run"
    assert all(not has for _, has in round_1_2), (
        f"offramp text must NOT appear before 2 distinct tools have failed, saw it on: "
        f"{[s for s, has in round_1_2 if has]}"
    )


def test_offramp_does_not_force_a_stop_model_can_keep_going():
    """The offramp is additive, not mandatory — a model that keeps
    choosing to call tools past the exhaustion point must be allowed to,
    right up to the mechanical ceiling."""
    seen_stages = []

    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        seen_stages.append(stage)
        n = int(stage.split("_")[1])
        if n == 1:
            return '{"thought": "try corpus", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'
        if n == 2:
            return '{"thought": "try web", "tool": "web_scrape", "inputs": {"url": "https://x"}, "is_complete": false}'
        # Even though the offramp is available from round 3 on, this model
        # keeps trying (a different query each time) instead of using it.
        return f'{{"thought": "trying again", "tool": "search_corpus", "inputs": {{"query": "attempt {n}"}}, "is_complete": false}}'

    def fake_exec_retry(tool, inputs, ctx, rn, emit, tool_emitter, skip_retry=False):
        return _CORPUS_FAIL if tool == "search_corpus" else _WEB_FAIL

    ctx = _make_ctx("copilot")  # 3 rounds — offramp legal from round 3, which is also the mechanical last round here
    with patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", side_effect=fake_exec_retry):
        run_react(ctx, emitter=None)

    # Ran all 3 rounds (didn't get force-stopped early just because the
    # offramp became available) — the round-3 response above doesn't set
    # unfinished_reason, so the loop proceeds through its normal exhausted-
    # iterations path at the mechanical ceiling.
    assert seen_stages == ["react_1", "react_2", "react_3"]


def test_no_offramp_when_something_already_succeeded():
    """A successful tool result mid-turn means there's real evidence to
    work with — must not offer the "give up" format even after some
    earlier attempt failed."""

    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        assert "multiple different approaches" not in user, (
            f"offramp text must not appear on {stage} — a tool already succeeded this turn"
        )
        if stage == "react_1":
            return '{"thought": "try corpus", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'
        if stage == "react_2":
            return '{"thought": "try web too", "tool": "web_scrape", "inputs": {"url": "https://x"}, "is_complete": false}'
        return '{"thought": "done", "tool": null, "inputs": {}, "is_complete": true, "answer": "a real long enough answer here", "confidence": "high"}'

    call_n = {"n": 0}

    def fake_exec_retry(tool, inputs, ctx, rn, emit, tool_emitter, skip_retry=False):
        call_n["n"] += 1
        if tool == "search_corpus":
            return _SEARCH_SUCCESS  # succeeds — no structural exhaustion possible after this
        return _WEB_FAIL

    ctx = _make_ctx("agentic")
    with patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", side_effect=fake_exec_retry):
        run_react(ctx, emitter=None)
