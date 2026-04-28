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
            vertex_ai_search_datastore=getattr(
                c, "vertex_ai_search_datastore", ""
            )
            or "",
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
    mode: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """
    Call LLM via dynamic model router, record to llm_calls.
    Returns (text, usage_dict).
    usage_dict includes: model, provider, latency_ms, latency_s, stage.

    mode: chat router mode (e.g. copilot, agentic). ``copilot`` restricts Thompson sampling
    to faster model tiers; ``None`` / ``agentic`` uses the full eligible pool per stage.
    """
    _ensure_env()

    router = None
    spec = None
    provider: Any = None
    router_meta: dict[str, Any] | None = None

    # Select model via router — with token budget so the bandit can't pick a model
    # the request physically can't fit into (prevents Groq 413/429 class of failures).
    # Estimator: chars/4 is a standard client-side heuristic for English/code prompts.
    # It's intentionally coarse — we only need to filter candidates, not bill tokens.
    estimated_prompt_tokens = max(1, len(prompt) // 4)
    try:
        from app.services.model_registry import get_router
        router = get_router()
        spec, router_meta = router.select(
            stage=stage,
            phi_detected=phi_detected,
            is_planner=parser,
            mode=mode,
            estimated_prompt_tokens=estimated_prompt_tokens,
            expected_output_tokens=max_tokens,
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
        # Phase 2.5b — parse 429 "try again in X" hints so the bandit's
        # tpd_tracker short-circuits this model for the remaining window
        # instead of retrying and failing every turn until the daily
        # quota rolls. Groq's error body carries the reset hint
        # (observed 2026-04-17: "Please try again in 1h28m56.928s").
        try:
            from app.services import tpd_tracker as _tpd_tracker
            retry_after = _tpd_tracker.parse_retry_after_seconds(str(e))
            if retry_after and retry_after > 0 and spec is not None:
                deadline = time.monotonic() + retry_after
                _tpd_tracker.mark_rate_limited_until(spec.model_id, deadline)
        except Exception:
            # TPD hint parsing is best-effort; never let it mask the
            # actual provider error being re-raised below.
            pass
        usage = zero_usage()
        raise
    finally:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        model_id   = spec.model_id if spec else (usage.get("model") or "unknown")
        provider_n = spec.provider if spec else (usage.get("provider") or "unknown")
        cost_usd   = float(usage.get("cost_usd") or 0.0)

        # Phase 2.5b — feed the tpd_tracker so the filter in
        # _filter_by_token_budget can protect future calls. Total tokens
        # (prompt + completion) is what providers charge against the daily
        # quota; fall back to prompt_tokens + completion_tokens if
        # total_tokens isn't present in the usage dict.
        if success:
            try:
                from app.services import tpd_tracker as _tpd_tracker
                total = (
                    int(usage.get("total_tokens") or 0)
                    or (int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                        + int(usage.get("completion_tokens") or usage.get("output_tokens") or 0))
                )
                if total > 0:
                    _tpd_tracker.record_usage(model_id, total)
            except Exception:
                pass

        # Update router EMA (quality score added later by adjudicator).
        # On failure, also feed the live-health detector so the bandit
        # routes around models that are currently misbehaving (Vertex
        # backend slow, Anthropic 529, etc.) without waiting for the
        # 24h average to shift.
        if spec and router is not None:
            try:
                if success:
                    router.update_ema(
                        model_id=model_id,
                        latency_ms=latency_ms,
                        cost_usd=cost_usd,
                    )
                else:
                    # error_type set in the except block above. Treat
                    # TimeoutError (and our wrapper's variant) as the
                    # canonical "backend slow" signal; other exceptions
                    # are recorded too but don't trigger latency-based
                    # degradation, only the timeout-count threshold.
                    is_timeout = (error_type or "").lower() in (
                        "timeouterror", "asynciotimeouterror", "futuretimeouterror"
                    ) or "abandoned after" in str(usage.get("error") or "")
                    if hasattr(router, "record_call_failure"):
                        router.record_call_failure(
                            model_id=model_id,
                            latency_ms=latency_ms,
                            was_timeout=bool(is_timeout),
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
            if router_meta.get("router_mode"):
                out_usage["router_mode"] = router_meta.get("router_mode")
            if router_meta.get("router_mode_filter_note"):
                out_usage["router_mode_filter_note"] = router_meta.get("router_mode_filter_note")
            if router_meta.get("candidates_after_mode_filter") is not None:
                out_usage["router_candidates_after_mode_filter"] = int(
                    router_meta["candidates_after_mode_filter"]
                )
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
    mode: str | None = None,
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
                mode=mode,
            )
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run_sync()
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_run_sync).result()
