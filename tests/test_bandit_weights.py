"""Tests for mode-aware composite weights (Sprint 2 #0.1)."""
from __future__ import annotations

import pytest

from app.services.bandit_weights import (
    MODE_WEIGHTS,
    derive_bandit_mode,
    get_weights,
    weights_for_stage,
)
from app.services.model_registry import composite_router_signal


class TestDeriveBanditMode:
    def test_quick_maps_to_fast(self) -> None:
        assert derive_bandit_mode("quick") == "fast"

    def test_copilot_maps_to_normal(self) -> None:
        assert derive_bandit_mode("copilot") == "normal"

    def test_agentic_maps_to_thinking(self) -> None:
        assert derive_bandit_mode("agentic") == "thinking"

    def test_none_and_unknown_default_to_normal(self) -> None:
        assert derive_bandit_mode(None) == "normal"
        assert derive_bandit_mode("") == "normal"
        assert derive_bandit_mode("whatever") == "normal"

    def test_case_insensitive_and_whitespace_tolerant(self) -> None:
        assert derive_bandit_mode("  QUICK  ") == "fast"
        assert derive_bandit_mode("Agentic") == "thinking"


class TestGetWeights:
    def test_known_modes_sum_to_one(self) -> None:
        for mode in ("fast", "normal", "thinking"):
            w = get_weights(mode)
            assert abs(sum(w.values()) - 1.0) < 1e-9
            assert set(w.keys()) == {"quality", "reliability", "latency", "cost"}

    def test_fast_latency_dominates(self) -> None:
        w = get_weights("fast")
        assert w["latency"] > w["quality"]
        assert w["latency"] > w["cost"]

    def test_thinking_quality_dominates(self) -> None:
        w = get_weights("thinking")
        assert w["quality"] > w["latency"]
        assert w["quality"] > w["cost"]

    def test_unknown_mode_falls_back_to_normal(self) -> None:
        assert get_weights("garbage") == get_weights("normal")


class TestWeightsForStage:
    def test_fast_on_integrator_clamps_to_normal(self) -> None:
        w, mode = weights_for_stage("integrator", "fast")
        assert mode == "normal"
        assert w == get_weights("normal")

    def test_fast_on_critique_clamps_to_normal(self) -> None:
        _, mode = weights_for_stage("critique", "fast")
        assert mode == "normal"

    def test_fast_on_react_stays_fast(self) -> None:
        _, mode = weights_for_stage("react_1", "fast")
        assert mode == "fast"

    def test_thinking_on_integrator_stays_thinking(self) -> None:
        _, mode = weights_for_stage("integrator", "thinking")
        assert mode == "thinking"

    def test_unknown_stage_honors_requested_mode(self) -> None:
        _, mode = weights_for_stage("custom_stage", "fast")
        assert mode == "fast"


class TestCompositeRouterSignalModeAware:
    """Integration: composite_router_signal must reflect the weights."""

    _STATS_FAST_MODEL = {
        # Mocks llama-4-scout: mediocre quality, great latency, cheap.
        "avg_quality": 0.5,
        "hard_error_rate": 0.0,
        "p95_latency_ms": 1500.0,
        "avg_cost_usd": 0.0005,
    }
    _STATS_SLOW_QUALITY = {
        # Mocks claude-sonnet: great quality, poor latency, expensive.
        "avg_quality": 0.9,
        "hard_error_rate": 0.0,
        "p95_latency_ms": 20000.0,
        "avg_cost_usd": 0.02,
    }

    def test_fast_mode_favors_fast_model(self) -> None:
        c_fast_fast, _ = composite_router_signal(self._STATS_FAST_MODEL, stage="react_1", bandit_mode="fast")
        c_fast_quality, _ = composite_router_signal(self._STATS_SLOW_QUALITY, stage="react_1", bandit_mode="fast")
        assert c_fast_fast > c_fast_quality

    def test_thinking_mode_favors_quality_model(self) -> None:
        c_thinking_fast, _ = composite_router_signal(self._STATS_FAST_MODEL, stage="react_1", bandit_mode="thinking")
        c_thinking_quality, _ = composite_router_signal(self._STATS_SLOW_QUALITY, stage="react_1", bandit_mode="thinking")
        assert c_thinking_quality > c_thinking_fast

    def test_normal_mode_is_between_fast_and_thinking(self) -> None:
        # For the quality-heavy model, normal composite should fall between
        # fast (penalizes slow) and thinking (rewards quality).
        c_fast, _ = composite_router_signal(self._STATS_SLOW_QUALITY, stage="react_1", bandit_mode="fast")
        c_normal, _ = composite_router_signal(self._STATS_SLOW_QUALITY, stage="react_1", bandit_mode="normal")
        c_thinking, _ = composite_router_signal(self._STATS_SLOW_QUALITY, stage="react_1", bandit_mode="thinking")
        assert c_fast < c_normal < c_thinking

    def test_fast_on_integrator_uses_normal_weights(self) -> None:
        # Requesting fast on integrator must behave identically to normal.
        c_requested_fast, brk = composite_router_signal(
            self._STATS_SLOW_QUALITY, stage="integrator", bandit_mode="fast",
        )
        c_normal, _ = composite_router_signal(
            self._STATS_SLOW_QUALITY, stage="integrator", bandit_mode="normal",
        )
        assert c_requested_fast == c_normal
        assert brk["bandit_mode"] == "normal"

    def test_default_mode_matches_normal(self) -> None:
        c_default, _ = composite_router_signal(self._STATS_FAST_MODEL, stage="react_1")
        c_normal, _ = composite_router_signal(self._STATS_FAST_MODEL, stage="react_1", bandit_mode="normal")
        assert c_default == c_normal

    def test_breakdown_exposes_weights(self) -> None:
        _, brk = composite_router_signal(
            self._STATS_FAST_MODEL, stage="react_1", bandit_mode="fast",
        )
        assert "bandit_mode" in brk
        assert "bandit_weights" in brk
        assert abs(sum(brk["bandit_weights"].values()) - 1.0) < 1e-9


class TestModeWeightsShape:
    def test_all_modes_have_same_keys(self) -> None:
        ref = set(MODE_WEIGHTS["normal"].keys())
        for name, w in MODE_WEIGHTS.items():
            assert set(w.keys()) == ref, f"mode {name!r} missing keys"

    def test_all_weights_nonneg(self) -> None:
        for _, w in MODE_WEIGHTS.items():
            for k, v in w.items():
                assert v >= 0.0, f"{k} must be >= 0"
