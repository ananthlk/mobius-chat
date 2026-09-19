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

import json
import logging
import time
from dataclasses import replace
from typing import Any

from app.pipeline.v2 import announce as _announce
from app.pipeline.v2 import executor as ex
from app.pipeline.v2 import posture as P
from app.pipeline.v2 import shadow as sh
from app.pipeline.v2 import trace as _trace

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

# The ABSOLUTE cap, which no amount of remaining budget may exceed.
#
# 🔴 WHY THE FUSE GREW A SECOND NUMBER. Live turn 98f8a6e1 stopped on
# `unusable_rounds` after 2 rounds with 84.8s of a 95s promise UNSPENT, on an
# agentic question whose ceiling was 6 rounds. The governor had judged the gap
# affordable on both rounds and said EXTEND; the fuse ended the turn anyway,
# and v1 answered the same question in 6 rounds and scored 0.969 against
# v2's 0.384.
#
# The rationale for 2 was itself budget reasoning — "at v2's ceilings a third
# wasted round is most of the budget" — expressed as a round count. Where the
# budget is nearly spent that inference holds; where 89% of it remains it is
# simply false, and the fuse was enforcing an arithmetic that no longer
# applied.
#
# So the fuse now asks the question it was always approximating: can we still
# AFFORD another round? If yes, keep going, up to this hard cap. If no, stop
# at MAX_UNUSABLE_ROUNDS as before.
#
# THE RUNAWAY IS STILL CAUGHT. Three independent bounds remain — this cap, the
# per-mode round ceiling, and the wall-clock check at the top of every round —
# and the 98-round failure this fuse exists for cannot be reached through any
# of them.
MAX_UNUSABLE_ROUNDS_HARD = 4

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
def _start_next_steps_ahead(ctx, state, step) -> None:
    """Start the next-steps call the first round the end is in sight."""
    try:
        from app.pipeline.v2 import ahead as _ahead

        if not _ahead.predicted(state):
            return
        answer = _best_running_answer(ctx) or ""
        from app.pipeline.v2.integrator import default_runner
        if _ahead.start(ctx, question=getattr(ctx, "message", "") or "",
                        answer_so_far=answer,
                        open_gaps=_open_gap_texts(state),
                        runner=default_runner(ctx)):
            step(_trace.Step(
                "ahead",
                "Asking what comes next — while the answer is still forming",
                (_trace.kv("why", "the governor can see the end: few gaps, the "
                                  "count is not rising, and evidence came back"),
                 _trace.kv("what", "next steps + follow-up questions"),
                 _trace.kv("instead of", "a blocking call after the answer is "
                                         "written, which the person waits through")),
                {"gaps_open": len(_open_gap_texts(state))},
                "v2 governor (converging)", "v2.ahead", "running"))
    except Exception:      # a prefetch must never end a turn
        logger.debug("[v2.loop] ahead start failed", exc_info=True)


def _payload_as_text(payload) -> str:
    """A preloaded payload as the TEXT the round context expects.

    `result` is a string by convention — nine readers in react_loop call
    .strip() on it, and a raw dict there killed a live turn this morning.
    Structured payloads are serialized rather than dropped: a dict from an MCP
    tool is still the evidence the turn paid for.
    """
    if isinstance(payload, str):
        return payload
    if payload is None:
        return ""
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        return str(payload)


def _record_contract(ctx, decision_json: dict, preloaded, tool_results) -> None:
    """Parse this round into the v2 contract and hang it on ctx.

    🔴 WHY THIS EXISTS. `ctx._v2_last_contract` was set in exactly ONE place —
    react_loop.py:7832, inside v1's loop. The governor loop never set it, so on
    our own loop:

        _v2_last_contract is None -> facts = () -> grounded_parts = 0
        -> the integrator has nothing to check -> `thin`
        -> abstain.thin_evidence -> THE ANSWER CARD IS FLATTENED TO TEXT

    Measured live 2026-09-15 (cid 311c9175): twelve sources, a correct answer,
    rendered as unformatted text saying "no supporting sources".

    Calls v2's own contract module rather than reproducing v1's block: two
    parsers of one response are two readings that can disagree.

    Document ids are attached from the sources IN HAND, because the verifier
    scopes by id — unscoped is 144s against 0.5s scoped. ctx.sources is still
    empty at this point (it is populated by _finalize_response, after every
    round), so the pool is preload plus what this turn's tools returned.
    """
    try:
        from app.pipeline.v2 import contract as _v2c

        resp = _v2c.parse(decision_json if isinstance(decision_json, dict)
                          else None)
        pool = []
        for group in (preloaded or [], tool_results or []):
            for r in group:
                if isinstance(r, dict):
                    pool.extend(r.get("sources") or [])
        id_by_name = {}
        for src in pool:
            name = (src.get("document_name") if isinstance(src, dict)
                    else getattr(src, "document_name", None))
            doc_id = (src.get("document_id") if isinstance(src, dict)
                      else getattr(src, "document_id", None))
            if name and doc_id and name not in id_by_name:
                id_by_name[name] = doc_id
        if id_by_name:
            resp = _v2c.with_document_ids(resp, id_by_name)
        ctx._v2_last_contract = resp
    except Exception:      # a contract parse must never end a turn
        logger.warning("[v2.loop] contract parse failed", exc_info=True)


def _verify_answer(ctx, emitter, step) -> None:
    """Check every fact against the page it cites, before the person reads it.

    Ananth: "if the critique is really found anything then we go back to react,
    else we move on.. this is a quality control, and we add good emits for it"
    — and, on seeing the governor loop run: "are we still running the
    deterministic critique in the post processing? we should emit that too."

    ANSWER: it ran, and NOT HERE. The block lives inside run_react() (v1's
    loop), so a turn on the governor loop was never checked at all and emitted
    nothing. Same seam as the contract above.

    ONCE PER TURN: react can propose complete on several rounds and the facts
    are cumulative, so paying for each proposal would tax reconsidering.
    """
    if getattr(ctx, "_v2_verified_once", False):
        return
    ctx._v2_verified_once = True
    try:
        import os

        from app.pipeline.react_loop import _preload_runner_toolreg
        from app.pipeline.v2 import verify as _v2v

        facts = tuple(getattr(getattr(ctx, "_v2_last_contract", None),
                              "facts", ()) or ())
        vr = _v2v.verify(
            facts,
            lambda t, i: _preload_runner_toolreg(
                t, i, ctx, speculative=False, emitter=emitter))
        ctx._v2_verify = vr

        # WHICH OF THE THREE HAPPENED — "we could not check" must never read as
        # "we checked and it is fine". The story, not the machine: whether the
        # answer was checked, and what follows.
        if vr.skipped:
            head = ("⊘ I could not check these claims against their sources — "
                    "the answer stands unverified")
        elif vr.findings:
            head = (f"⚠ {len(vr.findings)} claim(s) do not match the page they "
                    "cite — going back to correct them")
        else:
            head = (f"✓ Checked {vr.supported} claim(s) against the page each "
                    "one cites — all confirmed"
                    + (f" ({vr.unverifiable} could not be checked)"
                       if vr.unverifiable else ""))
        detail = [_trace.kv("from", "verify_claims (Tool Manifest) — "
                                    "deterministic, not an LLM opinion"),
                  _trace.kv("bar", f"{vr.bar} — ours, passed explicitly; we "
                                   "DELETE on not_supported so a low bar "
                                   "loses fewer true claims"),
                  _trace.kv("facts", len(facts)),
                  _trace.kv("took", f"{vr.duration_ms}ms")]
        for f in vr.findings[:4]:
            detail.append(_trace.item(str(f)[:200], "✗"))
        for pb in vr.problems:
            detail.append(_trace.kv("not checked", pb))
        # WHY they could not be checked, not just how many. The verifier
        # returns a reason on every unverifiable row and this emit used to
        # drop it, so "8 could not be checked" looked unexplainable when the
        # explanation had been handed to us eight times.
        _uw = tuple(getattr(vr, "unverifiable_why", ()) or ())
        for _why, _n in _uw[:3]:
            detail.append(_trace.kv("could not check", f"{_n}x — {_why}"))
        step(_trace.Step("verify", head, tuple(detail),
                         {"findings": len(vr.findings), "checked": vr.checked,
                          "supported": vr.supported,
                          "unverifiable": vr.unverifiable,
                          "unverifiable_why": [
                              {"why": w, "count": n} for w, n in _uw[:5]],
                          "skipped": vr.skipped, "bar": vr.bar},
                         "verify_claims", "verify", "done"))
        logger.info("[v2.loop] cid=%s verify checked=%d supported=%d "
                    "unverifiable=%d findings=%d ms=%d skipped=%s why=%s",
                    (getattr(ctx, "correlation_id", "") or "")[:8],
                    vr.checked, vr.supported, vr.unverifiable,
                    len(vr.findings), vr.duration_ms, vr.skipped[:60] or "-",
                    "; ".join(f"{n}x {w}" for w, n in _uw[:3])
                    or "-")
        if vr.reopen:
            ctx._v2_verified_findings = tuple(f.repair() for f in vr.findings)
    except Exception:      # pragma: no cover — never fail a turn on a check
        logger.warning("[v2.loop] verify failed", exc_info=True)


def _upgrade_signal(current: str, result) -> str:
    """The retrieval signal, upgraded from a tool result. v1's rule, exactly.

    🔴 THE DEFECT THIS EXISTS TO FIX. v2 passed RETRIEVAL_SIGNAL_NO_SOURCES to
    _finalize_response as a LITERAL — in the same call that handed over the
    sources. So every v2 turn reported "no sources" while carrying twelve, and
    the success branch wrote the failure value.

    Measured live 2026-09-15, cid 36aa6171, one response:

        sources: 12
        final_signal: "no_sources"
        source_confidence_strip: "no_sources"
        cited_source_indices: []
        abstained: true, rule_id "abstain.thin_evidence"
        "Shown as text, not as a card — nothing in the sources grounds this"

    One constant produced all of it: the citation strip, the empty citation
    list, the abstention, and the card being suppressed into plain text. The
    answer was correct and was presented to the user as unsupported.

    v1's rule (react_loop.py:9508) is the contract every downstream consumer
    already reads, so it is adopted rather than reinvented: any signal that is
    not the no-sources literal upgrades, and the last such wins.
    """
    from app.pipeline.react_loop import RETRIEVAL_SIGNAL_NO_SOURCES
    if not isinstance(result, dict):
        return current
    # A FAILED tool's signal must not upgrade anything — v1 guards the source
    # list the same way one line above, and a signal from a call that errored
    # describes the error, not the corpus.
    if result.get("success") is False or result.get("error") is not None:
        return current
    sig = result.get("signal")
    if sig and sig != RETRIEVAL_SIGNAL_NO_SOURCES:
        return sig

    # 🔴 A PRELOAD RESULT HAS NO `signal` KEY AT ALL.
    #
    # preload.execute returns {ok, payload, summary, sources, tool, ...} —
    # `signal` is a REACT tool-result field and preload does not produce one.
    # So this function could never upgrade from preloaded evidence, and a turn
    # answered entirely from preload reported no_sources.
    #
    # Measured on pinned turn a8cbd6d8, after the source seeding landed:
    #     sources: 13 · sources block refs: 13 · strip: no_sources
    # Thirteen documents published and the strip still said we had none, which
    # drives the confidence badge and the abstention.
    #
    # Sources ARE the signal here: a result carrying corpus sources IS corpus
    # retrieval, whatever key it does or does not use to say so. Read the fact
    # rather than the field, because the field belongs to the other producer.
    if result.get("sources"):
        from app.services.doc_assembly import RETRIEVAL_SIGNAL_CORPUS_ONLY
        return RETRIEVAL_SIGNAL_CORPUS_ONLY
    return current


def _outcome_word(res: dict) -> str:
    """One of the five shared words for what a tool did.

    Imported from mobius-contracts rather than spelled here: the vocabulary is
    shared with the formatter, and a second copy drifts. NOTHING here may say
    "not found" — WORLD_CLAIMS is empty, so no tool outcome licenses telling a
    person a thing does not exist.
    """
    try:
        from mobius_contracts.taxonomies import tool_outcome as _to
    except Exception:
        return "answered" if res.get("success") else "could_not_run"
    if not res.get("success"):
        return _to.COULD_NOT_RUN
    return _to.ANSWERED if str(res.get("result") or "").strip() else _to.EMPTY


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



def _has_grounded_facts(ctx) -> bool:
    """Did this turn learn anything it could CITE?

    Structural, not textual. A turn that completes with an empty `facts`
    tuple has nothing with a document and page behind it, whatever its prose
    says — which is the same signal `verify` already uses to decide there is
    nothing to check ("no facts with a document and page").
    """
    _c = getattr(ctx, "_v2_last_contract", None)
    return bool(getattr(_c, "facts", ()) or ())


def _round_state(ctx: Any, round_index: int, elapsed_s: float,
                 extensions_used: int,
                 pending_tools: tuple[str, ...] = ()) -> P.RoundState | None:
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
        pending_tools=pending_tools,
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

    def step(st) -> None:
        """One structured step. Bare-string `emit` stays for the plain
        emitter; this is what a client that renders detail actually gets."""
        try:
            _trace.emit_step(emitter, getattr(ctx, "correlation_id", "") or "",
                             st, thread_id=getattr(ctx, "thread_id", None))
        except Exception:      # a trace must never end a turn
            logger.debug("[v2.loop] step emit failed", exc_info=True)

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
    # 🔴 INITIALISED BEFORE THE PRELOAD BLOCK THAT READS IT.
    #
    # This was declared AFTER the preload try/except and used INSIDE it
    # (_upgrade_signal on each preloaded result), so the first preloaded tool
    # raised UnboundLocalError — which my own except caught and reported as a
    # single line. Measured live on a pinned turn, cid ee783367:
    #
    #     ✓ rag · via Tool Manifest (speculative) · 3688ms · 14 source(s)
    #     · preload unavailable: UnboundLocalError: cannot access local
    #       variable 'final_signal' where it is not associated with a value
    #
    # Fourteen sources fetched, paid for, and discarded; the answer went out
    # with sources: 0 and no citations. The swallow that exists so a preload
    # failure cannot end a turn hid a preload failure instead — a guard doing
    # its job and costing everything the step was for.
    #
    # It only bites on THIS loop, so every live test missed it: v1's loop never
    # runs this block. A pinned turn found it in one go, which is the argument
    # for pinning rather than reasoning about the off path.
    from app.pipeline.react_loop import RETRIEVAL_SIGNAL_NO_SOURCES as _NO_SRC
    final_signal = _NO_SRC

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

        step(_announce.tools_running(_q))
        _off = _tr_estimate(
            _q,
            caller_mode=react_chat_mode_label(getattr(ctx, "chat_mode", None)),
            correlation_id=getattr(ctx, "correlation_id", None),
        )
        # 🔴 RAG IS A FLOOR — BUT AN EVIDENCE FLOOR, NOT A CONSTANT.
        #
        # THE ORIGINAL DEFECT. This read `[...] or ["rag"]`, which adds rag
        # only when estimate returns NOTHING. Ananth: "no we will always do
        # rag", ruled after rag ranked 12th of 13 on a question only rag could
        # answer.
        #
        # MY FIRST FIX WAS ALSO WRONG, and Tool Manifest was right to object:
        # inserting rag unconditionally is "the governor overruling Tool
        # Manifest's own budget arithmetic with nothing but a constant".
        # estimate does not merely fail to rank rag — it SUPPRESSES it, with a
        # stated reason, and a suppression with a reason deserves an argument
        # rather than a constant.
        #
        # WHAT IS ACTUALLY MEASURED (canonical question, 2026-09-15):
        #
        #   rag_needed = False
        #   rag_reason = "offered tools reach density 1.00 over [...] — every
        #                 code the question raised is covered, so retrieval
        #                 would add nothing"
        #
        # The two tools carrying that coverage are marked `inputs_status =
        # fillable` WITH inputs. Both are then rejected at execution:
        # appeals_get_playbook for `missing required ['payor']; unknown
        # argument(s) ['query']`, payor_fact for `missing required ['payor',
        # 'predicate']`. estimate filled arguments against a different
        # signature than the callee declares, so the density is over tools
        # that cannot run. "Retrieval would add nothing" was false: with rag
        # forced, rag returned 21 sources.
        #
        # SO THE FLOOR IS ON THE OUTCOME, NOT THE PLAN. We honour the
        # suppression — if the chosen tools DO return evidence, rag stays
        # suppressed and their arithmetic stands. We only retrieve when the
        # preload produced none. A suppression justified by coverage is then
        # checked against whether that coverage materialised, which is the one
        # thing a caller can see and the ranker cannot.
        #
        # This is deliberately NOT "could not check" treated as "checked
        # false": we run the plan first and read the result.
        _offer = [t.tool_key for t in (getattr(_off, "tools", None) or [])]

        # 🔴 CARRY THE ARGUMENTS THE MANIFEST ALREADY FILLED.
        #
        # I passed plan() the tool KEYS and dropped `ToolOffer.inputs`. With no
        # inputs supplied, preload.execute falls back to {"query": question}
        # (preload.py:825) — so both appeals tools were called with the whole
        # sentence and rejected before calling:
        #
        #   ⊘ appeals_get_playbook  missing required ['payor'];
        #                           unknown argument(s) ['query']
        #   ⊘ appeals_lookup_rules  missing required ['carc'];
        #                           unknown argument(s) ['query']
        #
        # Measured on the same question, cid ee783367: estimate had ALREADY
        # resolved the entities and filled them —
        #   appeals_get_playbook  inputs={'payor': 'sunshine health', 'carc': '22'}
        #   appeals_lookup_rules  inputs={'carc': '22', 'payor': 'sunshine health'}
        # both inputs_status=fillable — and react then called them with exactly
        # those values two and three rounds later, successfully.
        #
        # So the arguments existed at round zero and my code discarded them,
        # costing two rounds to rediscover. I reported this seam as Tool
        # Manifest's defect earlier today; on this path it is mine.
        _offer_inputs = {
            t.tool_key: dict(t.inputs)
            for t in (getattr(_off, "tools", None) or [])
            if getattr(t, "inputs", None)
        }
        _rag_suppressed = ("rag" not in _offer)
        if not _offer:
            _offer = ["rag"]
            _rag_suppressed = False
        if _offer:
            _plan = _v2pre.plan(_offer, inputs=_offer_inputs)
            _preloaded = _v2pre.execute(
                _plan,
                lambda _t, _i: _preload_runner_toolreg(
                    _t, _i, ctx, speculative=True, emitter=emitter),
                _q,
                inputs=_offer_inputs,
            )
            if _rag_suppressed and not _has_evidence(_preloaded):
                # Their reason was falsified by their own plan's results.
                # Say so out loud — a floor that fires silently is a governor
                # overruling a peer without telling anyone.
                emit(f"  rag was suppressed — "
                     f"{(getattr(_off, 'rag_reason', '') or '')[:120]}")
                emit("  …but the preloaded tools returned no evidence, "
                     "so retrieving anyway")
                _rag = _v2pre.execute(
                    _v2pre.plan(["rag"]),
                    lambda _t, _i: _preload_runner_toolreg(
                        _t, _i, ctx, speculative=True, emitter=emitter),
                    _q,
                )
                _preloaded = list(_preloaded or []) + list(_rag or [])
            # The literal 1 is deliberate and the reason is in react_loop's
            # copy: preload runs BEFORE the round loop, so no round variable
            # exists yet. Writing `rn` there shipped a turn with no evidence
            # and a trace that still said "Looking this up before I answer".
            ctx._v2_preload_round = 1
            ctx._v2_preloaded = _preloaded
            # The virtual-tool-result channel round 1 reads from. Same shape
            # and same `result` STRING convention as v1's (react_loop.py:6067)
            # — a dict here is what crashed a live turn earlier today.
            if not getattr(ctx, "seed_tool_results", None):
                ctx.seed_tool_results = []
            for _sr in (_preloaded or []):
                if isinstance(_sr, dict) and _sr.get("ok") and _sr.get("payload"):
                    ctx.seed_tool_results.append({
                        "tool": _sr.get("tool"),
                        "success": True,
                        "result": _payload_as_text(_sr["payload"]),
                        "result_summary": _sr.get("summary") or "",
                        # round_virtual=0: fetched BEFORE round 1 spoke, so no
                        # round is credited with evidence it did not fetch.
                        "round_virtual": 0,
                        "sources": _sr.get("sources") or [],
                    })
            for _pr in (_preloaded or []):
                final_signal = _upgrade_signal(final_signal, _pr)
            ctx._v2_suggest = getattr(_plan, "suggest", []) or []
            step(_announce.tools_selected(
                offered=_offer,
                chosen=[r.get("tool") for r in (_preloaded or [])
                        if isinstance(r, dict) and r.get("tool")],
                suggest=ctx._v2_suggest,
                floor=not (getattr(_off, "tools", None) or []),
            ))
    except Exception as _pre_e:   # pragma: no cover — never fail a turn on preload
        # Fail-soft, and SAY SO. react can reason without preload; it cannot
        # reason correctly about why its evidence is thin if nothing says the
        # step did not run.
        emit(f"  preload unavailable: {type(_pre_e).__name__}: {_pre_e}")

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

    # 🔴 ROUND 1 MUST SEE WHAT PRELOAD FETCHED.
    #
    # This was `= []`, so build_reasoning_context — which reads ONLY the list
    # passed to it (react/prompts.py:913) — received nothing on round 1, and
    # the preloaded evidence never reached the model.
    #
    # Measured on a pinned turn, cid c11ea4af: preload ran rag (14 sources),
    # appeals_get_playbook and appeals_lookup_rules successfully, and round 1
    # reported "tools in hand: none · evidence: 637 chars". Three consequences,
    # all from this one line:
    #
    #   1. the permission-to-finish I had just added had nothing to read, so
    #      it could not say "this is already answered";
    #   2. the governor chose `nothing_worth_buying` at 12s of a 31s promise
    #      while holding fourteen unread sources;
    #   3. round 3 REQUESTED appeals_get_playbook — a tool preload had already
    #      run successfully — and the turn ended before it could re-run.
    #
    # v1 does it at react_loop.py:6155 (`tool_results: list[dict] = seed`) and
    # I did not carry it across when I wrote this loop. Seeded from the SAME
    # ctx.seed_tool_results v1 builds, so the two cannot disagree about what
    # round 1 was handed.
    tool_results: list[dict] = list(getattr(ctx, "seed_tool_results", None) or [])
    ctx.react_trace_rounds = getattr(ctx, "react_trace_rounds", None) or []
    # 🔴 SEEDED FROM PRELOAD, LIKE tool_results ABOVE.
    #
    # This was `= []` and only ever appended inside the ROUND tool loop, so a
    # turn that answered from preloaded evidence published NOTHING. Measured on
    # pinned turn 58e37239, immediately after fixing the round context: preload
    # returned 14 rag sources, round 1 read 32,812 chars of evidence and
    # answered in ONE round — and the response carried
    #
    #     sources: 0 · cited_source_indices: [] · strip: no_sources
    #
    # An 817-character answer with fourteen documents behind it and no way for
    # the reader to check any of it. The better the turn got, the fewer
    # citations it had: a round that answers without calling a tool never
    # reaches the only line that collected sources.
    #
    # Same shape as the round-context defect one commit ago, and the same
    # lesson: preload has SEVERAL consumers and I wired one at a time. Seeded
    # from ctx.seed_tool_results so there is one channel carrying preload
    # forward, not two that can disagree.
    all_sources: list = [
        _s
        for _sr in (getattr(ctx, "seed_tool_results", None) or [])
        for _s in (_sr.get("sources") or [])
    ]
    last_tool: str | None = None
    unusable = 0
    # One reach for a document per turn; see the rung-0 note below.
    _reached_for_a_document = False
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
        state = _round_state(ctx, rn, elapsed, extensions_used,
                             pending_tools=tuple(t for t, _ in pending))
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

        # 🔴 EVERY ROUND, NOT ONLY THE LAST.
        #
        # This step was emitted only on the branch that ENDS the turn, so the
        # one decision this loop exists to make was invisible on every round
        # that continued. A reader saw tools run and a model answer, with the
        # posture that chose them never stated — which is the same opacity the
        # loop was built to remove, wearing a trace.
        step(_announce.posture(
            round=rn, decision=decision, action=action,
            spent_s=time.monotonic() - t0,
            budget_s=sh.promise_seconds(ctx, None),
            rounds_left=max_rounds - rn,
            gaps_open=_open_gap_texts(state),
            has_answer=bool(_best_running_answer(ctx)),
            targeting=_gap_text(state, decision.gap_targeted)))

        # 🔴 ASK FOR THE NEXT STEPS WHILE THE LOOP IS STILL RUNNING.
        #
        # Ananth: "predict when we are near an answer and ask the next steps
        # and asks prompt right then". posture.converging() already computes
        # that prediction every round and, until now, only one caller read it —
        # to decide whether a turn may overrun its budget.
        #
        # Fired here, the call overlaps the rounds that remain instead of
        # becoming a blocking tail after the answer is written. Wrong
        # predictions cost one discarded call and never a wrong answer.
        _start_next_steps_ahead(ctx, state, step)

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
                # The structured posture step above already carried this
                # decision, with its clock, its gaps and its branch. A second
                # bare-string copy of the same fact is how one decision starts
                # reading as two.
                break

        # ── 2. TOOLS — whatever the last round asked for, before reasoning ──
        for _t, _in in pending:
            step(_announce.tool_call(round=rn, tool=_t, inputs=_in or {},
                                     closing=(_open_gap_texts(state) or [None])[0]))
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
            step(_announce.tool_result(
                round=rn, tool=_t, ok=bool(_res.get("success")),
                outcome=_outcome_word(_res),
                sources=len(_res.get("sources") or []),
                chars=len(str(_res.get("result") or ""))))
            final_signal = _upgrade_signal(final_signal, _res)
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

            # 🔴 THE FACTS SCHEMA WAS ONLY EVER ASKED FOR BY v1'S LOOP.
            #
            # `system_suffix` carries the "facts": [{fact, document, page}]
            # block. Its ONLY caller was react_loop.py:7171 — v1's loop
            # running the v2 shadow. The governor loop never appended it, so
            # on our own loop the model was never asked to name the document a
            # fact came from.
            #
            # It answered anyway, with facts that had no provenance. Three
            # consumers then reported that honestly and none of them said why:
            #
            #   verify:    "skipped=no facts with a document and page"
            #   contract:  "fact with no document"
            #   citations: cited_source_indices [] beside 13 published sources
            #
            # Every one of those is a could-not-check, and I read past all
            # three — including a line my own verify emit was printing.
            #
            # Fourth time this session that work existed only in the loop that
            # is OFF. The others were ahead-prefetch, verify, and format rules.
            # Bound BEFORE the try: the user-side statement below reads it,
            # and an exception in system_suffix would otherwise leave it
            # unbound — turning a prompt-decoration failure into a NameError
            # that ends the turn. The except exists precisely so a suffix
            # failure cannot end a turn; an unbound local would have defeated
            # it from three lines away.
            _suffix = ""
            try:
                from app.pipeline.v2 import prompts as _v2pr
                _suffix = _v2pr.system_suffix(ctx)
                if _suffix:
                    system = (system or "") + _suffix
            except Exception:   # a prompt suffix must never end a turn
                logger.warning("[v2.loop] system_suffix failed", exc_info=True)

            user = build_reasoning_context(ctx, tool_results, rn, max_rounds)

            # 🔴 ONCE IS NOT ENOUGH AGAINST A 53,000-CHARACTER PROMPT.
            #
            # The facts schema was appended to the SYSTEM prompt only, and
            # live turns came back with no facts at all — 44 of 60 verify
            # calls skipped with "no facts with a document and page", so the
            # verifier and the citations both sat idle on most turns.
            #
            # It is not length alone: the same suffix works at 18k. The real
            # prompt is 53,213 chars for EXPLORE and restates v1's response
            # shape TWELVE times. One statement of an addition, at the very
            # end, loses to twelve statements of the shape it is adding to.
            #
            # Measured, same question, gemini pinned:
            #     system only (before)   0 / 9 runs produced facts+provenance
            #     system AND user        5 / 7
            #
            # Repetition is what wins, and the user message is where it is
            # read last. The cost is ~3.5k duplicated characters per round,
            # paid because a round whose facts are missing cannot be verified
            # or cited at all — the whole chain downstream is dark.
            if _suffix:
                user = (user or "") + _suffix

            # The rung-0 exit set this on the previous round: what we held did
            # not answer, and we have not yet asked whether we can NAME a
            # document. Deep Research owns the wording; we read it.
            if getattr(ctx, "_v2_name_a_document", False):
                try:
                    from app.pipeline.v2 import ladder as _ladder
                    _rung = _ladder.name_a_document_block()
                    if _rung:
                        user = (user or "") + _rung
                except Exception:   # a rung must never end a turn
                    logger.warning("[v2.loop] ladder rung failed", exc_info=True)
                ctx._v2_name_a_document = False

            _llm_t0 = time.monotonic()
            raw = _call_llm_json(system, user, max_tokens=2048, ctx=ctx,
                                 stage=f"react_{rn}")
            _llm_s = time.monotonic() - _llm_t0
            step(_announce.prompt_built(
                round=rn, posture=decision.posture, source=prompt_source,
                system_chars=len(system or ""), user_chars=len(user or ""),
                evidence_tools=[r.get("tool") for r in tool_results
                                if isinstance(r, dict) and r.get("tool")]))
            res.rounds[-1]["v2_prompt_source"] = prompt_source
            try:
                ctx.v2_prompt_source = prompt_source
            except Exception:
                pass
        except Exception as exc:
            logger.warning("[v2.loop] round %s model call failed: %s", rn, exc)
            res.stopped_by = "model_error"
            break

        # 🔴 A MODEL THAT SAID NOTHING IS NOT A MODEL THAT SAID SOMETHING
        # UNUSABLE.
        #
        # _call_llm_json ends `return (raw or "").strip()`. A provider that
        # answers HTTP 200 with zero output tokens raises NOTHING, so the
        # except-branch above never sees it and every recovery rung hanging off
        # `except` is skipped. Deep Research measured this on their side:
        # 21,823 tokens in, output_tokens 0 back, reported to the user as
        # "0 slots · response did not parse" — a statement about the REQUEST,
        # when in truth nobody had answered it.
        #
        # Falling through is worse than failing here. "" parses to {}, which
        # this loop counts as an unusable ROUND, so an unanswered call is spent
        # against MAX_UNUSABLE_ROUNDS and the turn ends having burned its
        # budget on rounds the model never took part in.
        #
        # This is could-not-check dressed as checked-false, one level up: the
        # difference between "the model did not answer" and "the model answered
        # badly" is the difference between retrying and giving up, so the two
        # get different names and `model_empty` stays distinguishable in
        # telemetry rather than hiding inside model_error.
        if not (raw or "").strip():
            logger.warning(
                "[v2.loop] round %s: model returned EMPTY (no exception, no "
                "tokens). Not counted as an unusable round.", rn)
            res.stopped_by = "model_empty"
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
        _record_contract(ctx, decision_json, _preloaded, tool_results)
        tool = (decision_json.get("tool") or "").strip() or None
        answer = (decision_json.get("answer") or "").strip()

        # 🔴 RUNG 0 IS NOT THE LADDER. The escalation question has TWO parts —
        # "does what I am holding answer this, with a sentence I can quote?
        # and if NOT, can I NAME a document?" — and this loop only ever asked
        # the first. It answered "no" and completed.
        #
        # Measured on the A/B: v2 ran NO retrieval at all on 11 of 15 turns
        # and issued 5 distinct queries where v1 issued 24. Live case, "does
        # Aetna cover doula services": v1 retrieved twice and answered from
        # the second query; v2 answered from preload alone with "not found in
        # the available materials" while the corpus held it. v2's honesty was
        # intact and its recall was not.
        #
        # THE PROMPT IS NOT THE GAP. Given the same not-found evidence, EXPLORE
        # plans fetch_document 4 times out of 4 — measured directly against the
        # live model. The model knows the move. It was never given the round.
        #
        # NOT KEYED ON THE WORDING OF THE ANSWER. "not found", "is not
        # specified" and their cousins are prose, and matching prose is the
        # defect this file has shipped four times. The signal is structural:
        # the contract carries NO grounded facts, so the turn is completing
        # without having learned anything it can cite.
        #
        # ONCE, and only while affordable. A second refusal is the model
        # telling us the same thing twice, and paying for it again is how a
        # retry becomes a loop.
        if (decision_json.get("is_complete") and answer
                and not _reached_for_a_document
                and not _has_grounded_facts(ctx)):
            _round_cost = float(getattr(state, "next_round_cost_s", 0.0) or 0.0)
            if _round_cost > 0.0 and _left >= _round_cost:
                _reached_for_a_document = True
                ctx._v2_name_a_document = True   # read by the next prompt
                logger.info(
                    "[v2.loop] cid=%s completing with NO grounded facts and "
                    "%.1fs left — one round to NAME a document before "
                    "reporting absence",
                    (getattr(ctx, "correlation_id", "") or "")[:8], _left)
                continue

        if decision_json.get("is_complete") and answer:
            # THE MODEL SAYS IT IS DONE. In v1 this ends the turn. Here it is
            # an INPUT to the next decision, not the decision itself -- which
            # is the entire reason this loop exists. The governor gets the next
            # round to disagree, and on the three-payer question it would have.
            res.answer = answer
            _verify_answer(ctx, emitter, step)
            step(_announce.model_replied(
                round=rn, proposes_complete=True, answer_chars=len(answer),
                elapsed_s=_llm_s,
                gaps_closed=(_enrichment_from(decision_json) or {}).get("gaps_closed") or (),
                gaps_open=(_enrichment_from(decision_json) or {}).get("gaps_open") or ()))
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
                # Can we still afford a round? The fuse's own reasoning was
                # about cost; ask about cost rather than about a count.
                _round_cost = float(getattr(state, "next_round_cost_s", 0.0) or 0.0)
                _affordable = _round_cost > 0.0 and _left >= _round_cost
                if _affordable and unusable < MAX_UNUSABLE_ROUNDS_HARD:
                    logger.info(
                        "[v2.loop] cid=%s %d unusable round(s), continuing: "
                        "%.1fs left affords another at %.1fs (hard cap %d)",
                        (getattr(ctx, "correlation_id", "") or "")[:8],
                        unusable, _left, _round_cost, MAX_UNUSABLE_ROUNDS_HARD)
                    continue
                res.stopped_by = "unusable_rounds"
                logger.info(
                    "[v2.loop] cid=%s STOPPING on %d unusable round(s): "
                    "%.1fs left, next round costs %.1fs",
                    (getattr(ctx, "correlation_id", "") or "")[:8],
                    unusable, _left, _round_cost)
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

        _enr = _enrichment_from(decision_json) or {}
        step(_announce.model_replied(
            round=rn, tool=tool, tools=[t for t, _ in pending],
            answer_chars=len(answer), elapsed_s=_llm_s,
            gaps_closed=_enr.get("gaps_closed") or (),
            gaps_open=_enr.get("gaps_open") or ()))
        ctx.react_trace_rounds.append({
            "round": rn, "tool": tool,
            "inputs": decision_json.get("inputs") or {},
            "enrichment": _enr,
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
        step(_announce.dropped(tools=[t for t, _ in pending]))
        logger.info("[v2.loop] cid=%s pending tools dropped at exit: %s",
                    (getattr(ctx, "correlation_id", "") or "")[:8], _dropped)
        ctx.react_trace_rounds.append({
            "round": None, "tool": None, "inputs": {},
            "dropped_pending": [t for t, _ in pending],
        })

    # 🔴 THE FALLBACK LABEL IS A FALLBACK, NOT AN OVERWRITE.
    #
    # This was the `else` of `if pending:` and it read
    # `res.stopped_by = "max_rounds"` unconditionally. The comment says "loop
    # exhausted its rounds without a governor decision to stop" -- that is a
    # `while ... else`, which fires when the loop ends WITHOUT break. Written
    # as an if/else on `pending` it fires on every exit that left no queued
    # tool, which is every CLEAN exit: model_complete_no_gaps, unusable_rounds,
    # model_error and every governor branch were all relabelled "max_rounds"
    # on their way out.
    #
    # So the one field that says WHY a turn ended reported a budget exhaustion
    # for turns that finished early with budget in hand -- and `exit_mode` was
    # already guarded with `or`, which is what makes the bare assignment above
    # it read as deliberate. Two lines, one guarded, one not.
    #
    # Dropped pending work and running out of rounds are also independent
    # facts; nesting them made one hide the other.
    if not res.stopped_by:
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

    step(_announce.exited(
        stopped_by=res.stopped_by or "unset", exit_mode=res.exit_mode,
        rounds=len(res.rounds), spent_s=time.monotonic() - t0,
        budget_s=sh.promise_seconds(ctx, None), answer_chars=len(answer)))

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
    logger.info("[v2.loop] cid=%s final_signal=%s sources=%d",
                (getattr(ctx, "correlation_id", "") or "")[:8],
                final_signal, len(all_sources))
    _finalize_response(ctx, answer, all_sources,
                       final_signal, last_tool, emitter)


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


def _has_evidence(results) -> bool:
    """Did the preload actually produce anything to reason over?

    Not "did it run" and not "did it succeed" — a tool can succeed and return
    an empty body, and a tool rejected before calling succeeds at nothing
    while raising no error. The only question the floor cares about is whether
    there is text or a source in hand.
    """
    for r in (results or []):
        if not isinstance(r, dict):
            continue
        if r.get("sources"):
            return True
        if str(r.get("result") or "").strip():
            return True
    return False


def _gap_text(state: P.RoundState, gap_id) -> str:
    """The targeted gap in the model's own words.

    `decision.because` names the gap by id ("closing Sba95e6"). That is the
    right thing to log and the wrong thing to show: a reader cannot tell a
    well-aimed round from a badly-aimed one without the words.
    """
    if not gap_id:
        return ""
    # The ROOT gap is the question itself. It is excluded from
    # _open_gap_texts, so resolving it by lookup returns "" and the headline
    # falls back to "closing G0" — the id, leaked to a reader, on the FIRST
    # round of every turn.
    if gap_id == P.ROOT_GAP_ID:
        return "the question as asked — nothing narrowed yet"
    for g in state.open_gaps:
        if g.gap_id == gap_id:
            return g.text
    return ""


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


def _with_v2_format_rules(base: str, posture) -> tuple[str, str]:
    """v2's own FORMAT RULES, replacing v1's fixed-shape ones.

    Ananth: "create a new prompt set so that we dont disrupt v1.. this way we
    can use that modular". So this APPENDS a v2-only block rather than editing
    react/prompts.py, and v1's answers are byte-identical to yesterday's.

    WHY IT IS NEEDED. v1's rules say "follow with 2-4 short bullet points" on
    every answer, so every answer reaches the formatter as bullets. The
    classifier is not the problem — hand it the same content written three
    ways and it returns bullets / steps / stats correctly. Nothing ever asked
    for a different shape.

    ONLY ON THE ROUND THAT WRITES WHAT THE PERSON READS. A judging round is not
    producing the answer field, and 2.2k characters of shape guidance in a
    prompt that is already 76k is noise where it cannot apply. Same gating the
    answer_shape block uses, and for the same reason.

    LAST WINS. The base already carries v1's rules from the composition; this
    is appended after, and the later instruction is the one the model follows.
    That is why the block says explicitly not to end with a "Next step:" line —
    it is overriding something the reader of the prompt has already been told.
    """
    try:
        if posture is None or getattr(posture, "value", "") != "communicate":
            return base, "not_applied (not the communicating round)"
        from app.pipeline.v2.format_rules import V2_FORMAT_RULES_TEXT
        return (f"{base}\n\n{V2_FORMAT_RULES_TEXT}", "applied")
    except Exception:      # a prompt addition must never end a turn
        logger.warning("[v2.loop] v2 format rules not applied", exc_info=True)
        return base, "failed"


def _v2_system_prompt(max_rounds: int, mode: str, user_profile: dict | None,
                      v1_builder, allowed_tools=None,
                      agent_role: str = "explore",
                      posture=None, failed: bool = False) -> tuple[str, dict]:
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
        from app.pipeline.v2.posture_prompts import (
            COMMUNICATE_FAILURE, POSTURE_PROMPTS,
        )
        block = POSTURE_PROMPTS.get(posture) if posture is not None else None
        # 🔴 COMMUNICATE HAS TWO PROMPTS, NOT ONE WITH A BRANCH.
        #
        # The Deterministic UX seat's reason is mechanical, not stylistic: the
        # formatter treats a thin-evidence turn as a REFUSAL and will not fill
        # it, because a cited bullet list beside an ungrounded answer is the
        # most confident-looking thing on a screen. A failure that arrives
        # SHAPED like a success fights the gate that keeps it honest.
        #
        # Load-bearing since the v1 fallback was removed: a failed turn now
        # publishes its own words, so those words are the product.
        if posture is not None and getattr(posture, "value", "") == "communicate" \
                and failed:
            block = COMMUNICATE_FAILURE
    except Exception:
        block = None
    base, _fmt_applied = _with_v2_format_rules(base, posture)
    if not block:
        # COMMUNICATE is deliberately None pending the Deterministic UX seat,
        # and an unknown posture is a real state. Either way the base prompt
        # is complete on its own — say which posture had no block rather than
        # implying one was applied.
        source = {**source, "posture_block": None, "format_rules": _fmt_applied,
                  "posture": getattr(posture, "value", None), "failed": failed}
        return base, source
    source = {**source, "format_rules": _fmt_applied}
    return (f"{base}\n\nTHIS ROUND\n{block}",
            {**source, "posture_block": "applied",
             "posture": getattr(posture, "value", None), "failed": failed})


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
