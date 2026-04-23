"""Tests for ``_cache_preaudited_critic_skip`` (2026-04-23).

Pure-function tests — no LLM, no Chroma. Verifies the gate logic that
decides whether to skip the critic audit when the finalized answer is
already grounded in a pre-audited cached turn.
"""
from __future__ import annotations

import pytest

from app.pipeline.context import PipelineContext
from app.pipeline.react_loop import _cache_preaudited_critic_skip


def _ctx(cache_candidates=None) -> PipelineContext:
    c = PipelineContext(correlation_id="cid", thread_id="tid", message="q?")
    if cache_candidates is not None:
        c.cache_candidates = cache_candidates
    return c


def _cache_seed_tr():
    """The virtual round-0 tool result the orchestrator injects."""
    return {
        "tool": "cached_answer_lookup",
        "success": True,
        "result": "cached answer text",
        "round_virtual": 0,
    }


def _real_tool_tr():
    return {
        "tool": "search_corpus",
        "success": True,
        "result": "fresh retrieval",
    }


# ── Skip criteria met ────────────────────────────────────────────────


def test_skip_when_all_gates_pass(monkeypatch):
    monkeypatch.setenv("CACHE_ASSIST_SKIP_CRITIC_WHEN_PREAUDITED", "1")
    ctx = _ctx(cache_candidates=[
        {"critic_approved": True, "similarity": 0.95},
        {"critic_approved": True, "similarity": 0.90},
    ])
    skip, reason = _cache_preaudited_critic_skip(ctx, [_cache_seed_tr()], rn=1)
    assert skip is True
    assert "all_gates_passed" in reason


# ── Skip criteria NOT met ────────────────────────────────────────────


def test_skip_respects_env_kill_switch(monkeypatch):
    monkeypatch.setenv("CACHE_ASSIST_SKIP_CRITIC_WHEN_PREAUDITED", "0")
    ctx = _ctx(cache_candidates=[{"critic_approved": True}])
    skip, reason = _cache_preaudited_critic_skip(ctx, [_cache_seed_tr()], rn=1)
    assert skip is False
    assert reason == "env_disabled"


def test_no_skip_when_rn_gt_1():
    ctx = _ctx(cache_candidates=[{"critic_approved": True}])
    skip, reason = _cache_preaudited_critic_skip(ctx, [_cache_seed_tr()], rn=2)
    assert skip is False
    assert "rn=2" in reason


def test_no_skip_when_tool_results_empty():
    ctx = _ctx(cache_candidates=[{"critic_approved": True}])
    skip, reason = _cache_preaudited_critic_skip(ctx, [], rn=1)
    assert skip is False
    assert reason == "no_tool_results"


def test_no_skip_when_mixed_cache_and_fresh_tools():
    """A fresh search_corpus call alongside cache means the final answer
    has new content that wasn't in the original critic audit."""
    ctx = _ctx(cache_candidates=[{"critic_approved": True}])
    skip, reason = _cache_preaudited_critic_skip(
        ctx, [_cache_seed_tr(), _real_tool_tr()], rn=1,
    )
    assert skip is False
    assert "mixed" in reason


def test_no_skip_when_only_real_tools_no_cache_seed():
    """No cache seed in tool_results → can't skip."""
    ctx = _ctx(cache_candidates=[])
    skip, reason = _cache_preaudited_critic_skip(ctx, [_real_tool_tr()], rn=1)
    assert skip is False
    # Path-dependent: mixed_cache_and_fresh (when non_cache is non-empty)
    # or cache_seed_absent (when non_cache drains then candidates gate).
    # Either is acceptable as long as we don't skip.


def test_no_skip_when_candidates_not_on_ctx():
    """Cache seed present but ctx.cache_candidates is empty — can't
    verify approval state, don't skip."""
    ctx = _ctx(cache_candidates=[])
    skip, reason = _cache_preaudited_critic_skip(ctx, [_cache_seed_tr()], rn=1)
    assert skip is False
    assert reason == "no_candidates_on_ctx"


def test_no_skip_when_any_candidate_not_approved():
    """Defense in depth: partial approval runs the critic. We can't
    tell WHICH candidate the LLM used, so if any shown candidate was
    unapproved, audit the final answer to be safe."""
    ctx = _ctx(cache_candidates=[
        {"critic_approved": True, "similarity": 0.95},
        {"critic_approved": False, "similarity": 0.90},
    ])
    skip, reason = _cache_preaudited_critic_skip(ctx, [_cache_seed_tr()], rn=1)
    assert skip is False
    assert "not_all_candidates_critic_approved" in reason


def test_no_skip_when_all_candidates_unapproved():
    ctx = _ctx(cache_candidates=[
        {"critic_approved": False},
        {"critic_approved": False},
    ])
    skip, reason = _cache_preaudited_critic_skip(ctx, [_cache_seed_tr()], rn=1)
    assert skip is False
    assert "not_all_candidates_critic_approved" in reason


def test_no_skip_when_candidate_missing_critic_approved_key():
    """Missing key → falsy → defensive: don't skip."""
    ctx = _ctx(cache_candidates=[{"similarity": 0.95}])  # no critic_approved key
    skip, _ = _cache_preaudited_critic_skip(ctx, [_cache_seed_tr()], rn=1)
    assert skip is False
