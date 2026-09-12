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
    TOOL_IDENTITY = 10
    USER_IDENTITY = 20
    ROLE = 30
    QUESTION = 40
    # The draft answer, when there IS one. Sits between the question and the
    # evidence because that is what the critic round reads in that order:
    # what was asked, what we said, what we said it from.
    ANSWER = 45
    # Three separate slots, NOT one shared slot with an alphabetical tiebreak.
    # They were THIS_ROUND=5 together, and sorting by (slot, id) put
    # "suggest" before "this_round" -- so the prompt read "here are tools you
    # may request" BEFORE "work this gap and no other". Prompt order decided
    # by variable naming is not an ordering; it is an accident that happens to
    # be stable.
    #
    # The real sequence: what already came back, then which gap this round is
    # for, then what you may ask for next.
    EVIDENCE = 50       # already retrieved, judge this
    TARGET = 60         # work this gap and no other
    TOOLS = 70         # tools you may request next
    USEFUL = 80        # previous answers you found useful
    NOT_USEFUL = 90    # info you did NOT find useful
    COMPLETE = 100     # mark as complete


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
    # The answer already written this turn, if one has been. Its PRESENCE is
    # what turns the next round into the critic round -- see _drafting().
    answer: str = ""
    # 🔴 THIS ROUND EXISTS TO WRITE THE ANSWER.
    #
    # Ananth, 2026-09-12: "if the answer is complete then the next round should
    # have communicate with the extended answer.. i think this is missing".
    #
    # It was. Measured: react said complete=true in round 1, whose roles were
    # judge · plan · summarise — COMMUNICATE was in the not-sent list. So the
    # answer the user reads was written by a round that had been asked to
    # SUMMARISE the evidence, never to answer the person. Those are different
    # jobs, which is the entire reason the roles are separate.
    finalising: bool = False
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


def _drafting(f: Facts) -> bool:
    """True while the answer is still being WRITTEN.

    Ananth, 2026-09-12: "the round after = next steps + critic .. this allows
    us to bypass the integrate enricher .. and bypass a whole module by
    allowing react to consume those roles.. its just another loop"

    That is the whole mechanism. Enrichment was a separate module that read a
    finished answer and bolted critique and next steps onto it -- a second
    system, with its own model call, its own failure mode, and no access to the
    evidence the answer came from. As a ROLE it is just the next turn of the
    loop react is already in, holding everything react already holds.

    Which means the roles split cleanly in two by one fact: has an answer been
    written yet. Before -- judge, plan, summarise, communicate. After --
    critic, next steps. Nothing renders on both sides, so no round exceeds the
    cap and no round is asked to draft and review the same text at once.
    """
    return not f.answer.strip()


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
          when=lambda f: _drafting(f) and (bool(f.preloaded)),
          render=lambda f: "[YOUR ROLE — JUDGE] Evidence has already been "
                           "retrieved for you below. Read it and decide: does "
                           "it answer the question? Name every part it does "
                           "NOT answer as a gap.",
          owner="governor"),

    Block("role_plan", Slot.ROLE, rank=2,
          # PLAN needs something to plan FOR. Rendering it with no gap asks
          # react to choose a tool for nothing -- the round-1 contradiction
          # that had it searching twice.
          # Never on the finalising round: planning the next tool while
          # writing the final answer is the two-jobs-one-round contradiction
          # this stack exists to prevent.
          when=lambda f: _drafting(f) and (bool(f.gaps) and bool(f.suggest))
                         and not f.finalising,
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
          when=lambda f: _drafting(f) and (bool(f.preloaded or f.useful)),
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
          when=lambda f: _drafting(f) and (
              f.finalising or (bool(f.preloaded or f.useful)
                               and not (bool(f.gaps) and bool(f.suggest)))),
          render=lambda f: "[YOUR ROLE — COMMUNICATE] This is the answer the "
                           "user reads. Answer every part they asked, in the "
                           "order they asked it, naming each one. Cite the "
                           "evidence for each claim. Where a part is "
                           "unanswered, say which part and why — do not leave "
                           "the reader to notice the omission.",
          owner="governor"),

    Block("role_critic", Slot.ROLE, rank=5,
          # Replaces the enrichment module's critique pass. It has what that
          # module never had: the evidence the answer was written from, and the
          # record of what was rejected getting there.
          when=lambda f: not _drafting(f),
          render=lambda f: "[YOUR ROLE — CRITIC] Read the answer above against "
                           "the evidence below it. Name any claim the evidence "
                           "does not support, any part of the question it "
                           "quietly skipped, and any place it sounds more "
                           "certain than the evidence allows. If it is sound, "
                           "say so — do not invent a criticism.",
          owner="governor"),

    Block("role_next_steps", Slot.ROLE, rank=6,
          when=lambda f: not _drafting(f),
          render=lambda f: "[YOUR ROLE — NEXT STEPS] Say what would actually "
                           "close what is still open: the specific document, "
                           "payer, or question to go after. Only steps this "
                           "answer's own gaps call for — not generic advice.",
          owner="governor"),

    Block("question", Slot.QUESTION,
          when=lambda f: bool(f.question),
          render=lambda f: f"[THE QUESTION] {f.question}",
          owner="chat"),

    Block("answer", Slot.ANSWER,
          when=lambda f: not _drafting(f),
          render=lambda f: "[THE ANSWER GIVEN — critique this]\n" + f.answer,
          owner="governor"),

    Block("this_round", Slot.TARGET,
          # 🔴 NAME THE QUERY'S MATERIAL, NOT JUST THE GAP.
          #
          # Ananth, 2026-09-12: "when asking to reframe .. it should state the
          # full gap and question with the right payor all the details so that
          # we can use it. it said ask a targeted question, but how".
          #
          # It said "Work this gap and no other: 'Sunshine Health's general
          # care management philosophy'" and left react to invent the rest —
          # which entity, which document, what the last query already returned.
          # An instruction that names a goal without its material is a request
          # to guess, and the guess is what produced a repeat query.
          #
          # So the block carries everything a query needs: the gap, the
          # original question it came from, and what was already set aside so
          # the same source is not asked for twice.
          when=lambda f: bool(f.targeted_gap),
          render=lambda f: (
              f"[THIS ROUND] Work this gap and no other:\n"
              f"  gap:      {f.targeted_gap}\n"
              + (f"  asked:    {f.question}\n" if f.question else "")
              + (f"  avoid:    do not re-retrieve "
                 + "; ".join(f.discarded[:3]) + "\n" if f.discarded else "")
              + "  Write a query that names the specific entity and the thing "
                "being asked about it — not the whole question again, and not "
                "one word from it. If the last query returned the wrong "
                "material, say what you need that it did not give you."),
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
          # 🔴 INTENT, NOT A CHECKBOX.
          #
          # Ananth, 2026-09-12: "the complete piece also feels a bit more like
          # a check box of did we answer all 3 payors without really saying did
          # it meet the users intent".
          #
          # He is right, and the old wording invited it: "are you satisfied
          # with the level of answer AND the evidence" reads as coverage
          # arithmetic, and a model answering it counts parts. Coverage is
          # already measured mechanically by the integrator — asking react for
          # the same number is a second, worse copy of a check we can compute.
          #
          # What we cannot compute is whether the person who asked can ACT on
          # it. The identity block says who they are and that they are about to
          # do something; this asks whether the answer is good enough for that.
          when=lambda f: f.can_complete,
          render=lambda f: (
              "[IS THIS DONE?] Not \"did I cover every part\" — we measure "
              "that ourselves. The question is whether the person who asked "
              "can ACT on this answer.\n"
              "  They are an operator about to do something with it. Could "
              "they do that thing now, or would they still have to go and "
              "look something up?\n"
              "  is_complete=true means: yes, they can act on it.\n"
              "  is_complete=false means: something they need is still "
              "missing — say WHAT, in complete_why, in their terms.\n"
              "  A part nobody looked for is not complete. Neither is a part "
              "answered so vaguely that they would have to check it anyway."),
          owner="governor"),
)


# Ananth: "no more than 2 or 3 roles judge, plan, summarise", then
# "FINAL = SUMMARIZE + COMMUNICATE", then "the round after = next steps +
# critic". SIX roles exist; the cap is per ROUND, and the `when` conditions --
# not a truncation -- are what hold it. Two exclusions do all the work:
#
#   _drafting(f)           splits {judge, plan, summarise, communicate}
#                          from {critic, next_steps}. Nothing spans it.
#   plan needs a tool to
#   suggest; communicate    keeps the drafting side at three.
#   needs nothing left to
#   suggest
#
# A cap enforced by SLICING would silently drop whichever role sorted last, and
# a missing instruction is invisible in the output it fails to produce.
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

    # ── THE THREAD'S STORED EVIDENCE ───────────────────────────────────
    # Ananth: "we also need it to store the facts/things it found useful vs not
    # useful (so that we dont have to send it again)".
    #
    # THIS IS THE READ THAT MAKES THE WRITE WORTH ANYTHING. Without it the
    # ledger is a table nobody opens -- the producer-with-no-consumer defect,
    # with a schema. Stored facts come FIRST because they are already judged:
    # a fact costs ~100 characters to carry where the passage behind it costs
    # ~9,000, and it survives the chunks it came from.
    #
    # Fail-soft: a store that is down must not cost the turn its in-turn
    # memory, so this only ever ADDS to what the rounds already produced.
    # ── WHAT THIS THREAD ALREADY KNOWS ──────────────────────────────────
    # Through the MEMORY MANAGER, never the store directly. Ananth: "YOU NEED
    # A MEMORY MANAGER MODULE TO DO THIS UNLESS THERE IS ALREADY ONE THAT
    # WORKS" -- there was not: ctx._evidence_memory (turn, chunks),
    # persistence/memory.py (worker, session state) and thread_evidence
    # (thread, facts) did not know about each other, and reading the store
    # from here would have made this a fourth.
    #
    # v2/memory.py owns WHICH tier answers and how much rides along; this
    # function only asks. Stored facts lead the USEFUL block because they are
    # already judged and cost ~100 characters where their passages cost ~9,000.
    stored_useful: list[str] = []
    try:
        from app.pipeline.v2 import memory as _v2mem
        _rc = _v2mem.recall(ctx)
        stored_useful = list(_rc.facts)
        for _l in _rc.not_useful:
            if _l and _l not in discarded:
                discarded.append(_l)
        if _rc.unavailable:
            # NOT swallowed. An empty memory and an unreachable one produce the
            # same empty list and carry opposite advice.
            import logging as _lg
            _lg.getLogger(__name__).info(
                "[v2.memory] degraded for this turn: %s", "; ".join(_rc.unavailable))
    except Exception:
        pass

    return Facts(
        question=(getattr(ctx, "message", None) or "").strip(),
        user_name=str(prof.get("display_name") or "") if isinstance(prof, dict) else "",
        user_prefs=tuple(prefs),
        org=str(getattr(ctx, "org_name", "") or ""),
        gaps=tuple((g.gap_id, g.text) for g in getattr(state, "open_gaps", ()) or ()),
        targeted_gap=targeted_gap,
        # Set by react_loop when react has proposed complete and we are taking
        # one more round purely to write the answer.
        finalising=bool(getattr(ctx, "_v2_finalising", False)),
        preloaded=tuple((p.get("tool"), bool(p.get("ok")), str(p.get("summary") or ""))
                        for p in (preloaded or [])),
        suggest=tuple(suggest),
        # Stored facts first, then this turn's own -- the stored ones are
        # already judged and cost a fraction of the passages they replace.
        useful=tuple((stored_useful + useful)[-5:]),
        discarded=tuple(discarded[-3:]),
        can_complete=True,
    )


# ── WHAT THE LIVE FRAME TAKES FROM THIS REGISTRY ────────────────────────────

# frame.py already owns §5 (gaps), §8 (complete), §9 (tools), §10 (preloaded),
# and chat owns §3 (the question). Rendering those from here TOO would put two
# authors on one section -- the defect frame.py's own docstring names. So the
# frame takes exactly the parts nothing else renders:
#
#   ROLE        replaces frame's single §6 role string with the role STACK
#   USEFUL      what react kept, carried across rounds
#   NOT_USEFUL  what it saw and rejected -- which has never been told back to
#               it, so every round is free to re-retrieve and re-read it
FRAME_SLOTS: tuple[Slot, ...] = (Slot.ROLE, Slot.USEFUL, Slot.NOT_USEFUL)


def frame_sections(f: Facts) -> tuple[list[str], tuple[str, ...]]:
    """(lines, rendered_ids) for the slots the live frame delegates here.

    Same `when` conditions and same ordering as assemble() -- this is a
    PROJECTION of the registry, not a second selection with its own rules. A
    role that assemble() would render and this would not is a divergence that
    only shows up in production prompts.
    """
    text, rendered, _ = assemble(f)
    keep = {b.id for b in REGISTRY if b.slot in FRAME_SLOTS}
    ids = tuple(i for i in rendered if i in keep)
    by_id = {b.id: b for b in REGISTRY}
    return [by_id[i].render(f) for i in ids], ids
