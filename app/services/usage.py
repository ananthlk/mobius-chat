"""Usage type for LLM token tracking (billing and cost-plus pricing)."""
from typing import TypedDict


class LLMUsageDict(TypedDict, total=False):
    """Per-call LLM usage: provider, model, input/output tokens."""
    provider: str
    model: str
    input_tokens: int
    output_tokens: int


def usage_dict(provider: str, model: str, input_tokens: int, output_tokens: int) -> LLMUsageDict:
    """Build a usage dict for a single LLM call."""
    return LLMUsageDict(
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def zero_usage(provider: str = "", model: str = "") -> LLMUsageDict:
    """Usage with zero tokens (e.g. on error or unknown)."""
    return usage_dict(provider=provider, model=model, input_tokens=0, output_tokens=0)
