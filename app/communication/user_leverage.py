"""User-as-leverage: prompts when stuck. Ask the user for documents, links, or knowledge we don't have.

See docs/RELENTLESS_CONTINUITY_PLAN.md §6.
"""
from __future__ import annotations

from typing import Literal

StuckReason = Literal["no_evidence", "missing_code", "conflicting_info", "partial_answer", "tool_failed"]


def format_user_ask(
    stuck_reason: StuckReason,
    sub_objective_text: str = "",
    answered_parts: list[str] | None = None,
) -> str:
    """Build a specific ask for the user when we're stuck."""
    sub = (sub_objective_text or "this").strip()
    if stuck_reason == "no_evidence":
        return (
            f"I couldn't find information about \"{sub}\" in our policy materials or the web. "
            "Do you have a document, link, or PDF that might contain this? I can read and summarize it for you."
        )
    if stuck_reason == "missing_code":
        return (
            "I couldn't find that specific code in our materials. "
            "Do you have a code list, CMS link, or payer handbook where it might be listed?"
        )
    if stuck_reason == "conflicting_info":
        return (
            "I found conflicting information. "
            "Do you have a more recent source or effective date we should prioritize?"
        )
    if stuck_reason == "partial_answer":
        answered = answered_parts or []
        if answered:
            parts = "; ".join(f'"{a[:60]}..."' if len(a) > 60 else f'"{a}"' for a in answered[:3])
            return (
                f"I was able to answer {parts}. However, I couldn't find \"{sub}\" "
                "in our materials or the web. Do you have any documents or links that might help?"
            )
        return (
            f"I couldn't find \"{sub}\" in our materials or the web. "
            "Do you have a document, link, or source that might help?"
        )
    if stuck_reason == "tool_failed":
        return (
            f"The search didn't return useful results for \"{sub}\". "
            "Do you have a direct link or alternative source I could try?"
        )
    return (
        f"I couldn't find \"{sub}\" in our materials. "
        "Do you have a document, link, or source that might help?"
    )
