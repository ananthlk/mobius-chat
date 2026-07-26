"""CallManager — the llm.call() invocation layer of LLMManager.

Composes ConfigManager (config) + PromptManager (prompt) + the existing
model_registry/generate() machinery (model select, retry, 429/TPD, fallback),
writes one llm_call_log row, and returns the typed LLMResponse.

Authoritative spec: docs/SPEC_LLM_MANAGER.md §1.1 / §1.2 / §3.3 / §5.

Reuse, not rewrite (§9): the LLM invocation itself (router select, provider
call, exponential-backoff retry, 429 queue+jitter, model fallback, EMA update,
TPD tracking) is the machinery already in app/services/llm_manager.generate().
CallManager delegates to it through the ``invoke_fn`` seam.

Migration-gated wiring (051 must land first): the production invoke_fn extends
generate()/llm_analytics.build_record to persist the new llm_call_log columns
(module_key, template_id, variant_id, turn_id, is_hard_pinned). Those columns
do not exist until migration 051, so the default invoke_fn raises until wired —
the derivation logic below (the novel part) is fully testable without the DB.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

from app.services.config_manager import ConfigManager
from app.services.llm_manager_errors import TurnIdRequiredError
from app.services.llm_manager_types import EngagementContext, LLMResponse
from app.services.prompt_manager import PromptManager, PromptTemplate

logger = logging.getLogger(__name__)

ExperimentAxis = Literal["model", "prompt", "none"]


@dataclass(frozen=True)
class CalibrationSnapshot:
    """Frozen instrument for a calibration batch (Eval condition): pins the
    template AND the temperature. While active, hot-reload, A/B, and config temp
    edits are all bypassed so the measured surface doesn't shift mid-run."""

    module_key: str
    template: PromptTemplate
    temperature: float


@dataclass(frozen=True)
class InvokePlan:
    """Everything the invoke layer needs — the resolved plan for one call.
    Separated from invocation so the derivation (§5/DQ-4) is pure + testable."""

    module_key: str
    system_prompt: str
    user_prompt: str
    template_id: int
    variant_id: str
    turn_id: str
    max_tokens: int
    temperature: float
    phi_detected: bool
    experiment_axis: ExperimentAxis
    # Model resolution:
    forced_model: str | None   # None → bandit chooses; non-None → pinned
    is_hard_pinned: bool        # exclude from model-arm training (DQ-4/C2a)


@dataclass(frozen=True)
class InvokeResult:
    """What invoke_fn returns — raw call outcome, pre-LLMResponse."""

    content: str
    model_used: str
    call_id: str
    latency_ms: int
    tokens_input: int
    tokens_output: int
    cost_usd: float
    did_fallback: bool
    fallback_from: str | None = None


InvokeFn = Callable[[InvokePlan], Awaitable[InvokeResult]]
EmitFn = Callable[[dict], None]


def _default_invoke(_plan: InvokePlan) -> Awaitable[InvokeResult]:
    raise NotImplementedError(
        "CallManager.invoke_fn not wired. Production wiring extends "
        "llm_manager.generate()/llm_analytics.build_record for the new "
        "llm_call_log columns and lands with migration 051."
    )


class CallManager:
    def __init__(
        self,
        prompt_manager: PromptManager,
        config_manager: ConfigManager,
        *,
        invoke_fn: InvokeFn | None = None,
        emit_fn: EmitFn | None = None,
        current_best_fn: Callable[[str], str | None] | None = None,
    ) -> None:
        self._pm = prompt_manager
        self._cm = config_manager
        self._invoke = invoke_fn or _default_invoke
        self._emit = emit_fn
        # Resolves the bandit's current-best model for a module (model_registry).
        # Used only for experiment_axis="prompt" (pin model to best). Default:
        # None → let the invoke layer's router pick (still suppresses A/B).
        self._current_best = current_best_fn or (lambda _mk: None)

    # ── the derivation (pure, §5/DQ-4) ───────────────────────────────────────

    def _plan(
        self,
        module_key: str,
        rendered,  # RenderedPrompt
        config,    # LLMConfig
        *,
        turn_id: str,
        max_tokens: int | None,
        phi_detected: bool,
        experiment_axis: ExperimentAxis,
        temperature: float,
        calibration: bool,
    ) -> InvokePlan:
        # experiment_axis="prompt" pins the model to the bandit's current-best
        # (A/B on prompts, model held constant). Otherwise a config hard-pin wins;
        # else None (bandit chooses).
        if experiment_axis == "prompt":
            forced_model = config.model_id or self._current_best(module_key)
        else:
            forced_model = config.model_id

        # is_hard_pinned = "the model selection was forced" (DQ-4/C2a): a config
        # hard-pin, OR a prompt-A/B turn (model pinned to best → not a bandit
        # model decision → excluded from model-arm training; variant arm still
        # trained), OR a calibration turn (C2b — calibration pins by construction
        # and must never leak into prod bandit training). experiment_axis="model"
        # is genuine model exploration → NOT hard-pinned. "none" → bandit normal.
        is_hard_pinned = calibration or (config.model_id is not None) or (experiment_axis == "prompt")

        return InvokePlan(
            module_key=module_key,
            system_prompt=rendered.system_prompt,
            user_prompt=rendered.user_prompt,
            template_id=rendered.template_id,
            variant_id=rendered.variant_id,
            turn_id=turn_id,
            max_tokens=max_tokens if max_tokens is not None else config.max_tokens,
            temperature=temperature,
            phi_detected=phi_detected,
            experiment_axis=experiment_axis,
            forced_model=forced_model,
            is_hard_pinned=is_hard_pinned,
        )

    async def calibration_snapshot(
        self, module_key: str, engagement_ctx: EngagementContext
    ) -> CalibrationSnapshot:
        """Freeze the instrument for a calibration batch: the module's 'default'
        template (Q2) + the resolved temperature (Eval condition). Pass the
        returned handle into call(snapshot=...) for every turn in the batch."""
        template = await self._pm.default_template(module_key)
        config = await self._cm.get(module_key, engagement_ctx)
        return CalibrationSnapshot(
            module_key=module_key, template=template, temperature=config.temperature
        )

    # ── public entry point ───────────────────────────────────────────────────

    async def call(
        self,
        module_key: str,
        engagement_ctx: EngagementContext,
        *,
        template_vars: dict,
        turn_id: str,
        max_tokens: int | None = None,
        phi_detected: bool = False,
        experiment_axis: ExperimentAxis = "none",
        snapshot: CalibrationSnapshot | None = None,
    ) -> LLMResponse:
        # DEP-1: turn_id is required and must be non-empty. Fail loud so the
        # Attribution rule is enforced at the call boundary, not hoped for at
        # the analytics layer.
        if not turn_id:
            raise TurnIdRequiredError(
                "CallManager.call: turn_id is required (DEP-1) and must be non-empty."
            )

        config = await self._cm.get(module_key, engagement_ctx)

        if snapshot is not None:
            # Calibration: frozen template + frozen temperature, no A/B, and the
            # turn is hard-pinned (C2b). Calibration is not an experiment turn.
            rendered = await self._pm.render(
                module_key, engagement_ctx, template_vars, frozen_template=snapshot.template
            )
            temperature = snapshot.temperature
            experiment_axis = "none"
            calibration = True
        else:
            # experiment_axis drives A/B: only an active prompt experiment samples;
            # "model" (serve default) and "none" (deterministic) do not (§5).
            ab_allowed = experiment_axis == "prompt"
            rendered = await self._pm.render(
                module_key, engagement_ctx, template_vars, ab_allowed=ab_allowed
            )
            temperature = config.temperature
            calibration = False

        plan = self._plan(
            module_key,
            rendered,
            config,
            turn_id=turn_id,
            max_tokens=max_tokens,
            phi_detected=phi_detected,
            experiment_axis=experiment_axis,
            temperature=temperature,
            calibration=calibration,
        )

        result = await self._invoke(plan)

        if result.did_fallback and self._emit is not None:
            # B2F on fallback (§3.3). Shim: existing emit path until ClientChannel
            # lands (Tech Health flag 1 / §7 seam).
            self._emit(
                {
                    "type": "b2f",
                    "event": "model_fallback",
                    "module_key": module_key,
                    "turn_id": turn_id,
                    "fallback_from": result.fallback_from,
                    "model_used": result.model_used,
                }
            )

        return LLMResponse(
            content=result.content,
            model_used=result.model_used,
            template_id=rendered.template_id,
            variant_id=rendered.variant_id,
            call_id=result.call_id,
            latency_ms=result.latency_ms,
            tokens_used=result.tokens_input + result.tokens_output,
            cost_usd=result.cost_usd,
        )
