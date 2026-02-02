"""Final responder: turn plan + answers into one chat-friendly message via LLM (or fallback). Can stream the draft via message_chunk_callback."""
import asyncio
import logging
from collections.abc import Callable

from app.planner.schemas import Plan
from app.services.usage import LLMUsageDict
from app.trace_log import trace_entered

logger = logging.getLogger(__name__)


def _emit(emitter: Callable[[str], None] | None, msg: str) -> None:
    if emitter and msg.strip():
        emitter(msg.strip())


def _fallback_message(plan: Plan, stub_answers: list[str]) -> str:
    """Simple concatenation without internal labels or repeated questions. Plain paragraphs."""
    parts: list[str] = []
    for i, sq in enumerate(plan.subquestions):
        ans = stub_answers[i] if i < len(stub_answers) else "[No answer yet]"
        parts.append(ans.strip())
    return "\n\n".join(p for p in parts if p)


async def _stream_integrator(
    prompt: str,
    message_chunk_callback: Callable[[str], None],
) -> str:
    """Stream integrator LLM output; call message_chunk_callback for each chunk. Returns full text."""
    from app.services.llm_provider import get_llm_provider
    provider = get_llm_provider()
    full: list[str] = []
    async for chunk in provider.stream_generate(prompt):
        if chunk:
            full.append(chunk)
            message_chunk_callback(chunk)
    return "".join(full)


def format_response(
    plan: Plan,
    stub_answers: list[str],
    user_message: str,
    emitter: Callable[[str], None] | None = None,
    message_chunk_callback: Callable[[str], None] | None = None,
) -> tuple[str, LLMUsageDict | None]:
    """Turn plan + answers into one chat-friendly message via LLM. If message_chunk_callback is set, stream the draft; returns (message, None) for usage when streaming. On LLM failure, returns fallback and None usage."""
    trace_entered("responder.final.format_response", subquestions=len(plan.subquestions))
    if not plan.subquestions:
        return ("", None)

    _emit(emitter, "Formatting the response for chat…")
    usage: LLMUsageDict | None = None

    try:
        from app.chat_config import get_chat_config
        from app.services.llm_provider import get_llm_provider

        cfg = get_chat_config()
        answers_block = "\n\n".join(
            f"Answer {i + 1}:\n{(stub_answers[i] if i < len(stub_answers) else '[No answer yet]').strip()}"
            for i in range(len(plan.subquestions))
        )
        prompt_system = cfg.prompts.integrator_system
        prompt_user = cfg.prompts.integrator_user_template.format(
            user_message=user_message.strip(),
            answers_block=answers_block,
        )
        prompt = f"{prompt_system}\n\n{prompt_user}"

        if message_chunk_callback:
            text = asyncio.run(_stream_integrator(prompt, message_chunk_callback))
            text = (text or "").strip()
            if text:
                _emit(emitter, "Done.")
                return (text, None)
        else:
            provider = get_llm_provider()
            text, usage = asyncio.run(provider.generate_with_usage(prompt))
            text = (text or "").strip()
            if text:
                _emit(emitter, "Done.")
                return (text, usage)
    except Exception as e:
        logger.warning("Integrator LLM failed, using fallback: %s", e)
        _emit(emitter, "Using simple format.")

    return (_fallback_message(plan, stub_answers), None)
