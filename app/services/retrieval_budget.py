"""Chat's real, request-level context-window math for
``token_budget_for_retrieval`` -- Task #98 (2026-08-15, Chat Master/Ananth).

RAG's Structure stage (mobius-rag/app/services/retriever/shape/structure.py)
has always guessed the retrieval token budget from a static per-caller_mode
table (chat.default=3000, chat.thinking=16000, ...) because chat never sent
a real value. Ananth's direct correction: "RAG does not have to guess" --
chat knows its own context window, system prompt, conversation history, and
answer-generation reserve; it should compute the real number and pass it.
RAG's own docstring frames the formula as::

    token_budget_for_retrieval = context_window
                                - system_prompt_tokens
                                - conversation_history_tokens
                                - answer_generation_reserve

This is what dominates the growth Ananth called out ("a thread's first turn
vs. turn 20 differ by thousands of tokens") -- conversation_history_tokens is
the one term that actually varies per-request; the other three are stable
per caller_mode/model. ``None`` (unset) is still a valid return path for
callers that want RAG's static table -- this module always returns an int,
callers decide whether to pass it.

Uses the same chars // 4 token-estimate heuristic already used elsewhere in
this codebase (llm_manager.py) rather than a real tokenizer -- consistent
with how every other token-budget decision in this repo is made, and RAG
trims via its own mmr_select regardless, so an estimate within the right
order of magnitude is what actually matters here, not tokenizer-exact
precision.
"""
from __future__ import annotations

from typing import Any

# System prompt + tool manifest overhead for a react round. Not measured
# per-call (the real system prompt is resolved dynamically via LLMManager
# v2 composition, see react/prompts.py's resolve_react_system_prompt_v2) --
# this is a stable estimate of that overhead's typical size.
_SYSTEM_PROMPT_RESERVE_TOKENS = 1500

# Headroom for the round's own output (thought + tool call / final answer
# JSON). Matches the generous per-round output budget widened 2026-08-08
# (final_parallel.py's max_tokens comments) for the same "thinking tokens
# eat into max_tokens" reason -- react rounds see the same model family.
_ANSWER_GENERATION_RESERVE_TOKENS = 4096

# Never send RAG a budget so small retrieval would be pointless (a few
# short chunks at most) -- floor matches roughly chat.copilot's own static
# table entry, so a degenerate computation still gets copilot-tier content.
_MIN_BUDGET_FLOOR = 2000

# Conservative fallback context window (tokens) when no react-eligible
# model can be resolved from the registry -- smaller than every model
# actually in the roster, so a lookup failure under-budgets rather than
# over-promises RAG a payload chat can't actually fit.
_FALLBACK_CONTEXT_WINDOW_TOKENS = 128 * 1024

# Same 3-turn window / preview caps as the actual conversation-history
# render in react/prompts.py's build_reasoning_context (the "substantive
# follow-up" branch — the common case). The rarer transform-intent branch
# uses a larger 3000-char preview; using the compact caps here is a
# deliberate conservative choice (slightly under-estimates history size in
# that rarer case, which makes the computed budget slightly generous, not
# a hard violation -- RAG still trims to what's actually needed).
_HISTORY_TURN_WINDOW = 3
_HISTORY_ASSISTANT_PREVIEW_CHARS = 400


def _min_react_context_window_tokens() -> int:
    """Conservative context window: the smallest spec_context_k among
    models actually eligible for the react_1 stage, so the budget stays
    safe regardless of which model the bandit picks for this turn."""
    from app.services.model_registry import MODEL_ROSTER

    candidates = [
        m.spec_context_k
        for m in MODEL_ROSTER.values()
        if m.enabled and "react_1" in m.eligible_stages
    ]
    if not candidates:
        return _FALLBACK_CONTEXT_WINDOW_TOKENS
    return min(candidates) * 1024


def _estimate_conversation_history_tokens(ctx: Any) -> int:
    total_chars = 0
    summary = (getattr(ctx, "previous_thread_summary", None) or "").strip()
    total_chars += len(summary[:600])  # matches the render's own 600-char cap

    for turn in (getattr(ctx, "last_turns", None) or [])[:_HISTORY_TURN_WINDOW]:
        if not isinstance(turn, dict):
            continue
        user_q = turn.get("user_content") or turn.get("message") or ""
        assistant_full = turn.get("assistant_content") or ""
        total_chars += len(user_q)
        total_chars += min(len(assistant_full), _HISTORY_ASSISTANT_PREVIEW_CHARS)

    return total_chars // 4


def compute_token_budget_for_retrieval(ctx: Any) -> int:
    """Chat's real per-request retrieval token budget for this turn.

    Always returns a usable int (never None) -- floored at
    ``_MIN_BUDGET_FLOOR`` so a degenerate/edge-case computation still
    yields a workable retrieval payload rather than starving RAG.
    """
    context_window = _min_react_context_window_tokens()
    history_tokens = _estimate_conversation_history_tokens(ctx)
    budget = (
        context_window
        - _SYSTEM_PROMPT_RESERVE_TOKENS
        - history_tokens
        - _ANSWER_GENERATION_RESERVE_TOKENS
    )
    return max(budget, _MIN_BUDGET_FLOOR)
