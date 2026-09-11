"""R0 shadow — v2 computes, v1 decides, both emit, divergence is recorded.

WHY SHADOW AND NOT REPLAY: replay proves equality only on the traffic you
replayed, and six of the seven moved decisions read mode, elapsed time or round
number -- which is exactly where untested combinations live. Shadow runs on real
traffic with real combinations, and costs one extra emit per round.

WHY NOT BYTE EQUALITY: v1 emits a DIRECTIVE (search/consolidate/extend/finalize/
complete) and v2 emits a POSTURE. Different vocabularies for overlapping
decisions, so the assertion is agreement under a STATED mapping. The mapping is
the claim being tested; a divergence is a finding about one of the two, and
which one is not assumed here.

THIS MODULE MUST NEVER AFFECT A TURN. Everything is wrapped; a failure logs and
returns. Same posture as the attestation write, and for the same reason: an
observer that can break the thing it observes is not an observer.
"""

from __future__ import annotations

import logging

from app.pipeline.v2.posture import (
    explain, gap_id_for, jaccard,
    REPEAT_JACCARD,
    Attempt, Budget, Decision, Gap, Posture, RoundState, select,
)

logger = logging.getLogger(__name__)

# ── the mapping, and its relationship to the one v1 already uses ────────────
#
# CORRECTED 2026-09-11 on the chat seat's finding. The first version mapped
# 'extend' -> EXPLORE alongside 'search', which contradicted
# governor._DIRECTIVE_TO_AGENT_ROLE -- a mapping that is LIVE (react_loop.py
# :4760-4761 calls directive_to_agent_role() to select the prompt composition)
# and groups 'extend' with 'consolidate' as "synthesize". Two declarations of
# one idea, disagreeing: the defect removed three times this week.
#
# CITATION CORRECTED 2026-09-11 (chat seat). An earlier version of this comment
# said _DIRECTIVE_TO_AGENT_ROLE is documented KNOWN LOSSY. It is not: that
# docstring belongs to agent_role_to_reasoning_depth() at governor.py:275, and
# the DEPTH path was already routed around the lossy hop in 2026-08-04
# (directive_to_reasoning_depth goes direct). _DIRECTIVE_TO_AGENT_ROLE itself is
# live and unflagged -- it selects the prompt COMPOSITION.
#
# The decision below stands on the second reason, not the misattributed first:
# 'extend' is two different pieces of work wearing one name.
#
# AND the chat seat then verified something that makes the split load-bearing
# rather than merely tidy -- a live v1 defect, three facts:
#   react_loop.py:4724  _pp_pre_state hardcodes proposes_complete=False
#   governor.py:191     the :197 extend path sits INSIDE `if proposes_complete`
#   => every PRE-ROUND extend is necessarily the :209 path -- still gathering
#      evidence -- and directive_to_agent_role("extend") -> "synthesize"
#      selects a SYNTHESIZE composition for a round that is still gathering.
# Same bucket, same shape as the depth bug Ananth caught live on 2026-08-04;
# depth was fixed by going direct from directive, composition still routes
# through agent_role and still carries it.
#
# 'extend' is two different pieces of work wearing one name:
#   governor.py:197  proposes_complete AND groundedness failed
#                    -> appends the critique to tool_results; the next round is
#                       aimed at NAMED unsupported claims. That is remediation.
#   governor.py:209  confidence bar not met AND base rounds exhausted
#                    -> genuinely "keep gathering, just out of budget".
DIRECTIVE_TO_POSTURE = {
    "search": Posture.EXPLORE,
    "consolidate": Posture.NARROW,
    "finalize": Posture.COMMUNICATE,
    "complete": Posture.COMMUNICATE,
    # 'extend' is deliberately absent -- it is resolved by reason, below.
}

# Substrings of governor.py's own reason strings. Matched rather than
# re-derived, and a drift test asserts they still exist in that file: if the
# prose changes, the test fails loudly instead of this silently falling through
# to the default.
_EXTEND_REMEDIATION = "quality issue flagged"          # governor.py:197
_EXTEND_OUT_OF_ROUNDS = "round budget exhausted"        # governor.py:209

# Where this mapping deliberately disagrees with _DIRECTIVE_TO_AGENT_ROLE, and
# why. A test asserts this covers every disagreement -- so a NEW divergence
# cannot appear silently.
DELIBERATE_DIVERGENCE = {
    "extend": (
        "v1 collapses extend into 'synthesize' with consolidate; governor.py"
        ":282-295 documents that collapse as a known mapping bug caught live "
        "2026-08-04. We split extend on its reason instead."
    ),
}

_AGENT_ROLE_TO_POSTURE = {
    "explore": Posture.EXPLORE,
    "synthesize": Posture.NARROW,
    "draft": Posture.COMMUNICATE,
}


def map_directive(directive: str | None, reason: str | None = None) -> Posture | None:
    """v1 directive (+ its reason) -> v2 posture. None when unmapped."""
    d = (directive or "").strip().lower()
    if d == "extend":
        r = (reason or "").lower()
        if _EXTEND_REMEDIATION in r:
            return Posture.NARROW        # aimed at named claims: remediation
        if _EXTEND_OUT_OF_ROUNDS in r:
            return Posture.EXPLORE       # keep gathering, just out of budget
        return None                      # an extend we do not recognise is UNMAPPED,
                                         # not silently bucketed
    return DIRECTIVE_TO_POSTURE.get(d)


def state_from_ctx(ctx, *, round_index: int, elapsed_s: float,
                   promise_latency_s: float, round_cost_s: float,
                   acting_cost_s: float) -> RoundState | None:
    """Build v2's RoundState from what v1 already produces.

    R0 needs no new writes: the gap text v1 already emits per round
    (evidence_review.gaps_open) is enough to exercise the machine. Ids are
    minted LOCALLY here and are not persisted -- thread_gaps is not yet
    populated, and a shadow must not create state.

    This function is the impure edge on purpose. posture.py reads no ctx, no
    clock and no DB; everything it needs arrives as an argument, which is the
    property that makes two runs comparable at all.
    """
    try:
        rounds = list(getattr(ctx, "react_trace_rounds", None) or [])
        history: list[int] = []
        open_texts: list[str] = []
        attempts_by_text: dict[str, list[Attempt]] = {}

        closed_all: list[str] = []
        all_texts: list[str] = []          # every gap text this turn, in order
        texts_by_round: list[list[str]] = []   # grouped, so siblings never merge
        for r in rounds:
            enr = (r or {}).get("enrichment") or {}
            gaps = [g for g in (enr.get("gaps_open") or []) if isinstance(g, str)]
            closed = [g for g in (enr.get("gaps_closed") or []) if isinstance(g, str)]
            closed_all.extend(closed)
            history.append(len(gaps))
            texts_by_round.append(list(gaps))
            for g in gaps:
                if g not in all_texts:
                    all_texts.append(g)
            open_texts = gaps  # the latest round's list is the live one
            tool = (r or {}).get("tool")
            query = (((r or {}).get("inputs") or {}).get("query"))
            # ── THE PAYLOAD CHECK, wired 2026-09-11 ─────────────────────────
            # This was hard-coded False, which made `converging()` unable to
            # return True, which made `may_overrun()` unable to allow -- so the
            # overrun Ananth asked for ("would you relax a constraint when one
            # more round would close it") was STRUCTURALLY DEAD. Built, tested,
            # reported, and never once reachable.
            #
            # What it must NOT be: the tool's own success flag. A tool can
            # succeed and return nothing, and that is the exact case the check
            # exists to catch.
            #
            # What it IS, and its basis: the round's enrichment recorded either
            # a CLOSED gap or a non-empty running answer. Both are downstream
            # of content actually coming back -- react cannot close a gap or
            # extend the running answer from an empty result. It is a PROXY,
            # not the raw payload, and it is named one: the raw result lives
            # behind raw_result_ref, which state_from_ctx deliberately does not
            # dereference because reading tool payloads on the decision path is
            # how an observer starts costing what it observes.
            _running = str(enr.get("running_answer") or "").strip()
            returned = bool(closed) or bool(_running)
            for g in gaps:
                attempts_by_text.setdefault(g, [])
                if tool:
                    attempts_by_text[g].append(
                        Attempt(round_index=int((r or {}).get("round") or 0),
                                tool=tool, query=query,
                                returned_payload=returned))

        opened_at: dict[str, int] = {}
        for r in rounds:
            enr = (r or {}).get("enrichment") or {}
            for g in (enr.get("gaps_open") or []):
                opened_at.setdefault(g, int((r or {}).get("round") or 0))

        # CONTENT-ADDRESSED, not positional. f"S{i+1}" made the id a POSITION
        # in the latest round's list, so 7 of 35 turns had an id change meaning
        # mid-turn -- S1 was Sunshine Health at one round and Humana at the
        # next. Stable ids also let a REWORDED gap be recognised rather than
        # minted fresh: react restates the same sub-question in slightly
        # different words between rounds ("fax number for LTC waiver appeals
        # coordinator at S..." / "fax number for the LTC waiver appeals
        # coordinator"), and a pure hash would treat those as two gaps and
        # reset the age and lever counts that make `stuck` reachable.
        #
        # REPEAT_JACCARD is the threshold posture.py already uses to decide
        # whether a new query complies with a directive -- the same question
        # ("is this the same thing restated?"), so the same constant.
        # Built over every text seen this turn, ROUND BY ROUND -- and a text
        # may only inherit an id from an EARLIER round, never from a sibling in
        # its own round.
        #
        # 🔴 SIBLINGS MUST NOT MERGE, found live 2026-09-11 on
        # cid 28a02bf9 while Ananth was watching:
        #
        #   S381309  "Sunshine Health prior authorization requirements for..."
        #   S381309  "Humana prior authorization requirements for..."
        #
        # Two payers, one id. Those texts differ only by a proper noun, so
        # jaccard = 0.778 >= 0.70 and the "same thing restated" rule fused
        # them. That is WORSE than the positional ids it replaced: positional
        # ids mislabelled gaps, this one silently DELETED one -- and it did it
        # in the multi-payer comparison case the system most exists for.
        #
        # The principled rule, not a tuned threshold: two gaps react listed in
        # the SAME round are different gaps BY CONSTRUCTION -- it named them
        # separately, in one breath, about one state. A rewording can only
        # happen ACROSS rounds, because it is the model saying the same thing
        # again LATER. Sibling-blindness makes the threshold unable to cause
        # this class of error at all, rather than making it less likely.
        canon: dict[str, str] = {}   # text -> the text whose id it inherits
        for _round_texts in texts_by_round:
            _established = list(canon.values())      # earlier rounds ONLY
            for t in _round_texts:
                if t in canon:
                    continue
                match = next((k for k in _established
                              if jaccard(k, t) >= REPEAT_JACCARD), None)
                canon[t] = match or t
        gaps = tuple(
            Gap(gap_id=gap_id_for(canon[t]), text=t,
                opened_round=opened_at.get(canon[t], opened_at.get(t, round_index)),
                attempted_by=tuple(attempts_by_text.get(t, ())),
                # RECORDED, never absorbed silently. The id survived a
                # rewording; the fact that it had to is the signal.
                reworded_from=("" if canon[t] == t else canon[t]),
                reworded_similarity=(1.0 if canon[t] == t
                                     else jaccard(canon[t], t)))
            for t in open_texts
        )
        remaining = max(0.0, promise_latency_s - elapsed_s)
        return RoundState(
            question=str(getattr(ctx, "message", "") or ""),
            round_index=round_index,
            open_gaps=gaps,
            gaps_open_history=tuple(history),
            budget=Budget(remaining_s=remaining, remaining_c=0.0,
                          band_s=band_seconds(promise_latency_s)),
            next_round_cost_s=round_cost_s,
            acting_cost_s=acting_cost_s,
            validate_cost_s=9.6,
            gaps_closed=tuple(dict.fromkeys(closed_all)),
        )
    except Exception as exc:
        logger.warning("[v2.shadow] state_from_ctx failed r%s: %s", round_index, exc)
        return None


def compare(v1_directive: str | None, state: RoundState,
            v1_reason: str | None = None) -> dict | None:
    """Run v2's decision beside v1's. Returns the comparison, or None on failure.

    v1's answer is NOT passed into select(); v2 decides from state alone. If it
    were passed in, agreement would be guaranteed and the comparison would be
    measuring nothing -- the shape this program calls green by construction.
    """
    try:
        d: Decision = select(state)
    except Exception as exc:
        logger.warning("[v2.shadow] select() raised at round %s: %s",
                       getattr(state, "round_index", "?"), exc)
        return None

    expected = map_directive(v1_directive, v1_reason)
    agrees = (expected is not None) and (expected is d.posture)

    return {
        "event": "v2_shadow",
        "round": state.round_index,
        "v1_directive": v1_directive,
        "v1_reason": v1_reason,
        "v1_maps_to": expected.value if expected else None,
        "v2_posture": d.posture.value,
        "v2_directive": d.directive.value if d.directive else None,
        "v2_because": d.because,
        # THE THINKING, PERSISTED. Ananth, 2026-09-11: "there is no way in the
        # AI world for anyone to understand what the model is doing, and the
        # thinking is really the only way."
        #
        # Every input and every intermediate the decision was made from, not a
        # verdict: open gaps with their age, levers and per-attempt payload
        # checks; the budget arithmetic including the shortfall; which branch
        # of select() fired; and the predicates that were true. explain() CALLS
        # the same predicates select() called rather than reimplementing them,
        # so it cannot become a second author of the decision it reports.
        "v2_decision_inputs": explain(state, d),
        # A deliberate, evidenced draw on the band -- not a miss. Carried so the
        # record can tell the two apart; they are identical in a latency number.
        "v2_overran": bool(getattr(d, "overran", False)),
        "v2_gap_targeted": d.gap_targeted,
        # The ledger has read these two since 068 and compare() never wrote
        # them, so every row landed with [] while its own rationale said "gaps
        # open" -- a row contradicting itself. Fifth consumer-with-no-producer
        # found today, and the first one in a field I had already shipped to
        # another seat as evidence.
        "gaps_opened": [g.gap_id for g in state.open_gaps],
        "gaps_closed": list(state.gaps_closed),
        # THE verdict, authored HERE and nowhere else. An earlier version
        # returned only the two booleans and let emit() derive the string as a
        # local -- so the persisted column read a key that never existed and
        # every row stored NULL. Worse, I had told the FE seat "compare()
        # authors it once; the page is a viewer", which was true of the design
        # and false of the code. One author, and the string is it.
        "verdict": ("agree" if agrees else "unmapped" if expected is None
                    else "diverge"),
        "agrees": agrees,
        # unmapped is NOT a disagreement -- it means v1 said something the
        # mapping does not cover, which is a finding about the mapping.
        "unmapped": expected is None,
    }


def emit(correlation_id: str, comparison: dict | None) -> None:
    """One structured line per round. Never raises into the caller.

    Emitted whether or not the two agree: a shadow that only reports
    disagreements cannot distinguish 'they agreed' from 'the shadow did not
    run', and those are the two readings that matter most.
    """
    if not comparison:
        return
    try:
        # Accumulate for the settle-time batch write. The stream is a VIEW; the
        # row is truth. A shadow whose only record is a log line violates the
        # emit contract it was built under.
        pass
        verdict = comparison.get("verdict", "unknown")
        logger.info(
            "[v2.shadow] %s cid=%s r%s v1=%s v2=%s — %s",
            verdict.upper(), str(correlation_id)[:8], comparison["round"],
            comparison["v1_directive"], comparison["v2_posture"],
            comparison["v2_because"],
            extra={**comparison, "correlation_id": correlation_id},
        )
    except Exception as exc:
        logger.warning("[v2.shadow] emit failed: %s", exc)


def divergence_rate(verdicts: list[str]) -> dict:
    """The rate, WITH its population stated — never a bare percentage.

    Tool Selection's finding, 2026-09-11, applied here before it bit: a
    verdict enum answers more than one question, and `unmapped` answers a
    different one from `agree`/`diverge`.

        DECISION   agree · diverge     "the two seats chose differently"
        MAPPING    unmapped            "v1 said something I do not cover"

    An `unmapped` round is not v2 disagreeing with v1. Folded into a
    denominator it inflates divergence and blames the posture machine for a
    hole in the translation table. Today there are ZERO unmapped rows, which
    is exactly why this is the moment to write it — the guard has to exist
    before the value does, or the first one through is counted wrong and the
    number is already in a report.

    Their harder lesson is the reason this is a function and not a comment:
    a correction can land FURTHER from the truth than the original when the
    fix addresses the numerator and leaves the denominator carrying rows that
    were never in scope. `excluded_unmapped` is reported ALONGSIDE and is
    never folded in.
    """
    agree = sum(1 for v in verdicts if v == "agree")
    diverge = sum(1 for v in verdicts if v == "diverge")
    unmapped = sum(1 for v in verdicts if v == "unmapped")
    decided = agree + diverge
    return {
        "population": "rounds where BOTH seats made a coverable decision",
        "decided": decided,
        "agree": agree,
        "diverge": diverge,
        # None, not 0.0. A rate over an empty population is not zero
        # divergence; it is no measurement.
        "pct_diverge": round(100.0 * diverge / decided, 1) if decided else None,
        "excluded_unmapped": unmapped,
        "unclassified": len(verdicts) - decided - unmapped,
    }


# Fallback ONLY for a turn that has no promise -- enqueued before the promise
# existed. Never a synthesised promise: one invented mid-turn would backfill
# the exact gap the attestation measures.
_NO_PROMISE_FALLBACK_S = 31.0


def promise_seconds(ctx, contract) -> float:
    """The budget v2 decides against: the PROMISE.

    Not `contract.soft_target_s` -- that is v1's internal nudge (copilot 12.0s)
    and the promise is the contract (copilot 31.0s). Deciding against the nudge
    starved every round: `spendable` False, `worth_spending` None, and a NARROW
    that read like a judgement and was arithmetic.

    Order: the turn's own frozen promise, then the tier table, then a declared
    fallback. Never the soft target.
    """
    p = getattr(ctx, "promise", None)
    lat = getattr(p, "latency_s", None) if p is not None else None
    if isinstance(lat, (int, float)) and lat > 0:
        return float(lat)
    logger.info("[v2] no promise on ctx; budget falls back to %.1fs",
                _NO_PROMISE_FALLBACK_S)
    return _NO_PROMISE_FALLBACK_S


# The promise's BAND — the stated tolerance, per tier. 13±5 · 31±8 · 95±25.
#
# Budget.band_s defaulted to 0.0 and state_from_ctx never set it, so
# may_overrun's band arm was inert: a turn genuinely out of promise time could
# never overrun even when converging, and the one that DID overrun drew on
# unreserved promise time while its message said "from a 0.0s band".
#
# Keyed on the PROMISED SECONDS rather than on the tier name, because the tier
# name is resolved in three places and the promised value is already on the
# frozen promise. An unrecognised promise gets NO band -- a synthesised
# tolerance is the same defect as a synthesised promise.
_BAND_BY_PROMISE_S = {13.0: 5.0, 31.0: 8.0, 95.0: 25.0}


def band_seconds(promise_s: float) -> float:
    """The stated tolerance for this promise, or 0.0 when we do not have one."""
    return _BAND_BY_PROMISE_S.get(round(float(promise_s), 3), 0.0)
