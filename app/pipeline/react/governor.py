"""Product Promise round governor — the contract-driven replacement for
react's scattered, hardcoded round policies (Ananth's proposal, spec'd in
docs/REACT_PRODUCT_PROMISE_SPEC.md, owned by the "Product promise contract"
agent; this module is ReAct's side of the implementation, built to their
spec against react_loop.py's actual mechanics).

Gated entirely behind ``MOBIUS_PRODUCT_PROMISE_ENABLED`` (default OFF) per
Chat Architecture's ruling, 2026-07-30: when OFF, none of this runs and
today's `react_max_iterations_for_mode()`/`is_guidance_round()`/
`critic_enabled()`/`should_run_critic()` behavior is completely unchanged.
The flag is the ONLY thing authorized to bypass the existing critic gates —
nothing in this module bypasses them independently.

Scope note: this module DOES now touch composition selection, per Chat
Architecture's ruling, 2026-07-30 (option (b), confirmed): `directive`
REPLACES `react_agent_role()` as the live selector for the
react_explore/synthesize/draft compositions (see
`_DIRECTIVE_TO_AGENT_ROLE`/`directive_to_agent_role()` below) — the
selector logic changed, the 3 compositions themselves did not (still
byte-identical content; Phase B/temperature-routing stays closed,
`directive` owns that intent now). The actual selector swap lives in
react_loop.py's composition-resolution block, not in this module —
this file only supplies the mapping function.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

Directive = Literal["search", "consolidate", "extend", "finalize", "complete"]
ConfidenceBar = Literal["high", "medium", "low"]

# Same clamp semantics as app/worker/run.py's _turn_deadline_seconds() —
# duplicated rather than imported to avoid a pipeline->worker-entrypoint
# import (worker/run.py is a process entrypoint, not a library module).
_DEFAULT_TURN_DEADLINE_S = 90
_TURN_DEADLINE_MIN_S = 10
_TURN_DEADLINE_MAX_S = 900

# Margin subtracted from the hard ceiling before triggering the governor's
# own hard-stop — leaves headroom for the finalize path itself (LLM call +
# response assembly) to complete before the infra-level MOBIUS_TURN_DEADLINE_S
# fires and kills the whole turn with no response at all.
FINALIZE_MARGIN_S = 5.0


def product_promise_enabled() -> bool:
    return (os.environ.get("MOBIUS_PRODUCT_PROMISE_ENABLED") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _turn_deadline_seconds() -> int:
    raw = (os.environ.get("MOBIUS_TURN_DEADLINE_S") or "").strip()
    if not raw:
        return _DEFAULT_TURN_DEADLINE_S
    try:
        n = int(raw)
        return max(_TURN_DEADLINE_MIN_S, min(_TURN_DEADLINE_MAX_S, n))
    except ValueError:
        return _DEFAULT_TURN_DEADLINE_S


@dataclass(frozen=True)
class ProductPromiseContract:
    max_rounds: int
    max_extension_rounds: int
    confidence_bar: ConfidenceBar
    soft_target_s: float
    hard_ceiling_s: float  # clamped to <= MOBIUS_TURN_DEADLINE_S at construction
    tone: str = ""
    reasoning_visibility: Literal["high", "medium", "low"] = "medium"


# Per-mode defaults, Chat Master-approved (SPEC_REACT_PRODUCT_PROMISE, §
# "Per-mode defaults"). hard_ceiling_s is filled in at construction time via
# default_contract_for_mode() — clamped to the actual runtime turn deadline,
# not hardcoded here, so it can never promise more than infra allows.
_MODE_DEFAULTS: dict[str, dict] = {
    "quick": {"max_rounds": 2, "max_extension_rounds": 0, "confidence_bar": "low", "soft_target_s": 6.0},
    "copilot": {"max_rounds": 3, "max_extension_rounds": 1, "confidence_bar": "medium", "soft_target_s": 12.0},
    "agentic": {"max_rounds": 10, "max_extension_rounds": 3, "confidence_bar": "high", "soft_target_s": 120.0},
    "task": {"max_rounds": 3, "max_extension_rounds": 0, "confidence_bar": "medium", "soft_target_s": 15.0},
}


def default_contract_for_mode(
    chat_mode: str | None, *, tone: str = "", reasoning_visibility: str = "medium",
) -> ProductPromiseContract:
    """Build the per-mode default contract. `hard_ceiling_s` is always the
    real runtime turn deadline (never a value baked in at spec-writing
    time) so a later MOBIUS_TURN_DEADLINE_S change is picked up automatically."""
    mode = (chat_mode or "copilot").strip().lower()
    defaults = _MODE_DEFAULTS.get(mode, _MODE_DEFAULTS["copilot"])
    hard_ceiling_s = float(_turn_deadline_seconds())
    return ProductPromiseContract(
        max_rounds=defaults["max_rounds"],
        max_extension_rounds=defaults["max_extension_rounds"],
        confidence_bar=defaults["confidence_bar"],
        soft_target_s=min(defaults["soft_target_s"], hard_ceiling_s),
        hard_ceiling_s=hard_ceiling_s,
        tone=tone,
        reasoning_visibility=reasoning_visibility,  # type: ignore[arg-type]
    )


@dataclass(frozen=True)
class RoundState:
    """Per-round inputs to evaluate(). Built fresh each round by the caller
    (react_loop.py) from the parsed decision + loop bookkeeping."""

    proposes_complete: bool  # decision.get("is_complete", False)
    self_reported_confidence: str | None  # decision.get("confidence")
    critic_verdict: Literal["clean", "flagged"] | None  # existing should_run_critic()-gated
                                                          # critic path, independent of the new
                                                          # mandatory floor below — None if that
                                                          # path didn't run this round (heuristic
                                                          # gate skipped it, or MOBIUS_REACT_CRITIC off)
    groundedness_passed: bool | None  # None unless proposes_complete AND confidence_bar in
                                       # {"medium","high"} AND the mandatory floor ran this round
    elapsed_s: float
    base_rounds_remaining: int  # contract.max_rounds - rounds used so far (not counting extensions)
    extension_rounds_available: int  # contract.max_extension_rounds - extensions granted so far


def evaluate(contract: ProductPromiseContract, state: RoundState) -> tuple[Directive, str]:
    """Pure function — no I/O, no side effects. Returns (directive, reason).
    Precedence order matches SPEC_REACT_PRODUCT_PROMISE exactly; first match
    wins. `reason` is a short human-readable string for round_directive_text()
    and telemetry — not re-derived by the caller.
    """
    hard_stop_by_time = state.elapsed_s >= (contract.hard_ceiling_s - FINALIZE_MARGIN_S)
    hard_stop_by_rounds = state.base_rounds_remaining <= 0 and state.extension_rounds_available <= 0
    if hard_stop_by_time or hard_stop_by_rounds:
        reason = "time budget exhausted" if hard_stop_by_time else "round budget exhausted"
        return "finalize", f"budget exhausted ({reason}) — emit best available answer now"

    if state.proposes_complete:
        critic_clean = state.critic_verdict != "flagged"
        if critic_clean and (contract.confidence_bar == "low" or state.groundedness_passed is True):
            return "complete", "confidence bar met — emit is_complete"
        if state.groundedness_passed is False:
            if state.extension_rounds_available > 0 and state.elapsed_s < contract.soft_target_s:
                return "extend", "quality issue flagged — going deeper on the unsupported claim(s)"
            return "finalize", "groundedness check failed and no extension budget remains — ship with a warning"

    if state.elapsed_s >= contract.soft_target_s:
        return "consolidate", "time running low — synthesize from what you have, no more tool calls"

    if (
        state.base_rounds_remaining <= 0
        and state.extension_rounds_available > 0
        and state.elapsed_s < contract.soft_target_s
        and (state.self_reported_confidence not in ("high",) or state.critic_verdict == "flagged")
    ):
        return "extend", "confidence bar not yet met and round budget exhausted — extending"

    return "search", "confidence bar not met — keep gathering evidence"


def round_directive_text(
    *, round_n: int, max_rounds: int, elapsed_s: float, soft_target_s: float,
    directive: Directive, reason: str,
) -> str:
    """The per-round directive line injected into the context message
    (NOT a composition block — see this module's docstring for why)."""
    return (
        f"Round {round_n} of {max_rounds}. {elapsed_s:.0f}s elapsed of {soft_target_s:.0f}s soft target. "
        f"Directive: {reason}."
    )


# Composition-selector mapping (Chat Architecture ruling, 2026-07-30 — option
# (b) of the two the ReAct agent flagged: `directive` REPLACES
# `react_agent_role()` as the live selector for the same 3 compositions,
# which stay byte-identical content; only the selector logic changes.
# Phase B (temperature routing via agent_role) is closed — directive owns
# that intent now. "complete" has no entry: it's terminal (no next round to
# select a composition for) and is unreachable from a pre-round evaluate()
# call anyway, since that call always passes proposes_complete=False.
_DIRECTIVE_TO_AGENT_ROLE: dict[str, str] = {
    "search": "explore",
    "consolidate": "synthesize",
    "extend": "synthesize",
    "finalize": "draft",
}


def directive_to_agent_role(directive: Directive) -> str:
    """Maps a pre-round directive to the react_explore/synthesize/draft
    agent_role string `resolve_react_system_prompt_v2` expects. Raises on
    "complete" (or anything unrecognized) — that's a programming error at
    the call site, not a runtime condition to fail-soft on: a pre-round
    directive should never be "complete" (see the module comment above)."""
    return _DIRECTIVE_TO_AGENT_ROLE[directive]


# ── Model-bandit selection criteria (2026-08-04) ────────────────────────
#
# Ananth's react-prep ask: the model bandit should be able to select on
# latency/reasoning-depth per call, not just per-session chat_mode. Scoped
# with Chat Architecture + LLM Agent (who own the actual selection
# mechanics in model_registry.py/bandit_weights.py/llm_manager.py) + Eval
# (calibration-risk sign-off — confirmed safe: the Beta posterior is
# recomputed fresh per call from mode-invariant raw PG stats, so per-call
# reasoning_depth variance can't corrupt the learned quality prior).
# These two functions are ReAct's side of that split: pure, testable
# derivations from state react already has (agent_role, governor timing),
# producing the two new `router.select()`/`generate()` kwargs LLM Agent's
# side defines. Both gated behind MOBIUS_PRODUCT_PROMISE_ENABLED like
# everything else in this module — None when off, matching every other
# governor signal's fail-soft posture (LLM Agent's design: None on either
# kwarg is exactly today's pre-existing selection behavior).

_AGENT_ROLE_TO_REASONING_DEPTH: dict[str, str] = {
    "explore": "fast",       # a lookup round — cheap, latency-favoring model is fine
    "synthesize": "thinking",  # real synthesis work — favor quality
    "draft": "thinking",       # same — draft is the completing round, not a cheap lookup
}


def agent_role_to_reasoning_depth(agent_role: str | None) -> str | None:
    """Maps react's per-round agent_role to the bandit's reasoning_depth
    hint (a soft nudge into bandit_weights.py's existing fast/normal/
    thinking tables — no new weight math, just a finer-grained input than
    session-level chat_mode). None when agent_role is None (governor off,
    or the round hasn't resolved a composition-selector role) or
    unrecognized — the bandit's own mode-derived default applies.

    KNOWN LOSSY — kept for reference / any other caller, but react_loop.py's
    actual selection call uses ``directive_to_reasoning_depth()`` below
    instead. agent_role is a 3-bucket collapse of directive
    (_DIRECTIVE_TO_AGENT_ROLE above): both "consolidate" (time pressure —
    wrap up NOW) and "extend" (deliberately spending MORE budget on a
    groundedness problem, not time-pressured) collapse to "synthesize",
    which this function then maps to "thinking" for BOTH — backwards for
    consolidate, and "finalize" (budget exhausted, must respond
    immediately) collapses to "draft" -> also wrongly "thinking". Caught
    live 2026-08-04 (Ananth: "feels like the fast mode is not triggering
    right" on a real turn where consolidate fired and picked reasoning_depth
    =thinking) — not a triggering bug, a real mapping bug from routing
    through the lossy agent_role intermediate instead of directive directly."""
    if agent_role is None:
        return None
    return _AGENT_ROLE_TO_REASONING_DEPTH.get(agent_role)


_DIRECTIVE_TO_REASONING_DEPTH: dict[str, str] = {
    "search": "fast",        # exploring/looking things up — cheap is fine
    "consolidate": "fast",   # time pressure (soft_target_s exceeded) — wrap up FAST
    "extend": "thinking",    # deliberately spending more budget on a groundedness
                              # problem, NOT time-constrained — the one case where
                              # "spend more to get it right" is the actual intent
    "finalize": "fast",      # budget exhausted — must respond NOW, no time for a
                              # slower "thinking"-weighted model
}


def directive_to_reasoning_depth(directive: Directive | None) -> str | None:
    """Maps the governor's pre-round directive DIRECTLY to reasoning_depth
    — the precise version of agent_role_to_reasoning_depth() above, which
    loses the search/consolidate/extend/finalize distinction by routing
    through the 3-bucket agent_role first. Same fail-soft posture: None
    when directive is None (governor off) or "complete" (terminal, never
    seen at the pre-round call site this feeds)."""
    if directive is None:
        return None
    return _DIRECTIVE_TO_REASONING_DEPTH.get(directive)


def latency_budget_ms(
    contract: ProductPromiseContract, elapsed_s: float, directive: Directive | None,
) -> int | None:
    """Hard latency ceiling (ms) for the NEXT LLM call, for the bandit's
    hard pre-filter — only set when directive is "consolidate"/"finalize"
    (time is already tight; most rounds are unconstrained, returning None,
    per LLM Agent's design). Deliberately reuses the SAME time-accounting
    the governor uses for its own hard-stop (hard_ceiling_s,
    FINALIZE_MARGIN_S) rather than an independently-invented number, so
    the model-selection deadline and the governor's own emit-now deadline
    can't drift apart as two different formulas computing "how much time
    is left" differently.

    Returns None (unconstrained) once remaining time is already at/past
    the margin — at that point the governor's own hard-stop path is what
    handles it, not a latency filter on the next model pick."""
    if directive not in ("consolidate", "finalize"):
        return None
    remaining_s = contract.hard_ceiling_s - elapsed_s - FINALIZE_MARGIN_S
    if remaining_s <= 0:
        return None
    return int(remaining_s * 1000)


# ── Query-intent reasoning-depth floor (2026-08-04) ─────────────────
#
# Ananth, live testing: reasoning_depth was purely stage/directive-derived
# (see directive_to_reasoning_depth above) — a "generate a detailed report"
# turn and an "is X covered?" turn got identical per-round effort, even
# when the user was already in the highest-budget mode. The gap: mode
# selection is the coarse query-intent signal the user already gave us,
# but the per-round depth computation ignored it below the synthesize/
# finalize rounds. This adds a FLOOR derived from the raw message,
# combined with the stage-derived depth via resolve_reasoning_depth()
# below — floor semantics only, never lowers what the stage earned.

_DEPTH_RANK = {"fast": 0, "normal": 1, "thinking": 2}


def resolve_reasoning_depth(
    stage_depth: str | None, query_floor: str | None,
) -> str | None:
    """Combine the round's stage-derived depth with the turn's query-intent
    floor. Floor semantics only — query intent can RAISE depth above what
    the stage earned, never lower it: a "consolidate" round under governor
    time pressure stays fast even on a report query; equally, a report
    query's floor can't be dragged down by a stage that would otherwise
    want fast. None when both inputs are None (fail-soft, matches every
    other bandit-criteria function in this module)."""
    candidates = [d for d in (stage_depth, query_floor) if d is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda d: _DEPTH_RANK[d])


# Conservative on purpose (Chat Architecture's explicit instruction, citing
# Ananth's feedback): err toward None over a false "thinking" classification
# — a missed report query gets normal (stage-derived) treatment; a
# misclassified quick question gets over-allocated thinking, which costs
# latency/money for no benefit. Keyword heuristic, deliberately NOT an LLM
# classification call — same reasoning as declining Eval's empirical
# groundedness pre-read: don't spend a network hop + judge-call latency,
# pre-Round-1, on EVERY turn, for a signal a cheap deterministic check
# gets close enough on.
_THINKING_INTENT_KEYWORDS = (
    "report", "analysis", "breakdown", "detailed", "comprehensive",
    "summary of", "credentialing report", "assessment",
)


def extract_query_intent_floor(message: str) -> str | None:
    """Pre-Round-1 reasoning_depth floor derived from the raw user message
    — the query-intent half of resolve_reasoning_depth()'s inputs. Returns
    "thinking" when the message signals a report/analysis-shaped ask, else
    None (let the stage-derived depth decide entirely — this is a floor,
    not an override, so "no signal" must mean "no opinion")."""
    low = (message or "").lower()
    if any(kw in low for kw in _THINKING_INTENT_KEYWORDS):
        return "thinking"
    return None


# The other half of Ananth's ask ("give report mode really long token/time
# limits"): a deep-intent query should also get more wall-clock, not just a
# better model. 1.5x is a starting prior, not tuned against real turns yet.
_CEILING_SCALE_BY_INTENT: dict[str, float] = {"thinking": 1.5}
_CEILING_SCALE_ABSOLUTE_CAP_S = 600.0


def scale_ceiling_for_intent(base_ceiling_s: float, query_floor: str | None) -> float:
    """Extend hard_ceiling_s for deep-intent queries — 1.5x for "thinking"
    intent, unchanged (1.0x) otherwise. Capped at both
    _CEILING_SCALE_ABSOLUTE_CAP_S AND the real infra deadline
    (_turn_deadline_seconds()) — the governor must never promise more
    wall-clock than the worker's own background-thread deadline
    (MOBIUS_TURN_DEADLINE_S) will actually allow; that's a harder,
    independent kill-switch this function can't see past, and a contract
    promising time the infra will yank out from under it is worse than no
    scaling at all — an abrupt kill instead of a graceful finalize.

    Because default_contract_for_mode() already sets hard_ceiling_s to
    exactly _turn_deadline_seconds() for every mode (no per-mode headroom
    below the infra deadline today), this scaling has NO visible effect
    until MOBIUS_TURN_DEADLINE_S itself is raised above its current value —
    that's an intentional consequence of the clamp, not a bug in this
    function."""
    scale = _CEILING_SCALE_BY_INTENT.get(query_floor or "", 1.0)
    scaled = base_ceiling_s * scale
    return min(scaled, _CEILING_SCALE_ABSOLUTE_CAP_S, float(_turn_deadline_seconds()))
