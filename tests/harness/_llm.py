"""Scripted LLM response factories for harness scenarios.

Each factory returns a ``fake_llm`` callable with the same signature as
``_call_llm_json`` in app.pipeline.react.prompts:

    fake_llm(system, user, max_tokens=800, ctx=None,
             stage="planner", **kwargs) -> str

Responses are keyed by call number so a scripted sequence produces
deterministic round counts regardless of what the prompt says.  The
caller patches ``app.pipeline.react_loop._call_llm_json`` with the
returned function.

All response JSON matches the planner decision schema:
    {
      "thought": str,
      "tool": null | str,
      "inputs": {...} | null,
      "is_complete": bool,
      "answer": str | null,
      "evidence_review": {...} | null,
      "unfinished_reason": str | null,
    }

Critic calls (stage == "react_completion_critic" or "groundedness_critic")
get a generic PASS response so they don't interfere with round arithmetic.
"""
from __future__ import annotations

import json

# ── Shared response strings ────────────────────────────────────────────

_FINALIZE = json.dumps({
    "thought": "Sufficient evidence found.",
    "tool": None,
    "inputs": None,
    "is_complete": True,
    "answer": "The answer is X.",
    "evidence_review": {
        "running_answer": "X covered.",
        "gaps_closed": ["primary question"],
        "gaps_open": [],
    },
})

_TOOL_CALL = json.dumps({
    "thought": "Need more information.",
    "tool": "healthcare_query",
    "inputs": {"query": "prior authorization requirements"},
    "is_complete": False,
    "answer": None,
    "evidence_review": {
        "running_answer": "",
        "gaps_closed": [],
        "gaps_open": ["primary question"],
    },
})

_TOOL_CALL_THEN_DONE = [_TOOL_CALL, _FINALIZE]

_EXHAUSTION = json.dumps({
    "thought": "Still searching.",
    "tool": "healthcare_query",
    "inputs": {"query": "more detail"},
    "is_complete": False,
    "answer": None,
    "evidence_review": {
        "running_answer": "Partial.",
        "gaps_closed": [],
        "gaps_open": ["still open"],
    },
})

_CLARIFY_RESPONSE = json.dumps({
    "thought": "Query is ambiguous.",
    "tool": None,
    "is_complete": True,
    "answer": "Which Sunshine Health plan do you mean?",
    "evidence_review": None,
})

_CRITIC_PASS = json.dumps({
    "groundedness": "pass",
    "confidence": "high",
    "issues": [],
})


def _is_critic_stage(stage: str) -> bool:
    return stage in ("react_completion_critic", "groundedness_critic", "critic")


def finalize_on_round_1(system, user, max_tokens=800, ctx=None, stage="planner", **kw) -> str:
    """Planner always finalizes immediately; one round used."""
    if _is_critic_stage(stage):
        return _CRITIC_PASS
    return _FINALIZE


def tool_then_finalize(system, user, max_tokens=800, ctx=None, stage="planner", **kw) -> str:
    """Planner calls a tool on round 1, finalizes on round 2.

    Uses the round number embedded in ``ctx.react_rounds_used`` so the
    response sequence is keyed on actual loop state rather than a
    call-counter that could drift if the critic makes extra calls.
    Falls back to a simple counter for contexts that don't set the attr.
    """
    if _is_critic_stage(stage):
        return _CRITIC_PASS
    # ctx.react_rounds_used is incremented at the TOP of each round
    # before the planner call, so round-1 call sees react_rounds_used=1.
    rn = int(getattr(ctx, "react_rounds_used", 0) or 0) if ctx is not None else 0
    return _TOOL_CALL if rn <= 1 else _FINALIZE


def never_finalize(system, user, max_tokens=800, ctx=None, stage="planner", **kw) -> str:
    """Planner never finalizes — always requests a tool.  Loop runs to ceiling."""
    if _is_critic_stage(stage):
        return _CRITIC_PASS
    return _EXHAUSTION


def make_scripted(responses: list[str]):
    """Return a fake_llm that plays back ``responses`` in order.

    After the sequence is exhausted, repeats the last entry so callers
    that run more rounds than the script anticipates don't error.

    Per-call state lives on the returned function object; create a fresh
    one per test via ``make_scripted(...)`` inside the test body (don't
    reuse across tests — the counter persists on the function).
    """
    state = {"i": 0}

    def _fake(system, user, max_tokens=800, ctx=None, stage="planner", **kw) -> str:
        if _is_critic_stage(stage):
            return _CRITIC_PASS
        idx = min(state["i"], len(responses) - 1)
        state["i"] += 1
        return responses[idx]

    return _fake


_TOOL_RESULT = {
    "tool": "healthcare_query",
    "success": True,
    "result": "The prior authorization requirement is X.",
    "signal": "corpus_only",
    "sources": [{"document_name": "Manual", "index": 1, "text": "X"}],
    "usage": None,
}


def fake_execute_tool(tool, inputs, ctx, round_num, emit_fn, tool_emitter,
                      skip_retry=False, open_gaps=None):
    """Stub tool execution — always succeeds with a minimal corpus result."""
    return dict(_TOOL_RESULT, tool=tool)
