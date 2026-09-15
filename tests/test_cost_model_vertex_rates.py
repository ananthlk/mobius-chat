"""Same shape as tests/test_cost_model_anthropic_rates.py, for the six new
Gemini 3.x model_registry.py entries added 2026-09-15. cost_model.py is
the table compute_cost() actually bills real usage against -- a second,
separate table from model_registry.py's ModelSpec priors -- so a new
model added to one and not the other silently bills real usage at $0.00.
"""
from __future__ import annotations

from app.services.cost_model import compute_cost, get_rates

_NEW_GEMINI_3X_MODELS = [
    "gemini-3.1-pro-preview",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]


def test_every_vertex_model_in_model_registry_has_a_cost_model_rate():
    from app.services.model_registry import MODEL_ROSTER

    for model_id, spec in MODEL_ROSTER.items():
        if spec.provider != "vertex":
            continue
        in_rate, out_rate = get_rates("vertex", model_id)
        assert (in_rate, out_rate) != (0.0, 0.0), (
            f"{model_id} has no cost_model.py rate -- real usage would be "
            f"billed as $0.00. Add an entry to _DEFAULT_RATES."
        )


def test_gemini_3x_rates_match_model_registry_pricing():
    # The two tables must agree, converted for units ($/1M vs $/1K).
    from app.services.model_registry import MODEL_ROSTER

    for model_id in _NEW_GEMINI_3X_MODELS:
        spec = MODEL_ROSTER[model_id]
        in_rate, out_rate = get_rates("vertex", model_id)
        assert in_rate == round(spec.spec_input_per_1m_usd / 1000, 6), model_id
        assert out_rate == round(spec.spec_output_per_1m_usd / 1000, 6), model_id


def test_compute_cost_is_nonzero_for_all_new_gemini_3x_models():
    for model_id in _NEW_GEMINI_3X_MODELS:
        cost = compute_cost({
            "provider": "vertex",
            "model": model_id,
            "input_tokens": 1000,
            "output_tokens": 1000,
        })
        assert cost > 0.0, f"{model_id} computed $0.00 for real usage"


def test_gemini_2_5_flash_and_pro_corrected_and_in_sync():
    # 2026-09-15: found THREE independently-wrong numbers for the same two
    # models across model_registry.py and cost_model.py while pricing the
    # Gemini 3.x additions -- both enabled=True and serving live traffic.
    # Verified against ai.google.dev's published rates.
    from app.services.model_registry import MODEL_ROSTER

    for model_id, expected in [
        ("gemini-2.5-flash", (0.30, 2.50)),
        ("gemini-2.5-pro", (1.25, 10.00)),
    ]:
        spec = MODEL_ROSTER[model_id]
        assert spec.spec_input_per_1m_usd == expected[0], model_id
        assert spec.spec_output_per_1m_usd == expected[1], model_id

        in_rate, out_rate = get_rates("vertex", model_id)
        assert in_rate == round(spec.spec_input_per_1m_usd / 1000, 6), model_id
        assert out_rate == round(spec.spec_output_per_1m_usd / 1000, 6), model_id
