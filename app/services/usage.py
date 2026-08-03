"""Usage type for LLM token tracking (billing and cost-plus pricing)."""
from typing import TypedDict


class LLMUsageDict(TypedDict, total=False):
    """Per-call LLM usage: provider, model, input/output tokens."""
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    # Real, provider-verified source URLs (e.g. Perplexity's online-search
    # grounding). Optional — most providers never set this. Distinct from
    # a model's self-reported in-text citations, which are not verified.
    citations: list[str]


def usage_dict(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    citations: list[str] | None = None,
) -> LLMUsageDict:
    """Build a usage dict for a single LLM call."""
    d: LLMUsageDict = LLMUsageDict(
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    if citations:
        d["citations"] = citations
    return d


def zero_usage(provider: str = "", model: str = "") -> LLMUsageDict:
    """Usage with zero tokens (e.g. on error or unknown)."""
    return usage_dict(provider=provider, model=model, input_tokens=0, output_tokens=0)
