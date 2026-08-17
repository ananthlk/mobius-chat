"""Task #102 (2026-08-16, Chat Master/Ananth ruling): integrator_a's
max_tokens raised on chat.thinking (agentic) turns only -- Retriever's
same-day chat.thinking retrieval-budget increase (6000->16000) pushed a much
larger prompt into integrator_a, and Gemini 2.5's thinking tokens share the
same budget as visible output, starving the actual answer content (confirmed
live: 46.7% of integrator_a calls under 250 output tokens in a 48h sample).

Updated same day: 8192 -> 16384 ("best in class detailed output, no
artificial truncation" -- a full policy answer with tables/codes/limits can
need 4000-6000 tokens on its own; 16384 gives real headroom above that)."""
from __future__ import annotations

from app.responder.final_parallel import (
    _INTEGRATOR_A_MAX_TOKENS_CHAT_THINKING,
    _INTEGRATOR_A_MAX_TOKENS_DEFAULT,
    _integrator_a_max_tokens,
)


def test_agentic_gets_raised_budget():
    assert _integrator_a_max_tokens("agentic") == _INTEGRATOR_A_MAX_TOKENS_CHAT_THINKING
    assert _INTEGRATOR_A_MAX_TOKENS_CHAT_THINKING == 16384


def test_copilot_stays_at_default():
    assert _integrator_a_max_tokens("copilot") == _INTEGRATOR_A_MAX_TOKENS_DEFAULT
    assert _INTEGRATOR_A_MAX_TOKENS_DEFAULT == 4096


def test_quick_stays_at_default():
    assert _integrator_a_max_tokens("quick") == _INTEGRATOR_A_MAX_TOKENS_DEFAULT


def test_task_stays_at_default():
    assert _integrator_a_max_tokens("task") == _INTEGRATOR_A_MAX_TOKENS_DEFAULT


def test_none_stays_at_default():
    assert _integrator_a_max_tokens(None) == _INTEGRATOR_A_MAX_TOKENS_DEFAULT
