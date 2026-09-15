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

# 🔴 v2's OWN ROUND CEILINGS. Ananth: "we develop our own round ceilings".
#
# This used to take react_max_iterations_for_mode(mode) — v1's number — with
# the comment "keeps the arms comparable on the one axis the governor is
# supposed to be deciding". That rationale was the A/B, and the A/B is not
# what this loop is for any more.
#
# Lower than v1's across the board, deliberately. v1 needs rounds because it
# starts blind: measured 2026-09-15, its pre-round state hardcodes confidence
# to None, reads absent as "bar not met", and spends round 1 searching on
# 40 turns out of 40. This loop preloads BEFORE round 1, so round 1 already
# has evidence and the ceiling is what a turn should need, not what a blind
# one does.
#
# [GUESS] on the numbers, not on the shape — they are ours to move now, and
# moving them is a measurement, not a merge conflict with v1.
V2_MAX_ROUNDS: dict[str, int] = {
    "quick":   2,    # a 13s promise cannot afford a third round
    "copilot": 4,
    "agentic": 6,
}
V2_MAX_ROUNDS_DEFAULT = 4

# When the model returns nothing usable we do NOT silently retry forever: two
# consecutive unusable rounds ends the turn. A loop that cannot tell "the model
# is stuck" from "keep trying" is how a runaway starts.
# 🔴 OUR OWN, AND NAMED FOR WHAT IT CATCHES. Ananth: "this could be tool
# failures or others.. we should use, but lets use our own".
#
# A round is UNUSABLE when the model returns neither an answer nor a tool to
# call — so the turn spent a model call and learned nothing. The causes are
# not all the model's: a tool that could_not_run leaves the round with nothing
# to reason over, and react then has nothing to propose. Counting them is how
# a turn stops burning rounds on a failure it cannot see.
#
# Two, not three: at v2's ceilings (quick 2, copilot 4, agentic 6) a third
# wasted round is most of the budget.
MAX_UNUSABLE_ROUNDS = 2

# ── THE FALLBACK RESERVE ────────────────────────────────────────────────────
#
# v2's loop must never spend the turn's whole budget, because when it produces
# nothing it hands the turn to v1 -- and v1 then needs time to run a full
# pipeline of its own. Live on 2026-09-11 it did exactly the wrong thing:
#
#   01:16:36  round 1, remaining 95.0s
#   01:20:01  round 2, remaining 0.0s      <- ONE round took 3m25s
#   01:20:01  deferred to v1
#   01:21:35  turn_deadline_exceeded (300s)
#
# The safety net spent the money the safety net needed. So the loop gets a
# FRACTION of the promise and leaves the rest for the fallback: a v2 turn that
# is going badly must fail EARLY and cheaply, while a v1 turn is still
# affordable.
#
# 0.6 is a [GUESS] chosen so a v2 turn still gets the majority and the
# remainder covers v1's measured p50 (rag rounds: p50 7.0s, p90 13.7s).
# 🔴 THE WHOLE PROMISE. Ananth, 2026-09-15: "v2 gets whole budget".
#
# This was 0.6 — v2 spent 60% and reserved 40% so that, when it produced
# nothing, it could hand the turn to v1 and v1 would still have time to run a
# full pipeline. On a 95s agentic promise that reserved 38 seconds of every
# turn for a fallback that no longer exists.
#
# The reserve and the fallback were one decision and they are removed together.
# A loop that owns its failures does not need to keep money aside for someone
# else's recovery.
V2_BUDGET_FRACTION = 1.0

# A round can overrun regardless -- spendable() is checked BEFORE a round and
# nothing stops one already running. This is the wall-clock backstop, checked
# at the top of every round against the turn's own clock rather than against
# the governor's arithmetic.
def _budget_exhausted(elapsed_s: float, promise_s: float) -> tuple[bool, float]:
    """(exhausted, seconds left for v2). Never lets v2 hold the whole turn."""
    allowance = max(0.0, promise_s * V2_BUDGET_FRACTION)
    return (elapsed_s >= allowance, max(0.0, allowance - elapsed_s))


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
    )
    from app.pipeline.react.parsing import _parse_react_decision_json
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

    # 🔴 THE CLOCK STARTS BEFORE THE TOOLS, NOT AFTER.
    #
    # I put the preload step above this line first, and
    # test_the_wall_clock_stops_a_loop_that_overran caught it: with t0 set
    # afterwards, preload's time did not count against the promise, and a turn
    # that had already overrun got its budget silently reset by the step that
    # spent the most of it. Preload is turn time. The promise covers it.
    t0 = time.monotonic()

    # ── TOOLS, BEFORE THE FIRST ROUND ───────────────────────────────────────
    #
    # 🔴 THIS LOOP HAD NO TOOLS STEP. It would have started every turn blind
    # while the shared loop preloaded — react reasoning from nothing, then
    # spending its first round fetching what preload already had.
    #
    # IMPORTED, NOT EXTRACTED. The equivalent block in react_loop.py is 251
    # lines and reachable only from v1's loop. I started by pulling it out into
    # a shared module — and that edits v1, which is the coupling this loop
    # exists to remove. Ananth: "i want you to create it because it will allow
    # us to refactor without impacting v1." A read-only import costs v1
    # nothing; moving its code costs it a regression surface.
    #
    # So the PRIMITIVES are shared (v2/preload.py is already v2-owned, and the
    # runner translates a tool result into react's result shape) and the
    # SEQUENCE is ours — which is the same rule the rest of this file follows.
    _preloaded = None
    try:
        from app.pipeline.v2 import preload as _v2pre
        from app.pipeline.react_loop import _preload_runner_toolreg

        _q = (getattr(ctx, "message", None) or "").strip()

        # 🔴 NO hasattr GUARD AROUND THE THING THAT DOES THE WORK. My first
        # draft called a helper I had not verified existed, behind
        # `if hasattr(...)` — so the tools step would have been silently
        # skipped on every turn and looked like a preload that found nothing.
        # A guard around a capability you have not checked is how a step that
        # never runs passes review.
        from toolreg.estimate import estimate as _tr_estimate

        _off = _tr_estimate(
            _q,
            caller_mode=react_chat_mode_label(getattr(ctx, "chat_mode", None)),
            correlation_id=getattr(ctx, "correlation_id", None),
        )
        # rag is the floor: v1's block falls back to ["rag"] when estimate
        # returns nothing, because a turn with no retrieval is worse than a
        # turn with an unranked one. Ananth: "no we will always do rag".
        _offer = [t.tool_key for t in (getattr(_off, "tools", None) or [])] or ["rag"]
        if _offer:
            _plan = _v2pre.plan(_offer)
            _preloaded = _v2pre.execute(
                _plan,
                lambda _t, _i: _preload_runner_toolreg(
                    _t, _i, ctx, speculative=True, emitter=emitter),
                _q,
            )
            # The literal 1 is deliberate and the reason is in react_loop's
            # copy: preload runs BEFORE the round loop, so no round variable
            # exists yet. Writing `rn` there shipped a turn with no evidence
            # and a trace that still said "Looking this up before I answer".
            ctx._v2_preload_round = 1
            ctx._v2_preloaded = _preloaded
            ctx._v2_suggest = getattr(_plan, "suggest", []) or []
            emit(f"  preloaded {len(_preloaded or [])} tool(s)")
    except Exception as _pre_e:   # pragma: no cover — never fail a turn on preload
        # Fail-soft, and SAY SO. react can reason without preload; it cannot
        # reason correctly about why its evidence is thin if nothing says the
        # step did not run.
        emit(f"  preload unavailable: {type(_pre_e).__name__}")

    res = V2LoopResult()
    # 🔴 THE ROUND EXECUTES WHAT THE LAST ROUND ASKED FOR, AT ITS TOP.
    #
    # Ananth's shape: "preload >> should we run >> yes - loop >> select tool +
    # execute (could be many) >> build the system prompt >> get the llm posture
    # >> execute react >> parse output and get ready for next".
    #
    # The order matters beyond tidiness. Executing at the END of a round means
    # a round's last act is a tool call whose result nothing has reasoned over
    # yet, so "what happened" is split across two iterations and the trace
    # reads out of order. Executing at the TOP makes a round self-contained:
    # here is the evidence, here is the thinking, here is what to fetch next.
    #
    # A LIST, not a tool. "could be many" is the point — react naming three
    # independent tools should cost one round, not three. Today they run in
    # sequence because chat's dispatch mutates ctx; once execution is Tool
    # Manifest's, the list is what lets them run at once.
    pending: list[tuple[str, dict]] = []
    mode = react_chat_mode_label(getattr(ctx, "chat_mode", None))
    # OUR ceiling for this mode; MAX_ROUNDS_HARD stays the fuse.
    max_rounds = min(V2_MAX_ROUNDS.get(mode, V2_MAX_ROUNDS_DEFAULT),
                     MAX_ROUNDS_HARD)

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

        # WALL CLOCK, before anything else. The governor's own arithmetic is
        # checked inside select(); this is the backstop for a round that
        # already overran it -- a single tool call took 205s against a 95s
        # promise and nothing noticed until the round ended.
        _spent, _left = _budget_exhausted(elapsed, sh.promise_seconds(ctx, None))
        if _spent:
            res.stopped_by = "v2_budget_exhausted"
            logger.warning(
                "[v2.loop] cid=%s out of its allowance after %.1fs (round %s) "
                "-- stopping so the fallback is still affordable",
                (getattr(ctx, "correlation_id", "") or "")[:8], elapsed, rn)
            break

        # ── 1. DECIDE, before spending anything ─────────────────────────────
        state = _round_state(ctx, rn, elapsed, extensions_used)
        if state is None:
            # No state means no decision. Ending here is honest; guessing is
            # not. The turn still publishes through the one terminal.
            res.stopped_by = "no_state"
            break

        decision = P.select(state)
        exit_mode = P.exit_mode(state)
        action = ex.decide(decision, exit_mode, extensions_used=extensions_used,
                           # See executor.decide: BUDGET is the label for "work
                           # remains", not for "out of money". The executor
                           # reads affordability, never infers it.
                           affordable=P.spendable(state))
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
            # 🔴 A TURN MUST NOT EXIT WITHOUT AN ANSWER.
            #
            # This `break` sits BEFORE the model call, so a governor that says
            # "stop" on round 1 exits having never asked the model to write
            # anything. That was unreachable while this loop had no tools step:
            # with nothing preloaded there was always a gap worth buying, so
            # round 1 always continued. Adding preload made it reachable
            # immediately — evidence in hand, no gap worth buying, and
            # `stopped_by=nothing_worth_buying` with zero LLM calls.
            #
            # Caught by test_a_full_round_runs_and_produces_an_answer, which is
            # exactly the test its docstring claims to be: "the test that would
            # have caught all three live failures". It caught a fourth.
            #
            # The governor decides whether to buy more EVIDENCE. It does not
            # decide whether the person gets an answer. So a stop with no
            # answer yet converts into one final communicate round rather than
            # an exit — and only then does the loop end.
            if not _best_running_answer(ctx):
                emit(f"  governor: {decision.posture.value} — "
                     f"{action.because[:70]} (one round to write it)")
                decision = P.Decision(P.Posture.COMMUNICATE,
                                      "stopping with no answer written yet",
                                      branch="forced_communicate")
            else:
                res.exit_mode = (action.exit_mode or exit_mode).value \
                    if hasattr(action.exit_mode or exit_mode, "value") else str(exit_mode)
                res.stopped_by = decision.branch
                emit(f"  governor: {decision.posture.value} — {action.because[:90]}")
                break

        # ── 2. TOOLS — whatever the last round asked for, before reasoning ──
        for _t, _in in pending:
            emit(f"  round {rn}: {_t}")
            try:
                # KEYWORDS against the real signature, checked with
                # inspect.signature rather than recalled: SIX required
                # parameters, and `tool_emitter` is a different channel from
                # `emit_fn`. Omitting it raised TypeError on a live turn and
                # the except swallowed it into "tool failed".
                _res = _execute_tool_with_retry(
                    _t, _in or {}, ctx, rn, emit, emitter,
                    skip_retry=(mode == "quick"),
                    open_gaps=_open_gap_texts(state),
                )
            except Exception as exc:
                logger.warning("[v2.loop] tool %s failed: %s", _t, exc)
                _res = {"tool": _t, "success": False, "result": ""}
            tool_results.append(_res)
            last_tool = _t
            for _s in (_res.get("sources") or []):
                all_sources.append(_s)
            ctx.react_trace_rounds.append({
                "round": rn, "tool": _t, "inputs": _in or {}, "enrichment": None,
            })
        pending = []

        # ── 3. ACT — the same prompt, model and tools v1 would use ──────────
        extensions_used += 1
        try:
            system, prompt_source = _v2_system_prompt(
                max_rounds, mode, getattr(ctx, "user_profile", None),
                _react_reasoning_system,
                allowed_tools=getattr(ctx, "allowed_tools", None),
                agent_role=_agent_role_for(decision.posture),
                posture=decision.posture,
            )
            user = build_reasoning_context(ctx, tool_results, rn, max_rounds)
            raw = _call_llm_json(system, user, max_tokens=2048, ctx=ctx,
                                 stage=f"react_{rn}")
            res.rounds[-1]["v2_prompt_source"] = prompt_source
            try:
                ctx.v2_prompt_source = prompt_source
            except Exception:
                pass
        except Exception as exc:
            logger.warning("[v2.loop] round %s model call failed: %s", rn, exc)
            res.stopped_by = "model_error"
            break

        # _call_llm_json RETURNS A STRING. My first version did
        # `raw if isinstance(raw, dict) else {}`, which was therefore ALWAYS
        # {} -- every round parsed as unusable, the loop bailed on its own
        # MAX_UNUSABLE_ROUNDS fuse, and it published an EMPTY answer that
        # everything downstream then filled with an ungrounded one. Zero tool
        # calls, zero sources, a confident three-payer comparison with nothing
        # behind it.
        #
        # Third return-shape I guessed today (get_block, RenderedComposition,
        # this). react's own parser handles the fence-stripping and balanced
        # -object extraction; reimplementing it would be a second parser to
        # drift.
        decision_json = _parse_react_decision_json(raw) or {}
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

        # ── 5. GET READY FOR NEXT — queue, do not execute ───────────────────
        #
        # The round ends by deciding what to fetch; the NEXT round's top
        # executes it. Nothing is called after the model has spoken, so a
        # round's trace reads in the order it happened.
        #
        # `tools` (a list) is read first and `tool` (one) is the fallback, so
        # react naming three independent tools costs ONE round. The prompt does
        # not offer the plural yet — this is the loop being ready for it rather
        # than the model being asked for it, and a list of one behaves exactly
        # as today until that prompt lands.
        _many = decision_json.get("tools")
        if isinstance(_many, list) and _many:
            for _entry in _many:
                if isinstance(_entry, dict) and _entry.get("tool"):
                    pending.append((_entry["tool"], _entry.get("inputs") or {}))
                elif isinstance(_entry, str):
                    pending.append((_entry, {}))
        else:
            pending.append((tool, decision_json.get("inputs") or {}))

        ctx.react_trace_rounds.append({
            "round": rn, "tool": tool,
            "inputs": decision_json.get("inputs") or {},
            "enrichment": _enrichment_from(decision_json),
        })

    # 🔴 A REQUESTED TOOL THAT NEVER RAN IS INFORMATION, NOT NOTHING.
    #
    # Executing at the TOP of a round means a tool react asked for in its LAST
    # round is never executed — the governor decided not to buy another round,
    # so the request dies with it. That is the right call (a tool the governor
    # would not fund does not become affordable by being already requested),
    # but it must be SAID. Silently dropping work the model asked for is the
    # defect this fleet has spent the week removing in every other form.
    if pending:
        _dropped = ", ".join(t for t, _ in pending)
        emit(f"  not run — turn ended before these could execute: {_dropped}")
        logger.info("[v2.loop] cid=%s pending tools dropped at exit: %s",
                    (getattr(ctx, "correlation_id", "") or "")[:8], _dropped)
        ctx.react_trace_rounds.append({
            "round": None, "tool": None, "inputs": {},
            "dropped_pending": [t for t, _ in pending],
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

    # ── AN EMPTY ANSWER MUST NOT PUBLISH ────────────────────────────────────
    # My own rule, written into the framing hook this morning and NOT written
    # here: "finalising an empty answer turns a governor decision into a blank
    # screen, which is worse than the round it is trying to save."
    #
    # Live it was worse than a blank screen. The loop handed _finalize_response
    # an empty string and everything downstream composed an answer from
    # NOTHING -- zero tool calls, zero sources, and a fluent three-payer
    # comparison the corpus never supported. A void does not stay a void; it
    # gets filled.
    #
    # So a loop that produced nothing DEFERS to v1 rather than publishing its
    # absence. v1's loop is the known-good path; handing it the turn costs
    # latency and yields a grounded answer, which is the right trade every
    # time.
    if not answer.strip():
        # 🔴 NO FALLBACK. Ananth, 2026-09-15: "no fall back".
        #
        # This handed the turn to v1's loop. That net caught a real bug today —
        # the loop could exit before the model was ever asked to write — but it
        # also cost 40% of every turn's budget held in reserve, and it meant a
        # v2 failure was invisible because v1 quietly answered instead.
        #
        # So the failure is v2's now, and it is SAID rather than repaired by
        # someone else. The turn still publishes through the one terminal —
        # there is no second publish path and no silent exit — but what
        # publishes is an honest failure, not a void and not v1's answer
        # wearing v2's label.
        logger.warning(
            "[v2.loop] cid=%s produced NO answer (rounds=%d stopped_by=%s)",
            (getattr(ctx, "correlation_id", "") or "")[:8],
            len(res.rounds), res.stopped_by)
        try:
            ctx.v2_stopped_by = res.stopped_by
        except Exception:
            pass
        answer = (
            "I could not put an answer together for this one. "
            f"(The loop stopped at: {res.stopped_by or 'unknown'}.)"
        )
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
                      v1_builder, allowed_tools=None,
                      agent_role: str = "explore",
                      posture=None) -> tuple[str, dict]:
    """The composed prompt, plus THIS ROUND'S POSTURE.

    🔴 WRAPPED, NOT PATCHED AT EACH RETURN. The builder below has THREE exits
    — the v2 composition, react's real composition, and the legacy builder —
    and appending the posture block at each one is three places to forget it.
    A prompt that silently loses the posture is the defect this whole module
    exists to remove: the governor picks a posture and the model never learns
    which one it is in.

    WHAT THE BASE ALREADY CARRIES, so this does not restate it: response
    shape, format rules, the tool manifest and user preferences all come from
    the composition unchanged (the LLM seat's scoping, 2026-09-11 —
    react.v2_governor replaces only the identity and critical-rules framing).
    The posture block says what THIS ROUND IS FOR and nothing else.

    INTERIM BY DESIGN. Ananth: "the objective is to move this to prompt
    manager and dynamically create it." The posture text is a Python constant
    today and belongs in the prompt DB as its own block, selected by posture
    the way the mode block is selected by mode. Appending here is what makes
    it TWEAKABLE now — "until we plug it in we cannot tweak" — not where it
    should live.
    """
    base, source = _v2_system_prompt_base(
        max_rounds, mode, user_profile, v1_builder,
        allowed_tools=allowed_tools, agent_role=agent_role)
    try:
        from app.pipeline.v2.posture_prompts import POSTURE_PROMPTS
        block = POSTURE_PROMPTS.get(posture) if posture is not None else None
    except Exception:
        block = None
    if not block:
        # COMMUNICATE is deliberately None pending the Deterministic UX seat,
        # and an unknown posture is a real state. Either way the base prompt
        # is complete on its own — say which posture had no block rather than
        # implying one was applied.
        source = {**source, "posture_block": None,
                  "posture": getattr(posture, "value", None)}
        return base, source
    return (f"{base}\n\nTHIS ROUND\n{block}",
            {**source, "posture_block": "applied",
             "posture": getattr(posture, "value", None)})


def _agent_role_for(posture) -> str:
    """Which react composition this posture wants.

    react selects a composition per round from its directive
    (governor.directive_to_agent_role). The postures map onto the same three
    roles, so v2 asks for the composition its posture implies rather than
    always taking round-1's.
    """
    return {
        "explore": "explore", "validate": "critique",
        "narrow": "synthesize", "alternatives": "synthesize",
        "communicate": "draft", "frame": "explore",
    }.get(getattr(posture, "value", str(posture)), "explore")


def _v2_system_prompt_base(max_rounds: int, mode: str, user_profile: dict | None,
                      v1_builder, *, allowed_tools=None,
                      agent_role: str = "explore") -> tuple[str, dict]:
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
    # ── REACT'S LIVE COMPOSITION PATH ───────────────────────────────────────
    # Ananth: "is this a prompt thing — check v1 prompt." It was.
    #
    # react builds its round prompt at react_loop.py:4807 via
    # resolve_react_system_prompt_v2 whenever MOBIUS_PROMPT_SOURCE=composition
    # -- which is SET in dev. I used `_react_reasoning_system` instead, the
    # legacy builder react itself describes as "rarely hit live", AND passed no
    # allowed_tools. A prompt whose tool manifest is empty gives the model
    # nothing to call, which produces exactly the "no usable tool call" that
    # made every round unusable on the first live run.
    #
    # So the fallback is react's REAL prompt, not a museum piece of it.
    try:
        from app.pipeline.react.prompts import resolve_react_system_prompt_v2

        resolved = resolve_react_system_prompt_v2(
            max_rounds, mode, user_profile, allowed_tools, agent_role)
        if resolved is not None and (resolved.system_prompt or "").strip():
            return resolved.system_prompt, {
                "source": "v1_composition",
                "agent_role": agent_role,
                "composition_id": resolved.composition_id,
                "composition_hash": resolved.composition_hash,
            }
    except Exception as exc:
        logger.debug("[v2.loop] react composition unavailable: %s", exc)

    # Last resort: the legacy builder, WITH allowed_tools this time.
    return (v1_builder(max_rounds, mode, user_profile,
                       allowed_tools=allowed_tools),
            {"source": "v1_legacy", "agent_role": agent_role})
