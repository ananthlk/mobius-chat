"""Did react actually follow the instruction, or ignore it?

Ananth, 2026-09-12: *"what we need is a set of telemetry on what instructions
the llm follows vs what it ignores over time."*

THE ONE RULE: compliance is OBSERVED FROM THE TRACE, never asked of the model.

A "did you follow it?" field would be a self-report, and this system has
already shipped a model asserting "no sources found" beside a confident answer,
and a closure number that has to be cross-checked before it can be believed.
An instruction-following metric built on self-report would be the most
flattering number in the product and the least true.

THE SECOND RULE: a statement whose compliance cannot be SEEN is recorded
UNOBSERVABLE, never as followed and never as ignored.

Three-quarters of the value of this module is that distinction. "We could not
check" and "it ignored us" are opposite facts about an instruction: the first
says rewrite the telemetry, the second says rewrite the statement. Collapsing
them is the could-not-check / checked-false defect that has cost this program
eight instances, and here it would quietly condemn instructions that were
obeyed.

WHEN IT RUNS: at turn end, over the whole trace. A statement sent at round N is
judged by round N+1's behaviour, which does not exist when the statement is
sent -- so evaluating inline would require a retroactive update, and a row
rewritten later is a row nobody can trust.

PURE. Trace in, verdicts out.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.pipeline.v2.shadow import _targeting, _tokens


class Verdict(str, Enum):
    FOLLOWED = "followed"
    IGNORED = "ignored"
    # Not "unknown": the instruction may well have been followed, and this says
    # only that the trace cannot show it. Kept out of the rate entirely.
    UNOBSERVABLE = "unobservable"
    # The statement names a choice that is legitimately react's -- the
    # governor's dissent is answerable either way. Recorded so the ANSWER can
    # be counted without pretending a refusal is disobedience.
    DECLINED = "declined"


@dataclass(frozen=True)
class Observation:
    statement_id: str
    round_index: int
    verdict: Verdict
    basis: str          # what was compared, in words, so a reader can argue


def _query_of(r: dict) -> str:
    return str(((r or {}).get("inputs") or {}).get("query") or "")


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ── the observers ───────────────────────────────────────────────────────────
#
# Each takes (the round the statement was SENT for, the NEXT round, the gaps
# open at send time) and returns a verdict. `nxt` is None when the statement
# was sent on the last round -- and that is UNOBSERVABLE, not compliance:
# there is no subsequent behaviour to look at.

def _obs_ask_as_asked(cur, nxt, gaps):
    """FRM-2: one query naming every part."""
    q = _query_of(cur)
    if not q or len(gaps) < 2:
        return Verdict.UNOBSERVABLE, "no query recorded, or single-part question"
    aimed = _targeting(gaps, q)
    hit = sum(1 for v in aimed.values() if v)
    if hit >= len(gaps):
        return Verdict.FOLLOWED, f"query named all {len(gaps)} parts"
    return Verdict.IGNORED, f"query named {hit} of {len(gaps)} parts: {q[:60]!r}"


def _obs_work_this_gap(cur, nxt, gaps, target=None):
    """EVD-4: work THIS gap. Compliance is the same predicate the ledger uses
    to attribute an attempt -- one definition of "this query aimed there", not
    two that can drift."""
    if nxt is None:
        return Verdict.UNOBSERVABLE, "statement sent on the final round"
    q = _query_of(nxt)
    if not q or not target:
        return Verdict.UNOBSERVABLE, "no next query recorded"
    aimed = _targeting(gaps, q)
    if aimed.get(target):
        return Verdict.FOLLOWED, f"next query aimed at {target[:40]!r}"
    return Verdict.IGNORED, f"next query did not name it: {q[:60]!r}"


def _obs_do_not_repeat(cur, nxt, gaps):
    """EVD-2: do not re-run the query you just ran."""
    if nxt is None:
        return Verdict.UNOBSERVABLE, "statement sent on the final round"
    a, b = _query_of(cur), _query_of(nxt)
    if not a or not b:
        return Verdict.UNOBSERVABLE, "a query was not recorded"
    j = _jaccard(a, b)
    # 0.70 is REPEAT_JACCARD, reused deliberately: if the ledger calls two
    # queries the same, the compliance metric must not call them different.
    if j >= 0.70:
        return Verdict.IGNORED, f"next query {j:.2f} similar to the last"
    return Verdict.FOLLOWED, f"next query {j:.2f} similar to the last"


def _obs_final_round(cur, nxt, gaps):
    """SAF-1: no further tool call."""
    if nxt is None:
        return Verdict.FOLLOWED, "no round followed the final-round instruction"
    return Verdict.IGNORED, f"a round {nxt.get('round')} ran with tool={nxt.get('tool')!r}"


def _obs_dissent(cur, nxt, gaps, target=None):
    """CTL-1: the governor's dissent. BOTH answers are legitimate -- react may
    confirm complete or spend the round -- so this measures the RESPONSE, and
    never counts a refusal as disobedience. Ananth: "not to do so
    unilaterally"."""
    if nxt is None:
        return Verdict.DECLINED, "react held complete; no further round"
    q = _query_of(nxt)
    aimed = _targeting(gaps, q) if q else {}
    if target and aimed.get(target):
        return Verdict.FOLLOWED, "react accepted and searched the named part"
    return Verdict.DECLINED, "react continued without searching the named part"


# statement id -> observer, or None where compliance CANNOT be seen.
#
# The Nones are the honest half of this table. CTL-3 asks react to consider
# whether it did enough; nothing in the trace distinguishes "considered it and
# was satisfied" from "did not read it". Inventing a proxy would produce a
# number that moves, which is worse than a gap that is labelled.
OBSERVERS: dict[str, object] = {
    "FRM-2": _obs_ask_as_asked,
    "EVD-4": _obs_work_this_gap,
    "EVD-2": _obs_do_not_repeat,
    "SAF-1": _obs_final_round,
    "CTL-1": _obs_dissent,
    "CTL-3": None,   # a disposition, not an act
    "FRM-1": None,   # gaps_open is emitted in the SAME response; see below
    "FRM-7": None,   # needs answer-text classification; not built
    "STR-1": None,   # "ask for the part it did not return" -- no crisp signal
    "STR-2": None,
    "EVD-1": None,   # round-2 review; overlaps FRM-1's problem
}

# FRM-1 is worth its own note. It asks react to name every part in gaps_open --
# emitted in the SAME response as the tool call, not the next one. It looks
# observable and is not: a turn where gaps_open has three entries and the query
# names one has FOLLOWED FRM-1 and IGNORED FRM-2. Scoring it as one number
# would blame the wrong instruction, which is exactly the mistake that cost
# this seat two withdrawn causal claims tonight.


def observe_turn(rounds: list[dict],
                 sent: dict[int, list[dict]]) -> list[Observation]:
    """Judge every statement sent this turn.

    `sent` maps round_index -> [{"id": ..., "gaps": [...], "target": ...}],
    recorded when the block was built. Without it this function would have to
    re-derive what was sent, and a re-derivation that disagrees with what
    actually went to the model is a second author of the record.
    """
    by_index = {int(r.get("round") or 0): r for r in rounds if isinstance(r, dict)}
    out: list[Observation] = []
    for rn, items in sorted(sent.items()):
        cur, nxt = by_index.get(rn), by_index.get(rn + 1)
        if cur is None:
            continue
        for item in items:
            sid = item.get("id")
            obs = OBSERVERS.get(sid, "missing")
            if obs is None:
                out.append(Observation(sid, rn, Verdict.UNOBSERVABLE,
                                       "no observer: compliance is not visible "
                                       "in the trace"))
                continue
            if obs == "missing":
                out.append(Observation(sid, rn, Verdict.UNOBSERVABLE,
                                       "statement has no entry in OBSERVERS"))
                continue
            gaps = [g for g in (item.get("gaps") or []) if isinstance(g, str)]
            try:
                if sid in ("EVD-4", "CTL-1"):
                    v, why = obs(cur, nxt, gaps, item.get("target"))
                else:
                    v, why = obs(cur, nxt, gaps)
            except Exception as e:      # an observer must never break a turn
                v, why = Verdict.UNOBSERVABLE, f"observer raised: {e!r}"
            out.append(Observation(sid, rn, v, why))
    return out


def rate(observations: list[Observation]) -> dict[str, dict]:
    """Per-statement follow rate.

    UNOBSERVABLE and DECLINED are EXCLUDED FROM THE DENOMINATOR and reported
    beside it. Folding them in would let an instruction nobody can measure
    drag down one that is being obeyed, and would count a legitimate refusal
    as disobedience.
    """
    acc: dict[str, dict] = {}
    for o in observations:
        a = acc.setdefault(o.statement_id, {
            "sent": 0, "followed": 0, "ignored": 0,
            "unobservable": 0, "declined": 0,
        })
        a["sent"] += 1
        a[o.verdict.value] += 1
    for a in acc.values():
        judged = a["followed"] + a["ignored"]
        a["judged"] = judged
        a["follow_rate"] = round(a["followed"] / judged, 3) if judged else None
    return acc


# ── ACK CHECKERS ────────────────────────────────────────────────────────────
#
# An ack proves the section was READ. These prove the round ACTED on it. Both
# are recorded and the DISAGREEMENT is the signal -- an ack that has never once
# contradicted the trace is measuring nothing and should be deleted.
#
# No ack ships without its checker. An unchecked ack reads as evidence while
# proving nothing, which is strictly worse than no ack at all.

@dataclass(frozen=True)
class AckCheck:
    key: str
    verdict: Verdict
    claimed: str
    observed: str
    basis: str


def check_parts(ack: dict, this_round: dict, gaps: list[str]) -> AckCheck:
    """`parts` says what the question asks for. Does the query cover them?

    THE 3/3 FAILURE: three runs named three payers in gaps_open and then queried
    one, with the model's own words -- "starting with Molina Healthcare". The
    ack makes the contradiction explicit instead of leaving it to be inferred
    from two fields that were never compared.
    """
    claimed = [p for p in (ack.get("parts") or []) if isinstance(p, str)]
    q = _query_of(this_round)
    if len(claimed) < 2:
        return AckCheck("parts", Verdict.UNOBSERVABLE, str(claimed), q,
                        "single-part question: nothing to under-cover")
    # 🔴 A NARROWED ROUND IS NOT AN IGNORED INSTRUCTION.
    #
    # "Cover every part in one query" is the ROUND-1 instruction. From round 2
    # the governor names ONE part and says the others are not for this round --
    # so a query naming one part is obedience, and scoring it as IGNORED
    # condemns exactly the behaviour we asked for.
    #
    # Caught in simulation, not live: the honest-and-compliant case came back
    # `parts: ignored`. A follow-rate built on that would have been permanently
    # red, and the fix would have been to "correct" a prompt that was working.
    if ack.get("working_gap"):
        return AckCheck("parts", Verdict.UNOBSERVABLE, f"{len(claimed)} parts", q,
                        "the governor named one part this round; a narrow "
                        "query is instructed, not a lapse")
    if not q:
        return AckCheck("parts", Verdict.UNOBSERVABLE, str(claimed), "",
                        "no query recorded this round")
    aimed = _targeting(claimed, q)
    hit = sum(1 for v in aimed.values() if v)
    if hit >= len(claimed):
        return AckCheck("parts", Verdict.FOLLOWED, f"{len(claimed)} parts", q,
                        f"query named all {len(claimed)}")
    missed = [p for p, v in aimed.items() if not v]
    return AckCheck("parts", Verdict.IGNORED, f"{len(claimed)} parts", q,
                    f"query named {hit} of {len(claimed)}; missed {missed}")


def check_working_gap(ack: dict, this_round: dict,
                      gaps_by_id: dict[str, str]) -> AckCheck:
    """`working_gap` names the ONE part being worked. Does the query aim there?

    Catches "says Sunshine, queries Molina" -- a contradiction invisible today
    because nothing compares the two. Uses the SAME predicate the ledger uses
    to attribute an attempt: one definition of "this query aimed there", never
    two that can drift.
    """
    gid = ack.get("working_gap")
    q = _query_of(this_round)
    if not gid:
        return AckCheck("working_gap", Verdict.UNOBSERVABLE, "null", q,
                        "react declared no working part this round")
    text = gaps_by_id.get(gid)
    if not text:
        # Naming an id that is not open is itself a finding: the ledger and the
        # model disagree about what exists.
        return AckCheck("working_gap", Verdict.IGNORED, str(gid), q,
                        "acked a gap id that is not in the open ledger")
    if not q:
        return AckCheck("working_gap", Verdict.UNOBSERVABLE, str(gid), "",
                        "no query recorded this round")
    aimed = _targeting(list(gaps_by_id.values()), q)
    if aimed.get(text):
        return AckCheck("working_gap", Verdict.FOLLOWED, str(gid), q,
                        "query aimed at the acked part")
    return AckCheck("working_gap", Verdict.IGNORED, str(gid), q,
                    "query did not name the acked part")


def check_dissent(ack: dict, next_round: dict | None,
                  dissent_gap_text: str | None,
                  gaps: list[str]) -> AckCheck:
    """`dissent` answers the governor's second opinion.

    BOTH ANSWERS ARE LEGITIMATE -- Ananth: "not to do so unilaterally". So a
    decline is never disobedience. What this catches is ACCEPTED-IN-WORDS,
    DECLINED-IN-FACT: react says "accepted" and the next round does not search
    the named part. That is the only failure mode here, and it is invisible
    without the ack.
    """
    said = str(ack.get("dissent") or "").strip().lower()
    if not dissent_gap_text:
        return AckCheck("dissent", Verdict.UNOBSERVABLE, said, "",
                        "no dissent was raised this round")
    if said.startswith("declined"):
        return AckCheck("dissent", Verdict.DECLINED, said, "",
                        "react declined, which is its call")
    if next_round is None:
        return AckCheck("dissent", Verdict.IGNORED, said, "(no next round)",
                        "accepted in words; the turn ended without searching it")
    q = _query_of(next_round)
    aimed = _targeting(gaps, q) if q else {}
    if aimed.get(dissent_gap_text):
        return AckCheck("dissent", Verdict.FOLLOWED, said, q,
                        "accepted and the next round searched it")
    return AckCheck("dissent", Verdict.IGNORED, said, q,
                    "accepted in words; the next round searched something else")


# ── TURN-END EVALUATION ─────────────────────────────────────────────────────

def evaluate_turn(ctx) -> dict:
    """Judge every statement and every ack for one turn. Called at turn end.

    WHY TURN END AND NOT INLINE: a statement sent at round N is judged by
    round N+1's behaviour, which does not exist when the statement is sent.
    Evaluating inline would need a retroactive update, and a row rewritten
    later is a row nobody can trust.

    Returns a plain dict so the caller can persist it without this module
    knowing anything about storage -- it stays pure and testable.
    """
    rounds = [r or {} for r in (getattr(ctx, "react_trace_rounds", None) or [])]
    sent = dict(getattr(ctx, "v2_statements_sent", None) or {})
    by_index = {int(r.get("round") or 0): r for r in rounds}

    statements = [
        {"id": o.statement_id, "round": o.round_index,
         "verdict": o.verdict.value, "basis": o.basis}
        for o in observe_turn(rounds, sent)
    ]

    acks: list[dict] = []
    for rn, items in sorted(sent.items()):
        cur = by_index.get(rn)
        if cur is None:
            continue
        ack = cur.get("ack")
        if not isinstance(ack, dict):
            # NOT a failure verdict. The frame asks for an ack; react not
            # returning one is a fact about the PROMPT, not about the round,
            # and scoring it as disobedience would blame the wrong thing.
            acks.append({"round": rn, "key": "*", "verdict": Verdict.UNOBSERVABLE.value,
                         "basis": "react returned no ack this round"})
            continue
        gaps = [g for g in (items[0].get("gaps") or []) if isinstance(g, str)] if items else []
        # id -> text, so `working_gap` is checked against the REAL ledger
        # rather than against the ack's own claim about it. Recorded when the
        # frame is built; an empty map makes the check UNOBSERVABLE, which is
        # the correct answer when we cannot see the ledger, not a pass.
        by_id = dict(getattr(ctx, "v2_gap_ids", None) or {})
        dissent_gap = next(
            (it.get("target") for it in items
             if it.get("id") == "CTL-1" and it.get("target")), None)
        nxt = by_index.get(rn + 1)
        for chk in (check_parts(ack, cur, gaps),
                    check_working_gap(ack, cur, by_id),
                    check_complete(ack, cur),
                    check_dissent(ack, nxt, dissent_gap, gaps)):
            acks.append({"round": rn, "key": chk.key, "verdict": chk.verdict.value,
                         "claimed": chk.claimed, "observed": chk.observed,
                         "basis": chk.basis})

    return {"statements": statements, "acks": acks,
            "rate": rate([Observation(s["id"], s["round"],
                                      Verdict(s["verdict"]), s["basis"])
                          for s in statements])}


def check_complete(ack: dict, this_round: dict) -> AckCheck:
    """`complete` against `is_complete` IN THE SAME RESPONSE.

    The cheapest of the four and the only one that needs no other round: react
    saying "complete: false" in its ack while setting is_complete=true in the
    same object is an internal contradiction, and it is the one failure mode
    where the ack and the decision cannot both be right.

    BUILT BECAUSE IT WAS CLAIMED. I sent the prompt seat a table listing four
    checkers and had built three; they grepped for the fourth, did not find it,
    and said so -- explicitly to stop "four checkers exist" becoming a fact the
    way "rag decomposes by entity" did earlier tonight. The correct response to
    that is to build the missing one, not to trim the claim.
    """
    if "complete" not in ack:
        return AckCheck("complete", Verdict.UNOBSERVABLE, "", "",
                        "react did not ack a completion call")
    if "is_complete" not in this_round:
        # Not a pass. The round record predates this field, or the wiring is
        # gone -- either way the comparison did not happen, and saying so is
        # the difference between could-not-check and checked-false.
        return AckCheck("complete", Verdict.UNOBSERVABLE, str(ack.get("complete")),
                        "", "the round did not record is_complete")
    claimed = bool(ack.get("complete"))
    actual = bool(this_round.get("is_complete"))
    if claimed == actual:
        return AckCheck("complete", Verdict.FOLLOWED, str(claimed), str(actual),
                        "the ack and the decision agree")
    return AckCheck("complete", Verdict.IGNORED, str(claimed), str(actual),
                    f"ack said complete={claimed} while the response set "
                    f"is_complete={actual}")


# ── "was another round worth it?" — the claim, checked against what happened ──
#
# Ananth, 2026-09-12: "the governor knows if a next round is feasible.. so the
# question to llm is .. do you think another round is worth it to improve the
# score.. lets see what it says we dont have to rely on it, just asking may be
# helpful".
#
# NOT A GATE. Nothing reads this verdict to decide anything -- it exists to
# build the record that would let us decide later, and it says so. The useful
# number is the DISAGREEMENT rate: how often "another round is worth it" was
# followed by a round that closed nothing. An ack that always agrees with what
# happened is measuring nothing and should be deleted.
#
# This is also the only ack whose truth arrives AFTER the round that made the
# claim, which is why it is checked at turn end against the following round
# rather than inline.

def check_next_round_worth(ack: dict, this_round: dict,
                           next_round: dict | None) -> tuple[Verdict, str]:
    """Did the next round deliver what the claim promised it would?

    FOLLOWED      claimed worth it AND the next round closed a gap or kept new
                  evidence; or claimed NOT worth it and the turn stopped.
    IGNORED       claimed worth it and the next round changed nothing -- the
                  round was bought on a promise it did not keep.
    UNOBSERVABLE  no claim, or no next round to judge it by. A turn that ended
                  for BUDGET reasons cannot tell us whether the model's
                  judgement was right, and scoring that as correct would credit
                  the claim for our own decision to stop.
    """
    claim = (ack or {}).get("next_round_worth_it")
    if not isinstance(claim, bool):
        return Verdict.UNOBSERVABLE, "no next_round_worth_it in the ack"

    if next_round is None:
        if claim is False:
            # It said stop and the turn stopped. Weak evidence -- the turn may
            # have stopped for budget -- so it is named as agreement, not proof.
            return Verdict.FOLLOWED, "said another round would not help; turn ended"
        return (Verdict.UNOBSERVABLE,
                "said another round would help, but no next round ran — "
                "cannot tell a wrong call from a budget stop")

    enr = (next_round or {}).get("enrichment") or {}
    closed = list(enr.get("gaps_closed") or ())
    kept = enr.get("kept")
    moved = bool(closed) or (isinstance(kept, int) and kept > 0)

    if claim and moved:
        return Verdict.FOLLOWED, (
            f"next round closed {len(closed)} gap(s)" if closed
            else f"next round kept {kept} new chunk(s)")
    if claim and not moved:
        return Verdict.IGNORED, (
            "said another round would help; the next round closed nothing and "
            "kept nothing")
    if not claim and moved:
        # It said stop, we went anyway, and the round paid off. That is OUR
        # call being wrong-footed, not the model's -- recorded as DECLINED so
        # it never reads as the model having ignored an instruction.
        return Verdict.DECLINED, (
            "said another round would not help, but the round that ran did "
            "close something")
    return Verdict.FOLLOWED, "said another round would not help; it did not"
