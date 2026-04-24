"""Bandit / Thompson helpers (MOBIUS_BANDIT_PRIORS_ONLY)."""
from __future__ import annotations

import os

import pytest

from app.services.model_registry import (
    CHEAP_STAGES,
    CREDENTIALING_SKILL_STAGES,
    INTEGRATOR_ROSTER_STAGE,
    MODEL_ROSTER,
    PHI_SAFE_STAGES,
    _bandit_priors_only,
    _bandit_stats_row,
    _build_bandit_state,
    composite_norm_caps_for_stage,
    composite_router_signal,
    composite_score_api_spec,
    composite_stage_bucket,
    per_call_router_composite,
    react_round_from_stage,
    vertex_roster_eligible_stages,
)


def test_vertex_only_for_credentialing_skill_and_roster_integrator() -> None:
    """Medicaid NPI report paths need 1M-class context; only Gemini 2.5 Pro/Flash advertise those stages."""
    v = vertex_roster_eligible_stages()
    assert INTEGRATOR_ROSTER_STAGE in v
    for st in CREDENTIALING_SKILL_STAGES:
        assert st in v
    for mid in ("gemini-2.5-pro", "gemini-2.5-flash"):
        spec = MODEL_ROSTER[mid]
        for st in CREDENTIALING_SKILL_STAGES + [INTEGRATOR_ROSTER_STAGE]:
            assert st in spec.eligible_stages
    cheap = set(CHEAP_STAGES)
    for mid, spec in MODEL_ROSTER.items():
        if mid in ("gemini-2.5-pro", "gemini-2.5-flash"):
            continue
        if set(spec.eligible_stages) == set(PHI_SAFE_STAGES):
            continue
        if set(spec.eligible_stages) <= cheap:
            continue
        for st in CREDENTIALING_SKILL_STAGES + [INTEGRATOR_ROSTER_STAGE]:
            assert st not in spec.eligible_stages, (mid, st)


def test_composite_score_api_spec_has_formula_and_stage_caps() -> None:
    spec = composite_score_api_spec()
    assert spec.get("formula")
    assert spec.get("weights")
    caps = spec.get("stage_caps")
    assert isinstance(caps, dict) and "planner" in caps
    assert "latency_cap_ms" in caps["planner"] and "cost_cap_usd" in caps["planner"]


def test_bandit_priors_only_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MOBIUS_BANDIT_PRIORS_ONLY", raising=False)
    assert _bandit_priors_only() is False
    monkeypatch.setenv("MOBIUS_BANDIT_PRIORS_ONLY", "1")
    assert _bandit_priors_only() is True


def test_beta_prior_uses_per_model_benchmark() -> None:
    """Each model's beta_prior uses its ema_quality (benchmark), not a constant."""
    pro = MODEL_ROSTER["gemini-2.5-pro"]
    flash = MODEL_ROSTER["gemini-2.5-flash"]
    lite = MODEL_ROSTER["gemini-2.0-flash-lite"]
    assert pro.ema_quality > flash.ema_quality > lite.ema_quality
    a_p, b_p = pro.beta_prior
    a_f, b_f = flash.beta_prior
    a_l, b_l = lite.beta_prior
    mean_p = a_p / (a_p + b_p)
    mean_f = a_f / (a_f + b_f)
    mean_l = a_l / (a_l + b_l)
    assert abs(mean_p - pro.ema_quality) < 0.01
    assert abs(mean_f - flash.ema_quality) < 0.01
    assert abs(mean_l - lite.ema_quality) < 0.01
    assert mean_p > mean_f > mean_l


def test_bandit_stats_row_strips_quality_when_priors_only(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MOBIUS_BANDIT_PRIORS_ONLY", "1")
    raw = {"quality_samples": 5000, "avg_quality": 0.4, "total_calls": 6000}
    row = _bandit_stats_row(raw)
    assert row["quality_samples"] == 0
    assert row["avg_quality"] is None
    assert row["total_calls"] == 6000


def test_build_bandit_state_uses_pure_prior_when_quality_stripped(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MOBIUS_BANDIT_PRIORS_ONLY", "1")
    spec = MODEL_ROSTER.get("gemini-2.5-pro")
    assert spec is not None
    fake_pg = {
        "quality_samples": 9999,
        "avg_quality": 0.1,
        "hard_error_rate": 0.0,
        "any_error_rate": 0.0,
        "total_calls": 10000,
    }
    row = _bandit_stats_row(fake_pg)
    st = _build_bandit_state(spec, row)
    assert abs(st.alpha - spec.beta_prior[0]) < 0.01
    assert abs(st.beta - spec.beta_prior[1]) < 0.01


def test_composite_router_signal_matches_view_formula() -> None:
    """Default (no explicit bandit_mode) uses the 'normal' weights profile.

    Weights refreshed 2026-04-24 (Sprint 2 #0.1): the bandit now weights
    quality higher than cost under ``normal`` to match the demo baseline
    observations. Keeps the linear-cap structure identical so the
    ``model_composite_scores`` SQL view stays consistent after we backfill.
    """
    from app.services.bandit_weights import get_weights
    w = get_weights("normal")
    stats = {
        "avg_quality": 0.8,
        "hard_error_rate": 0.05,
        "p95_latency_ms": 7500.0,
        "avg_cost_usd": 0.025,
    }
    comp, brk = composite_router_signal(stats)
    rel = max(0.0, 1.0 - 0.1)
    lat_f = max(0.0, 1.0 - 7500.0 / 15000.0)
    cost_f = max(0.0, 1.0 - 0.025 / 0.05)
    expected = 0.8 * w["quality"] + rel * w["reliability"] + lat_f * w["latency"] + cost_f * w["cost"]
    assert abs(comp - expected) < 1e-6
    assert abs(brk["term_quality"] - 0.8 * w["quality"]) < 1e-6
    assert abs(brk["term_reliability"] - rel * w["reliability"]) < 1e-6
    assert brk["bandit_mode"] == "normal"


def test_per_call_composite_uses_token_list_price_when_tokens_present() -> None:
    _, brk = per_call_router_composite(
        1000,
        999.0,
        0.5,
        True,
        stage="classifier",
        provider="vertex",
        model="gemini-2.5-flash",
        input_tokens=1000,
        output_tokens=500,
    )
    assert brk["cost_list_usd"] > 0
    assert brk["cost_metric_usd"] == brk["cost_list_usd"]
    assert brk["cost_usd_billed"] == 999.0


def test_react_rounds_have_distinct_increasing_caps() -> None:
    lat1, c1 = composite_norm_caps_for_stage("react_1")
    lat2, c2 = composite_norm_caps_for_stage("react_2")
    lat4, c4 = composite_norm_caps_for_stage("react_4")
    assert lat1 < lat2 < lat4
    assert c1 < c2 < c4
    assert composite_stage_bucket("react_1") == "react_1"
    assert composite_stage_bucket("react_4") == "react_4"
    assert react_round_from_stage("react_2") == 2


def test_react_round_clamped_past_max_uses_round_4_caps() -> None:
    lat_hi, _ = composite_norm_caps_for_stage("react_999")
    lat4, _ = composite_norm_caps_for_stage("react_4")
    assert lat_hi == lat4


def test_composite_planner_bucket_wider_latency_than_default() -> None:
    lat_def, _ = composite_norm_caps_for_stage("rag")
    lat_plan, _ = composite_norm_caps_for_stage("planner")
    assert lat_plan > lat_def
    stats = {
        "avg_quality": 0.5,
        "hard_error_rate": 0.0,
        "p95_latency_ms": 40000.0,
        "avg_cost_usd": 0.0,
    }
    _, brk_default = composite_router_signal(stats, stage="rag")
    _, brk_plan = composite_router_signal(stats, stage="planner")
    assert brk_default["term_latency"] < brk_plan["term_latency"]


def test_build_bandit_state_blends_total_calls_with_composite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOBIUS_BANDIT_PRIORS_ONLY", raising=False)
    spec = MODEL_ROSTER.get("gemini-2.5-pro")
    assert spec is not None
    stats = {
        "total_calls": 100,
        "quality_samples": 0,
        "avg_quality": None,
        "hard_error_rate": 0.0,
        "p95_latency_ms": 0.0,
        "avg_cost_usd": 0.0,
    }
    comp, _ = composite_router_signal(stats)
    st = _build_bandit_state(spec, stats)
    assert st.call_count == 100
    exp = max(0.01, min(0.99, float(comp)))
    assert abs(st.mean - exp) < 1e-5
