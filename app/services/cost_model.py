"""Cost model for LLM usage: map (provider, model) to $/1K tokens for cost-plus pricing."""
from typing import Any

from app.services.usage import LLMUsageDict

# Default $ per 1K tokens (input, output). Source: Vertex AI / provider pricing; Ollama = 0 (local).
_DEFAULT_RATES: dict[tuple[str, str], tuple[float, float]] = {
    ("vertex", "gemini-2.5-flash"): (0.000075, 0.0003),  # ~$0.075/1M in, ~$0.30/1M out
    ("vertex", "gemini-2.0-flash"): (0.0001, 0.0004),
    ("vertex", "gemini-1.5-flash"): (0.000075, 0.0003),
    ("vertex", "gemini-1.5-pro"): (0.00125, 0.005),
    # Ollama: local, no API cost
    ("ollama", "llama3.1:8b"): (0.0, 0.0),
    ("ollama", "llama3.2:3b"): (0.0, 0.0),
}


def get_rates(provider: str, model: str) -> tuple[float, float]:
    """Return (input_usd_per_1k, output_usd_per_1k) for (provider, model)."""
    key = ((provider or "").lower().strip(), (model or "").strip())
    return _DEFAULT_RATES.get(key, (0.0, 0.0))


def compute_cost(usage: LLMUsageDict) -> float:
    """Compute cost in USD for a single LLM usage. Unknown (provider, model) -> 0."""
    provider = (usage.get("provider") or "").strip()
    model = (usage.get("model") or "").strip()
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    in_rate, out_rate = get_rates(provider, model)
    return (input_tokens / 1000.0) * in_rate + (output_tokens / 1000.0) * out_rate


def register_rate(provider: str, model: str, input_usd_per_1k: float, output_usd_per_1k: float) -> None:
    """Register or override (provider, model) rate for cost calculation."""
    key = ((provider or "").lower().strip(), (model or "").strip())
    if key:
        _DEFAULT_RATES[key] = (float(input_usd_per_1k), float(output_usd_per_1k))
