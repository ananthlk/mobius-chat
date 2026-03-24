"""
llm_manager.py — Single entry point for all LLM calls.
Sprint -1: wires ModelRouter for dynamic per-stage model selection.

Changes from previous version:
  - get_llm_provider(parser=parser) replaced by router.select()
  - stage parameter now actually drives model selection
  - phi_detected parameter added (passed from pipeline context)
  - ab_variant set to selected model_id for analytics
  - router.update_ema() called after each call
  - auto_enable_from_env() called on first generate()
"""
from __future__ import annotations

import time
from typing import Any

from app.services.llm_analytics import _write_async, build_record
from app.services.usage import LLMUsageDict, zero_usage

_env_checked = False


def _ensure_env() -> None:
    """Auto-enable models from API keys on first call."""
    global _env_checked
    if _env_checked:
        return
    _env_checked = True
    try:
        from app.services.model_registry import auto_enable_from_env
        auto_enable_from_env()
    except Exception:
        pass


def _provider_from_spec(spec) -> "Any":
    """Instantiate LLMProvider from ModelSpec."""
    from app.chat_config import get_chat_config
    from app.services.llm_provider import (
        VertexAIProvider, OllamaProvider, get_llm_provider,
    )

    c = get_chat_config().llm

    if spec.provider == "vertex":
        return VertexAIProvider(
            project_id=c.vertex_project_id,
            location=c.vertex_location,
            model=spec.model_id,
        )
    elif spec.provider == "ollama":
        return OllamaProvider(
            base_url=c.ollama_base_url,
            model=spec.model_id,
            num_predict=c.ollama_num_predict,
        )
    elif spec.provider == "groq":
        from app.services.llm_provider import GroqProvider
        return GroqProvider(model=spec.model_id)
    elif spec.provider == "anthropic":
        from app.services.llm_provider import AnthropicProvider
        return AnthropicProvider(model=spec.model_id)
    elif spec.provider == "together":
        from app.services.llm_provider import TogetherProvider
        return TogetherProvider(model=spec.model_id)
    else:
        # Unknown provider — fall back to configured default
        return get_llm_provider(parser=False)


async def generate(
    prompt: str,
    stage: str = "planner",
    max_tokens: int = 1000,
    config_sha: str | None = None,
    correlation_id: str | None = None,
    thread_id: str | None = None,
    parser: bool = False,
    phi_detected: bool = False,
    complexity: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """
    Call LLM via dynamic model router, record to llm_calls.
    Returns (text, usage_dict).
    usage_dict includes: model, provider, latency_ms, latency_s, stage.
    """
    _ensure_env()

    router = None
    spec = None
    provider: Any = None
    router_meta: dict[str, Any] | None = None

    # Select model via router
    try:
        from app.services.model_registry import get_router
        router = get_router()
        spec, router_meta = router.select(
            stage=stage,
            phi_detected=phi_detected,
            is_planner=parser,
        )
        provider = _provider_from_spec(spec)
    except Exception as e:
        # Router failure — fall back to default provider
        import logging
        logging.getLogger(__name__).warning(
            "ModelRouter.select() failed, using default: %s", e
        )
        from app.services.llm_provider import get_llm_provider
        provider = get_llm_provider(parser=parser)
        spec = None
        router_meta = {
            "mode": "router_error_fallback",
            "reason": f"ModelRouter raised an exception; using configured default provider. ({type(e).__name__})",
            "router_stage": stage,
        }

    t0 = time.perf_counter()
    usage: LLMUsageDict = zero_usage()
    text = ""
    success = False
    error_type: str | None = None

    try:
        text, usage = await provider.generate_with_usage(
            prompt, max_tokens=max_tokens, stage=stage
        )
        success = True
    except Exception as e:
        error_type = type(e).__name__
        usage = zero_usage()
        raise
    finally:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        model_id   = spec.model_id if spec else (usage.get("model") or "unknown")
        provider_n = spec.provider if spec else (usage.get("provider") or "unknown")
        cost_usd   = float(usage.get("cost_usd") or 0.0)

        # Update router EMA (quality score added later by adjudicator)
        if spec and router is not None:
            try:
                router.update_ema(
                    model_id=model_id,
                    latency_ms=latency_ms,
                    cost_usd=cost_usd,
                )
            except Exception:
                pass

        # Analytics record
        is_ab = (getattr(spec, "call_count", 0) < 100) if spec else False
        record = build_record(
            model=model_id,
            provider=provider_n,
            stage=stage,
            success=success,
            prompt=prompt,
            output_text=text,
            usage=usage,
            latency_ms=latency_ms,
            config_sha=config_sha,
            correlation_id=correlation_id,
            thread_id=thread_id,
            is_ab_call=is_ab,
            ab_variant=model_id,
            complexity=complexity,
            phi_detected=phi_detected,
            error_type=error_type,
        )
        # Await write so asyncio.run(generate_sync(...)) finishes DB insert before closing the loop
        # (write_record(..., create_task) would leave work pending and lose the row).
        try:
            await _write_async(record)
        except Exception:
            pass

        out_usage = dict(usage)
        out_usage["latency_ms"] = latency_ms
        out_usage["latency_s"]  = round(latency_ms / 1000.0, 2)
        out_usage["model"]      = model_id
        out_usage["provider"]   = provider_n
        out_usage["stage"]      = stage
        out_usage["llm_call_id"] = str(record["call_id"])
        out_usage["is_ab_call"] = bool(is_ab) if spec else False
        if router_meta:
            out_usage["router_selection"] = router_meta.get("mode")
            out_usage["router_reason"] = router_meta.get("reason")
            if router_meta.get("exploration_round") is not None:
                out_usage["router_exploration_round"] = bool(router_meta["exploration_round"])
            if router_meta.get("circuit_relief") is not None:
                out_usage["router_circuit_relief"] = bool(router_meta["circuit_relief"])
            if router_meta.get("candidates_eligible") is not None:
                out_usage["router_candidates_eligible"] = int(router_meta["candidates_eligible"])
            if router_meta.get("candidates_after_circuit_breaker") is not None:
                out_usage["router_candidates_after_breaker"] = int(
                    router_meta["candidates_after_circuit_breaker"]
                )
            if router_meta.get("model_avg_quality") is not None:
                out_usage["router_avg_quality_at_pick"] = router_meta["model_avg_quality"]
            if router_meta.get("model_quality_samples") is not None:
                out_usage["router_quality_samples_at_pick"] = int(
                    router_meta["model_quality_samples"]
                )
            if router_meta.get("router_composite_at_pick") is not None:
                out_usage["router_composite_at_pick"] = router_meta["router_composite_at_pick"]
            if router_meta.get("router_composite_breakdown"):
                out_usage["router_composite_breakdown"] = router_meta["router_composite_breakdown"]

    return (text, out_usage)


def generate_sync(
    prompt: str,
    stage: str = "planner",
    max_tokens: int = 1000,
    config_sha: str | None = None,
    correlation_id: str | None = None,
    thread_id: str | None = None,
    parser: bool = False,
    phi_detected: bool = False,
    complexity: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Sync wrapper for scripts/eval (creates one event loop).

    If called while an event loop is already running (e.g. async tests), runs
    ``generate`` in a fresh thread so ``asyncio.run`` is valid.
    """
    import asyncio
    import concurrent.futures

    def _run_sync() -> tuple[str, dict[str, Any]]:
        return asyncio.run(
            generate(
                prompt,
                stage=stage,
                max_tokens=max_tokens,
                config_sha=config_sha,
                correlation_id=correlation_id,
                thread_id=thread_id,
                parser=parser,
                phi_detected=phi_detected,
                complexity=complexity,
            )
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run_sync()
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_run_sync).result()
