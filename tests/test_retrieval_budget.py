"""Task #98 (2026-08-15, Chat Master/Ananth): token_budget_for_retrieval --
chat's real context-window math replacing RAG's static per-mode guess."""
from __future__ import annotations

from app.services.retrieval_budget import (
    _MIN_BUDGET_FLOOR,
    compute_token_budget_for_retrieval,
)


class FakeCtx:
    def __init__(self, previous_thread_summary=None, last_turns=None):
        self.previous_thread_summary = previous_thread_summary
        self.last_turns = last_turns or []


def test_first_turn_no_history_gives_generous_budget():
    ctx = FakeCtx()
    budget = compute_token_budget_for_retrieval(ctx)
    assert budget > _MIN_BUDGET_FLOOR
    assert isinstance(budget, int)


def test_budget_shrinks_as_conversation_history_grows():
    ctx_empty = FakeCtx()
    ctx_with_history = FakeCtx(
        previous_thread_summary="x" * 600,
        last_turns=[
            {"user_content": "q" * 500, "assistant_content": "a" * 2000},
            {"user_content": "q" * 500, "assistant_content": "a" * 2000},
            {"user_content": "q" * 500, "assistant_content": "a" * 2000},
        ],
    )
    budget_empty = compute_token_budget_for_retrieval(ctx_empty)
    budget_with_history = compute_token_budget_for_retrieval(ctx_with_history)
    assert budget_with_history < budget_empty


def test_never_returns_below_floor():
    # Pathological huge history should still floor, not go negative.
    ctx = FakeCtx(
        previous_thread_summary="x" * 10_000,
        last_turns=[{"user_content": "q" * 2_000_000, "assistant_content": "a" * 100_000}] * 3,
    )
    budget = compute_token_budget_for_retrieval(ctx)
    assert budget == _MIN_BUDGET_FLOOR


def test_only_reads_last_3_turns():
    ctx_3 = FakeCtx(last_turns=[{"user_content": "q" * 100, "assistant_content": "a" * 400}] * 3)
    ctx_10 = FakeCtx(last_turns=[{"user_content": "q" * 100, "assistant_content": "a" * 400}] * 10)
    assert compute_token_budget_for_retrieval(ctx_3) == compute_token_budget_for_retrieval(ctx_10)


def test_non_dict_turns_ignored_not_crashed():
    ctx = FakeCtx(last_turns=["not a dict", None, 42])
    budget = compute_token_budget_for_retrieval(ctx)
    assert isinstance(budget, int)
