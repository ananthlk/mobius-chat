"""Ananth, 2026-09-15: relayed Google's Gemini Enterprise Agent Platform
retirement notice for gemini-2.5-flash/flash-lite/pro (project
mobius-os-dev, retiring in phases starting 2026-10-20) and asked "can we
test if the new models are available" -- then, once confirmed live,
"add all these models with the right priors and pricing... so that
bandit can start to learn about these models, including lite versions."

Every model_id below was verified reachable with a REAL generateContent
call against our own project (mobius-os-dev) before being added here --
not assumed from the retirement email's marketing names. That call also
surfaced the actual blocker: none of these respond on
location=us-central1 (our then-current VERTEX_LOCATION) -- only on
location=global. The existing 2.5 models also answer fine on global, so
.env's VERTEX_LOCATION was switched there (gitignored, dev-only; the
deployed Cloud Run env needs the same change before these entries are
live in production, or they repeat the exact "stale roster, drawn and
404ing forever" shape already found and deferred for three dead Groq
models on 2026-09-10 -- project-model-roster-deferred).
"""
from __future__ import annotations

from app.services.model_registry import MODEL_CATEGORIES, MODEL_ROSTER

_NEW_GEMINI_3X_MODELS = [
    "gemini-3.1-pro-preview",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]


def test_all_six_models_registered():
    for model_id in _NEW_GEMINI_3X_MODELS:
        assert model_id in MODEL_ROSTER, f"{model_id} missing from MODEL_ROSTER"
        assert MODEL_ROSTER[model_id].provider == "vertex"


def test_enabled_matching_existing_vertex_convention():
    # Unlike Anthropic/Groq (gated behind auto_enable_from_env on an API
    # key), Vertex models are enabled directly -- Vertex is "already
    # configured" per the section header above gemini-2.5-pro/flash.
    # Ananth explicitly asked for these to be live so the bandit can draw
    # them, matching that existing pattern rather than the provider-key gate.
    for model_id in _NEW_GEMINI_3X_MODELS:
        assert MODEL_ROSTER[model_id].enabled is True, model_id


def test_hipaa_eligible_matching_vertex_baa():
    # All existing Vertex Gemini entries are hipaa_eligible=True (the BAA
    # covers the platform). New Vertex-hosted first-party Gemini models
    # inherit that, same as gemini-2.5-pro/flash.
    for model_id in _NEW_GEMINI_3X_MODELS:
        assert MODEL_ROSTER[model_id].hipaa_eligible is True, model_id


def test_pricing_matches_verified_rates():
    # Verified against ai.google.dev's published per-token rates,
    # 2026-09-15. Vertex prices its own first-party Gemini models
    # identically to the Gemini API (documented Google policy) -- unlike
    # third-party Vertex partner models (e.g. Claude), which carry
    # separately negotiated Vertex rates.
    pro = MODEL_ROSTER["gemini-3.1-pro-preview"]
    assert pro.spec_input_per_1m_usd == 2.00
    assert pro.spec_output_per_1m_usd == 12.00

    for model_id in ["gemini-3.8-flash", "gemini-3.7-flash"]:
        spec = MODEL_ROSTER[model_id]
        # Both share an identical promotional rate through 2026-12-31.
        assert spec.spec_input_per_1m_usd == 0.75, model_id
        assert spec.spec_output_per_1m_usd == 3.75, model_id

    flash = MODEL_ROSTER["gemini-3.5-flash"]
    assert flash.spec_input_per_1m_usd == 1.50
    assert flash.spec_output_per_1m_usd == 9.00

    flash_lite_35 = MODEL_ROSTER["gemini-3.5-flash-lite"]
    assert flash_lite_35.spec_input_per_1m_usd == 0.30
    assert flash_lite_35.spec_output_per_1m_usd == 2.50

    flash_lite_31 = MODEL_ROSTER["gemini-3.1-flash-lite"]
    assert flash_lite_31.spec_input_per_1m_usd == 0.25
    assert flash_lite_31.spec_output_per_1m_usd == 1.50


def test_quality_priors_stepped_by_generation():
    # Provisional cold-start priors (no real telemetry exists yet) --
    # newer/pricier generations should carry the higher prior, matching
    # the existing stepped convention used for the Claude additions.
    pro_2_5 = MODEL_ROSTER["gemini-2.5-pro"].ema_quality
    pro_3_1 = MODEL_ROSTER["gemini-3.1-pro-preview"].ema_quality
    assert pro_2_5 < pro_3_1

    flash_2_5 = MODEL_ROSTER["gemini-2.5-flash"].ema_quality
    flash_3_5 = MODEL_ROSTER["gemini-3.5-flash"].ema_quality
    flash_3_7 = MODEL_ROSTER["gemini-3.7-flash"].ema_quality
    flash_3_8 = MODEL_ROSTER["gemini-3.8-flash"].ema_quality
    assert flash_2_5 < flash_3_5 < flash_3_7 < flash_3_8

    lite_2_0 = MODEL_ROSTER["gemini-2.0-flash-lite"].ema_quality
    lite_3_1 = MODEL_ROSTER["gemini-3.1-flash-lite"].ema_quality
    lite_3_5 = MODEL_ROSTER["gemini-3.5-flash-lite"].ema_quality
    assert lite_2_0 < lite_3_1 < lite_3_5


def test_pro_preview_excludes_stages_locked_to_gemini_2_5_pro():
    # rag_eval_adjudicate and rag_fact_check are deliberately LOCKED to
    # gemini-2.5-pro alone, for deterministic calibration/fact-check
    # scoring -- adding a second eligible model would break that lock.
    stages = MODEL_ROSTER["gemini-3.1-pro-preview"].eligible_stages
    assert "rag_eval_adjudicate" not in stages
    assert "rag_fact_check" not in stages
    # It should still share the rest of gemini-2.5-pro's broad reasoning
    # surface, or it can never actually be compared against it.
    assert "thread_summary" in stages


def test_lite_variants_use_fast_only_stages():
    from app.services.model_registry import FAST_ONLY_STAGES

    for model_id in ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]:
        assert MODEL_ROSTER[model_id].eligible_stages == FAST_ONLY_STAGES, model_id
        assert MODEL_ROSTER[model_id].benchmark_category == "tiny_classifier", model_id


def test_model_categories_mirror_kept_in_sync():
    for model_id in _NEW_GEMINI_3X_MODELS:
        assert MODEL_CATEGORIES.get(model_id) == MODEL_ROSTER[model_id].benchmark_category, model_id


def test_candidates_include_new_models_once_enabled():
    from app.services.model_registry import ModelRouter

    r = ModelRouter()
    cands = r._get_candidates("react_1", phi_detected=False)
    ids = {c.model_id for c in cands}
    for model_id in ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash", "gemini-3.1-pro-preview"]:
        assert model_id in ids, f"{model_id} not eligible for react_1"
