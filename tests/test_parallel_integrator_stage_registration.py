"""Task #20 (2026-08-12, Chat Master-approved): integrator_a/critic/enrichment
stage-registration gap.

The parallel integrator's 3 sub-stages (final_parallel.py's integrator_a/
integrator_critic/integrator_enrichment futures) were never registered in
any eligible_stages list, so _get_candidates returned empty and every call
fell through to the hard fallback_no_models("gemini-2.5-flash") -- not a
token-budget issue, a structural registration gap. Fix: a dedicated
PARALLEL_INTEGRATOR_STAGES list added to ONLY gemini-2.5-flash and
gemini-2.5-pro's eligible_stages -- flash-biased start (flash is the
incumbent with real quality history), bandit compares against a single
challenger, not opened to the full 15-model CORE_REASONING_STAGES pool.
"""
from __future__ import annotations

from app.services.model_registry import (
    CORE_REASONING_STAGES,
    MODEL_ROSTER,
    PARALLEL_INTEGRATOR_STAGES,
    ModelRouter,
)


def test_stage_names_match_final_parallel_literals():
    """Regression guard against a future rename in final_parallel.py drifting
    silently out of sync with this list."""
    assert PARALLEL_INTEGRATOR_STAGES == [
        "integrator_a",
        "integrator_critic",
        "integrator_enrichment",
    ]


def test_candidates_non_empty_for_all_three_stages():
    r = ModelRouter()
    for stage in PARALLEL_INTEGRATOR_STAGES:
        cands = r._get_candidates(stage, phi_detected=False)
        assert cands, f"{stage} has zero candidates -- would fall through to fallback_no_models"


def test_pool_is_exactly_flash_and_pro_not_full_roster():
    """The explicit constraint: flash-biased start, NOT the full
    unconstrained reasoning pool."""
    r = ModelRouter()
    for stage in PARALLEL_INTEGRATOR_STAGES:
        cands = {c.model_id for c in r._get_candidates(stage, phi_detected=False)}
        assert cands == {"gemini-2.5-flash", "gemini-2.5-pro"}


def test_parallel_integrator_stages_not_added_to_core_reasoning_stages():
    """The 3 stage names must NOT appear in CORE_REASONING_STAGES itself --
    that would silently open all 15 CORE_REASONING_STAGES models instead of
    the curated 2-model pool."""
    for stage in PARALLEL_INTEGRATOR_STAGES:
        assert stage not in CORE_REASONING_STAGES


def test_no_other_model_carries_these_stages():
    """Every other roster entry must be unaffected -- confirms the change is
    scoped to exactly the 2 intended models."""
    for model_id, spec in MODEL_ROSTER.items():
        if model_id in ("gemini-2.5-flash", "gemini-2.5-pro"):
            continue
        for stage in PARALLEL_INTEGRATOR_STAGES:
            assert stage not in spec.eligible_stages, (
                f"{model_id} unexpectedly eligible for {stage}"
            )
