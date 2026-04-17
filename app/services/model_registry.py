"""
Model registry and dynamic router for Mobius LLM calls.

Sprint -1 infrastructure. Replaces hardcoded get_llm_provider() with
data-driven selection across all available models per stage.

Design:
  - MODEL_ROSTER: every available model with capabilities + benchmark priors
  - ModelRouter: selects model per stage using Thompson sampling (Beta distribution)
  - Phase 1 (< 10 quality samples): explore using per-model benchmark priors (ema_quality)
  - Phase 2 (10-100 samples): weighted by blended prior + real data
  - Phase 3 (100+ samples, confidence=locked): exploit best, 5% drift detection
  - Circuit breaker: immediate pull if error_rate_24h > 15% or hard_error_rate > 20%
  - Forced exploration: every EXPLORATION_INTERVAL turns, least-sampled model gets a slot

Reset / fair restart (ops):
  - MOBIUS_BANDIT_PRIORS_ONLY=1 — Thompson draws use **benchmark priors only** (ignores PG
    adjudication history for the Beta blend). Circuit breakers still use real error rates.
    Forced exploration picks the model with **fewest total_calls** (not quality_samples) so
    traffic spreads while you accumulate fresh scores. Turn off when satisfied with new data.

Hard constraints (technical only — not quality assumptions):
  - phi_detected=True → only hipaa_eligible models
  - stage phi_detector → only models with phi_detector in eligible_stages (prompt-guard)
  - planner + ReAct reasoning pool (react_*) → spec_context_k >= MIN_PLANNER_CONTEXT_K
  - gemini-2.0-flash-lite & similar → CHEAP_STAGES only (context too small for open routing)
  - mode=copilot (chat) → Thompson pool excludes heavy ``benchmark_category`` values
    (``frontier_reasoning``, ``open_large``); only faster tiers compete (Flash-class, groq_fast, open_mid, etc.).
    mode=agentic or omitted → no extra category filter (legacy scripts omit ``mode``).

Reasoning-capable models get CORE_REASONING_STAGES (planner through adjudicator); credentialing skill +
roster-heavy integrator are Vertex Gemini–only; priors + PG
stats + circuit breakers let data decide fitness per stage (Flash vs Pro on planner, etc.).
ReAct uses stages ``react_1``..``react_4`` with **separate** composite caps and PG rows per round.
Optional ``MOBIUS_REACT_DEEP_ROUNDS_MIN_CONTEXT_K`` restricts low-context models from rounds 3–4.
"""
from __future__ import annotations

import json
import logging
import os
import random
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

logger = logging.getLogger(__name__)

ProviderType = Literal["vertex", "groq", "anthropic", "openai", "ollama", "together"]

EXPLORATION_INTERVAL = 20     # every N turns per stage, force least-sampled model
CIRCUIT_BREAKER_ERROR = 0.20  # hard error rate threshold → pull from rotation
CIRCUIT_BREAKER_24H   = 0.15  # 24h error spike threshold → temp pull
EMA_ALPHA             = 0.15  # learning rate for in-memory EMA (supplements PG)

# Keep in sync with app.pipeline.react_loop.MAX_ITERATIONS (ReAct reasoning rounds).
REACT_REASONING_ROUNDS_MAX = 4

# Legacy flat caps (``model_composite_scores`` SQL view still uses these until migrated).
COMPOSITE_LAT_CAP_MS = 15000.0
COMPOSITE_COST_CAP_USD = 0.05

# Linear normalization ceilings per *call type* (stage bucket). Router + bandit use these; PG rows are
# already per stage×model, so p95/avg cost compare to the right scale for that stage.
# ReAct: react_1..react_4 get **separate** buckets (later rounds = larger caps) so fast cheap models are not
# unfairly penalized in early rounds and slower/larger models are not capped as harshly when context grows.
_COMPOSITE_LAT_CAP_MS_BY_BUCKET: dict[str, float] = {
    "default": 15000.0,
    "planner": 90000.0,  # initial plan only; not merged with react_N
    "react_1": 40000.0,
    "react_2": 65000.0,
    "react_3": 95000.0,
    "react_4": 125000.0,
    "integrator": 60000.0,
    # Same workload as integrator but prompts can include full Medicaid NPI report + step CSVs
    "integrator_roster": 120000.0,
    "rag": 35000.0,
    "context": 25000.0,
    "classifier": 10000.0,
    "badge": 8000.0,
    "critique": 15000.0,
    "phi_detector": 5000.0,
    "adjudicator": 40000.0,
    "credentialing": 120000.0,
    "roster_clean":  10000.0,   # fast batch classification, should be < 10s
}

_COMPOSITE_COST_CAP_USD_BY_BUCKET: dict[str, float] = {
    "default": 0.05,
    "planner": 0.12,
    "react_1": 0.045,
    "react_2": 0.075,
    "react_3": 0.11,
    "react_4": 0.15,
    "integrator": 0.08,
    "integrator_roster": 0.12,
    "rag": 0.04,
    "context": 0.025,
    "classifier": 0.003,
    "badge": 0.002,
    "critique": 0.006,
    "phi_detector": 0.001,
    "adjudicator": 0.035,
    "credentialing": 0.20,
    "roster_clean":  0.004,  # ~300 names, flash-class models
}


def react_round_from_stage(stage: str | None) -> int | None:
    """If ``stage`` is ``react_<n>``, return n clamped to 1..REACT_REASONING_ROUNDS_MAX; else None."""
    s = (stage or "").strip().lower()
    if not s.startswith("react_"):
        return None
    suf = s[6:]
    if not suf.isdigit():
        return None
    n = int(suf)
    return max(1, min(REACT_REASONING_ROUNDS_MAX, n))


def composite_stage_bucket(stage: str | None) -> str:
    """Map llm ``stage`` to a cap bucket. ReAct rounds use distinct react_1..react_4 buckets."""
    s = (stage or "").strip().lower()
    if not s:
        return "default"
    if s == "plan":
        return "planner"
    if s.startswith("react_"):
        rn = react_round_from_stage(s)
        if rn is not None:
            return f"react_{rn}"
        return "planner"
    if s == "planner":
        return "planner"
    if s.startswith("credentialing_"):
        return "credentialing"
    if s in _COMPOSITE_LAT_CAP_MS_BY_BUCKET:
        return s
    return "default"


def composite_norm_caps_for_stage(stage: str | None) -> tuple[float, float]:
    """Return (latency_cap_ms, cost_cap_usd) for linear terms: 1 - min(metric, cap)/cap."""
    b = composite_stage_bucket(stage)
    lat = _COMPOSITE_LAT_CAP_MS_BY_BUCKET.get(b) or _COMPOSITE_LAT_CAP_MS_BY_BUCKET["default"]
    cost = _COMPOSITE_COST_CAP_USD_BY_BUCKET.get(b) or _COMPOSITE_COST_CAP_USD_BY_BUCKET["default"]
    return (float(lat), float(cost))


def composite_score_api_spec() -> dict[str, Any]:
    """Serializable definition of the router/report composite for admin UI (hamburger report)."""
    bucket_keys = sorted(
        set(_COMPOSITE_LAT_CAP_MS_BY_BUCKET.keys()) | set(_COMPOSITE_COST_CAP_USD_BY_BUCKET.keys())
    )
    stage_caps = {
        b: {
            "latency_cap_ms": float(
                _COMPOSITE_LAT_CAP_MS_BY_BUCKET.get(b, _COMPOSITE_LAT_CAP_MS_BY_BUCKET["default"])
            ),
            "cost_cap_usd": float(
                _COMPOSITE_COST_CAP_USD_BY_BUCKET.get(b, _COMPOSITE_COST_CAP_USD_BY_BUCKET["default"])
            ),
        }
        for b in bucket_keys
    }
    return {
        "title": "Composite score (router ranking)",
        "summary": (
            "Scalar in [0, 1] used for Thompson sampling (blend with benchmark Beta prior) and for ordering "
            "models in this report. Component weights sum to 1.0."
        ),
        "formula": "composite = (q × 0.25) + (rel × 0.25) + (latTerm × 0.25) + (costTerm × 0.25)",
        "weights": {
            "quality": 0.25,
            "reliability": 0.25,
            "latency": 0.25,
            "cost": 0.25,
        },
        "quality": {
            "definition": (
                "q = AVG(quality_score) over adjudicated calls in the window; if none, 0.5. "
                "Clamped to [0, 1]."
            ),
        },
        "reliability": {
            "definition": (
                "rel = max(0, 1 − 2 × hard_error_rate), where hard_error_rate is the fraction of calls "
                "with success = false in the window."
            ),
        },
        "latency_term": {
            "definition": (
                "latTerm = max(0, 1 − min(p95_latency_ms, latency_cap_ms) / latency_cap_ms). "
                "p95 is over successful calls. latency_cap_ms depends on the stage bucket (see stage_caps)."
            ),
        },
        "cost_term": {
            "definition": (
                "costTerm = max(0, 1 − min(avg_cost_usd, cost_cap_usd) / cost_cap_usd). "
                "avg_cost_usd is the mean of llm_calls.cost_usd on successful calls. cost_cap_usd is per "
                "stage bucket. Compare to implied list $: (avg_input_tokens/1000)×($/1K in) + "
                "(avg_output_tokens/1000)×($/1K out) using registered rates below."
            ),
        },
        "stage_caps": stage_caps,
        "stage_bucket_rules": (
            "`planner` / `plan` → planner. `react_1` … `react_4` each have their own caps (later rounds allow "
            "higher latency/cost — context grows). Other `react_*` names fall back to planner. "
            "`credentialing_*` → credentialing; else exact stage if listed in stage_caps, else default."
        ),
        "react_deep_rounds_note": (
            "Optional: set MOBIUS_REACT_DEEP_ROUNDS_MIN_CONTEXT_K (e.g. 32) to drop models below that context K "
            "from ReAct rounds 3–4 only, so larger models compete when reasoning is heaviest."
        ),
        "token_pricing_note": (
            "Registered $/1K input and output come from app.services.cost_model (same table as chat cost display). "
            "avg_list_price_usd in each row recomputes expected $ from average tokens × those rates."
        ),
    }


def composite_router_signal(
    stats: dict[str, Any],
    stage: str | None = None,
) -> tuple[float, dict[str, Any]]:
    """Scalar + breakdown: quality, reliability, p95 latency, avg cost — linear caps **per stage type**.

    ``stage`` should be the llm_calls stage (e.g. router's effective_stage). Falls back to
    ``stats.get("stage")`` when ``stage`` is omitted.
    """
    st = stage if stage is not None else (stats.get("stage") if isinstance(stats.get("stage"), str) else None)
    lat_cap, cost_cap = composite_norm_caps_for_stage(st)
    raw_q = stats.get("avg_quality")
    q = float(raw_q) if raw_q is not None else 0.5
    q = max(0.0, min(1.0, q))
    hard = float(stats.get("hard_error_rate") or 0.0)
    rel = max(0.0, 1.0 - hard * 2.0)
    p95 = float(stats.get("p95_latency_ms") or 0.0)
    lat_factor = max(0.0, 1.0 - min(p95, lat_cap) / lat_cap) if lat_cap > 0 else 0.0
    cost = float(stats.get("avg_cost_usd") or 0.0)
    cost_factor = max(0.0, 1.0 - min(cost, cost_cap) / cost_cap) if cost_cap > 0 else 0.0
    t_q = q * 0.25
    t_rel = rel * 0.25
    t_lat = lat_factor * 0.25
    t_cost = cost_factor * 0.25
    comp = min(1.0, max(0.0, t_q + t_rel + t_lat + t_cost))
    brk: dict[str, Any] = {
        "composite": comp,
        "term_quality": t_q,
        "term_reliability": t_rel,
        "term_latency": t_lat,
        "term_cost": t_cost,
        "avg_quality": q,
        "hard_error_rate": hard,
        "p95_latency_ms": p95,
        "avg_cost_usd": cost,
        "latency_cap_ms": lat_cap,
        "cost_cap_usd": cost_cap,
        "stage_bucket": composite_stage_bucket(st),
    }
    return comp, brk


def per_call_router_composite(
    latency_ms: float | int | None,
    cost_usd: float | None,
    quality_score: float | None,
    success: bool,
    *,
    stage: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> tuple[float, dict[str, Any]]:
    """Same weights as ``composite_router_signal`` for one call.

    Cost term prefers **list price from tokens × registered $/1K** (see ``cost_model.compute_cost``);
    falls back to ``cost_usd`` when tokens or rates are missing.
    """
    from app.services.cost_model import compute_cost

    lat_cap, cost_cap = composite_norm_caps_for_stage(stage)
    q = float(quality_score) if quality_score is not None else 0.5
    q = max(0.0, min(1.0, q))
    hard = 0.0 if success else 1.0
    rel = max(0.0, 1.0 - hard * 2.0)
    lat_ms = float(latency_ms or 0.0)
    lat_factor = max(0.0, 1.0 - min(lat_ms, lat_cap) / lat_cap) if lat_cap > 0 else 0.0

    in_t = int(input_tokens or 0)
    out_t = int(output_tokens or 0)
    usage_dict: dict[str, Any] = {
        "provider": (provider or "").strip(),
        "model": (model or "").strip(),
        "input_tokens": in_t,
        "output_tokens": out_t,
    }
    list_price = float(compute_cost(usage_dict)) if (in_t or out_t) else 0.0
    billed = float(cost_usd or 0.0)
    cost_metric = list_price if (in_t or out_t) and list_price > 0 else billed
    cost_factor = max(0.0, 1.0 - min(cost_metric, cost_cap) / cost_cap) if cost_cap > 0 else 0.0

    t_q = q * 0.25
    t_rel = rel * 0.25
    t_lat = lat_factor * 0.25
    t_cost = cost_factor * 0.25
    comp = min(1.0, max(0.0, t_q + t_rel + t_lat + t_cost))
    brk: dict[str, Any] = {
        "composite": comp,
        "term_quality": t_q,
        "term_reliability": t_rel,
        "term_latency": t_lat,
        "term_cost": t_cost,
        "quality_used": q,
        "hard_error_this_call": hard,
        "latency_ms": lat_ms,
        "cost_usd_billed": billed,
        "cost_list_usd": list_price,
        "cost_metric_usd": cost_metric,
        "latency_cap_ms": lat_cap,
        "cost_cap_usd": cost_cap,
        "stage_bucket": composite_stage_bucket(stage),
    }
    return comp, brk


def _bandit_priors_only() -> bool:
    """Ignore adjudicated quality history for Thompson + exploration spread (see module docstring)."""
    return os.environ.get("MOBIUS_BANDIT_PRIORS_ONLY", "").strip().lower() in ("1", "true", "yes")


# Safety margin on capacity estimates — accounts for tokenizer mismatch between our
# client-side counter and the provider's server-side tokenizer.
_TOKEN_BUDGET_SAFETY = 1.05


def _filter_by_token_budget(
    candidates: list["ModelSpec"],
    *,
    estimated_prompt_tokens: int,
    expected_output_tokens: int | None,
) -> tuple[list["ModelSpec"], dict[str, Any]]:
    """Remove candidates that can't fit this request.

    Two independent budgets are checked:

    1. **Context window** (``spec_context_k * 1000``): hard ceiling — the model can't
       physically accept more tokens than this in a single request.
    2. **Per-minute TPM** (``spec_tpm_limit``): soft ceiling from the provider's
       rate-limit tier. Requests larger than TPM trigger 413 "Request too large"
       regardless of context window. Models with ``None`` here are treated as
       unlimited (unknown budget; trust the context window only).

    Returns ``(surviving_candidates, meta)``. ``meta`` always contains
    ``estimated_prompt_tokens``, ``expected_output_tokens``, ``request_tokens``,
    ``candidates_trimmed_by_context``, ``candidates_trimmed_by_tpm``.
    """
    meta: dict[str, Any] = {
        "estimated_prompt_tokens": estimated_prompt_tokens,
        "expected_output_tokens": expected_output_tokens,
    }

    after_ctx: list[ModelSpec] = []
    trimmed_ctx = 0
    for c in candidates:
        out_t = expected_output_tokens if expected_output_tokens is not None else c.default_max_output_tokens
        request_tokens = int((estimated_prompt_tokens + out_t) * _TOKEN_BUDGET_SAFETY)
        if c.spec_context_k * 1000 >= request_tokens:
            after_ctx.append(c)
        else:
            trimmed_ctx += 1

    after_tpm: list[ModelSpec] = []
    trimmed_tpm = 0
    for c in after_ctx:
        out_t = expected_output_tokens if expected_output_tokens is not None else c.default_max_output_tokens
        request_tokens = int((estimated_prompt_tokens + out_t) * _TOKEN_BUDGET_SAFETY)
        # None TPM = unknown/unlimited; keep the candidate.
        if c.spec_tpm_limit is None or c.spec_tpm_limit >= request_tokens:
            after_tpm.append(c)
        else:
            trimmed_tpm += 1

    # Use a representative request size in meta (uses the first surviving candidate's
    # output default, or 1024 as a neutral baseline).
    representative_out = (
        expected_output_tokens
        if expected_output_tokens is not None
        else (after_tpm[0].default_max_output_tokens if after_tpm else 1024)
    )
    meta["request_tokens"] = int(
        (estimated_prompt_tokens + representative_out) * _TOKEN_BUDGET_SAFETY
    )
    meta["candidates_trimmed_by_context"] = trimmed_ctx
    meta["candidates_trimmed_by_tpm"] = trimmed_tpm

    return after_tpm, meta


def _react_deep_rounds_min_context_k() -> int:
    """If > 0, ReAct rounds 3+ require at least this spec_context_k (favor larger models when context grows)."""
    raw = os.environ.get("MOBIUS_REACT_DEEP_ROUNDS_MIN_CONTEXT_K", "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _bandit_stats_row(raw: dict[str, Any]) -> dict[str, Any]:
    """PG row as seen by the bandit; may strip quality observations when priors-only mode."""
    if not _bandit_priors_only():
        return raw
    out = dict(raw)
    out["quality_samples"] = 0
    out["avg_quality"] = None
    return out

# Planner / ReAct prompts can be multi-kilotoken; exclude sub-4K-window models from that pool only.
MIN_PLANNER_CONTEXT_K = 4

# Groq: these models often emit native `tool_calls` while the Mobius planner/ReAct prompts expect a JSON
# object in message.content only (no `tools` array → implicit tool_choice none → HTTP 400 from Groq).
# They remain eligible for integrator/RAG/etc.; only planner + react_* are restricted.
GROQ_MODEL_IDS_EXCLUDE_PLANNER_REACT: frozenset[str] = frozenset(
    {
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "openai/gpt-oss-safeguard-20b",
        "qwen/qwen3-32b",
    }
)

# Thompson sampling: copilot chat mode must not draw frontier / large reasoning models.
COPILOT_EXCLUDED_THOMPSON_BENCHMARK_CATEGORIES: frozenset[str] = frozenset(
    {
        "frontier_reasoning",
        "open_large",
    }
)
COPILOT_ALLOWED_THOMPSON_FALLBACK_CATEGORIES: frozenset[str] = frozenset(
    {
        "frontier_fast",
        "tiny_classifier",
        "groq_fast",
        "open_mid",
    }
)


# ── BENCHMARK PRIORS ─────────────────────────────────────────────────────────
# Each model uses its benchmark (ema_quality) as prior mean, not a constant.
# (same scale as the old “~10 pseudo-observations” tiers). Thompson observations (when
BENCHMARK_PRIOR_STRENGTH = 10.0  # α+β for per-model prior; mean = ema_quality

MODEL_CATEGORIES: dict[str, str] = {
    "gemini-2.5-pro":                                "frontier_reasoning",
    "gemini-2.5-flash":                              "frontier_fast",
    "gemini-2.0-flash-lite":                         "tiny_classifier",
    "claude-sonnet-4-6":                             "frontier_reasoning",
    "claude-haiku-4-5-20251001":                     "frontier_fast",
    "gpt-4o":                                        "frontier_reasoning",
    "gpt-4o-mini":                                   "frontier_fast",
    "llama-3.3-70b-versatile":                       "groq_fast",
    "llama-3.1-8b-instant":                          "groq_fast",
    "openai/gpt-oss-120b":                           "open_large",
    "openai/gpt-oss-20b":                            "open_mid",
    "qwen/qwen3-32b":                                "open_large",
    "meta-llama/llama-4-scout-17b-16e-instruct":     "groq_fast",
    "moonshotai/kimi-k2-instruct-0905":              "frontier_reasoning",
    "meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo": "frontier_reasoning",
    "Qwen/Qwen2.5-72B-Instruct-Turbo":              "open_large",
    "deepseek-ai/DeepSeek-V3":                       "frontier_reasoning",
    "llama3.1:8b":                                   "open_mid",
    "mistral:7b":                                    "open_mid",
    "phi4:14b":                                      "tiny_classifier",
}


@dataclass
class ModelSpec:
    """Everything the router needs to know about a model."""
    model_id:        str
    provider:        ProviderType
    display_name:    str
    enabled:         bool = True

    # Hard constraints — enforced before composite scoring
    hipaa_eligible:  bool = False
    eligible_stages: list[str] = field(default_factory=list)

    # Published spec sheet (not learned — fixed at registration)
    spec_tokens_per_sec:    float = 0.0    # published throughput
    spec_context_k:         int   = 32     # context window in K tokens
    spec_input_per_1m_usd:  float = 0.0
    spec_output_per_1m_usd: float = 0.0

    # Rate-limit budget (provider TPM/RPM for OUR key/tier). ``None`` = unknown/unlimited.
    # This is the *per-minute* budget, not the context window — distinct constraints.
    # Groq free tier: gpt-oss-20b = 8_000 TPM, llama-3.3-70b = 12_000 TPM, etc.
    spec_tpm_limit:             int | None = None
    spec_rpm_limit:             int | None = None
    # Expected completion size for capacity-planning (request = prompt + completion).
    # Callers may override per-stage; this is the registry default.
    default_max_output_tokens:  int        = 1024

    # Benchmark category → drives prior
    benchmark_category: str = "frontier_fast"

    # Runtime state — updated from llm_calls PG data + EMA
    ema_quality:    float = 0.5
    ema_latency_ms: float = 5000.0
    ema_cost_usd:   float = 0.01
    call_count:     int   = 0
    quality_samples: int  = 0

    @property
    def confidence(self) -> str:
        if self.quality_samples >= 100: return "locked"
        if self.quality_samples >= 50:  return "high"
        if self.quality_samples >= 10:  return "medium"
        return "low"

    @property
    def beta_prior(self) -> tuple[float, float]:
        """Beta prior from model's benchmark (ema_quality). Mean = ema_quality, strength = BENCHMARK_PRIOR_STRENGTH."""
        mu = max(0.05, min(0.95, float(self.ema_quality)))
        k = BENCHMARK_PRIOR_STRENGTH
        a = mu * k
        b = (1.0 - mu) * k
        return (a, b)


# ── MODEL ROSTER ─────────────────────────────────────────────────────────────
# Reasoning-capable models: core chat pipeline (planner → adjudicator).
# Credentialing skill LLM stages + roster integrator are **Vertex Gemini–only** in MODEL_ROSTER
# (1M-token class context; avoids Anthropic org TPM limits and sub–1M third-party windows on huge reports).
# Tiny / flash-lite: CHEAP_STAGES only — context too small for unconstrained planner/RAG.
# Prompt guard: phi_detector only.

CORE_REASONING_STAGES: list[str] = [
    "planner",
    "integrator",
    "rag",
    "context",
    "critique",
    "badge",
    "classifier",
    "adjudicator",
]

# provider-roster-credentialing → POST /internal/skill-llm (draft/validate/critique/compose/report Q&A)
CREDENTIALING_SKILL_STAGES: list[str] = [
    "credentialing_draft",
    "credentialing_validate",
    "credentialing_critique",
    "credentialing_compose",
    "credentialing_report_qa",
]

# Chat integrator when Medicaid NPI / roster payloads are in the consolidator JSON (see integrator_llm_stage).
INTEGRATOR_ROSTER_STAGE = "integrator_roster"

REASONING_STAGES: list[str] = list(CORE_REASONING_STAGES) + list(CREDENTIALING_SKILL_STAGES)


def vertex_roster_eligible_stages() -> list[str]:
    """Stages for Gemini 2.5 Pro/Flash only: core chat + credentialing skill + heavy roster integrator."""
    return list(CORE_REASONING_STAGES) + list(CREDENTIALING_SKILL_STAGES) + [INTEGRATOR_ROSTER_STAGE]


def integrator_llm_stage(ctx: Any) -> str:
    """Return ``integrator_roster`` when this turn carries Medicaid NPI / roster-heavy context; else ``integrator``."""
    bp = getattr(ctx, "blueprint", None) or []
    if any(isinstance(b, dict) and (b.get("tool_hint") or "") == "roster_report" for b in bp):
        return INTEGRATOR_ROSTER_STAGE
    md = getattr(ctx, "roster_report_final_md", None)
    if isinstance(md, str) and len(md.strip()) > 0:
        return INTEGRATOR_ROSTER_STAGE
    if getattr(ctx, "roster_step_outputs", None):
        return INTEGRATOR_ROSTER_STAGE
    pdf = getattr(ctx, "roster_report_pdf_base64", None)
    if isinstance(pdf, str) and len(pdf) > 5000:
        return INTEGRATOR_ROSTER_STAGE
    for a in getattr(ctx, "answers", None) or []:
        if isinstance(a, str) and len(a) > 25_000:
            return INTEGRATOR_ROSTER_STAGE
    return "integrator"


CHEAP_STAGES = ["badge", "classifier", "critique", "adjudicator"]
PHI_SAFE_STAGES = ["phi_detector"]

# roster_clean: lightweight batch classification (junk-row detection).
# Fast/flash models only — no frontier reasoning models needed here.
ROSTER_CLEAN_STAGE = "roster_clean"
FAST_ONLY_STAGES = list(CHEAP_STAGES) + [ROSTER_CLEAN_STAGE]

MODEL_ROSTER: dict[str, ModelSpec] = {

    # ── GOOGLE VERTEX (BAA eligible — already configured) ────────────────────

    "gemini-2.5-pro": ModelSpec(
        model_id="gemini-2.5-pro",
        provider="vertex",
        display_name="Gemini 2.5 Pro",
        enabled=True,
        hipaa_eligible=True,
        eligible_stages=vertex_roster_eligible_stages(),
        spec_tokens_per_sec=100.0,
        spec_context_k=1000,
        spec_input_per_1m_usd=1.25,
        spec_output_per_1m_usd=5.00,
        benchmark_category="frontier_reasoning",
        ema_quality=0.88,
        ema_latency_ms=8000.0,
        ema_cost_usd=0.030,
    ),

    "gemini-2.5-flash": ModelSpec(
        model_id="gemini-2.5-flash",
        provider="vertex",
        display_name="Gemini 2.5 Flash",
        enabled=True,
        hipaa_eligible=True,
        eligible_stages=vertex_roster_eligible_stages() + [ROSTER_CLEAN_STAGE],
        spec_tokens_per_sec=300.0,
        spec_context_k=1000,
        spec_input_per_1m_usd=0.075,
        spec_output_per_1m_usd=0.30,
        benchmark_category="frontier_fast",
        ema_quality=0.78,
        ema_latency_ms=2500.0,
        ema_cost_usd=0.003,
    ),

    "gemini-2.0-flash-lite": ModelSpec(
        model_id="gemini-2.0-flash-lite",
        provider="vertex",
        display_name="Gemini 2.0 Flash Lite",
        enabled=True,
        hipaa_eligible=True,
        eligible_stages=FAST_ONLY_STAGES,
        spec_tokens_per_sec=500.0,
        spec_context_k=32,
        spec_input_per_1m_usd=0.018,
        spec_output_per_1m_usd=0.072,
        benchmark_category="tiny_classifier",
        ema_quality=0.65,
        ema_latency_ms=800.0,
        ema_cost_usd=0.0003,
    ),

    # ── GROQ (one API key, multiple production models) ────────────────────────

    "llama-3.3-70b-versatile": ModelSpec(
        model_id="llama-3.3-70b-versatile",
        provider="groq",
        display_name="Llama 3.3 70B (Groq)",
        enabled=False,                             # enable when GROQ_API_KEY set
        hipaa_eligible=False,                      # no Groq BAA
        eligible_stages=list(CORE_REASONING_STAGES) + [ROSTER_CLEAN_STAGE],
        spec_tokens_per_sec=280.0,
        spec_context_k=131,
        spec_input_per_1m_usd=0.59,
        spec_output_per_1m_usd=0.79,
        spec_tpm_limit=12_000,                     # Groq on_demand free tier
        spec_rpm_limit=30,
        benchmark_category="groq_fast",
        ema_quality=0.72,
        ema_latency_ms=1200.0,
        ema_cost_usd=0.005,
    ),

    "llama-3.1-8b-instant": ModelSpec(
        model_id="llama-3.1-8b-instant",
        provider="groq",
        display_name="Llama 3.1 8B Instant (Groq)",
        enabled=False,
        hipaa_eligible=False,
        eligible_stages=list(CORE_REASONING_STAGES) + [ROSTER_CLEAN_STAGE],
        spec_tokens_per_sec=560.0,
        spec_context_k=131,
        spec_input_per_1m_usd=0.05,
        spec_output_per_1m_usd=0.08,
        spec_tpm_limit=30_000,                     # Groq on_demand free tier (smaller model)
        spec_rpm_limit=30,
        benchmark_category="groq_fast",
        ema_quality=0.62,
        ema_latency_ms=400.0,
        ema_cost_usd=0.0003,
    ),

    "openai/gpt-oss-120b": ModelSpec(
        model_id="openai/gpt-oss-120b",
        provider="groq",
        display_name="GPT OSS 120B (Groq)",
        enabled=False,
        hipaa_eligible=False,
        eligible_stages=list(CORE_REASONING_STAGES),
        spec_tokens_per_sec=500.0,
        spec_context_k=131,
        spec_input_per_1m_usd=0.15,
        spec_output_per_1m_usd=0.60,
        spec_tpm_limit=8_000,                      # Groq on_demand free tier
        spec_rpm_limit=30,
        benchmark_category="open_large",
        ema_quality=0.78,
        ema_latency_ms=600.0,
        ema_cost_usd=0.004,
    ),

    "openai/gpt-oss-20b": ModelSpec(
        model_id="openai/gpt-oss-20b",
        provider="groq",
        display_name="GPT OSS 20B (Groq)",
        enabled=False,
        hipaa_eligible=False,
        eligible_stages=list(CORE_REASONING_STAGES) + [ROSTER_CLEAN_STAGE],
        spec_tokens_per_sec=1000.0,              # 1000 t/s — fastest on roster
        spec_context_k=131,
        spec_input_per_1m_usd=0.075,
        spec_output_per_1m_usd=0.30,
        spec_tpm_limit=8_000,                      # Groq on_demand free tier — observed 413 at 8907 tokens
        spec_rpm_limit=30,
        benchmark_category="open_mid",
        ema_quality=0.68,
        ema_latency_ms=300.0,
        ema_cost_usd=0.001,
    ),

    "qwen/qwen3-32b": ModelSpec(
        model_id="qwen/qwen3-32b",
        provider="groq",
        display_name="Qwen3 32B (Groq) — preview",
        enabled=False,
        hipaa_eligible=False,
        eligible_stages=list(CORE_REASONING_STAGES),
        spec_tokens_per_sec=400.0,
        spec_context_k=131,
        spec_input_per_1m_usd=0.29,
        spec_output_per_1m_usd=0.59,
        benchmark_category="open_large",
        ema_quality=0.76,
        ema_latency_ms=800.0,
        ema_cost_usd=0.004,
    ),

    "meta-llama/llama-4-scout-17b-16e-instruct": ModelSpec(
        model_id="meta-llama/llama-4-scout-17b-16e-instruct",
        provider="groq",
        display_name="Llama 4 Scout 17B (Groq) — preview",
        enabled=False,
        hipaa_eligible=False,
        eligible_stages=list(CORE_REASONING_STAGES) + [ROSTER_CLEAN_STAGE],
        spec_tokens_per_sec=750.0,
        spec_context_k=131,
        spec_input_per_1m_usd=0.11,
        spec_output_per_1m_usd=0.34,
        benchmark_category="groq_fast",
        ema_quality=0.70,
        ema_latency_ms=500.0,
        ema_cost_usd=0.002,
    ),

    # PHI safety classifier — special stage only
    "meta-llama/llama-prompt-guard-2-86m": ModelSpec(
        model_id="meta-llama/llama-prompt-guard-2-86m",
        provider="groq",
        display_name="Llama Prompt Guard 2 86M (Groq)",
        enabled=False,
        hipaa_eligible=False,
        eligible_stages=PHI_SAFE_STAGES,
        spec_tokens_per_sec=0.0,
        spec_context_k=1,
        spec_input_per_1m_usd=0.04,
        spec_output_per_1m_usd=0.04,
        benchmark_category="tiny_classifier",
        ema_quality=0.90,                        # narrow task, high prior
        ema_latency_ms=100.0,
        ema_cost_usd=0.0001,
    ),

    # ── ANTHROPIC (warm language quality) ─────────────────────────────────────

    "claude-sonnet-4-6": ModelSpec(
        model_id="claude-sonnet-4-6",
        provider="anthropic",
        display_name="Claude Sonnet 4.6",
        enabled=False,                             # enable when ANTHROPIC_API_KEY set
        hipaa_eligible=False,                      # needs Enterprise BAA for PHI
        eligible_stages=list(CORE_REASONING_STAGES),
        spec_tokens_per_sec=120.0,
        spec_context_k=200,
        spec_input_per_1m_usd=3.00,
        spec_output_per_1m_usd=15.00,
        benchmark_category="frontier_reasoning",
        ema_quality=0.90,
        ema_latency_ms=3500.0,
        ema_cost_usd=0.018,
    ),

    "claude-haiku-4-5-20251001": ModelSpec(
        model_id="claude-haiku-4-5-20251001",
        provider="anthropic",
        display_name="Claude Haiku 4.5",
        enabled=False,
        hipaa_eligible=False,
        eligible_stages=list(CORE_REASONING_STAGES) + [ROSTER_CLEAN_STAGE],
        spec_tokens_per_sec=300.0,
        spec_context_k=200,
        spec_input_per_1m_usd=0.80,
        spec_output_per_1m_usd=4.00,
        benchmark_category="frontier_fast",
        ema_quality=0.80,
        ema_latency_ms=1200.0,
        ema_cost_usd=0.005,
    ),

    # ── TOGETHER.AI (big open-source, cheap) ──────────────────────────────────

    "meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo": ModelSpec(
        model_id="meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo",
        provider="together",
        display_name="Llama 3.1 405B (Together)",
        enabled=False,
        hipaa_eligible=False,
        eligible_stages=list(CORE_REASONING_STAGES),
        spec_tokens_per_sec=80.0,
        spec_context_k=128,
        spec_input_per_1m_usd=0.90,
        spec_output_per_1m_usd=0.90,
        benchmark_category="frontier_reasoning",
        ema_quality=0.87,
        ema_latency_ms=5000.0,
        ema_cost_usd=0.007,
    ),

    "Qwen/Qwen2.5-72B-Instruct-Turbo": ModelSpec(
        model_id="Qwen/Qwen2.5-72B-Instruct-Turbo",
        provider="together",
        display_name="Qwen 2.5 72B (Together)",
        enabled=False,
        hipaa_eligible=False,
        eligible_stages=list(CORE_REASONING_STAGES),
        spec_tokens_per_sec=100.0,
        spec_context_k=32,
        spec_input_per_1m_usd=0.56,
        spec_output_per_1m_usd=0.56,
        benchmark_category="open_large",
        ema_quality=0.78,
        ema_latency_ms=3000.0,
        ema_cost_usd=0.004,
    ),

    "deepseek-ai/DeepSeek-V3": ModelSpec(
        model_id="deepseek-ai/DeepSeek-V3",
        provider="together",
        display_name="DeepSeek V3 (Together)",
        enabled=False,
        hipaa_eligible=False,
        eligible_stages=list(CORE_REASONING_STAGES),
        spec_tokens_per_sec=60.0,
        spec_context_k=128,
        spec_input_per_1m_usd=0.27,
        spec_output_per_1m_usd=1.10,
        benchmark_category="frontier_reasoning",
        ema_quality=0.86,
        ema_latency_ms=6000.0,
        ema_cost_usd=0.008,
    ),

    # ── OLLAMA / LOCAL ────────────────────────────────────────────────────────

    "llama3.1:8b": ModelSpec(
        model_id="llama3.1:8b",
        provider="ollama",
        display_name="Llama 3.1 8B (local)",
        enabled=True,                              # already configured
        hipaa_eligible=True,                       # self-hosted = data stays local
        eligible_stages=list(CORE_REASONING_STAGES) + [ROSTER_CLEAN_STAGE],
        spec_tokens_per_sec=0.0,                   # depends on hardware
        spec_context_k=128,
        spec_input_per_1m_usd=0.0,
        spec_output_per_1m_usd=0.0,
        benchmark_category="open_mid",
        ema_quality=0.60,
        ema_latency_ms=8000.0,                     # slow on MacOS CPU
        ema_cost_usd=0.0,
    ),
}


# ── BANDIT STATE ──────────────────────────────────────────────────────────────

@dataclass
class BanditState:
    """Beta distribution state for one model at one stage."""
    model_id: str
    alpha: float
    beta:  float
    call_count: int = 0

    def sample(self) -> float:
        """Sample from Beta(alpha, beta). High variance = exploration."""
        try:
            import numpy as np
            return float(np.random.beta(self.alpha, self.beta))
        except ImportError:
            # Fallback without numpy — use mean with small noise
            mean = self.alpha / (self.alpha + self.beta)
            noise = (random.random() - 0.5) * 0.1
            return max(0.01, min(0.99, mean + noise))

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def n(self) -> float:
        return self.alpha + self.beta


def _build_bandit_state(
    spec: ModelSpec,
    stats: dict,
    stage: str | None = None,
) -> BanditState:
    """
    Build Beta distribution from PG stats + benchmark prior.
    MOBIUS_BANDIT_PRIORS_ONLY → pure benchmark prior (call_count still reported).
    Otherwise: blend prior with pseudo-observations centered on ``composite_router_signal``
    (quality, hard-error reliability, p95 latency, avg cost; linear caps per call type / stage).
    Observation weight uses ``total_calls`` (0→100 cap), not quality-only sample count.

    ``stage`` should be the router's effective stage when ``stats`` omits ``stage`` (PG row).
    """
    prior_a, prior_b = spec.beta_prior
    total_calls = int(stats.get("total_calls") or 0)

    if _bandit_priors_only():
        return BanditState(
            model_id=spec.model_id,
            alpha=prior_a,
            beta=prior_b,
            call_count=total_calls,
        )

    if total_calls <= 0:
        return BanditState(
            model_id=spec.model_id,
            alpha=prior_a,
            beta=prior_b,
            call_count=0,
        )

    composite, _ = composite_router_signal(stats, stage=stage)
    composite = max(0.01, min(0.99, float(composite)))

    obs_weight = min(total_calls, 100) / 100.0
    prior_weight = 1.0 - obs_weight

    obs_a = total_calls * composite
    obs_b = total_calls * (1.0 - composite)

    blended_a = (prior_a * prior_weight) + (obs_a * obs_weight)
    blended_b = (prior_b * prior_weight) + (obs_b * obs_weight)

    return BanditState(
        model_id=spec.model_id,
        alpha=max(0.1, blended_a),
        beta=max(0.1, blended_b),
        call_count=total_calls,
    )


# ── ROUTER ────────────────────────────────────────────────────────────────────

class ModelRouter:
    """
    Dynamic model selector. Reads PG stats every REFRESH_INTERVAL_S seconds.
    Selects per stage using Thompson sampling with benchmark priors.
    """

    REFRESH_INTERVAL_S = 300  # reload PG stats every 5 minutes

    def __init__(self) -> None:
        self._stage_stats: dict[str, dict[str, dict]] = {}
        self._stage_call_counts: dict[str, int] = {}
        self._last_refresh: float = 0.0

    # ── Public API ────────────────────────────────────────────────────────────

    def select(
        self,
        stage: str,
        phi_detected: bool = False,
        is_planner: bool = False,
        mode: str | None = None,
        *,
        estimated_prompt_tokens: int | None = None,
        expected_output_tokens: int | None = None,
    ) -> tuple[ModelSpec, dict[str, Any]]:
        """Select best model for this stage. Returns (spec, meta) for UI / usage_breakdown.

        ``mode``: chat router mode — ``copilot`` restricts Thompson sampling to non–frontier-reasoning
        benchmark categories; ``agentic`` or ``None`` leaves the full eligible pool.

        ``estimated_prompt_tokens`` — token count of the assembled prompt. When provided,
        candidates whose context window or per-minute TPM budget can't hold
        ``estimated_prompt_tokens + expected_output_tokens`` are filtered BEFORE Thompson
        sampling runs. This prevents the classic failure mode where the bandit picks a
        model the request physically can't fit into (e.g. Groq gpt-oss-20b with an 8_000
        TPM ceiling gets picked for a 9_000-token request → 413). Leave ``None`` to skip
        the filter (legacy behavior).

        ``meta`` keys: mode, reason, router_stage, candidates_eligible,
        candidates_after_circuit_breaker, circuit_relief (bool), exploration_round (bool),
        router_composite_at_pick, router_composite_breakdown (PG row at decision time),
        model_avg_quality, model_quality_samples (when known from PG).

        Additional meta keys when ``estimated_prompt_tokens`` is set:
        ``estimated_prompt_tokens``, ``expected_output_tokens``, ``request_tokens``,
        ``candidates_trimmed_by_context``, ``candidates_trimmed_by_tpm``.
        """
        self._maybe_refresh()

        effective_stage = "planner" if is_planner else stage
        meta: dict[str, Any] = {
            "router_stage": effective_stage,
            "phi_safe_only": bool(phi_detected),
            "router_mode": (mode or "").strip().lower() or None,
        }
        candidates = self._get_candidates(effective_stage, phi_detected)
        meta["candidates_eligible"] = len(candidates)
        candidates, mode_note = self._apply_router_mode_filter(candidates, mode)
        if mode_note:
            meta["router_mode_filter_note"] = mode_note
        meta["candidates_after_mode_filter"] = len(candidates)

        # ── Token-aware filter ─────────────────────────────────────────────────
        # Runs before circuit breakers and Thompson draw so the bandit only
        # explores models that can physically handle this request.
        if estimated_prompt_tokens is not None:
            n_before = len(candidates)
            candidates, budget_meta = _filter_by_token_budget(
                candidates,
                estimated_prompt_tokens=int(estimated_prompt_tokens),
                expected_output_tokens=expected_output_tokens,
            )
            meta.update(budget_meta)
            # If every candidate was trimmed, fall back to the one with the largest
            # effective budget rather than erroring — degraded-mode is better than none.
            if not candidates and n_before > 0:
                # Unfiltered list (pre-budget) — pick the model with the largest
                # per-minute budget (TPM or context*1000 as proxy).
                fallback_pool = self._get_candidates(effective_stage, phi_detected)
                fallback_pool.sort(
                    key=lambda c: (
                        c.spec_tpm_limit if c.spec_tpm_limit is not None else c.spec_context_k * 1000
                    ),
                    reverse=True,
                )
                chosen = fallback_pool[0]
                meta["mode"] = "budget_fallback"
                meta["reason"] = (
                    f"No model can fit this request ({meta.get('request_tokens')} tokens). "
                    f"Falling back to largest-budget eligible model: {chosen.model_id}."
                )
                meta["candidates_after_circuit_breaker"] = 1
                meta["circuit_relief"] = False
                meta["exploration_round"] = False
                return chosen, meta

        if not candidates:
            logger.error(
                "No models available for stage=%s phi=%s — check MODEL_ROSTER",
                effective_stage, phi_detected
            )
            fb = MODEL_ROSTER["gemini-2.5-flash"]
            meta["mode"] = "fallback_no_models"
            meta["reason"] = (
                "No models matched this stage after eligibility filters "
                "(HIPAA / context window / enabled list). Hard fallback: gemini-2.5-flash."
            )
            meta["candidates_after_circuit_breaker"] = 0
            meta["circuit_relief"] = False
            meta["exploration_round"] = False
            return fb, meta

        stats = self._stage_stats.get(effective_stage, {})
        n_before_cb = len(candidates)
        candidates, circuit_relief = self._apply_circuit_breakers(
            candidates, stats, effective_stage
        )
        meta["candidates_after_circuit_breaker"] = len(candidates)
        meta["circuit_relief"] = circuit_relief
        if n_before_cb > len(candidates) and not circuit_relief:
            meta["circuit_breaker_trimmed"] = n_before_cb - len(candidates)

        # Forced exploration slot
        stage_calls = self._stage_call_counts.get(effective_stage, 0)
        exploration_round = bool(stage_calls > 0 and stage_calls % EXPLORATION_INTERVAL == 0)
        meta["exploration_round"] = exploration_round

        if exploration_round:
            chosen = self._forced_explore(candidates, stats)
            logger.info("Forced exploration: stage=%s → %s", effective_stage, chosen.model_id)
            base_mode = "exploration"
            if _bandit_priors_only():
                base_reason = (
                    f"Exploration round (every {EXPLORATION_INTERVAL} calls to this stage): "
                    "MOBIUS_BANDIT_PRIORS_ONLY=1 — picked the eligible model with the fewest "
                    "**total** calls so traffic spreads while the bandit runs on benchmark priors only."
                )
            else:
                base_reason = (
                    f"Exploration round (every {EXPLORATION_INTERVAL} calls to this stage): "
                    "picked the eligible model with the fewest adjudicated quality samples so the "
                    "router can compare models (A/B-style calibration)."
                )
        else:
            chosen = self._thompson_select(candidates, stats, effective_stage)
            base_mode = "thompson"
            if _bandit_priors_only():
                base_reason = (
                    "Thompson sampling on **benchmark priors only** (MOBIUS_BANDIT_PRIORS_ONLY=1): "
                    "PG adjudication history is ignored for draws; circuit breakers still use live errors."
                )
            else:
                base_reason = (
                    "Thompson sampling (Beta bandit): one random draw per eligible model; "
                    "highest draw wins. Observations blend benchmark priors with PG "
                    "composite (quality, reliability, p95 latency, avg cost — ¼ each; "
                    "linear caps per stage type)."
                )

        s_chosen = stats.get(chosen.model_id, {})
        if s_chosen:
            try:
                comp, brk = composite_router_signal(s_chosen, stage=effective_stage)
                meta["router_composite_at_pick"] = round(float(comp), 4)
                meta["router_composite_breakdown"] = {
                    k: round(float(v), 4) if isinstance(v, (int, float)) else v
                    for k, v in brk.items()
                }
            except (TypeError, ValueError):
                pass
            try:
                aq = s_chosen.get("avg_quality")
                if aq is not None:
                    meta["model_avg_quality"] = round(float(aq), 3)
            except (TypeError, ValueError):
                pass
            try:
                qs = s_chosen.get("quality_samples")
                if qs is not None:
                    meta["model_quality_samples"] = int(qs)
            except (TypeError, ValueError):
                pass

        if circuit_relief:
            meta["mode"] = f"circuit_relief+{base_mode}"
            meta["reason"] = (
                "Circuit breaker: every other candidate exceeded error-rate limits "
                f"(hard >{CIRCUIT_BREAKER_ERROR:.0%} or 24h >{CIRCUIT_BREAKER_24H:.0%} with enough volume); "
                "using the least-bad model. "
            ) + base_reason
        else:
            meta["mode"] = base_mode
            if meta.get("circuit_breaker_trimmed"):
                meta["reason"] = (
                    f"{base_reason} ({meta['circuit_breaker_trimmed']} model(s) withheld by circuit breaker.)"
                )
            else:
                meta["reason"] = base_reason

        self._stage_call_counts[effective_stage] = stage_calls + 1
        return chosen, meta

    def update_ema(
        self,
        model_id: str,
        latency_ms: int,
        cost_usd: float,
        quality_score: float | None = None,
    ) -> None:
        """Update in-memory EMA after a call. PG is source of truth on restart."""
        spec = MODEL_ROSTER.get(model_id)
        if not spec:
            return
        spec.call_count += 1
        spec.ema_latency_ms = EMA_ALPHA * latency_ms + (1 - EMA_ALPHA) * spec.ema_latency_ms
        spec.ema_cost_usd   = EMA_ALPHA * cost_usd   + (1 - EMA_ALPHA) * spec.ema_cost_usd
        if quality_score is not None:
            spec.ema_quality    = EMA_ALPHA * quality_score + (1 - EMA_ALPHA) * spec.ema_quality
            spec.quality_samples += 1

    def observe_quality(self, model_id: str, quality_score: float) -> None:
        """Apply an external quality observation (e.g. post-run adjudicator) without bumping call_count."""
        spec = MODEL_ROSTER.get(model_id)
        if not spec:
            return
        spec.ema_quality = EMA_ALPHA * quality_score + (1 - EMA_ALPHA) * spec.ema_quality
        spec.quality_samples += 1

    def get_stats_summary(self) -> list[dict]:
        """Admin endpoint — current model scores."""
        return [
            {
                "model_id":        m.model_id,
                "display_name":    m.display_name,
                "provider":        m.provider,
                "enabled":         m.enabled,
                "hipaa_eligible":  m.hipaa_eligible,
                "confidence":      m.confidence,
                "ema_quality":     round(m.ema_quality, 3),
                "ema_latency_ms":  round(m.ema_latency_ms),
                "ema_cost_usd":    round(m.ema_cost_usd, 5),
                "call_count":      m.call_count,
                "quality_samples": m.quality_samples,
                "prior_mean":      round(
                    m.beta_prior[0] / (m.beta_prior[0] + m.beta_prior[1]), 3
                ),
            }
            for m in MODEL_ROSTER.values()
        ]

    # ── Selection helpers ─────────────────────────────────────────────────────

    def _apply_router_mode_filter(
        self,
        candidates: list[ModelSpec],
        mode: str | None,
    ) -> tuple[list[ModelSpec], str | None]:
        """Copilot: drop heavy benchmark categories before Thompson / exploration."""
        m = (mode or "").strip().lower()
        if m != "copilot" or not candidates:
            return candidates, None
        excl = COPILOT_EXCLUDED_THOMPSON_BENCHMARK_CATEGORIES
        filtered = [c for c in candidates if c.benchmark_category not in excl]
        if filtered:
            return filtered, None
        allow = COPILOT_ALLOWED_THOMPSON_FALLBACK_CATEGORIES
        fallback = [c for c in candidates if c.benchmark_category in allow]
        if fallback:
            return fallback, "copilot_relaxed_to_allowed_benchmark_categories"
        logger.warning(
            "Copilot router_mode: no candidate outside %s; using unfiltered pool for this pick",
            excl,
        )
        return candidates, "copilot_fallback_unfiltered_pool"

    def _get_candidates(self, stage: str, phi_detected: bool) -> list[ModelSpec]:
        """Models eligible for ``stage``. ReAct ``react_*`` shares the same pool as ``planner``."""
        out: list[ModelSpec] = []
        planner_like = stage == "planner" or stage.startswith("react_")
        for m in MODEL_ROSTER.values():
            if not m.enabled:
                continue
            if phi_detected and not m.hipaa_eligible:
                continue
            es = m.eligible_stages
            if stage == "phi_detector":
                if "phi_detector" not in es:
                    continue
            elif set(es) == {"phi_detector"}:
                # Prompt-guard specialist — not a candidate for planner/RAG/etc.
                continue
            matches = stage in es or (stage.startswith("react_") and "planner" in es)
            if not matches:
                continue
            if planner_like and m.spec_context_k < MIN_PLANNER_CONTEXT_K:
                continue
            if (
                planner_like
                and m.provider == "groq"
                and m.model_id in GROQ_MODEL_IDS_EXCLUDE_PLANNER_REACT
            ):
                continue
            deep_min_k = _react_deep_rounds_min_context_k()
            react_rn = react_round_from_stage(stage)
            if (
                planner_like
                and deep_min_k > 0
                and react_rn is not None
                and react_rn >= 3
                and m.spec_context_k < deep_min_k
            ):
                continue
            out.append(m)
        return out

    def _apply_circuit_breakers(
        self,
        candidates: list[ModelSpec],
        stats: dict[str, dict],
        stage: str,
    ) -> tuple[list[ModelSpec], bool]:
        """Return (filtered_candidates, relief_all_tripped).

        When every candidate would be pulled, we return a single least-bad model and
        ``relief_all_tripped=True`` so the UI can explain the override.
        """
        safe = []
        for spec in candidates:
            s = stats.get(spec.model_id, {})
            hard_err   = s.get("hard_error_rate", 0.0)
            err_24h    = s.get("error_rate_24h", 0.0)
            total_calls = s.get("total_calls", 0)

            # Only apply circuit breakers when we have enough data
            if total_calls < 20:
                safe.append(spec)
                continue

            if hard_err > CIRCUIT_BREAKER_ERROR:
                logger.warning(
                    "Circuit breaker [hard]: stage=%s model=%s hard_err=%.1f%%",
                    stage, spec.model_id, hard_err * 100,
                )
                continue

            if err_24h > CIRCUIT_BREAKER_24H:
                logger.warning(
                    "Circuit breaker [24h]: stage=%s model=%s err_24h=%.1f%%",
                    stage, spec.model_id, err_24h * 100,
                )
                continue

            safe.append(spec)

        if not safe:
            # All tripped — use least-bad
            logger.error(
                "All candidates tripped circuit breakers for stage=%s — using least-bad",
                stage
            )
            return [
                min(
                    candidates,
                    key=lambda m: stats.get(m.model_id, {}).get("hard_error_rate", 1.0),
                )
            ], True

        return safe, False

    def _forced_explore(
        self,
        candidates: list[ModelSpec],
        stats: dict[str, dict],
    ) -> ModelSpec:
        """Force the model with fewest quality samples (or fewest total calls if priors-only)."""
        if _bandit_priors_only():
            return min(
                candidates,
                key=lambda m: stats.get(m.model_id, {}).get("total_calls", 0),
            )
        return min(
            candidates,
            key=lambda m: stats.get(m.model_id, {}).get("quality_samples", 0),
        )

    def _thompson_select(
        self,
        candidates: list[ModelSpec],
        stats: dict[str, dict],
        stage: str,
    ) -> ModelSpec:
        """Sample from each model's Beta distribution. Pick highest sample."""
        best: ModelSpec | None = None
        best_sample = -1.0

        for spec in candidates:
            s = _bandit_stats_row(stats.get(spec.model_id, {}))
            state = _build_bandit_state(spec, s, stage=stage)
            draw  = state.sample()
            if draw > best_sample:
                best_sample = draw
                best = spec

        return best or candidates[0]

    # ── PG refresh ────────────────────────────────────────────────────────────

    def _maybe_refresh(self) -> None:
        import time
        now = time.monotonic()
        if now - self._last_refresh < self.REFRESH_INTERVAL_S:
            return
        self._last_refresh = now
        try:
            import asyncio
            loop = asyncio.get_running_loop()
            loop.create_task(self._refresh_async())
        except RuntimeError:
            pass  # no running loop — will refresh next cycle

    async def _refresh_async(self) -> None:
        """Load per-stage per-model stats from model_performance_by_stage view."""
        try:
            from app.services.pg_pool import get_pool
            pool = await get_pool()
            if not pool:
                return
            async with pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT
                        stage, model,
                        total_calls, quality_samples,
                        hard_error_rate, any_error_rate, rate_limit_rate,
                        error_rate_24h,
                        avg_latency_ms, p95_latency_ms, avg_cost_usd,
                        avg_quality, quality_stddev
                    FROM model_performance_by_stage
                """)
            new_stats: dict[str, dict[str, dict]] = {}
            for row in rows:
                stage = row["stage"]
                model = row["model"]
                new_stats.setdefault(stage, {})[model] = dict(row)

                # Also update in-memory EMA from PG truth
                spec = MODEL_ROSTER.get(model)
                if spec and row["quality_samples"] and row["quality_samples"] > 0:
                    if row["avg_quality"]:
                        spec.ema_quality = float(row["avg_quality"])
                    if row["avg_latency_ms"]:
                        spec.ema_latency_ms = float(row["avg_latency_ms"])
                    if row["avg_cost_usd"]:
                        spec.ema_cost_usd = float(row["avg_cost_usd"])
                    spec.quality_samples = int(row["quality_samples"] or 0)
                    spec.call_count = int(row["total_calls"] or 0)

            self._stage_stats = new_stats
            logger.debug("ModelRouter: refreshed stats for %d stage-model pairs", len(rows))
        except Exception as e:
            logger.warning("ModelRouter: PG refresh failed (non-fatal): %s", e)


# Singleton
_router = ModelRouter()

def get_router() -> ModelRouter:
    return _router


def enable_model(model_id: str) -> None:
    """Enable a model at runtime (e.g. after API key is added to env)."""
    spec = MODEL_ROSTER.get(model_id)
    if spec:
        spec.enabled = True
        logger.info("Model enabled: %s", model_id)


def _ollama_available_models() -> set[str]:
    """Names present on local Ollama (GET /api/tags). Empty set if unreachable."""
    try:
        from app.chat_config import get_chat_config

        base = (get_chat_config().llm.ollama_base_url or "http://localhost:11434").rstrip("/")
    except Exception:
        base = (os.environ.get("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
    url = f"{base}/api/tags"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode())
        out: set[str] = set()
        for m in data.get("models") or []:
            name = (m.get("name") or "").strip()
            if name:
                out.add(name)
        return out
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError) as e:
        logger.debug("Ollama /api/tags unavailable (%s): %s", base, e)
        return set()


def auto_enable_from_env() -> None:
    """
    Enable models based on available API keys in environment.
    Call once at worker startup.
    """
    groq_key      = os.environ.get("GROQ_API_KEY", "").strip()
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    together_key  = os.environ.get("TOGETHER_API_KEY", "").strip()
    openai_key    = os.environ.get("OPENAI_API_KEY", "").strip()

    enabled = []
    ollama_available: set[str] | None = None
    for spec in MODEL_ROSTER.values():
        if spec.provider == "vertex":
            pass  # always enabled if vertex is configured
        elif spec.provider == "groq" and groq_key:
            spec.enabled = True
            enabled.append(spec.model_id)
        elif spec.provider == "anthropic" and anthropic_key:
            spec.enabled = True
            enabled.append(spec.model_id)
        elif spec.provider == "together" and together_key:
            spec.enabled = True
            enabled.append(spec.model_id)
        elif spec.provider == "openai" and openai_key:
            spec.enabled = True
            enabled.append(spec.model_id)
        elif spec.provider == "ollama":
            if ollama_available is None:
                ollama_available = _ollama_available_models()
            if spec.model_id in ollama_available:
                spec.enabled = True
                enabled.append(spec.model_id)
            else:
                spec.enabled = False

    if enabled:
        logger.info("Auto-enabled models from env: %s", enabled)
