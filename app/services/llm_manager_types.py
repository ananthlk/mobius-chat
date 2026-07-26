"""Shared dataclasses for the LLMManager (Chat pipeline refactor).

Public contract types used across PromptManager / ConfigManager / CallManager.
Authoritative spec: docs/SPEC_LLM_MANAGER.md §1.1 / §1.3 / §2.2.

EngagementContext note (build-order seam): Chat Architecture owns
TurnProfile/EngagementContext, but that module does not exist yet. This
frozen dataclass is the ratified §1.3 shape (DQ-3 CLOSED). When Chat
Architecture's TurnProfile lands, EngagementContext must resolve to a SINGLE
shared definition (they import from here, or this re-exports theirs) — do not
let two definitions drift. Tag-match only reads the five string attributes,
so any object exposing them satisfies PromptManager.
"""
from __future__ import annotations

from dataclasses import dataclass

# The five tag-match axes, in a fixed order. variant_tags in prompt_templates
# is keyed by these names. Unknown/absent values never raise — they fall
# through PromptManager's fallback chain (§3.1).
TAG_AXES: tuple[str, ...] = ("voice", "format", "autonomy", "intent", "caller_mode")


@dataclass(frozen=True)
class EngagementContext:
    """Tag source for prompt-variant selection (DQ-3 CLOSED — authoritative axes).

    voice:       "clinical" | "operational" | "executive" | "general"
    format:      "table" | "chart" | "bullets" | "prose" | "bars"
    autonomy:    "agentic" | "copilot"
    intent:      "explore" | "decide" | "retrieve" | "compare" | "act"
    caller_mode: "real_time" | "background" | "batch"

    Values are not validated here — unknown values are tolerated and fall
    through the tag-match fallback chain. The vocabularies are documented for
    authors; enforcement (if any) lives with the EngagementContext producer.
    """

    voice: str
    format: str
    autonomy: str
    intent: str
    caller_mode: str

    def as_tags(self) -> dict[str, str]:
        """The 5-axis tag dict, for matching against prompt_templates.variant_tags."""
        return {
            "voice": self.voice,
            "format": self.format,
            "autonomy": self.autonomy,
            "intent": self.intent,
            "caller_mode": self.caller_mode,
        }


@dataclass(frozen=True)
class LLMConfig:
    """Resolved per-module LLM config (ConfigManager output).

    model_id is None when the bandit owns model selection (DQ-4); non-None is a
    hard-pin escape hatch (the turn is then is_hard_pinned and excluded from
    model-arm training). Context-driven overrides (thinking → higher temp,
    clinical → lower) are already applied by ConfigManager when this is built.
    """

    module_key: str
    model_id: str | None
    temperature: float
    max_tokens: int
    top_p: float | None
    stop_sequences: tuple[str, ...] | None
    timeout_ms: int
    fallback_model: str | None


@dataclass(frozen=True)
class LLMResponse:
    """Public return contract of ``llm.call()`` (§1.1). All 8 fields populated
    on every successful call. ``tokens_used`` is the sum of input + output
    (the split is retained in llm_call_log; DQ-5)."""

    content: str
    model_used: str
    template_id: int
    variant_id: str
    call_id: str
    latency_ms: int
    tokens_used: int
    cost_usd: float
