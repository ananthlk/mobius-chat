"""LLM-backed messaging for clarification and refinement asks.

Used by the Communication Gate when type=clarification or type=refinement_ask.
Can optionally use LLM to humanize messages; falls back to raw when LLM unavailable.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


def format_clarification(
    intent: str = "",
    slots: list[str] | None = None,
    raw_message: str = "",
) -> str:
    """Format a jurisdiction/clarification prompt via LLM for natural tone; fallback to raw_message."""
    slots = slots or []
    slot_desc = ", ".join(s.replace("jurisdiction.", "") for s in slots) if slots else "jurisdiction"
    draft = str(raw_message or "").strip()

    try:
        from app.services.llm_provider import get_llm_provider

        provider = get_llm_provider()
        if draft:
            prompt = (
                "You are a helpful healthcare assistant. The user asked about filing an appeal or similar. "
                "We need to clarify which health plan before answering. Here is a draft prompt:\n"
                f"'{draft}'\n\n"
                "Rewrite it as a single, friendly, conversational sentence. "
                "Do not use markdown, bullets, or parentheses. Keep it under 25 words."
            )
        else:
            prompt = (
                "You are a helpful healthcare assistant. The user asked a question that requires clarification "
                f"before we can give an accurate answer. We need to know: {slot_desc}. "
                "Write a single, friendly, concise sentence asking the user to specify this. "
                "Do not use markdown or bullet points."
            )
        text, _ = asyncio.run(provider.generate_with_usage(prompt))
        if text and str(text).strip():
            return str(text).strip()
    except Exception as e:
        logger.debug("format_clarification LLM fallback: %s", e)

    return draft or f"To give you an accurate answer, could you please specify {slot_desc}?"


def format_refinement_ask(
    original: str = "",
    suggestions: list[str] | None = None,
    raw_message: str = "",
) -> str:
    """Format a query refinement prompt. Uses LLM when configured; else returns raw_message or a simple template."""
    if raw_message and str(raw_message).strip():
        return str(raw_message).strip()

    suggestions = suggestions or []

    try:
        from app.services.llm_provider import get_llm_provider

        provider = get_llm_provider()
        sugg_text = "\n".join(f"- {s}" for s in suggestions[:5]) if suggestions else "the same question rephrased"
        prompt = (
            "You are a helpful healthcare assistant. The user's question might be ambiguous. "
            f"Original: {original[:200]}\n\n"
            "Possible interpretations:\n"
            f"{sugg_text}\n\n"
            "Write a single, friendly sentence asking the user to confirm which they meant or to rephrase. "
            "Do not use markdown. Keep it under 2 sentences."
        )
        text, _ = asyncio.run(provider.generate_with_usage(prompt))
        if text and str(text).strip():
            return str(text).strip()
    except Exception as e:
        logger.debug("format_refinement_ask LLM fallback: %s", e)

    if suggestions:
        return f"Did you mean: {suggestions[0]}? Or would you like to rephrase your question?"
    return "Could you rephrase your question so I can give you a more accurate answer?"
