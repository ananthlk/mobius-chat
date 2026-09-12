"""The round prompt as BLOCKS: assembled from what we know, not a template.

Ananth, 2026-09-12:

    Tool identity      : Mobius
    User identity and preferences
    Role identity      : JUDGE (existing answers, identify gaps)
                         PLAN  (identify best tools to answer those gaps)
    Context offered    : the user question
                         answer this round by gap (by tool)
                         previous answers you found useful
                         info that you did NOT find useful
                         mark as complete

    "i want this to be as modular so that we can construct it based on what we
     know"

THE DESIGN RULE: a block renders only when its FACT EXISTS. Not "when the flag
is on" and not "always, empty if absent" -- an empty section is a claim that
there is nothing there, and react cannot tell that apart from a section nobody
filled in. Every `when` below answers "do we know this yet?".

TWO ROLES, NOT ONE. Ananth's split is the load-bearing part: JUDGE reads what
came back and names what is missing; PLAN says which tool would close it.
Round 1 with preload is nearly all JUDGE. A round with named gaps and a
suggestion list is both. Rendering PLAN when no gap is named asks react to
choose a tool for nothing -- which is the round-1 contradiction that had it
searching twice.

🔴 `discarded` IS NEW AND IS THE POINT OF THE EXERCISE. react already tells us
what it KEPT (`evidence_review.keep`); nothing has ever told react what it
previously discarded. Without it a later round re-retrieves the same unhelpful
material and re-reads it -- there is no memory of a negative result anywhere in
this system. It is the same distinction as never-searched vs searched-and-empty,
one level down: kept vs seen-and-rejected.

PURE. Facts in, text out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable


class Slot(IntEnum):
    """Render order. Identity first (who am I, who is asking), then role
    (what am I doing), then context (what do I have), then the ask."""
    TOOL_IDENTITY = 1
    USER_IDENTITY = 2
    ROLE = 3
    QUESTION = 4
    # Three separate slots, NOT one shared slot with an alphabetical tiebreak.
    # They were THIS_ROUND=5 together, and sorting by (slot, id) put
    # "suggest" before "this_round" -- so the prompt read "here are tools you
    # may request" BEFORE "work this gap and no other". Prompt order decided
    # by variable naming is not an ordering; it is an accident that happens to
    # be stable.
    #
    # The real sequence: what already came back, then which gap this round is
    # for, then what you may ask for next.
    EVIDENCE = 5        # already retrieved, judge this
    TARGET = 6          # work this gap and no other
    TOOLS = 7           # tools you may request next
    USEFUL = 8          # previous answers you found useful
    NOT_USEFUL = 9      # info you did NOT find useful
    COMPLETE = 10       # mark as complete


@dataclass(frozen=True)
class Facts:
    """Everything a block may read. A block needing something not here must
    add it, which makes the dependency visible rather than reaching into ctx
    and taking whatever happens to be present."""
    question: str = ""
    user_name: str = ""
    user_prefs: tuple[str, ...] = ()          # e.g. ("tone: friendly", "brief")
    org: str = ""
    # What is open, and what each gap has cost so far.
    gaps: tuple[tuple[str, str], ...] = ()    # (gap_id, text)
    targeted_gap: str = ""                    # the ONE gap this round is for
    # Evidence already retrieved this round, before react speaks.
    preloaded: tuple[tuple[str, bool, str], ...] = ()   # (tool, ok, summary)
    suggest: tuple[str, ...] = ()             # tools react may request
    # react's own running judgement, carried forward.
    useful: tuple[str, ...] = ()              # what it kept and why it mattered
    discarded: tuple[str, ...] = ()           # what it saw and rejected
    can_complete: bool = True


@dataclass(frozen=True)
class Block:
    id: str
    slot: Slot
    when: Callable[[Facts], bool]
    render: Callable[[Facts], str]
    owner: str = "governor"     # who owns the WORDING; see statement_text.py
    # Order WITHIN a slot. The four roles share Slot.ROLE, and the tiebreak was
    # `id` -- which would have printed COMMUNICATE first, before the judging it
    # depends on, purely because "c" sorts before "j". That is the same accident
    # the EVIDENCE/TARGET/TOOLS comment above describes, one level down. Roles
    # run in the order the work runs: judge -> plan -> summarise -> communicate.
    rank: int = 0


def _bullets(items) -> str:
    return "\n".join(f"  - {i}" for i in items)


REGISTRY: tuple[Block, ...] = (

    Block("tool_identity", Slot.TOOL_IDENTITY,
          when=lambda f: True,
          render=lambda f: "[WHO YOU ARE] Mobius — a payer-policy research "
                           "assistant. You answer from retrieved evidence, "
                           "never from background knowledge.",
          owner="llm_seat"),

    Block("user_identity", Slot.USER_IDENTITY,
          # Only when we actually know something. "User: (unknown)" is worse
          # than silence -- it asserts we looked and found nothing.
          when=lambda f: bool(f.user_name or f.user_prefs or f.org),
          render=lambda f: "[WHO IS ASKING] " + " · ".join(
              x for x in (f.user_name, f.org, "; ".join(f.user_prefs)) if x),
          owner="chat"),

    # ── ROLE: two jobs, rendered only when each is real ──────────────────
    Block("role_judge", Slot.ROLE, rank=1,
          when=lambda f: bool(f.preloaded),
          render=lambda f: "[YOUR ROLE — JUDGE] Evidence has already been "
                           "retrieved for you below. Read it and decide: does "
                           "it answer the question? Name every part it does "
                           "NOT answer as a gap.",
          owner="governor"),

    Block("role_plan", Slot.ROLE, rank=2,
          # PLAN needs something to plan FOR. Rendering it with no gap asks
          # react to choose a tool for nothing -- the round-1 contradiction
          # that had it searching twice.
          when=lambda f: bool(f.gaps) and bool(f.suggest),
          render=lambda f: "[YOUR ROLE — PLAN] For each gap still open, say "
                           "which tool would close it. Name the tool in your "
                           "gap report; you are not calling it this round.",
          owner="governor"),

    Block("role_summarise", Slot.ROLE, rank=3,
          # THE ROLE THAT PRODUCES THE DELIVERABLE. Judge and Plan can both
          # succeed and leave nothing written: one names gaps, the other names
          # tools, and neither answers the question. Ananth, 2026-09-12: "we
          # also need a role as a summarizer. this is critical."
          #
          # Renders whenever there is evidence to write FROM -- retrieved this
          # round, or kept from an earlier one. Not gated on completeness: a
          # PARTIAL answer written from real evidence is the correct outcome on
          # a multi-part question, and the grounding contract says so.
          when=lambda f: bool(f.preloaded or f.useful),
          render=lambda f: "[YOUR ROLE — SUMMARISE] Write the best answer the "
                           "kept evidence supports, and say plainly which "
                           "parts it does not cover. A partial answer from "
                           "real evidence is correct; a complete-looking "
                           "answer that fills gaps from memory is not.",
          owner="governor"),

    Block("role_communicate", Slot.ROLE, rank=4,
          # Ananth, 2026-09-12: "FINAL = SUMMARIZE + COMMUNICATE".
          #
          # SUMMARISE and COMMUNICATE are not the same job, and collapsing them
          # is why a technically-correct final answer can still fail the person
          # who asked. Summarise compresses the EVIDENCE: what does the kept
          # material support. Communicate addresses the QUESTION: answer the
          # parts asked, in the order asked, in their words -- a three-payer
          # question gets three named answers, not one merged paragraph that
          # happens to contain all three.
          #
          # Gated on there being nothing left to PLAN, which is exactly the
          # negation of role_plan's condition. That keeps any single round at
          # or under MAX_ROLES without a cap that silently drops a role: a
          # round still choosing tools is not the round that delivers.
          when=lambda f: bool(f.preloaded or f.useful)
                         and not (bool(f.gaps) and bool(f.suggest)),
          render=lambda f: "[YOUR ROLE — COMMUNICATE] This is the answer the "
                           "user reads. Answer every part they asked, in the "
                           "order they asked it, naming each one. Cite the "
                           "evidence for each claim. Where a part is "
                           "unanswered, say which part and why — do not leave "
                           "the reader to notice the omission.",
          owner="governor"),

    Block("question", Slot.QUESTION,
          when=lambda f: bool(f.question),
          render=lambda f: f"[THE QUESTION] {f.question}",
          owner="chat"),

    Block("this_round", Slot.TARGET,
          when=lambda f: bool(f.targeted_gap),
          render=lambda f: f"[THIS ROUND] Work this gap and no other: "
                           f"{f.targeted_gap!r}",
          owner="governor"),

    Block("preloaded", Slot.EVIDENCE,
          when=lambda f: bool(f.preloaded),
          render=lambda f: "[ALREADY RETRIEVED — judge this]\n" + "\n".join(
              f"  {t} -> {s}" if ok else f"  {t} -> ran, returned nothing"
              for t, ok, s in f.preloaded),
          owner="governor"),

    Block("suggest", Slot.TOOLS,
          when=lambda f: bool(f.suggest),
          render=lambda f: "[TOOLS YOU MAY REQUEST NEXT]\n  "
                           + " · ".join(f.suggest)
                           + "\n  Name one in your gap report; it will be run "
                             "for you. You are not calling a tool this round.",
          owner="governor"),

    Block("useful", Slot.USEFUL,
          when=lambda f: bool(f.useful),
          render=lambda f: "[WHAT YOU FOUND USEFUL SO FAR]\n" + _bullets(f.useful),
          owner="governor"),

    Block("not_useful", Slot.NOT_USEFUL,
          # THE NEW ONE. Nothing has ever told react what it already rejected,
          # so a later round re-retrieves and re-reads it. There is no memory
          # of a negative result anywhere in this system.
          when=lambda f: bool(f.discarded),
          render=lambda f: "[WHAT YOU ALREADY LOOKED AT AND REJECTED — do not "
                           "retrieve or re-read these]\n" + _bullets(f.discarded),
          owner="governor"),

    Block("complete", Slot.COMPLETE,
          when=lambda f: f.can_complete,
          render=lambda f: "[MARK COMPLETE?] Set is_complete=true only if you "
                           "are satisfied with the level of answer AND the "
                           "evidence behind it. A part left unanswered because "
                           "nobody looked is not complete.",
          owner="governor"),
)


# Ananth: "no more than 2 or 3 roles judge, plan, summarise", then
# "FINAL = SUMMARIZE + COMMUNICATE". Four roles EXIST; the cap is per ROUND,
# and the `when` conditions -- not a truncation -- are what hold it: PLAN and
# COMMUNICATE are mutually exclusive by construction (a round still choosing
# tools is not the round that delivers), so no round can reach four.
MAX_ROLES = 3


def assemble(f: Facts) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """(text, rendered_block_ids, skipped_block_ids).

    SKIPPED IS RETURNED, not discarded: "we did not know this" and "we chose
    not to say it" are different, and a prompt that cannot say which blocks it
    omitted cannot be debugged from its output.
    """
    rendered, skipped = [], []
    for b in sorted(REGISTRY, key=lambda b: (b.slot, b.rank, b.id)):
        try:
            ok = bool(b.when(f))
        except Exception:
            ok = False
        (rendered if ok else skipped).append(b)
    # A round with evidence and no role is a prompt that hands react material
    # and never says what to do with it.
    _roles = [b for b in rendered if b.slot is Slot.ROLE]
    assert len(_roles) <= MAX_ROLES, [b.id for b in _roles]

    text = "\n".join(b.render(f) for b in rendered)
    return text, tuple(b.id for b in rendered), tuple(b.id for b in skipped)


# ── FACTS FROM THE LIVE TURN ────────────────────────────────────────────────

def facts_from(ctx, state, *, targeted_gap: str = "",
               preloaded: list[dict] | None = None,
               suggest: tuple[str, ...] = ()) -> Facts:
    """Build Facts from what the turn actually holds. Every field is either
    present or absent -- nothing is fabricated to fill a slot.

    `useful` / `discarded` come from react's own per-round evidence_review:
    `keep` names the chunks it kept, and everything the round returned that is
    NOT in `keep` is what it saw and rejected. The rejected side has never been
    told back to react, so a later round re-retrieves and re-reads it.
    """
    rounds = [r or {} for r in (getattr(ctx, "react_trace_rounds", None) or [])]

    useful: list[str] = []
    discarded: list[str] = []
    for r in rounds:
        enr = (r or {}).get("enrichment") or {}
        # IDENTITIES, not counts. "20 passages were read and not kept" is an
        # instruction react cannot follow -- it never learns which 20. The
        # names+pages come from the round record (react_loop captures them;
        # never chunk text).
        for d in (enr.get("kept_docs") or []):
            if isinstance(d, str) and d not in useful:
                useful.append(d)
        for d in (enr.get("rejected_docs") or []):
            if isinstance(d, str) and d not in discarded:
                discarded.append(d)

    prefs: list[str] = []
    prof = getattr(ctx, "user_profile", None) or {}
    if isinstance(prof, dict):
        for key in ("tone", "verbosity", "routine"):
            if prof.get(key):
                prefs.append(f"{key}: {prof[key]}")

    return Facts(
        question=(getattr(ctx, "message", None) or "").strip(),
        user_name=str(prof.get("display_name") or "") if isinstance(prof, dict) else "",
        user_prefs=tuple(prefs),
        org=str(getattr(ctx, "org_name", "") or ""),
        gaps=tuple((g.gap_id, g.text) for g in getattr(state, "open_gaps", ()) or ()),
        targeted_gap=targeted_gap,
        preloaded=tuple((p.get("tool"), bool(p.get("ok")), str(p.get("summary") or ""))
                        for p in (preloaded or [])),
        suggest=tuple(suggest),
        useful=tuple(useful[-3:]),          # recent, not the whole turn
        discarded=tuple(discarded[-3:]),
        can_complete=True,
    )
