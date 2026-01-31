"""Worker: consume from request queue → planner (thinking) → store plan → LLM first-gen response → publish."""
import asyncio
import logging
import threading
from typing import Callable

from dotenv import load_dotenv
load_dotenv()  # load .env from project root (same credentials as Mobius RAG when using Vertex)

from app.planner import parse
from app.planner.schemas import Plan
from app.queue import get_queue
from app.responder import format_response
from app.storage import store_plan, store_response

logger = logging.getLogger(__name__)


def _stub_answer_for_subquestion(sq_id: str, kind: str, text: str) -> str:
    """Stub: return placeholder. Later: patient path = warning; non-patient path = RAG."""
    if kind == "patient":
        return "We don’t answer patient-specific questions yet. We can only answer policy and document questions."
    return f"[RAG answer for: {text[:60]}...] (stub)"


def _first_gen_llm_response(message: str, plan: Plan) -> tuple[str | None, str | None, str | None]:
    """Call LLM for a first-pass response. Returns (text, model_used, error). On failure: (None, None, error_msg)."""
    try:
        from app.chat_config import get_chat_config
        from app.services.llm_provider import get_llm_provider
        chat_cfg = get_chat_config()
        provider = get_llm_provider()
        model_used = getattr(provider, "model_name", None) or getattr(provider, "model", None) or chat_cfg.llm.model
        plan_summary = f"{len(plan.subquestions)} subquestion(s): " + "; ".join(
            f"{sq.id}={sq.kind}" for sq in plan.subquestions
        )
        prompt = chat_cfg.prompts.first_gen_user_template.format(
            message=message,
            plan_summary=plan_summary,
        )
        text = asyncio.run(provider.generate(prompt))
        return (text, model_used, None)
    except Exception as e:
        logger.warning("LLM first-gen failed, using stub: %s", e)
        return (None, None, str(e))


def process_one(correlation_id: str, payload: dict) -> None:
    """Process one request: plan → store → LLM first-gen (or stub) → final response → store + publish."""
    message = payload.get("message", "").strip()
    thinking_chunks: list[str] = []

    def on_thinking(chunk: str) -> None:
        thinking_chunks.append(chunk)
        logger.info("[thinking] %s", chunk[:80])

    plan = parse(message, thinking_emitter=on_thinking)
    store_plan(correlation_id, plan, thinking_log=thinking_chunks)

    logger.info("Calling LLM for first-gen response (message=%s)", message[:80])
    llm_text, model_used, llm_error = _first_gen_llm_response(message, plan)
    if llm_text and llm_text.strip():
        final_message = llm_text.strip()
        response_source = "llm"
        logger.info("LLM succeeded, model_used=%s", model_used)
    else:
        logger.warning("Using stub (llm_text empty or LLM failed). llm_error=%s", llm_error)
        stub_answers = [
            _stub_answer_for_subquestion(sq.id, sq.kind, sq.text)
            for sq in plan.subquestions
        ]
        final_message = format_response(plan, stub_answers)
        response_source = "stub"

    response_payload = {
        "status": "completed",
        "message": final_message,
        "plan": plan.model_dump(),
        "thinking_log": thinking_chunks,
        "response_source": response_source,
        "model_used": model_used if response_source == "llm" else None,
        "llm_error": llm_error if response_source == "stub" else None,
    }
    store_response(correlation_id, response_payload)
    get_queue().publish_response(correlation_id, response_payload)
    logger.info("Response published for %s", correlation_id)


def run_worker() -> None:
    """Blocking: consume requests and process each."""
    q = get_queue()
    q.consume_requests(process_one)


def start_worker_background() -> threading.Thread:
    """Start the worker in a background thread. Returns the thread. Use for in-memory queue (single process)."""
    t = threading.Thread(target=run_worker, daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    """Run worker standalone: python -m app.worker. Use with Redis (or other) queue so API and worker are separate."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [worker] %(levelname)s %(message)s")
    run_worker()
