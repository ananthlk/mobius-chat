"""Task #34 — per-stage bandit reward routing.

update_quality_for_correlation_stages_async used to broadcast the same
overall_score to any stage not explicitly mapped (STAGE_QUALITY_MAP
dict-miss default). These tests lock in the corrected per-stage routing
and the react_round efficiency penalty.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.services.adjudication.utils import STAGE_QUALITY_MAP, get_stage_quality_score
from app.services.llm_analytics import (
    _map_llm_call_stage_to_rubric_stage,
    update_quality_for_correlation_stages_async,
)


def test_react_stage_maps_to_react_round_not_planner():
    """Task #34: react_N used to share the "planner" bucket
    (addresses_question) -- now gets its own react_round key."""
    assert _map_llm_call_stage_to_rubric_stage("react_3") == "react_round"
    assert _map_llm_call_stage_to_rubric_stage("react_3") != "planner"


def test_react_round_maps_to_grounding():
    q = get_stage_quality_score("react_round", {"grounding": 0.75, "addresses_question": 0.1}, 0.5)
    assert q == 0.75


def test_rag_fact_check_maps_to_factual_consistency():
    q = get_stage_quality_score("rag_fact_check", {"factual_consistency": 0.6, "grounding": 0.9}, 0.5)
    assert q == 0.6


def test_rag_maps_to_grounding_only():
    """Narrowed from a 3-way average (grounding/source_authority/
    data_freshness) to grounding alone per Task #34 spec."""
    assert STAGE_QUALITY_MAP["rag"] == ["grounding"]
    q = get_stage_quality_score("rag", {"grounding": 0.4, "source_authority": 0.9}, 0.5)
    assert q == 0.4


def test_integrator_still_uses_overall_score():
    """Task #34 spec explicitly says keep current behavior for integrator."""
    q = get_stage_quality_score("integrator", {"grounding": 0.1}, 0.88)
    assert q == 0.88


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, sql, *args):
        return self._rows


class _FakeAcquireConn:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


def test_stages_get_differentiated_scores_not_broadcast():
    """End-to-end through update_quality_for_correlation_stages_async:
    different stages in the SAME turn must land different quality_score
    values when the adjudicator's sub_scores actually differ per
    dimension -- the exact bug report (react_round_1 and integrator
    getting the same number regardless of what each contributed)."""
    rows = [
        {"id": "call-integrator", "stage": "integrator"},
        {"id": "call-react-1", "stage": "react_1"},
        {"id": "call-ragfactcheck", "stage": "rag_fact_check"},
    ]
    sub_scores = {"grounding": 0.3, "factual_consistency": 0.9, "addresses_question": 0.6}
    overall_score = 0.55

    written = {}

    async def fake_update_quality_async(call_id, quality_score, quality_source, quality_ruler=None):
        written[call_id] = quality_score
        return True

    with patch("app.services.llm_analytics._acquire_conn", return_value=_FakeAcquireConn(_FakeConn(rows))), \
         patch("app.services.llm_analytics.update_quality_async", side_effect=fake_update_quality_async), \
         patch("app.storage.progress.publish_bandit_reward_event"):
        asyncio.run(
            update_quality_for_correlation_stages_async(
                "cid-differentiation-test", sub_scores, overall_score,
            )
        )

    assert written["call-integrator"] == overall_score  # unchanged behavior
    assert written["call-react-1"] == 0.3  # grounding, not overall_score
    assert written["call-ragfactcheck"] == 0.9  # factual_consistency, not overall_score
    # The core regression: these three must NOT all be the same number.
    assert len(set(written.values())) == 3


def test_react_round_efficiency_penalty_applied():
    """A turn that used most of its round budget gets its react_round
    reward pulled toward 0, even with a strong grounding sub-score."""
    rows = [{"id": "call-react-9", "stage": "react_9"}]
    sub_scores = {"grounding": 0.9}
    written = {}

    async def fake_update_quality_async(call_id, quality_score, quality_source, quality_ruler=None):
        written[call_id] = quality_score
        return True

    with patch("app.services.llm_analytics._acquire_conn", return_value=_FakeAcquireConn(_FakeConn(rows))), \
         patch("app.services.llm_analytics.update_quality_async", side_effect=fake_update_quality_async), \
         patch("app.storage.progress.publish_bandit_reward_event"):
        asyncio.run(
            update_quality_for_correlation_stages_async(
                "cid-efficiency-test", sub_scores, 0.5,
                react_rounds_used=9, react_max_rounds=10,
            )
        )
    # grounding=0.9 * (1 - 9/10) = 0.9 * 0.1 = 0.09
    assert written["call-react-9"] == pytest.approx(0.09)


def test_react_round_efficient_turn_keeps_most_of_grounding_score():
    rows = [{"id": "call-react-1", "stage": "react_1"}]
    sub_scores = {"grounding": 0.9}
    written = {}

    async def fake_update_quality_async(call_id, quality_score, quality_source, quality_ruler=None):
        written[call_id] = quality_score
        return True

    with patch("app.services.llm_analytics._acquire_conn", return_value=_FakeAcquireConn(_FakeConn(rows))), \
         patch("app.services.llm_analytics.update_quality_async", side_effect=fake_update_quality_async), \
         patch("app.storage.progress.publish_bandit_reward_event"):
        asyncio.run(
            update_quality_for_correlation_stages_async(
                "cid-efficient-test", sub_scores, 0.5,
                react_rounds_used=1, react_max_rounds=10,
            )
        )
    # grounding=0.9 * (1 - 1/10) = 0.9 * 0.9 = 0.81
    assert written["call-react-1"] == pytest.approx(0.81)


def test_react_round_no_efficiency_data_falls_back_to_raw_grounding():
    """When react_rounds_used/react_max_rounds aren't available (older
    call sites, or a turn where the pipeline never set them), react_round
    still gets the grounding sub-score -- just without the penalty. Never
    silently reverts to the old broadcast-overall_score behavior."""
    rows = [{"id": "call-react-1", "stage": "react_1"}]
    sub_scores = {"grounding": 0.65}
    written = {}

    async def fake_update_quality_async(call_id, quality_score, quality_source, quality_ruler=None):
        written[call_id] = quality_score
        return True

    with patch("app.services.llm_analytics._acquire_conn", return_value=_FakeAcquireConn(_FakeConn(rows))), \
         patch("app.services.llm_analytics.update_quality_async", side_effect=fake_update_quality_async), \
         patch("app.storage.progress.publish_bandit_reward_event"):
        asyncio.run(
            update_quality_for_correlation_stages_async("cid-no-efficiency-data", sub_scores, 0.5)
        )
    assert written["call-react-1"] == 0.65


def test_stage_scores_override_still_takes_priority_over_react_round_mapping():
    """Existing behavior preserved: when the adjudicator provides an
    explicit per-round stage_scores value, it wins over the react_round
    grounding mapping (and is NOT efficiency-penalized -- that penalty
    only applies to the fallback path)."""
    rows = [{"id": "call-react-2", "stage": "react_2"}]
    sub_scores = {"grounding": 0.1}
    written = {}

    async def fake_update_quality_async(call_id, quality_score, quality_source, quality_ruler=None):
        written[call_id] = quality_score
        return True

    with patch("app.services.llm_analytics._acquire_conn", return_value=_FakeAcquireConn(_FakeConn(rows))), \
         patch("app.services.llm_analytics.update_quality_async", side_effect=fake_update_quality_async), \
         patch("app.storage.progress.publish_bandit_reward_event"):
        asyncio.run(
            update_quality_for_correlation_stages_async(
                "cid-stage-scores-override", sub_scores, 0.5,
                stage_scores={"react_2": 0.95},
                react_rounds_used=9, react_max_rounds=10,
            )
        )
    assert written["call-react-2"] == 0.95
