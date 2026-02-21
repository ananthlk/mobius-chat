"""LLM-backed messaging for clarification and refinement asks.

Used by the Communication Gate when type=clarification or type=refinement_ask.
Can optionally use LLM to humanize messages; falls back to raw when LLM unavailable.

Clarification and refinement responses are wrapped as AnswerCard JSON for consistent
styling with final answers (same bubble/card appearance in the frontend).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def wrap_as_answer_card(direct_answer: str, mode: str = "FACTUAL") -> str:
    """Wrap plain text as AnswerCard JSON for consistent frontend styling."""
    return json.dumps({
        "mode": mode,
        "direct_answer": (direct_answer or "").strip(),
        "sections": [],
    })


def format_clarification(
    intent: str = "",
    slots: list[str] | None = None,
    raw_message: str = "",
    *,
    as_answer_card: bool = True,
) -> str:
    """Format a jurisdiction/clarification prompt via LLM for natural tone; fallback to raw_message.
    Returns AnswerCard JSON when as_answer_card=True for consistent styling with final answers."""
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
            out = str(text).strip()
            return wrap_as_answer_card(out) if as_answer_card else out
    except Exception as e:
        logger.debug("format_clarification LLM fallback: %s", e)

    out = draft or f"To give you an accurate answer, could you please specify {slot_desc}?"
    return wrap_as_answer_card(out) if as_answer_card else out


def format_refinement_ask(
    original: str = "",
    suggestions: list[str] | None = None,
    raw_message: str = "",
    *,
    as_answer_card: bool = True,
) -> str:
    """Format a query refinement prompt. Uses LLM when configured; else returns raw_message or a simple template.
    Returns AnswerCard JSON when as_answer_card=True for consistent styling with final answers."""
    if raw_message and str(raw_message).strip():
        out = str(raw_message).strip()
        return wrap_as_answer_card(out) if as_answer_card else out

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
            out = str(text).strip()
            return wrap_as_answer_card(out) if as_answer_card else out
    except Exception as e:
        logger.debug("format_refinement_ask LLM fallback: %s", e)

    if suggestions:
        out = f"Did you mean: {suggestions[0]}? Or would you like to rephrase your question?"
    else:
        out = "Could you rephrase your question so I can give you a more accurate answer?"
    return wrap_as_answer_card(out) if as_answer_card else out
