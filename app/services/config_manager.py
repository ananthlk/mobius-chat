"""ConfigManager — per-module LLM invocation config with context overrides.

Part of the LLMManager (Chat pipeline refactor). Reads llm_configs
(migration 050) and applies EngagementContext-driven overrides, returning a
resolved LLMConfig. Same 60s-TTL hot-reload pattern as PromptManager.

Authoritative spec: docs/SPEC_LLM_MANAGER.md §2.2 / §3.2.

Model ownership (DQ-4): ConfigManager NEVER competes on model. model_id stays
None (bandit chooses) unless the row hard-pins it; a non-null model_id makes the
turn is_hard_pinned (excluded from model-arm training). ConfigManager owns
temperature / max_tokens / top_p / stop / timeout / fallback only.

Context overrides — build note (flagged for Chat Architecture / Eval):
  The spec names two example overrides: "clinical voice → lower temperature"
  and "thinking mode → higher temperature". Only the first maps onto a defined
  EngagementContext axis (voice="clinical"). "thinking mode" is NOT one of the
  five ratified axes (voice/format/autonomy/intent/caller_mode), so its trigger
  is left UNIMPLEMENTED with a TODO rather than guessed — needs the axis defined
  (autonomy="agentic"? a caller_mode? a separate RoundManager signal?). The
  override mechanism below is data-driven so adding a rule is one list entry.
"""
from __future__ import annotations

import logging
import time
from dataclasses import replace

from app.services.llm_manager_types import EngagementContext, LLMConfig

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60

# Clamp applied when voice is clinical: clinical answers want lower-variance
# generation. Value is a tuning parameter (structure ratified; the exact ceiling
# is a knob to confirm), NOT a load-bearing contract.
_CLINICAL_TEMPERATURE_CEILING = 0.1


class ConfigManager:
    def __init__(self, *, ttl_seconds: int = _CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        # module_key -> (loaded_at_monotonic, LLMConfig | None)
        self._cache: dict[str, tuple[float, LLMConfig | None]] = {}

    async def get(self, module_key: str, ctx: EngagementContext | None = None) -> LLMConfig:
        """Resolved config for a module, with context overrides applied on top."""
        base = await self._load(module_key)
        if base is None:
            base = self._default_config(module_key)
        return _apply_context_overrides(base, ctx) if ctx is not None else base

    async def _load(self, module_key: str) -> LLMConfig | None:
        now = time.monotonic()
        cached = self._cache.get(module_key)
        if cached and (now - cached[0]) < self._ttl:
            return cached[1]
        row = await self._fetch_row(module_key)
        cfg = _row_to_config(row) if row is not None else None
        self._cache[module_key] = (now, cfg)
        return cfg

    async def _fetch_row(self, module_key: str):
        from app.services.pg_pool import get_pool

        pool = await get_pool()
        if pool is None:
            logger.warning("ConfigManager: no PG pool; module_key=%s", module_key)
            return None
        async with pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT module_key, model_id, temperature, max_tokens, top_p,
                       stop_sequences, timeout_ms, fallback_model
                FROM llm_configs
                WHERE module_key = $1
                """,
                module_key,
            )

    def invalidate(self, module_key: str | None = None) -> None:
        if module_key is None:
            self._cache.clear()
        else:
            self._cache.pop(module_key, None)

    @staticmethod
    def _default_config(module_key: str) -> LLMConfig:
        """Fallback when a module has no llm_configs row — mirrors the table
        defaults so an unconfigured module still runs (bandit picks the model)."""
        return LLMConfig(
            module_key=module_key,
            model_id=None,
            temperature=0.1,
            max_tokens=1000,
            top_p=None,
            stop_sequences=None,
            timeout_ms=30000,
            fallback_model=None,
        )


def _apply_context_overrides(base: LLMConfig, ctx: EngagementContext) -> LLMConfig:
    """Apply EngagementContext-keyed overrides on top of the base row.

    Data-driven: each rule is (predicate, mutation). Add a rule by appending
    one entry. Rules compose in order; later rules see earlier mutations.
    """
    cfg = base
    # Rule: clinical voice → lower temperature (clamp, never raise it).
    if ctx.voice == "clinical" and cfg.temperature > _CLINICAL_TEMPERATURE_CEILING:
        cfg = replace(cfg, temperature=_CLINICAL_TEMPERATURE_CEILING)
    # TODO(seam): "thinking mode → higher temperature" — trigger axis undefined
    # in the ratified 5-axis EngagementContext. Add here once Chat Architecture
    # confirms the signal (autonomy? caller_mode? separate flag).
    return cfg


def _row_to_config(r) -> LLMConfig:
    stops = r["stop_sequences"]
    return LLMConfig(
        module_key=r["module_key"],
        model_id=r["model_id"],
        temperature=float(r["temperature"]),
        max_tokens=int(r["max_tokens"]),
        top_p=float(r["top_p"]) if r["top_p"] is not None else None,
        stop_sequences=tuple(stops) if stops else None,
        timeout_ms=int(r["timeout_ms"]),
        fallback_model=r["fallback_model"],
    )
