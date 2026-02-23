"""Deterministic route triggers: explicit user phrases that force agent routing.

Confidence model:
- Single trigger, no clash → 1.0 (use route, override planner)
- Multiple triggers, same path → 1.0
- Multiple triggers, different paths (clash) → 0.0 (ask user to clarify via buttons)
- No trigger match → 0.0 (let planner/LLM decide)

Phase 1: Two paths only — web (tool) and RAG (our manual).
"""
from typing import Literal

AgentType = Literal["RAG", "tool"]

# Tool agent: web search + scrape + capability questions
TRIGGERS_WEB: tuple[str, ...] = (
    "search the web",
    "search google",
    "search for",
    "look up",
    "find on the internet",
    "scrape",
    "scrape this",
    "scrape url",
    "scrape page",
    "read this webpage",
    "read this url",
    "what can you do",
    "what can you help with",
    "your capabilities",
)

# RAG: our policy materials
TRIGGERS_RAG: tuple[str, ...] = (
    "check our materials",
    "check our manual",
    "look in the manual",
    "search our manual",
    "search our materials",
    "our docs",
    "policy lookup",
    "in our corpus",
)


def _matches(text: str, triggers: tuple[str, ...]) -> bool:
    """True if text contains any trigger (case-insensitive)."""
    t = (text or "").strip().lower()
    return any(tr in t for tr in triggers)


def detect_route(text: str) -> tuple[AgentType | None, float, list[dict] | None]:
    """Detect deterministic route from user message.

    Returns:
        (agent_override, confidence, clarify_choices)
        - agent_override: "tool" | "RAG" when confidence=1.0; None otherwise
        - confidence: 1.0 = use override; 0.0 = clash or no match
        - clarify_choices: when clash, list of {value, label} for buttons; else None
    """
    if not (text or "").strip():
        return (None, 0.0, None)

    has_web = _matches(text, TRIGGERS_WEB)
    has_rag = _matches(text, TRIGGERS_RAG)

    if has_web and has_rag:
        # Clash: both paths matched — ask user to clarify
        query = text.strip()
        return (
            None,
            0.0,
            [
                {"value": f"Search the web: {query}", "label": "Search web"},
                {"value": f"Search our manual: {query}", "label": "Search our manual"},
            ],
        )

    if has_web:
        return ("tool", 1.0, None)
    if has_rag:
        return ("RAG", 1.0, None)

    return (None, 0.0, None)
