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
    Attempt, Budget, Decision, Gap, Posture, RoundState, select,
)

logger = logging.getLogger(__name__)

# v1 directive -> the posture it corresponds to.
# 'extend' maps to EXPLORE because buying another round to keep going IS
# exploring; the extension is a budget act, not a different kind of work.
DIRECTIVE_TO_POSTURE = {
    "search": Posture.EXPLORE,
    "extend": Posture.EXPLORE,
    "consolidate": Posture.NARROW,
    "finalize": Posture.COMMUNICATE,
    "complete": Posture.COMMUNICATE,
}


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

        for r in rounds:
            enr = (r or {}).get("enrichment") or {}
            gaps = [g for g in (enr.get("gaps_open") or []) if isinstance(g, str)]
            history.append(len(gaps))
            open_texts = gaps  # the latest round's list is the live one
            tool = (r or {}).get("tool")
            query = (((r or {}).get("inputs") or {}).get("query"))
            for g in gaps:
                attempts_by_text.setdefault(g, [])
                if tool:
                    attempts_by_text[g].append(
                        Attempt(round_index=int((r or {}).get("round") or 0),
                                tool=tool, query=query,
                                # R0 cannot see the payload check yet, so this
                                # stays False. It makes CAPABILITY reachable in
                                # the shadow but never acted on -- and the
                                # divergence it produces is itself informative.
                                returned_payload=False))

        opened_at: dict[str, int] = {}
        for r in rounds:
            enr = (r or {}).get("enrichment") or {}
            for g in (enr.get("gaps_open") or []):
                opened_at.setdefault(g, int((r or {}).get("round") or 0))

        gaps = tuple(
            Gap(gap_id=f"S{i+1}", text=t, opened_round=opened_at.get(t, round_index),
                attempted_by=tuple(attempts_by_text.get(t, ())))
            for i, t in enumerate(open_texts)
        )
        remaining = max(0.0, promise_latency_s - elapsed_s)
        return RoundState(
            round_index=round_index,
            open_gaps=gaps,
            gaps_open_history=tuple(history),
            budget=Budget(remaining_s=remaining, remaining_c=0.0),
            next_round_cost_s=round_cost_s,
            acting_cost_s=acting_cost_s,
            validate_cost_s=9.6,
        )
    except Exception as exc:
        logger.warning("[v2.shadow] state_from_ctx failed r%s: %s", round_index, exc)
        return None


def compare(v1_directive: str | None, state: RoundState) -> dict | None:
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

    expected = DIRECTIVE_TO_POSTURE.get((v1_directive or "").strip().lower())
    agrees = (expected is not None) and (expected is d.posture)

    return {
        "event": "v2_shadow",
        "round": state.round_index,
        "v1_directive": v1_directive,
        "v1_maps_to": expected.value if expected else None,
        "v2_posture": d.posture.value,
        "v2_directive": d.directive.value if d.directive else None,
        "v2_because": d.because,
        "v2_gap_targeted": d.gap_targeted,
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
        verdict = ("AGREE" if comparison["agrees"]
                   else "UNMAPPED" if comparison["unmapped"] else "DIVERGE")
        logger.info(
            "[v2.shadow] %s cid=%s r%s v1=%s v2=%s — %s",
            verdict, str(correlation_id)[:8], comparison["round"],
            comparison["v1_directive"], comparison["v2_posture"],
            comparison["v2_because"],
            extra={**comparison, "correlation_id": correlation_id,
                   "verdict": verdict},
        )
    except Exception as exc:
        logger.warning("[v2.shadow] emit failed: %s", exc)
