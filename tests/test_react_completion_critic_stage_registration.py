"""Task #104 (2026-08-16, ReAct agent design doc §4/§6.4): react_completion_
critic stage registration. Same narrow-pool pattern as Task #20's
PARALLEL_INTEGRATOR_STAGES -- flash-biased start ({flash, pro}), not opened
to the full CORE_REASONING_STAGES pool, so this new stage doesn't fall
through to fallback_no_models the way integrator_a/critic/enrichment did
before #20 fixed the same class of gap."""
from __future__ import annotations

from app.services.model_registry import (
    MODEL_ROSTER,
    REACT_COMPLETION_CRITIC_STAGES,
    ModelRouter,
)


def test_stage_name_matches_design_doc():
    assert REACT_COMPLETION_CRITIC_STAGES == ["react_completion_critic"]


def test_candidates_non_empty():
    r = ModelRouter()
    cands = r._get_candidates("react_completion_critic", phi_detected=False)
    assert cands, "react_completion_critic has zero candidates -- would fall through to fallback_no_models"


def test_flash_and_pro_are_both_eligible():
    # Superset check, not an exact-set assertion -- local dev environments
    # may have other providers (e.g. Ollama) auto-enabled for a broader
    # pool via a separate mechanism unrelated to this stage's registration;
    # what matters here is that both intended narrow-pool models are in.
    flash = MODEL_ROSTER["gemini-2.5-flash"]
    pro = MODEL_ROSTER["gemini-2.5-pro"]
    assert "react_completion_critic" in flash.eligible_stages
    assert "react_completion_critic" in pro.eligible_stages
