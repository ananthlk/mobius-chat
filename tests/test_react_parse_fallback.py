"""ReAct parse-failure prose fallback — when a round's JSON is unparseable.

2026-08-04 (Ananth, live agentic-mode turn asking for a detailed 2-page
report: "feels like the auto escalation mode did not fully kick on and
extend"): round 4 of a 10-round agentic budget hit a JSON parse failure.
The model's raw text was "I'm still gathering information... broadening
my search" — a mid-process narration, not a finished answer — but the
loop shipped it as the final response anyway, 6 rounds early, and the
Product Promise governor's extend/structural-exhaustion logic never ran
because this code path returns before either is reached.

Root cause: the parse-failure fallback (react_loop.py, "Could not parse
model decision" block) trusted raw prose as a synthesised final answer
under two OR'd conditions — ``is_guidance_round(iteration, max_it)``
(correctly mode-relative: fires in the last ~20% of the round budget)
OR a flat ``iteration >= 2`` (absolute round-3 threshold, no relation to
the mode's actual budget). For copilot/task (max_it=3), those two
conditions are identical — is_guidance_round(2, 3) is already True, same
round. For agentic (max_it=10, guidance starts at round 8), the flat
threshold fired 5 rounds early. Dropped it: is_guidance_round alone
already covers every mode correctly since it scales with max_it.

These tests lock in: agentic doesn't finalize on a round-3 parse failure
(the exact live regression), while copilot's identical-by-construction
guidance round is unaffected (no behavior change for the short modes).
"""

from __future__ import annotations

from unittest.mock import patch

from app.pipeline.context import PipelineContext
from app.pipeline.react_loop import run_react

_SEARCH_SUCCESS = {
    "tool": "search_corpus", "success": True, "result": "a real, sufficiently long, useful snippet",
    "signal": "corpus_only", "sources": [], "usage": None,
}

_NARRATION_PROSE = (
    "I'm still gathering information to create that detailed summary report for you. "
    "My previous searches were a bit too narrow, so I'm broadening my scope to find more "
    "comprehensive policy documents on Florida Medicaid telehealth coverage, reimbursement, "
    "and requirements. I'll use a more general query to pull in all the relevant details."
)


def _make_ctx(chat_mode: str) -> PipelineContext:
    ctx = PipelineContext(correlation_id="parse-fallback-test", thread_id=None, message="q")
    ctx.effective_message = ctx.message
    ctx.merged_state = {}
    ctx.last_turns = []
    ctx.chat_mode = chat_mode
    ctx.thinking_chunks = []
    return ctx


def test_agentic_round_3_parse_failure_does_not_ship_narration_as_final():
    """The exact live regression: agentic mode (10 rounds), round 3 (well
    before the guidance band which starts at round 8) returns unparseable
    prose that reads as mid-process narration, not a finished answer. The
    loop must NOT treat this as the final response."""
    seen_stages = []

    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        seen_stages.append(stage)
        n = int(stage.split("_")[1]) if stage.startswith("react_") else None
        if n in (1, 2):
            return '{"thought": "search", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'
        if n == 3:
            return _NARRATION_PROSE  # not valid JSON -> parse failure
        return '{"thought": "done", "tool": null, "inputs": {}, "is_complete": true, "answer": "a real, sufficiently long, finished report here", "confidence": "high"}'

    ctx = _make_ctx("agentic")
    with patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_SUCCESS):
        run_react(ctx, emitter=None)

    assert "I'm still gathering information" not in (ctx.final_message or ""), (
        "the mid-process narration must not ship as the final answer on round 3 of a "
        "10-round budget -- the loop should fall through to the successful tool result "
        "or continue, not treat unfinished prose as done"
    )
    # A successful tool result exists (round 1's search_corpus), so the
    # parse-failure fallback's "use the last successful tool output"
    # branch finalizes with real evidence instead -- confirms the loop
    # didn't just silently produce nothing either.
    assert "a real, sufficiently long, useful snippet" in (ctx.final_message or "")


def test_copilot_round_3_parse_failure_still_ships_prose_unchanged():
    """Copilot mode (3 rounds): round 3 IS the guidance round already
    (is_guidance_round(2, 3) is True on its own) -- this exact case must
    keep working identically to before the fix, since dropping the flat
    `iteration >= 2` clause changes nothing here."""
    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        if stage in ("react_1", "react_2"):
            return '{"thought": "search", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'
        if stage == "react_3":
            return "Based on everything I found, telehealth is covered under Florida Medicaid for behavioral health, subject to standard prior authorization rules and provider licensure requirements."
        raise AssertionError(f"should not reach {stage}")

    ctx = _make_ctx("copilot")
    with patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_SUCCESS):
        run_react(ctx, emitter=None)

    assert "telehealth is covered under Florida Medicaid" in (ctx.final_message or "")
