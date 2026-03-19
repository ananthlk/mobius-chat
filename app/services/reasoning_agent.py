"""Reasoning agent: simple LLM-only path—no RAG, no retrieval. For conceptual questions, rationale, general explanation."""
import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

REASONING_SYSTEM = (
    "You are a helpful assistant. The user asked a question that does not require looking up documents. "
    "Provide a clear, concise explanation or answer using your general knowledge. "
    "Be accurate and helpful. If you're unsure, say so. Keep it conversational and not overly long."
)

SKILL_CONTEXT_SYSTEM = (
    "The following context is the output of a tool run (e.g. a credentialing report or NPI lookup). "
    "Answer the user's question using ONLY this context. Do not use general knowledge or search. "
    "If the context does not contain enough information to answer, say so briefly. "
    "Be concise and cite numbers/sections from the context when relevant."
)


def answer_reasoning(
    question: str,
    emitter=None,
    context: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Answer using pure LLM reasoning—no retrieval. Returns (answer_text, llm_usage).

    When context is provided (e.g. ACTIVE SKILL OUTPUT), answer from that context only.
    """
    try:
        from app.services.llm_provider import get_llm_provider

        provider = get_llm_provider()
        if context and context.strip():
            system = SKILL_CONTEXT_SYSTEM
            prompt = f"{system}\n\nContext:\n{context.strip()}\n\nUser question: {question}\n\nAnswer:"
        else:
            system = REASONING_SYSTEM
            prompt = f"{system}\n\nUser question: {question}\n\nAnswer:"
        raw, usage = asyncio.run(provider.generate_with_usage(prompt))
        answer = (raw or "").strip()
        if not answer:
            answer = "I'm not sure how to answer that. Could you rephrase or provide more context?"
        return (answer, usage)
    except Exception as e:
        logger.warning("Reasoning agent failed: %s", e, exc_info=True)
        return ("I had trouble generating an answer. Please try again.", None)
