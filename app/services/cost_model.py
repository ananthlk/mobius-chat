"""Cost model for LLM usage: map (provider, model) to $/1K tokens for cost-plus pricing."""
from typing import Any

from app.services.usage import LLMUsageDict

# Default $ per 1K tokens (input, output). Source: provider pricing pages Mar 2026.
# register_rate() can override at runtime for new models.
_DEFAULT_RATES: dict[tuple[str, str], tuple[float, float]] = {

    # ── GOOGLE VERTEX ─────────────────────────────────────────────────────────
    ("vertex", "gemini-2.5-flash"):      (0.000075, 0.000300),
    ("vertex", "gemini-2.5-pro"):        (0.001250, 0.005000),
    ("vertex", "gemini-2.0-flash"):      (0.000100, 0.000400),
    ("vertex", "gemini-2.0-flash-lite"): (0.000018, 0.000072),
    ("vertex", "gemini-1.5-flash"):      (0.000075, 0.000300),
    ("vertex", "gemini-1.5-pro"):        (0.001250, 0.005000),

    # ── GROQ (production) ─────────────────────────────────────────────────────
    ("groq", "llama-3.1-8b-instant"):    (0.000050, 0.000080),
    ("groq", "llama-3.3-70b-versatile"): (0.000590, 0.000790),
    ("groq", "openai/gpt-oss-120b"):     (0.000150, 0.000600),
    ("groq", "openai/gpt-oss-20b"):      (0.000075, 0.000300),
    # Groq preview
    ("groq", "qwen/qwen3-32b"):          (0.000290, 0.000590),
    ("groq", "meta-llama/llama-4-scout-17b-16e-instruct"): (0.000110, 0.000340),
    ("groq", "moonshotai/kimi-k2-instruct-0905"):          (0.001000, 0.003000),
    # Groq safety classifier — per 1M chars not tokens, approximated
    ("groq", "meta-llama/llama-prompt-guard-2-86m"):       (0.000040, 0.000040),
    ("groq", "meta-llama/llama-prompt-guard-2-22m"):       (0.000030, 0.000030),

    # ── ANTHROPIC ─────────────────────────────────────────────────────────────
    ("anthropic", "claude-sonnet-4-6"):          (0.003000, 0.015000),
    ("anthropic", "claude-haiku-4-5-20251001"):  (0.000800, 0.004000),

    # ── OPENAI ────────────────────────────────────────────────────────────────
    ("openai", "gpt-4o"):      (0.002500, 0.010000),
    ("openai", "gpt-4o-mini"): (0.000150, 0.000600),

    # ── TOGETHER.AI ───────────────────────────────────────────────────────────
    ("together", "meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo"): (0.000900, 0.000900),
    ("together", "Qwen/Qwen2.5-72B-Instruct-Turbo"):               (0.000560, 0.000560),
    ("together", "deepseek-ai/DeepSeek-V3"):                        (0.000270, 0.001100),

    # ── OLLAMA (local — zero cost) ────────────────────────────────────────────
    ("ollama", "llama3.1:8b"):   (0.0, 0.0),
    ("ollama", "llama3.2:3b"):   (0.0, 0.0),
    ("ollama", "mistral:7b"):    (0.0, 0.0),
    ("ollama", "phi4:14b"):      (0.0, 0.0),
}


def get_rates(provider: str, model: str) -> tuple[float, float]:
    """Return (input_usd_per_1k, output_usd_per_1k) for (provider, model)."""
    key = ((provider or "").lower().strip(), (model or "").strip())
    # Exact match first
    if key in _DEFAULT_RATES:
        return _DEFAULT_RATES[key]
    # Prefix match for versioned model names
    for (p, m), rates in _DEFAULT_RATES.items():
        if p == key[0] and key[1].startswith(m):
            return rates
    return (0.0, 0.0)


def compute_cost(usage: LLMUsageDict) -> float:
    """Compute cost in USD for a single LLM usage. Unknown (provider, model) -> 0."""
    provider     = (usage.get("provider") or "").strip()
    model        = (usage.get("model") or "").strip()
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    in_rate, out_rate = get_rates(provider, model)
    return (input_tokens / 1000.0) * in_rate + (output_tokens / 1000.0) * out_rate


def register_rate(
    provider: str,
    model: str,
    input_usd_per_1k: float,
    output_usd_per_1k: float,
) -> None:
    """Register or override (provider, model) rate for cost calculation."""
    key = ((provider or "").lower().strip(), (model or "").strip())
    if key:
        _DEFAULT_RATES[key] = (float(input_usd_per_1k), float(output_usd_per_1k))
