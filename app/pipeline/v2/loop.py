"""Orchestrator v2 — the governor's OWN round loop.

Ananth, 2026-09-11: *"so you don't have your own loop — why not start that, so
that we can really have a real A/B where both can complete."*

He is right, and it is the ceiling everything else hit tonight.

WHAT WAS WRONG WITH THE NARROW EXECUTOR
=======================================
Step 2 substituted v2's decision into v1's loop. That was the correct first
move — it held prompts, manifest, model and publish byte-identical, so a
divergence was attributable to the decision. But a substitution can only answer
the questions the host loop happens to ask, at the moments it asks them, and
v1's loop asks exactly one: *"the model says it is done — stop, extend, or ship
with a warning?"*

Measured consequences, all from live dev traffic today:

  * v2 decided on 81 of 342 rounds. 24%. On the rest its posture was computed,
    recorded, and discarded.
  * On q20 it said NARROW twice — "stop, nothing here is worth buying" — and
    the turn spent another 12s and a web_scrape and still answered "I was
    unable to find any information."
  * On the three-payer question it said EXPLORE with 62.6s of a 95s promise
    left, converging, naming the Sunshine Health gap it wanted to close. The
    model self-completed after round 2 and the answer said it could not find
    two of the three payers.

THE ASYMMETRY I BUILT, NAMED
============================
The framing hook can STOP a turn and cannot EXTEND one. I gave the governor a
brake and no accelerator. For an overspending turn that was right; for a turn
exiting with budget in hand it is exactly backwards, and it is not fixable
inside someone else's control flow — the loop belongs to v1, and only the owner
of a loop can decide to go round again.

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
=============================================
It is NOT a port of react_loop.py. That file is 6,451 lines with ten documented
port hazards, the first of which is that `max_it` grows mid-turn so a `range()`
loop silently drops every extension AND THE TESTS PASS. A rewrite loses things
no author finds in their own code.

It is a loop over functions react ALREADY exposes, every one of them imported
rather than reimplemented:

    _react_reasoning_system   the round's system prompt  (prompt_blocks)
    build_reasoning_context   the round's user message
    _call_llm_json            the model call             (LLM manager, bandit)
    _execute_tool_with_retry  tool dispatch              (manifest, registry, MCP)
    _finalize_response        publish                    (one terminal, always)

So the experiment holds: **the decision loop varies, and nothing else does.**
Prompts, manifest, model roster, retry semantics, publish path and renderer are
the same code both arms run. What v2 owns is the SEQUENCE — whether to go round
again, what to aim the round at, and when to stop.

ONE TERMINAL. Ananth, 2026-09-11: *"there should just be one way to communicate
out even on early exit, I have had too much trouble with that."* Every exit
from this loop goes through _finalize_response. There is no second publish.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Any

from app.pipeline.v2 import executor as ex
from app.pipeline.v2 import posture as P
from app.pipeline.v2 import shadow as sh

logger = logging.getLogger(__name__)

# A fuse, not a target. v1's own ceilings are 2/3/10 by mode; this bounds the
# LOOP so a defect in the decision core cannot spend without limit. The
# executor already carries MAX_V2_EXTENSIONS; this is the outer bound on total
# rounds and exists because on 2026-09-11 a decision-core defect produced 98
# rounds on one turn, and the thing that eventually stopped it was a person
# watching.
MAX_ROUNDS_HARD = 12          # [GUESS] — a fuse

# When the model returns nothing usable we do NOT silently retry forever: two
# consecutive unusable rounds ends the turn. A loop that cannot tell "the model
# is stuck" from "keep trying" is how a runaway starts.
MAX_UNUSABLE_ROUNDS = 2


class V2LoopResult:
    """What the loop did, for the record. Never the answer itself."""

    def __init__(self) -> None:
        self.rounds: list[dict] = []
        self.exit_mode: str | None = None
        self.stopped_by: str | None = None
        self.answer: str = ""


def _round_state(ctx: Any, round_index: int, elapsed_s: float,
                 extensions_used: int) -> P.RoundState | None:
    """The state this round's decision is made on.

    Uses shadow.state_from_ctx — the SAME builder the observer uses — so a v2
    turn's decisions are made on exactly the state a v2 shadow would have
    reported for it. Two builders would drift, and the first place they drifted
    would be the place the comparison mattered.
    """
    return sh.state_from_ctx(
        ctx,
        round_index=round_index,
        elapsed_s=elapsed_s,
        promise_latency_s=sh.promise_seconds(ctx, None),
        round_cost_s=0.0,
        acting_cost_s=0.0,
    )


def run_react_v2(ctx: Any, emitter: Any = None) -> None:
    """v2's own loop: FRAME → EXPLORE ⇄ → NARROW → ALTERNATIVES → VALIDATE →
    COMMUNICATE, with the governor deciding every transition.

    Signature matches run_react(ctx, emitter) so the orchestrator can route to
    one or the other without knowing which it called.
    """
    from app.pipeline.react.prompts import (
        _call_llm_json,
        _react_reasoning_system,
        build_reasoning_context,
        react_chat_mode_label,
        react_max_iterations_for_mode,
    )
    from app.pipeline.react_loop import (
        _execute_tool_with_retry,
        _finalize_response,
    )

    def emit(msg: str) -> None:
        if emitter:
            try:
                emitter(msg)
            except Exception:
                pass

    t0 = time.monotonic()
    res = V2LoopResult()
    mode = react_chat_mode_label(getattr(ctx, "chat_mode", None))
    # v1's ceiling for this mode is the SOFT bound; MAX_ROUNDS_HARD is the fuse.
    # Taking v1's number keeps the arms comparable on the one axis the governor
    # is supposed to be deciding rather than inheriting.
    max_rounds = min(react_max_iterations_for_mode(mode), MAX_ROUNDS_HARD)

    tool_results: list[dict] = []
    ctx.react_trace_rounds = getattr(ctx, "react_trace_rounds", None) or []
    all_sources: list = []
    last_tool: str | None = None
    unusable = 0
    extensions_used = 0

    emit("starting v2 (governor loop)…")

    rn = 0
    while rn < max_rounds:
        rn += 1
        elapsed = time.monotonic() - t0

        # ── 1. DECIDE, before spending anything ─────────────────────────────
        state = _round_state(ctx, rn, elapsed, extensions_used)
        if state is None:
            # No state means no decision. Ending here is honest; guessing is
            # not. The turn still publishes through the one terminal.
            res.stopped_by = "no_state"
            break

        decision = P.select(state)
        exit_mode = P.exit_mode(state)
        action = ex.decide(decision, exit_mode, extensions_used=extensions_used)
        inputs_record = P.explain(state, decision)

        logger.info(
            "[v2.loop] cid=%s round=%s branch=%s posture=%s -> %s gaps=%d "
            "remaining=%.1fs",
            (getattr(ctx, "correlation_id", "") or "")[:8], rn,
            decision.branch, decision.posture.value, action.directive,
            len(inputs_record.get("open_gaps") or []),
            inputs_record.get("budget", {}).get("remaining_s", -1),
        )

        res.rounds.append({
            "round": rn,
            "orchestrator_version": "v2",
            "v2_posture": decision.posture.value,
            "v2_directive": decision.directive.value if decision.directive else None,
            "v2_because": action.because,
            "v2_gap_targeted": decision.gap_targeted,
            "v2_overran": action.overran,
            "v2_applied": True,          # in THIS loop the decision is the act
            "v2_directive_applied": action.directive,
            "v2_prompt_mismatch": action.prompt_mismatch,
            "v2_decision_inputs": inputs_record,
            "gaps_opened": [g.gap_id for g in state.open_gaps],
            "gaps_closed": list(state.gaps_closed),
            # v1 did not run this turn. NULL, not a guessed value: a comparison
            # reading a fabricated v1 directive would be comparing v2 to a
            # prediction of v1 rather than to v1.
            "v1_directive": None,
            "v1_reason": None,
            "verdict": None,
        })

        if not action.continues:
            res.exit_mode = (action.exit_mode or exit_mode).value \
                if hasattr(action.exit_mode or exit_mode, "value") else str(exit_mode)
            res.stopped_by = decision.branch
            emit(f"  governor: {decision.posture.value} — {action.because[:90]}")
            break

        # ── 2. ACT — the same prompt, model and tools v1 would use ──────────
        extensions_used += 1
        try:
            system, prompt_source = _v2_system_prompt(
                max_rounds, mode, getattr(ctx, "user_profile", None),
                _react_reasoning_system,
            )
            user = build_reasoning_context(ctx, tool_results, rn, max_rounds)
            raw = _call_llm_json(system, user, ctx=ctx, stage="planner")
            res.rounds[-1]["v2_prompt_source"] = prompt_source
        except Exception as exc:
            logger.warning("[v2.loop] round %s model call failed: %s", rn, exc)
            res.stopped_by = "model_error"
            break

        decision_json = raw if isinstance(raw, dict) else {}
        tool = (decision_json.get("tool") or "").strip() or None
        answer = (decision_json.get("answer") or "").strip()

        if decision_json.get("is_complete") and answer:
            # THE MODEL SAYS IT IS DONE. In v1 this ends the turn. Here it is
            # an INPUT to the next decision, not the decision itself -- which
            # is the entire reason this loop exists. The governor gets the next
            # round to disagree, and on the three-payer question it would have.
            res.answer = answer
            emit("  model proposes complete — governor reviewing…")
            # Record the proposal and loop; the next iteration's select() sees
            # the updated gap state and decides whether to accept it.
            ctx.react_trace_rounds.append({
                "round": rn, "tool": None, "inputs": {},
                "enrichment": _enrichment_from(decision_json),
            })
            if _no_gaps_left(decision_json):
                res.stopped_by = "model_complete_no_gaps"
                break
            continue

        if not tool:
            unusable += 1
            if unusable >= MAX_UNUSABLE_ROUNDS:
                res.stopped_by = "unusable_rounds"
                break
            continue
        unusable = 0

        emit(f"  round {rn}: {tool}")
        try:
            # KEYWORDS, against the real signature — it takes round_num and
            # emit_fn, not the positional shape I first assumed. Checked with
            # inspect.signature rather than from memory, because every
            # positional guess in this codebase today has been wrong.
            result = _execute_tool_with_retry(
                tool=tool,
                inputs=decision_json.get("inputs") or {},
                ctx=ctx,
                round_num=rn,
                emit_fn=emit,
                open_gaps=_open_gap_texts(state),
            )
        except Exception as exc:
            logger.warning("[v2.loop] tool %s failed: %s", tool, exc)
            result = {"tool": tool, "success": False, "result": ""}

        tool_results.append(result)
        last_tool = tool
        for s in (result.get("sources") or []):
            all_sources.append(s)
        ctx.react_trace_rounds.append({
            "round": rn, "tool": tool,
            "inputs": decision_json.get("inputs") or {},
            "enrichment": _enrichment_from(decision_json),
        })

    else:
        # Loop exhausted its rounds without a governor decision to stop. This
        # is a BUDGET exit and it is recorded as one -- not as a completion.
        res.stopped_by = "max_rounds"
        res.exit_mode = res.exit_mode or "budget"

    # ── 3. ONE TERMINAL ─────────────────────────────────────────────────────
    # "There should just be one way to communicate out even on early exit."
    # Every path above reaches here.
    try:
        ctx.v2_shadow_rounds = res.rounds
        ctx.orchestrator_version = "v2"
        ctx.v2_exit_mode = res.exit_mode
        ctx.v2_stopped_by = res.stopped_by
    except Exception:
        pass

    answer = res.answer or _best_running_answer(ctx) or ""
    logger.info("[v2.loop] cid=%s DONE rounds=%d stopped_by=%s exit=%s len=%d",
                (getattr(ctx, "correlation_id", "") or "")[:8],
                len(res.rounds), res.stopped_by, res.exit_mode, len(answer))
    from app.pipeline.react_loop import RETRIEVAL_SIGNAL_NO_SOURCES
    _finalize_response(ctx, answer, all_sources,
                       RETRIEVAL_SIGNAL_NO_SOURCES, last_tool, emitter)


# ── helpers, deliberately tiny ──────────────────────────────────────────────

def _enrichment_from(decision_json: dict) -> dict | None:
    """react's evidence_review, in react's own shape.

    Built here in the SAME shape state_from_ctx reads, because that function is
    the single reader of it. A second shape would mean v2's loop and v2's
    observer disagreed about what a round found.
    """
    er = decision_json.get("evidence_review")
    if not isinstance(er, dict):
        return None
    return {
        "learned": decision_json.get("thought") or "",
        "running_answer": er.get("running_answer") or "",
        "gaps_closed": [g for g in (er.get("gaps_closed") or []) if isinstance(g, str)],
        "gaps_open": [g for g in (er.get("gaps_open") or []) if isinstance(g, str)],
    }


def _no_gaps_left(decision_json: dict) -> bool:
    er = decision_json.get("evidence_review")
    if not isinstance(er, dict):
        return True
    return not [g for g in (er.get("gaps_open") or []) if isinstance(g, str)]


def _open_gap_texts(state: P.RoundState) -> list[str]:
    return [g.text for g in state.open_gaps if g.gap_id != P.ROOT_GAP_ID]


def _best_running_answer(ctx: Any) -> str:
    """The model's own best-so-far, from the last round that produced one.

    The governor decides WHEN to stop; it never decides WHAT to say. If it
    stops a turn, the answer is whatever the model had already built from the
    evidence it had -- never a governor-written string.
    """
    for r in reversed(getattr(ctx, "react_trace_rounds", None) or []):
        enr = (r or {}).get("enrichment") or {}
        ra = str(enr.get("running_answer") or "").strip()
        if ra:
            return ra
    return ""


# ── v2's OWN prompts ────────────────────────────────────────────────────────
#
# Ananth, 2026-09-11: "you can create your own prompts as against using the
# prompts from v1 -- rather let v1 reuse its prompt, and over time you will
# replace tool selection and prompt selection with your own."
#
# This changes the experiment deliberately and the cost has to be stated:
# once prompts vary too, a divergence is no longer attributable to the DECISION
# alone. That was the whole design of step 2, and it was right for step 2.
#
# But there is a sharper reason he is right, which I did not see until this
# loop existed. v1's reasoning prompt DESCRIBES V1'S MACHINE -- it renders
# "Up to 3 reasoning rounds -- copilot: faster path" and the round-budget
# contract that goes with it. This loop does not have v1's round semantics; it
# decides per round on gaps and affordability. So feeding it v1's prompt is not
# holding a variable constant, it is TELLING THE MODEL IT IS IN A MACHINE IT IS
# NOT IN. The same prompt across two different loops is a mismatch, not a
# control -- which is the PROMPT_MISMATCH idea from the executor, one level up.
#
# v2 READS PROMPTS FROM prompt_blocks ONLY. [RULED] No prompt text in v2 code.
# These keys are the LLM seat's to author; until they exist the loop falls back
# to v1's prompt and RECORDS that it did, so no comparison can silently credit
# v2 with a prompt it never had.
# The module_key the LLM seat authors against. Absent today, which is why
# every round currently records source="v1_fallback".
V2_MODULE_KEY = "react.v2_governor"


def _v2_system_prompt(max_rounds: int, mode: str, user_profile: dict | None,
                      v1_builder) -> tuple[str, dict]:
    """(prompt, provenance). `provenance` names the composition, not a flag.

    The source is RECORDED rather than assumed. A run whose prompts silently
    came from v1 while the harness reported "prompts varied" would be the
    reverse of tonight's cost_usd defect: a field claiming a provenance the
    value does not have.
    """
    # resolve_composition_sync is react's OWN block reader — the same function
    # _react_reasoning_system uses, against a different module_key. Found by
    # reading the call site rather than guessing an API: my first version
    # imported prompt_blocks.get_block, which does not exist. It would have
    # thrown, been swallowed, and fallen back to v1 forever while the loop
    # reported it was using v2 prompts — a silent degrade of exactly the kind
    # this function's `source` return value exists to make impossible.
    try:
        from app.services.prompt_manager import resolve_composition_sync

        rc = resolve_composition_sync(
            V2_MODULE_KEY,
            conditions={"has_user_profile": bool(
                (user_profile or {}).get("rendered_prompt"))},
            template_vars={
                "max_rounds": max_rounds,
                "mode": mode,
                "user_profile_text": (user_profile or {}).get("rendered_prompt") or "",
            },
        )
        # `.system_prompt`, read off the RenderedComposition DATACLASS, not
        # guessed. My first version tried `.text` / `.rendered` -- neither
        # exists -- which would have returned None and fallen back to v1
        # forever WHILE THE SOURCE FIELD SAID v2. That is the identical defect
        # I had just confessed one line above (importing a get_block that does
        # not exist), committed again in the same function. Reading the call
        # site fixed the import; I then guessed the RETURN SHAPE instead of
        # reading the class.
        if rc is not None and (rc.system_prompt or "").strip():
            # The LLM seat's scoping (2026-09-11): react.v2_governor reuses
            # response_shape / format_rules / tool_manifest / user_profile
            # unchanged and replaces only the identity + critical_rules framing
            # -- the parts describing v1's fixed "up to N rounds" machine.
            #
            # So a binary "v2_blocks" is TOO COARSE: a mostly-shared
            # composition would report itself as wholly v2's. The composition
            # id, hash and block manifest are what actually say which prompt
            # ran, and they are already how llm_calls attributes a prompt.
            return rc.system_prompt, {
                "source": "v2_composition",
                "composition_id": rc.composition_id,
                "composition_hash": rc.composition_hash,
                "blocks": [f"{k}@{v}" for k, v in (rc.manifest or ())],
                "variant_id": rc.variant_id,
            }
    except Exception as exc:
        logger.debug("[v2.loop] v2 prompt composition unavailable: %s", exc)
    return v1_builder(max_rounds, mode, user_profile), {"source": "v1_fallback"}
