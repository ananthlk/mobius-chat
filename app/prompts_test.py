"""Build a single prompt by key and run one LLM call for config test-prompt endpoint."""
import asyncio
import json
import time
from typing import Any

from app.chat_config import get_chat_config
from app.services.llm_provider import get_llm_provider
from app.services.usage import LLMUsageDict


def _default_sample_input(prompt_key: str) -> dict[str, Any]:
    """Default sample inputs when frontend does not send them."""
    if prompt_key == "planner":
        return {"message": "What is prior authorization?"}
    if prompt_key == "rag_answering":
        return {
            "context": "(Sample context: Prior authorization is required for certain services. Check your plan documents.)",
            "question": "What is prior authorization?",
        }
    if prompt_key in ("integrator_factual", "integrator_canonical", "integrator_blended"):
        return {
            "consolidator_input_json": json.dumps({
                "user_message": "What is prior authorization?",
                "subquestions": [{"id": "sq1", "text": "What is prior authorization?"}],
                "answers": [{"sq_id": "sq1", "answer": "Prior authorization is a process where your doctor gets approval from your health plan before you receive certain services."}],
            }, indent=2),
        }
    if prompt_key == "first_gen":
        return {"message": "What is prior authorization?", "plan_summary": "One sub-question: What is prior authorization?"}
    return {}


def run_single_prompt_test(prompt_key: str, sample_input: dict[str, Any] | None) -> dict[str, Any]:
    """Build the one prompt for the given key with sample_input, run one LLM call, return output and usage.

    prompt_key: planner | rag_answering | integrator_factual | integrator_canonical | integrator_blended | first_gen
    sample_input: dict with message, context, question, consolidator_input_json, plan_summary as needed.
    Returns: { "output": str, "model_used": str | null, "duration_ms": int }
    """
    sample = sample_input if sample_input else _default_sample_input(prompt_key)
    if not sample:
        sample = _default_sample_input(prompt_key)
    cfg = get_chat_config()
    prompts = cfg.prompts

    if prompt_key == "planner":
        message = sample.get("message") or "What is prior authorization?"
        context = sample.get("context") or ""
        user = prompts.decompose_user_template.format(message=message, context=context)
        prompt = f"{prompts.decompose_system}\n\n{user}"
    elif prompt_key == "rag_answering":
        context = sample.get("context") or "(No context provided.)"
        question = sample.get("question") or "What is prior authorization?"
        prompt = prompts.rag_answering_user_template.format(context=context, question=question)
    elif prompt_key == "integrator_factual":
        consolidator_input_json = sample.get("consolidator_input_json")
        if isinstance(consolidator_input_json, dict):
            consolidator_input_json = json.dumps(consolidator_input_json, indent=2)
        if not consolidator_input_json:
            consolidator_input_json = _default_sample_input("integrator_factual")["consolidator_input_json"]
        user = prompts.integrator_user_template.format(consolidator_input_json=consolidator_input_json)
        prompt = f"{prompts.integrator_factual_system}\n\n{user}"
    elif prompt_key == "integrator_canonical":
        consolidator_input_json = sample.get("consolidator_input_json")
        if isinstance(consolidator_input_json, dict):
            consolidator_input_json = json.dumps(consolidator_input_json, indent=2)
        if not consolidator_input_json:
            consolidator_input_json = _default_sample_input("integrator_canonical")["consolidator_input_json"]
        user = prompts.integrator_user_template.format(consolidator_input_json=consolidator_input_json)
        prompt = f"{prompts.integrator_canonical_system}\n\n{user}"
    elif prompt_key == "integrator_blended":
        consolidator_input_json = sample.get("consolidator_input_json")
        if isinstance(consolidator_input_json, dict):
            consolidator_input_json = json.dumps(consolidator_input_json, indent=2)
        if not consolidator_input_json:
            consolidator_input_json = _default_sample_input("integrator_blended")["consolidator_input_json"]
        user = prompts.integrator_user_template.format(consolidator_input_json=consolidator_input_json)
        prompt = f"{prompts.integrator_blended_system}\n\n{user}"
    elif prompt_key == "first_gen":
        message = sample.get("message") or "What is prior authorization?"
        plan_summary = sample.get("plan_summary") or "One sub-question."
        user = prompts.first_gen_user_template.format(message=message, plan_summary=plan_summary)
        prompt = f"{prompts.first_gen_system}\n\n{user}"
    else:
        return {"output": "", "model_used": None, "duration_ms": 0, "error": f"Unknown prompt_key: {prompt_key}"}

    t0 = time.perf_counter()
    try:
        provider = get_llm_provider()
        text, usage = asyncio.run(provider.generate_with_usage(prompt))
        duration_ms = int((time.perf_counter() - t0) * 1000)
        model_used = (usage or {}).get("model") if isinstance(usage, dict) else None
        if usage is None:
            usage = {}
        if not isinstance(usage, dict):
            usage = {}
        return {
            "output": (text or "").strip(),
            "model_used": model_used or usage.get("model"),
            "duration_ms": duration_ms,
        }
    except Exception as e:
        duration_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "output": "",
            "model_used": None,
            "duration_ms": duration_ms,
            "error": str(e),
        }
