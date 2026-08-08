"""Integration tests for Task #76's dispatch branch in run_integrate:
MOBIUS_DYNAMIC_ENRICHMENT_PCT gate + sufficiency check together decide
whether Call A is skipped for the deterministic pass, with Call B/C
launched as fire-and-forget background jobs instead of the normal
synchronous parallel path.
"""
from __future__ import annotations

import json
import os
from unittest.mock import patch

from app.pipeline.context import PipelineContext
from app.planner.schemas import Plan, SubQuestion
from app.stages.integrate import run_integrate

_LONG_DRAFT = "Sunshine Health requires initial claims within 180 days of service. " * 3


def _make_plan() -> Plan:
    return Plan(subquestions=[SubQuestion(id="sq1", text="What is X?", kind="non_patient")])


def _make_ctx(**extra) -> PipelineContext:
    ctx = PipelineContext(
        correlation_id="dyn-enrich-cid", thread_id="dyn-enrich-thread", message="What is X?",
        plan=_make_plan(), answers=["Some answer about X."], sources=[], usages=[],
        retrieval_signals=[],
    )
    for k, v in extra.items():
        setattr(ctx, k, v)
    return ctx


def test_sufficient_answer_skips_call_a_and_launches_background_bc():
    ctx = _make_ctx(
        chat_mode="copilot", react_rounds_used=2, react_draft=_LONG_DRAFT,
        react_unfinished_reason=None,
    )
    with (
        patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "parallel", "MOBIUS_DYNAMIC_ENRICHMENT_PCT": "100"}),
        patch("app.stages.integrate.format_response_parallel") as mock_par,
        patch("app.stages.integrate.run_bc_background") as mock_bg,
    ):
        run_integrate(ctx)

    mock_par.assert_not_called()
    mock_bg.assert_called_once()
    payload = json.loads(ctx.response_payload["message"])
    assert "**180 days**" in payload["direct_answer"]


def test_gate_off_uses_normal_parallel_path():
    ctx = _make_ctx(
        chat_mode="copilot", react_rounds_used=2, react_draft=_LONG_DRAFT,
        react_unfinished_reason=None,
    )
    with (
        patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "parallel", "MOBIUS_DYNAMIC_ENRICHMENT_PCT": "0"}),
        patch("app.stages.integrate.format_response_parallel") as mock_par,
        patch("app.stages.integrate.run_bc_background") as mock_bg,
    ):
        mock_par.return_value = (
            json.dumps({"mode": "FACTUAL", "direct_answer": "x", "sections": []}),
            [{"stage": "integrator_a", "model": "m", "input_tokens": 0, "output_tokens": 0}],
        )
        run_integrate(ctx)

    mock_par.assert_called_once()
    mock_bg.assert_not_called()


def test_gate_on_but_not_sufficient_uses_normal_parallel_path():
    """Long multi-round run with open gaps -- needs_enhancement, full Call A."""
    ctx = _make_ctx(
        chat_mode="copilot", react_rounds_used=5, react_draft=_LONG_DRAFT,
        react_unfinished_reason=None,
    )
    with (
        patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "parallel", "MOBIUS_DYNAMIC_ENRICHMENT_PCT": "100"}),
        patch("app.stages.integrate.format_response_parallel") as mock_par,
        patch("app.stages.integrate.run_bc_background") as mock_bg,
    ):
        mock_par.return_value = (
            json.dumps({"mode": "FACTUAL", "direct_answer": "x", "sections": []}),
            [{"stage": "integrator_a", "model": "m", "input_tokens": 0, "output_tokens": 0}],
        )
        run_integrate(ctx)

    mock_par.assert_called_once()
    mock_bg.assert_not_called()


def test_dynamic_enrichment_does_not_apply_to_sequential_path():
    """Feature is scoped to the parallel path only -- sequential is untouched
    even with the gate at 100% and a sufficient answer."""
    ctx = _make_ctx(
        chat_mode="copilot", react_rounds_used=2, react_draft=_LONG_DRAFT,
        react_unfinished_reason=None,
    )
    with (
        patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "sequential", "MOBIUS_DYNAMIC_ENRICHMENT_PCT": "100"}),
        patch("app.stages.integrate.format_response") as mock_seq,
        patch("app.stages.integrate.run_bc_background") as mock_bg,
    ):
        mock_seq.return_value = (
            json.dumps({"mode": "FACTUAL", "direct_answer": "x", "sections": []}),
            {"stage": "integrator", "model": "m", "input_tokens": 0, "output_tokens": 0},
        )
        run_integrate(ctx)

    mock_seq.assert_called_once()
    mock_bg.assert_not_called()


def test_background_bc_launch_failure_does_not_break_the_response():
    """run_bc_background raising must not take down the deterministic-pass
    response -- background enrichment is a bonus, not a dependency."""
    ctx = _make_ctx(
        chat_mode="copilot", react_rounds_used=2, react_draft=_LONG_DRAFT,
        react_unfinished_reason=None,
    )
    with (
        patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "parallel", "MOBIUS_DYNAMIC_ENRICHMENT_PCT": "100"}),
        patch("app.stages.integrate.format_response_parallel") as mock_par,
        patch("app.stages.integrate.run_bc_background", side_effect=RuntimeError("boom")),
    ):
        run_integrate(ctx)

    mock_par.assert_not_called()
    assert ctx.response_payload is not None
    payload = json.loads(ctx.response_payload["message"])
    assert "180 days" in payload["direct_answer"]
