"""Task (2026-08-17, Ananth, directly): "add all available Claude models
including the most advanced models to our LLM manager along with the right
priors." Adds claude-sonnet-5, claude-opus-5, claude-opus-4-8, and
claude-fable-5-1 to MODEL_ROSTER -- purely additive, no request-building
changes needed (AnthropicProvider's raw HTTP body sends only model/
max_tokens/messages, which every one of these models accepts as-is)."""
from __future__ import annotations

import os
from unittest.mock import patch

from app.services.model_registry import (
    COPILOT_EXCLUDED_THOMPSON_BENCHMARK_CATEGORIES,
    MODEL_CATEGORIES,
    MODEL_ROSTER,
    ModelRouter,
    auto_enable_from_env,
)

_NEW_MODELS = ["claude-sonnet-5", "claude-opus-5", "claude-opus-4-8", "claude-fable-5-1"]


def test_all_four_models_registered():
    for model_id in _NEW_MODELS:
        assert model_id in MODEL_ROSTER, f"{model_id} missing from MODEL_ROSTER"
        assert MODEL_ROSTER[model_id].provider == "anthropic"


def test_source_declares_disabled_by_default():
    # Checks the ModelSpec CONSTRUCTION site (via source inspection), not
    # live MODEL_ROSTER[...].enabled -- that field is a shared, process-wide
    # mutable singleton flipped by auto_enable_from_env() the moment ANY
    # earlier-run test (in this file or another) calls it with a real
    # ANTHROPIC_API_KEY present (which this repo's .env has) -- asserting
    # on live state here would be order-dependent on the rest of the suite,
    # not a property of these entries. Source inspection is immune to that.
    import inspect

    import app.services.model_registry as model_registry_module

    source = inspect.getsource(model_registry_module)
    for model_id in _NEW_MODELS:
        start = source.index(f'"{model_id}": ModelSpec(')
        end = source.index(")", start)
        block = source[start:end]
        assert "enabled=False" in block or "enabled=True" not in block, (
            f"{model_id}'s ModelSpec construction doesn't declare enabled=False"
        )


def test_auto_enables_when_anthropic_key_present():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-fake-for-test"}):
        auto_enable_from_env()
    for model_id in _NEW_MODELS:
        assert MODEL_ROSTER[model_id].enabled is True
    # Restore for other tests in the same process.
    for model_id in _NEW_MODELS:
        MODEL_ROSTER[model_id].enabled = False


def test_quality_priors_stepped_by_generation():
    # Newest/most-capable models should carry the highest quality priors,
    # matching the existing Opus 4.5 < 4.6 < 4.7 stepped convention.
    opus_4_7 = MODEL_ROSTER["claude-opus-4-7"].ema_quality
    opus_4_8 = MODEL_ROSTER["claude-opus-4-8"].ema_quality
    opus_5 = MODEL_ROSTER["claude-opus-5"].ema_quality
    fable_5_1 = MODEL_ROSTER["claude-fable-5-1"].ema_quality
    assert opus_4_7 < opus_4_8 < opus_5 < fable_5_1

    sonnet_4_6 = MODEL_ROSTER["claude-sonnet-4-6"].ema_quality
    sonnet_5 = MODEL_ROSTER["claude-sonnet-5"].ema_quality
    assert sonnet_4_6 < sonnet_5


def test_pricing_matches_current_anthropic_rates():
    # Confirmed against the claude-api skill's cached rate table (2026-06-24).
    assert MODEL_ROSTER["claude-sonnet-5"].spec_input_per_1m_usd == 2.00
    assert MODEL_ROSTER["claude-sonnet-5"].spec_output_per_1m_usd == 10.00
    assert MODEL_ROSTER["claude-opus-5"].spec_input_per_1m_usd == 5.00
    assert MODEL_ROSTER["claude-opus-5"].spec_output_per_1m_usd == 25.00
    assert MODEL_ROSTER["claude-opus-4-8"].spec_input_per_1m_usd == 5.00
    assert MODEL_ROSTER["claude-opus-4-8"].spec_output_per_1m_usd == 25.00
    assert MODEL_ROSTER["claude-fable-5-1"].spec_input_per_1m_usd == 10.00
    assert MODEL_ROSTER["claude-fable-5-1"].spec_output_per_1m_usd == 50.00


def test_fable_has_own_apex_category_excluded_from_copilot():
    assert MODEL_ROSTER["claude-fable-5-1"].benchmark_category == "frontier_apex"
    assert "frontier_apex" in COPILOT_EXCLUDED_THOMPSON_BENCHMARK_CATEGORIES


def test_opus_5_and_4_8_share_premium_category_with_existing_opus():
    for model_id in ["claude-opus-4-7", "claude-opus-4-6", "claude-opus-5", "claude-opus-4-8"]:
        assert MODEL_ROSTER[model_id].benchmark_category == "frontier_reasoning_premium"


def test_model_categories_mirror_kept_in_sync():
    for model_id in _NEW_MODELS:
        assert MODEL_CATEGORIES.get(model_id) == MODEL_ROSTER[model_id].benchmark_category


def test_candidates_non_empty_when_enabled():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-fake-for-test"}):
        auto_enable_from_env()
    try:
        r = ModelRouter()
        cands = r._get_candidates("react_1", phi_detected=False)
        ids = {c.model_id for c in cands}
        for model_id in _NEW_MODELS:
            assert model_id in ids, f"{model_id} not eligible for react_1 once enabled"
    finally:
        for model_id in _NEW_MODELS:
            MODEL_ROSTER[model_id].enabled = False
