"""Communication Gate: single entry point for all user-facing messages.

Payload types: thinking | clarification | refinement_ask | final

- thinking: pass-through to append_thinking (no LLM)
- clarification: format via LLM, then append/publish
- refinement_ask: format via LLM, publish structured response
- final: stream to append_message_chunk, then publish_response
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from app.storage.progress import append_message_chunk, append_thinking

logger = logging.getLogger(__name__)

PayloadType = str  # "thinking" | "clarification" | "refinement_ask" | "final"


def send_to_user(
    correlation_id: str,
    payload: dict[str, Any],
    *,
    message_chunk_callback: Callable[[str], None] | None = None,
) -> None:
    """Route user-facing message by type. Single entry point for all outgoing communication.

    payload: { "type": "thinking"|"clarification"|"refinement_ask"|"final", "content": str, ... }
    - thinking: content = line(s) to show as thinking; pass-through to append_thinking
    - clarification: content = raw clarification text; optional "intent", "slots"; agent may format
    - refinement_ask: content = raw; optional "original", "suggestions"; agent may format
    - final: content = full message or chunk; if message_chunk_callback, stream chunk-by-chunk

    message_chunk_callback: used when type=final and we stream; gate passes chunks through.
    """
    ptype = (payload.get("type") or "").strip().lower()
    content = payload.get("content") or ""

    if ptype == "thinking":
        if content and str(content).strip():
            append_thinking(correlation_id, str(content).strip())
        return

    if ptype == "clarification":
        from app.communication.agent import format_clarification

        intent = payload.get("intent", "")
        slots = payload.get("slots") or []
        formatted = format_clarification(intent=intent, slots=slots, raw_message=content)
        append_message_chunk(correlation_id, formatted)
        return

    if ptype == "refinement_ask":
        from app.communication.agent import format_refinement_ask

        original = payload.get("original", "")
        suggestions = payload.get("suggestions") or []
        formatted = format_refinement_ask(original=original, suggestions=suggestions, raw_message=content)
        append_message_chunk(correlation_id, formatted)
        return

    if ptype == "final":
        if message_chunk_callback and content:
            message_chunk_callback(content)
        elif content:
            append_message_chunk(correlation_id, str(content))
        return

    logger.warning("Communication gate: unknown payload type=%r, treating as thinking", ptype)
    if content:
        append_thinking(correlation_id, str(content).strip())


def create_emitter(correlation_id: str) -> Callable[[str], None]:
    """Create an emitter that sends thinking chunks through the gate."""
    def emit(chunk: str) -> None:
        if chunk and str(chunk).strip():
            send_to_user(correlation_id, {"type": "thinking", "content": str(chunk).strip()})
    return emit
