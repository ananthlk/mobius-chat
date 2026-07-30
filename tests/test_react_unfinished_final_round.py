"""ReAct final-round self-report — the model's own terminal-state signal.

2026-07-30 (Ananth): the generic "I wasn't able to find a verified answer…"
honest-escalation string is a poor failure mode — it doesn't say what was
checked, what (if anything) was found, or what would actually unblock the
user. Rather than bolt a separate summarization call onto the end of the
loop, react's LAST round gets an extra context-message instruction
(_REACT_FINAL_ROUND_INSTRUCTION in react_loop.py) telling the model that if
it can't complete, it should self-report:

  unfinished_reason:  "need_more_time" | "need_more_info" | "no_path_forward"
  unfinished_summary: what it checked / found, specific to the question
  unblock_ask:        (need_more_info only) the specific missing fact

react_loop.py's exhausted-iterations fallback reads these straight off the
last round's parsed decision (Python for-loops don't scope, so `decision`
still holds the final round's dict when the loop exits) and renders a
tailored message. Non-compliance (older/less capable model, malformed
JSON) falls back to the original generic string — this file locks that
fail-soft floor as much as the new templates.
"""

from __future__ import annotations

from unittest.mock import patch

from app.pipeline.context import PipelineContext
from app.pipeline.react_loop import run_react

_GENERIC_FALLBACK = (
    "I wasn't able to find a verified answer to this question "
    "after checking our materials and searching the web. "
    "You may want to contact the payer directly or provide a link to their documentation."
)

_SEARCH_RESULT = {
    "tool": "search_corpus",
    "success": True,
    "result": "general utilization management policy text",
    "signal": "corpus_only",
    "sources": [],
    "usage": None,
}


def _make_ctx(chat_mode: str) -> PipelineContext:
    ctx = PipelineContext(correlation_id="test-unfinished", thread_id=None, message="What is the PA policy?")
    ctx.effective_message = ctx.message
    ctx.merged_state = {}
    ctx.last_turns = []
    ctx.chat_mode = chat_mode
    return ctx


def test_final_round_instruction_only_on_last_round():
    """Earlier rounds must NOT see the final-round instruction — only the
    actual last round should. Prevents the instruction leaking into every
    round's context (wasted tokens, confusing "this is final" on round 1).

    Uses copilot mode (3 rounds) rather than quick — quick mode has its own
    pre-existing "fast mode early exit" (finalizes after round 1 if the
    tool call succeeds with >=30 chars), which would short-circuit before
    ever reaching a "last round" at all and isn't what this test is about.
    """
    seen_final_instruction_on_round = []

    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        seen_final_instruction_on_round.append(("FINAL round" in user, stage))
        if stage in ("react_1", "react_2"):
            return '{"thought": "search", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'
        return (
            '{"thought": "still missing detail", "tool": null, "inputs": {}, "is_complete": false, '
            '"unfinished_reason": "no_path_forward", "unfinished_summary": "checked corpus, nothing found"}'
        )

    ctx = _make_ctx("copilot")  # 3 rounds
    with patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
        run_react(ctx, emitter=None)

    by_stage = dict((s, has_final) for has_final, s in seen_final_instruction_on_round)
    assert by_stage["react_1"] is False, "round 1 (not last) must not see the final-round instruction"
    assert by_stage["react_2"] is False, "round 2 (not last) must not see the final-round instruction"
    assert by_stage["react_3"] is True, "round 3 (last, copilot mode) must see the final-round instruction"


def test_need_more_info_surfaces_summary_and_specific_ask():
    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        if stage in ("react_1", "react_2"):
            return '{"thought": "search", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'
        return (
            '{"thought": "missing plan detail", "tool": null, "inputs": {}, "is_complete": false, '
            '"unfinished_reason": "need_more_info", '
            '"unfinished_summary": "I checked the provider manual and found general UM policy but nothing plan-specific.", '
            '"unblock_ask": "the exact plan name (HMO vs PPO) for this member"}'
        )

    ctx = _make_ctx("copilot")
    with patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
        run_react(ctx, emitter=None)

    final = ctx.final_message or ""
    assert "general UM policy" in final
    assert "exact plan name" in final
    assert "To help me find this:" in final


def test_need_more_time_offers_agentic_escalation_when_not_already_agentic():
    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        if stage in ("react_1", "react_2"):
            return '{"thought": "search", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'
        return (
            '{"thought": "promising lead, out of rounds", "tool": null, "inputs": {}, "is_complete": false, '
            '"unfinished_reason": "need_more_time", '
            '"unfinished_summary": "Found a related policy section but need to cross-reference two more documents."}'
        )

    ctx = _make_ctx("copilot")  # 3 rounds
    with patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
        run_react(ctx, emitter=None)

    final = ctx.final_message or ""
    assert "cross-reference two more documents" in final
    assert "Agentic mode" in final
    assert "copilot mode" in final


def test_need_more_time_in_agentic_mode_does_not_offer_escalation():
    """No higher tier exists above agentic — must not tell the user to
    switch to the mode they're already in."""
    call_count = {"n": 0}

    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        call_count["n"] += 1
        if call_count["n"] >= 10:
            return (
                '{"thought": "ran the full budget", "tool": null, "inputs": {}, "is_complete": false, '
                '"unfinished_reason": "need_more_time", '
                '"unfinished_summary": "Explored several angles but could not fully resolve this."}'
            )
        return '{"thought": "search", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'

    ctx = _make_ctx("agentic")  # 10 rounds
    with patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
        run_react(ctx, emitter=None)

    final = ctx.final_message or ""
    assert "Explored several angles" in final
    assert "Agentic mode" not in final, "already in agentic mode — nothing higher to offer"
    assert "specific next step" in final.lower()


def test_no_path_forward_shares_summary_with_no_specific_ask():
    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        if stage in ("react_1", "react_2"):
            return '{"thought": "search", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'
        return (
            '{"thought": "exhausted every angle", "tool": null, "inputs": {}, "is_complete": false, '
            '"unfinished_reason": "no_path_forward", '
            '"unfinished_summary": "Checked our corpus and the web; this payer has no published policy for this code."}'
        )

    ctx = _make_ctx("copilot")
    with patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
        run_react(ctx, emitter=None)

    final = ctx.final_message or ""
    assert "no published policy" in final
    assert "specific next step" in final.lower()


def test_noncompliant_model_falls_back_to_legacy_generic_string_exactly():
    """Fail-soft floor: a model that never adopts the new fields (older
    model, prompt not followed, malformed JSON) must produce the EXACT
    pre-existing string — zero behavior change on non-compliance. Copilot
    mode (not quick) so the pre-existing "fast mode" round-1 shortcut
    doesn't short-circuit before all 3 rounds run."""

    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        return '{"thought": "still looking", "tool": null, "inputs": {}, "is_complete": false}'

    ctx = _make_ctx("copilot")
    with patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
        run_react(ctx, emitter=None)

    assert ctx.final_message == _GENERIC_FALLBACK
