"""Token-aware Thompson sampling (Phase 2.5).

Regression test for the production failure where ``gpt-oss-20b`` (8_000 TPM) was
picked by the bandit for a 9_000-token request, producing a Groq 413 and leaking
provider JSON into the chat UI.

The fix: filter candidates by context window + per-minute TPM budget BEFORE the
Thompson draw runs. Models that physically can't fit the request are trimmed from
the candidate set.
"""

from __future__ import annotations

import pytest

from app.services.model_registry import (
    CORE_REASONING_STAGES,
    MODEL_ROSTER,
    ModelSpec,
    _filter_by_token_budget,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _spec(
    model_id: str,
    *,
    context_k: int,
    tpm_limit: int | None,
    default_out: int = 1024,
    enabled: bool = True,
) -> ModelSpec:
    return ModelSpec(
        model_id=model_id,
        provider="groq",  # type: ignore[arg-type]
        display_name=model_id,
        enabled=enabled,
        hipaa_eligible=False,
        eligible_stages=list(CORE_REASONING_STAGES),
        spec_context_k=context_k,
        spec_tpm_limit=tpm_limit,
        default_max_output_tokens=default_out,
    )


# ── _filter_by_token_budget ──────────────────────────────────────────────────


class TestTokenBudgetFilter:
    def test_regression_gpt_oss_20b_8000_tpm_is_excluded_for_large_request(self):
        """The exact failure mode from production: 9_000-token request, 8_000 TPM model."""
        small = _spec("small-8k-tpm", context_k=131, tpm_limit=8_000)
        large = _spec("large-30k-tpm", context_k=131, tpm_limit=30_000)
        survivors, meta = _filter_by_token_budget(
            [small, large],
            estimated_prompt_tokens=8_900,
            expected_output_tokens=512,
        )
        assert small not in survivors, "8_000 TPM model should NOT survive a 9_000-token request"
        assert large in survivors
        assert meta["candidates_trimmed_by_tpm"] == 1
        assert meta["candidates_trimmed_by_context"] == 0

    def test_context_window_filter_runs_before_tpm_filter(self):
        """Context overflow is a hard physical limit; TPM is a quota. Context must trim first."""
        tiny = _spec("tiny-ctx", context_k=4, tpm_limit=None)  # 4k ctx, unlimited TPM
        survivors, meta = _filter_by_token_budget(
            [tiny],
            estimated_prompt_tokens=10_000,
            expected_output_tokens=512,
        )
        assert survivors == []
        assert meta["candidates_trimmed_by_context"] == 1
        assert meta["candidates_trimmed_by_tpm"] == 0

    def test_none_tpm_treated_as_unlimited(self):
        """Models with no declared TPM (e.g. Anthropic, Vertex) are kept — we can't rule them out."""
        unknown_tpm = _spec("vertex-gemini-pro", context_k=1000, tpm_limit=None)
        survivors, _ = _filter_by_token_budget(
            [unknown_tpm],
            estimated_prompt_tokens=50_000,
            expected_output_tokens=2_000,
        )
        assert unknown_tpm in survivors

    def test_safety_margin_applied(self):
        """A 5% safety margin guards against tokenizer mismatch between client and provider."""
        # Exact-fit model (7_999 TPM) must be trimmed because margin pushes us over 8_000.
        exact = _spec("exact-fit", context_k=131, tpm_limit=7_999)
        survivors, meta = _filter_by_token_budget(
            [exact],
            estimated_prompt_tokens=7_000,
            expected_output_tokens=500,
        )
        # 7_500 * 1.05 = 7_875 → fits inside 7_999 TPM.
        assert exact in survivors
        # But a tighter budget: 8_000 * 1.05 = 8_400 → does NOT fit.
        tight = _spec("tight", context_k=131, tpm_limit=8_000)
        survivors2, _ = _filter_by_token_budget(
            [tight],
            estimated_prompt_tokens=7_500,
            expected_output_tokens=500,
        )
        assert tight not in survivors2

    def test_expected_output_tokens_defaults_to_spec(self):
        """If caller doesn't pass expected_output_tokens, use the ModelSpec default."""
        s = _spec("m", context_k=131, tpm_limit=8_000, default_out=2_000)
        # Prompt 5_000 + default 2_000 = 7_000 → *1.05 = 7_350 → fits in 8_000 TPM.
        survivors, _ = _filter_by_token_budget(
            [s],
            estimated_prompt_tokens=5_000,
            expected_output_tokens=None,
        )
        assert s in survivors
        # Same prompt but default_out=6_000 → 11_000 * 1.05 = 11_550 → does NOT fit.
        big_out = _spec("big-out", context_k=131, tpm_limit=8_000, default_out=6_000)
        survivors2, _ = _filter_by_token_budget(
            [big_out],
            estimated_prompt_tokens=5_000,
            expected_output_tokens=None,
        )
        assert big_out not in survivors2

    def test_empty_input_returns_empty(self):
        survivors, meta = _filter_by_token_budget(
            [], estimated_prompt_tokens=1_000, expected_output_tokens=100
        )
        assert survivors == []
        assert meta["candidates_trimmed_by_context"] == 0
        assert meta["candidates_trimmed_by_tpm"] == 0

    def test_meta_keys_present(self):
        s = _spec("m", context_k=131, tpm_limit=30_000)
        _, meta = _filter_by_token_budget(
            [s],
            estimated_prompt_tokens=1_000,
            expected_output_tokens=200,
        )
        assert "estimated_prompt_tokens" in meta
        assert "expected_output_tokens" in meta
        assert "request_tokens" in meta
        assert "candidates_trimmed_by_context" in meta
        assert "candidates_trimmed_by_tpm" in meta
        assert meta["estimated_prompt_tokens"] == 1_000
        assert meta["expected_output_tokens"] == 200


# ── MODEL_ROSTER sanity ──────────────────────────────────────────────────────


class TestRosterTpmLimits:
    def test_groq_failing_models_have_tpm_declared(self):
        """The two models that produced the production 413/429 must have TPM populated."""
        assert MODEL_ROSTER["openai/gpt-oss-20b"].spec_tpm_limit == 8_000
        assert MODEL_ROSTER["llama-3.3-70b-versatile"].spec_tpm_limit == 12_000

    def test_groq_tpm_is_below_context_window_in_tokens(self):
        """Smoke: if TPM ≥ context*1000, the TPM filter is redundant. For Groq free tier it isn't."""
        for mid in ("openai/gpt-oss-20b", "llama-3.3-70b-versatile", "openai/gpt-oss-120b"):
            spec = MODEL_ROSTER[mid]
            if spec.spec_tpm_limit is not None:
                assert spec.spec_tpm_limit < spec.spec_context_k * 1000, (
                    f"{mid}: TPM {spec.spec_tpm_limit} ≥ ctx {spec.spec_context_k}k — "
                    "context filter alone would be enough; TPM filter adds value only "
                    "when TPM is the tighter constraint."
                )

    def test_vertex_and_anthropic_tpm_unknown(self):
        """Providers without declared per-minute limits in our registry should keep TPM None."""
        for mid in ("gemini-2.5-pro", "gemini-2.5-flash", "claude-sonnet-4-6"):
            if mid in MODEL_ROSTER:
                assert MODEL_ROSTER[mid].spec_tpm_limit is None

    def test_default_max_output_tokens_set(self):
        """Every spec must have a sensible default (1024 is the registry fallback)."""
        for mid, spec in MODEL_ROSTER.items():
            assert spec.default_max_output_tokens >= 256, f"{mid}: too-small output default"


# ── select() integration ─────────────────────────────────────────────────────


class TestSelectIntegration:
    def test_select_without_estimated_tokens_is_backwards_compatible(self):
        """Legacy callers (no token hint) should see identical candidate lists."""
        from app.services.model_registry import ModelRouter

        router = ModelRouter()
        # Not all stages have candidates in every env; just smoke-test that it doesn't
        # error and still returns a spec when estimated_prompt_tokens is omitted.
        spec, meta = router.select(stage="planner")
        assert spec is not None
        assert "estimated_prompt_tokens" not in meta  # filter didn't run
        assert "candidates_trimmed_by_tpm" not in meta

    def test_select_with_estimated_tokens_records_meta(self):
        """Passing estimated_prompt_tokens should surface the budget fields in meta."""
        from app.services.model_registry import ModelRouter

        router = ModelRouter()
        spec, meta = router.select(
            stage="planner",
            estimated_prompt_tokens=2_000,
            expected_output_tokens=500,
        )
        assert spec is not None
        assert meta.get("estimated_prompt_tokens") == 2_000
        assert meta.get("expected_output_tokens") == 500
        assert "request_tokens" in meta
        assert "candidates_trimmed_by_context" in meta
        assert "candidates_trimmed_by_tpm" in meta
