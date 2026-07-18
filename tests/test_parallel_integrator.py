"""Tests for the parallel integrator path (final_parallel.py + integrate.py routing)."""
from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.context import PipelineContext
from app.planner.schemas import Plan, SubQuestion
from app.responder.final_parallel import _parse_json_response, format_response_parallel


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_plan() -> Plan:
    return Plan(subquestions=[SubQuestion(id="sq1", text="What is X?", kind="non_patient")])


def _make_ctx() -> PipelineContext:
    return PipelineContext(
        correlation_id="test-cid",
        thread_id="test-thread",
        message="What is X?",
        plan=_make_plan(),
        answers=["Some answer about X."],
        sources=[],
        usages=[],
        retrieval_signals=[],
    )


VALID_CORE_CARD = json.dumps({
    "mode": "FACTUAL",
    "direct_answer": "X is a test answer.",
    "sections": [{"intent": "references", "label": "Details", "format": "bullets", "bullets": ["Bullet 1"]}],
    "thread_summary": "Test query — X",
})

VALID_CRITIC = json.dumps({
    "citations": [{"claim": "X is a test", "doc_title": "Doc A", "locator": "p.1", "snippet": "verbatim text"}],
    "cited_source_indices": [1],
    "source_confidence_override": None,
    "confidence_note": None,
    "takeaways": ["Remember X."],
    "gaps": [],
})

VALID_ENRICHMENT = json.dumps({
    "next_questions_for_user": ["What is Y?", "How does X relate to Z?"],
    "next_steps": ["Submit within 90 days."],
    "suggested_actions": [],
})


def _fake_generate_sync(prompt: str, stage: str = "integrator_a", max_tokens: int = 4096, **kwargs) -> tuple[str, dict[str, Any]]:
    usage = {"stage": stage, "model": "test-model", "input_tokens": 10, "output_tokens": 20, "latency_ms": 100}
    if stage == "integrator_a":
        return (VALID_CORE_CARD, usage)
    if stage == "integrator_critic":
        return (VALID_CRITIC, usage)
    if stage == "integrator_enrichment":
        return (VALID_ENRICHMENT, usage)
    return ("{}", usage)


# ── Unit tests ────────────────────────────────────────────────────────────────

class TestParseJsonResponse:
    def test_valid_json(self):
        result = _parse_json_response('{"key": "value"}', "test")
        assert result == {"key": "value"}

    def test_json_with_fence(self):
        result = _parse_json_response('```json\n{"key": "value"}\n```', "test")
        assert result == {"key": "value"}

    def test_empty_string(self):
        assert _parse_json_response("", "test") == {}

    def test_malformed_returns_empty(self):
        result = _parse_json_response("not json at all {{{{", "test")
        assert isinstance(result, dict)


class TestFormatResponseParallel:
    def test_happy_path_merges_all_three(self):
        plan = _make_plan()
        with (
            patch("app.responder.final_parallel._call_llm", side_effect=_fake_generate_sync),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
        ):
            mock_prompts = MagicMock()
            mock_prompts.consolidator_factual_max = 0.4
            mock_prompts.consolidator_canonical_min = 0.6
            mock_prompts.integrator_parallel_core_system = "core sys"
            mock_prompts.integrator_parallel_critic_system = "critic sys"
            mock_prompts.integrator_parallel_enrichment_system = "enrichment sys"
            mock_prompts.integrator_user_template = "Input:\n{consolidator_input_json}\n\nReturn JSON."
            mock_cfg.return_value.prompts = mock_prompts

            result_json, usages = format_response_parallel(
                plan, ["answer"], user_message="What is X?"
            )

        card = json.loads(result_json)
        # Core fields present
        assert card["mode"] == "FACTUAL"
        assert "X is a test answer." in card["direct_answer"]
        # Critic fields merged
        assert len(card["citations"]) == 1
        assert card["citations"][0]["claim"] == "X is a test"
        assert card["takeaways"] == ["Remember X."]
        assert card["gaps"] == []
        # Enrichment fields merged
        assert "What is Y?" in card["next_questions_for_user"]
        assert card["next_steps"] == ["Submit within 90 days."]
        # 3 usage dicts returned
        assert len(usages) == 3
        assert {u["stage"] for u in usages} == {"integrator_a", "integrator_critic", "integrator_enrichment"}

    def test_fallback_when_core_fails(self):
        plan = _make_plan()

        def fail_a(prompt, stage="integrator_a", max_tokens=4096, **kw):
            if stage == "integrator_a":
                raise RuntimeError("LLM failure")
            return ("{}", {"stage": stage, "model": "m", "input_tokens": 0, "output_tokens": 0, "latency_ms": 0})

        with (
            patch("app.responder.final_parallel._call_llm", side_effect=fail_a),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
        ):
            mock_prompts = MagicMock()
            mock_prompts.consolidator_factual_max = 0.4
            mock_prompts.consolidator_canonical_min = 0.6
            mock_prompts.integrator_parallel_core_system = "core sys"
            mock_prompts.integrator_parallel_critic_system = "critic sys"
            mock_prompts.integrator_parallel_enrichment_system = "enrichment sys"
            mock_prompts.integrator_user_template = "Input:\n{consolidator_input_json}\n\nReturn JSON."
            mock_cfg.return_value.prompts = mock_prompts

            result_json, usages = format_response_parallel(
                plan, ["answer"], user_message="What is X?"
            )

        # ThreadPoolExecutor itself shouldn't crash; result should be a string
        assert isinstance(result_json, str)

    def test_critic_failure_doesnt_break_core(self):
        """B fails; A+C still succeed; card has no citations but has followups."""
        plan = _make_plan()

        def partial_fail(prompt, stage="integrator_a", max_tokens=4096, **kw):
            if stage == "integrator_critic":
                raise RuntimeError("critic down")
            return _fake_generate_sync(prompt, stage, max_tokens, **kw)

        with (
            patch("app.responder.final_parallel._call_llm", side_effect=partial_fail),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
        ):
            mock_prompts = MagicMock()
            mock_prompts.consolidator_factual_max = 0.4
            mock_prompts.consolidator_canonical_min = 0.6
            mock_prompts.integrator_parallel_core_system = "core sys"
            mock_prompts.integrator_parallel_critic_system = "critic sys"
            mock_prompts.integrator_parallel_enrichment_system = "enrichment sys"
            mock_prompts.integrator_user_template = "Input:\n{consolidator_input_json}\n\nReturn JSON."
            mock_cfg.return_value.prompts = mock_prompts

            result_json, usages = format_response_parallel(
                plan, ["answer"], user_message="What is X?"
            )

        card = json.loads(result_json)
        assert card["mode"] == "FACTUAL"
        assert "What is Y?" in card.get("next_questions_for_user", [])
        # citations absent (B failed) — card still valid
        assert "direct_answer" in card


# ── Integration test: A/B routing in integrate.py ────────────────────────────

class TestIntegratorModeRouting:
    def test_sequential_path_sets_mode_S(self):
        from app.stages.integrate import run_integrate
        ctx = _make_ctx()

        with (
            patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "sequential"}),
            patch("app.stages.integrate.format_response") as mock_seq,
            patch("app.stages.integrate.format_response_parallel") as mock_par,
        ):
            mock_seq.return_value = (VALID_CORE_CARD, {"stage": "integrator", "model": "m", "input_tokens": 0, "output_tokens": 0})
            run_integrate(ctx)

        assert ctx.integrator_mode == "S"
        mock_par.assert_not_called()
        mock_seq.assert_called_once()

    def test_parallel_path_sets_mode_P(self):
        from app.stages.integrate import run_integrate
        ctx = _make_ctx()

        fake_usage = {"stage": "integrator_a", "model": "m", "input_tokens": 0, "output_tokens": 0}

        with (
            patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "parallel"}),
            patch("app.stages.integrate.format_response") as mock_seq,
            patch("app.stages.integrate.format_response_parallel") as mock_par,
        ):
            mock_par.return_value = (VALID_CORE_CARD, [fake_usage])
            run_integrate(ctx)

        assert ctx.integrator_mode == "P"
        mock_seq.assert_not_called()
        mock_par.assert_called_once()

    def test_llm_performance_carries_integrator_mode(self):
        from app.stages.integrate import run_integrate
        ctx = _make_ctx()

        with (
            patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "sequential"}),
            patch("app.stages.integrate.format_response") as mock_seq,
        ):
            mock_seq.return_value = (VALID_CORE_CARD, {"stage": "integrator", "model": "m", "input_tokens": 0, "output_tokens": 0, "latency_ms": 200})
            run_integrate(ctx)

        assert ctx.response_payload is not None
        perf = ctx.response_payload.get("llm_performance", {})
        assert perf.get("integrator_mode") == "S"

    def test_parallel_pct_env(self):
        """MOBIUS_INTEGRATOR_PARALLEL_PCT=100 always picks parallel."""
        from app.stages.integrate import _pick_integrator_mode
        with patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "", "MOBIUS_INTEGRATOR_PARALLEL_PCT": "100"}):
            modes = {_pick_integrator_mode() for _ in range(20)}
        assert modes == {"parallel"}

    def test_default_is_sequential(self):
        from app.stages.integrate import _pick_integrator_mode
        with patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "", "MOBIUS_INTEGRATOR_PARALLEL_PCT": "0"}):
            modes = {_pick_integrator_mode() for _ in range(10)}
        assert modes == {"sequential"}
