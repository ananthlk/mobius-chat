"""Ananth, 2026-09-15: "many of the claude model prior costs etc are
missing in the model registry." Two separate pricing tables exist for
Anthropic models -- app/services/model_registry.py's ModelSpec fields
(the router's priors) and app/services/cost_model.py's _DEFAULT_RATES
(what compute_cost() actually bills real usage against) -- and they had
drifted apart with no test on either catching it.

cost_model.py's get_rates() has a prefix-match fallback for versioned
model names, but that only helps when some OTHER key in the table is a
prefix of the requested model_id. claude-opus-4-8 and claude-fable-5-1
had no entry and no key that was a prefix of either -- so every real
call to them was silently costed at $0.00 (the (0.0, 0.0) miss
fallback), with no error, warning, or log line anywhere. This is the
same "producer without a consumer" shape as other silent-zero bugs in
this codebase: the write path (bill this call) looked fine because
nothing raised, but the number it wrote was structurally wrong for two
specific models.
"""
from __future__ import annotations

from app.services.cost_model import compute_cost, get_rates


def test_every_anthropic_model_in_model_registry_has_a_cost_model_rate():
    # The two pricing tables must not drift apart silently -- every
    # enabled-or-not Anthropic ModelSpec needs a real (non-zero) rate in
    # cost_model.py, or real usage of that model bills as free.
    from app.services.model_registry import MODEL_ROSTER

    for model_id, spec in MODEL_ROSTER.items():
        if spec.provider != "anthropic":
            continue
        in_rate, out_rate = get_rates("anthropic", model_id)
        assert (in_rate, out_rate) != (0.0, 0.0), (
            f"{model_id} has no cost_model.py rate -- real usage would be "
            f"billed as $0.00. Add an entry to _DEFAULT_RATES."
        )


def test_opus_4_8_and_fable_5_1_have_explicit_rates_not_relying_on_prefix_luck():
    # These two were the ones actually missing. Pin them explicitly so a
    # future refactor of the prefix-fallback logic can't quietly
    # reintroduce the $0.00 bug for exactly these two models again.
    assert get_rates("anthropic", "claude-opus-4-8") == (0.005000, 0.025000)
    assert get_rates("anthropic", "claude-fable-5-1") == (0.010000, 0.050000)


def test_opus_4_5_4_6_4_7_share_opus_5_rate():
    # Verified against Anthropic's live pricing page (claude.com/pricing,
    # 2026-09-15): the whole Opus 4.5+ lineage is uniformly $5/$25.
    opus_5_rate = get_rates("anthropic", "claude-opus-5")
    for model_id in ["claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5"]:
        assert get_rates("anthropic", model_id) == opus_5_rate, model_id


def test_compute_cost_is_nonzero_for_previously_missing_models():
    # The actual failure mode this fixes: a real usage record for one of
    # these models silently computing to $0.00.
    for model_id in ["claude-opus-4-8", "claude-fable-5-1"]:
        cost = compute_cost({
            "provider": "anthropic",
            "model": model_id,
            "input_tokens": 1000,
            "output_tokens": 1000,
        })
        assert cost > 0.0, f"{model_id} computed $0.00 for real usage"
