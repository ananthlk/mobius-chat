"""The governor's sections of the round frame, and the ack it asks for.

Spec: docs/governor-prompt-frame-v1.md

The governor owns THREE of the frame's ten sections, and they are exactly the
three things react cannot hold for itself:

    §5  OPEN GAPS   the cross-round ledger -- what is open, what was tried
                    against each, and what came back
    §6  ROLE        the role this round is for
    §8  COMPLETE?   the satisfaction question, and a second opinion

Everything else in the frame belongs to another seat (§1/§7 LLM, §2/§3 Chat,
§9 Tool Manifest, §4/§10 react's own) and is NOT rendered here. A section
rendered by two authors is a contradiction waiting for whoever debugs it next
-- the LLM seat made that argument about `closure` and it was right.

THE ACK IS A COMPREHENSION CHECK, NOT A COMPLIANCE CHECK. It proves the
section was read; the checker in compliance.py proves the round acted on it.
Both are recorded, and the DISAGREEMENT is the signal. An ack that has never
once contradicted the trace is measuring nothing and should be deleted.

PURE. State in, text out.
"""

from __future__ import annotations

from app.pipeline.v2 import statements as ST
from app.pipeline.v2.posture import (
    Gap, Posture, latest_closure, targeted_attempts,
)

# Posture -> the role, in react's language rather than the machine's. NOT a
# posture name: "narrow" means nothing to the model, and a label it cannot act
# on is a label that changes nothing.
# EXPLORE has TWO roles and the posture alone cannot tell them apart.
# Directive.DISCOVER means "no part is named yet -- find out what is missing";
# CLOSE means "one part is named -- go get it". Rendering the CLOSE role on a
# DISCOVER round says "find evidence that closes a named open part" directly
# above a statement saying "name each part still missing", which is a
# contradiction the model has to resolve for us. Found by rendering round 2.
DISCOVER_ROLE = ("work out what this question still needs — review what came "
                 "back and name each part that is missing")

ROLE: dict[Posture, str] = {
    Posture.FRAME: "read the question and decide what it is actually asking",
    Posture.EXPLORE: "find evidence that closes a named open part",
    Posture.NARROW: "state what is claimed and what is still open — do not search",
    Posture.ALTERNATIVES: "offer a route to what could not be reached — do not search",
    Posture.VALIDATE: "check what is already claimed against the evidence kept",
    Posture.COMMUNICATE: "write the answer from what is kept — do not search",
}

# Only the acks whose CHECKER exists. An ack with no checker is a producer with
# no consumer wearing a schema, and it reads as evidence while proving nothing.
ACK_KEYS: tuple[tuple[str, str], ...] = (
    ("parts", "the distinct parts of the question you must answer"),
    ("working_gap", "the id of the ONE open part you are working this round, "
                    "or null if you are not searching"),
    ("complete", "true/false — your own call"),
    ("complete_why", "one clause: why"),
    ("dissent", "if a second opinion is given above: 'accepted' or "
                "'declined — <reason>'"),
)

# Asked ONLY when the governor has already decided another round is affordable.
#
# Ananth, 2026-09-12: "the governor knows if a next round is feasible.. so the
# question to llm is .. do you think another round is worth it to improve the
# score.. lets see what it says we dont have to rely on it, just asking may be
# helpful".
#
# The split is the point: FEASIBLE is ours (budget, rounds left) and WORTH IT
# is theirs (would more evidence change the answer). Asking when we cannot
# afford it invites a yes we cannot honour, and teaches the model its answer
# does not matter.
#
# The price is in the question because it is now measured, not guessed: a
# second round costs 3.26x the first in dollars and 2.82x in latency across
# 3,342 production turns. "Is it worth it" with no cost attached is a question
# whose answer is always yes.
NEXT_ROUND_ACK_KEYS: tuple[tuple[str, str], ...] = (
    ("next_round_worth_it", "true/false — would ANOTHER round materially "
                            "improve this answer? A further round costs "
                            "roughly 3x this one. Say false if more searching "
                            "would return the same thing"),
    ("next_round_why", "one clause: what specifically the next round would "
                       "get that this one did not"),
)


def _gap_line(g: Gap, current_round: int) -> list[str]:
    tried = targeted_attempts(g)
    out = [f"  [{g.gap_id}] {g.text}"]
    if not tried:
        # The distinction the whole ledger exists for. "Never searched" and
        # "searched and empty" produce the same sentence in an answer today.
        out.append("      attempts: none — this part has never been searched")
    for a in tried:
        got = "returned evidence" if a.returned_payload else "returned nothing"
        out.append(f"      r{a.round_index} {a.tool} {a.query!r} -> {got}")
    c = latest_closure(g)
    if c is not None and c.value is not None:
        prior = [x for x in g.closure_by[:-1]
                 if x.supported and x.value is not None]
        was = f" (was {prior[-1].value})" if prior else ""
        note = "" if c.supported else "  [not corroborated by evidence]"
        out.append(f"      closure: {c.value}{was}{note}"
                   + (f" — {c.why}" if c.why else ""))
    return out


def preload_sections(executed: list[dict], suggest: tuple[str, ...]) -> list[str]:
    """§10 PRIOR RESULTS and §9 TOOLS, for a round whose evidence already exists.

    `executed` is [{tool, ok, summary}] -- what ran and what came back, INCLUDING
    the ones that returned nothing. A tool that ran and found nothing is
    evidence; omitting it would let react assume it was never tried, which is
    the never-searched / searched-and-empty collapse this whole contract exists
    to end.

    §9 is the ESCAPE HATCH. Without it preload is a capability removal: react
    could previously call anything in the manifest and would now get only what
    we chose.
    """
    out: list[str] = []
    if executed:
        out.append("[§10 ALREADY RETRIEVED — this round's evidence, judge it]")
        for e in executed:
            tool = e.get("tool")
            if e.get("ok"):
                # FOUR PARTS, not a count. Ananth, 2026-09-12: "we need a real
                # good summary from rag.. its role, what it is trying to solve,
                # what it found and the new gap it is trying to close".
                #
                # role   why this tool was offered, in Tool Manifest's words
                # for    the gap this call was spent on
                # asked  the exact query -- so react can see whether a later
                #        query would be the same one, which is the repeat it
                #        has no other way to detect
                # found  documents, pages, and what the ask did not reach
                out.append(f"  {tool} — {e['role']}" if e.get("role")
                           else f"  {tool}")
                if e.get("for"):
                    out.append(f"      for:   {e['for']}")
                if e.get("asked"):
                    out.append(f"      asked: {e['asked']!r}")
                out.append(f"      found: {e.get('summary')}")
            else:
                # Said plainly. "Ran and found nothing" and "was never run" are
                # different facts and carry opposite advice.
                out.append(f"  {tool} -> ran, returned nothing"
                           + (f" ({e['summary']})" if e.get("summary") else ""))
    if suggest:
        out.append("[§9 TOOLS you may request next]")
        out.append("  " + " · ".join(suggest))
        out.append("  Name one in your gap report if you need it; the next "
                   "preload will run it. You are not choosing a tool this "
                   "round.")
    return out


def render(c: ST.Ctx, posture: Posture,
           directive=None, *, preloaded: list[dict] | None = None,
           suggest: tuple[str, ...] = (),
           facts=None,
           next_round_feasible: bool = False,
           numbered_passages: int = 0) -> tuple[str | None, ST.Selection]:
    # NOTE: `preloaded` shadows nothing -- it is the executed-tool list, and
    # its truthiness is what makes this a judgement round.
    """The governor's sections, in execution order, plus the ack request."""
    sel = ST.select(c, posture)
    gaps = c.state.open_gaps
    parts: list[str] = []

    # ── §5 OPEN GAPS ────────────────────────────────────────────────────────
    # §5 is the ledger of PARTS. When the only open gap is the root, there are
    # no parts yet -- the question is already in §3, and repeating it under a
    # "[§5 OPEN PARTS]" heading tells react it has an unsearched part when what
    # it actually has is an unanswered question.
    from app.pipeline.v2.posture import ROOT_GAP_ID as _ROOT
    gaps = tuple(g for g in gaps if g.gap_id != _ROOT)
    if gaps and c.round_index > 1:
        parts.append("[§5 OPEN PARTS — the governor's ledger across rounds]")
        for g in gaps:
            parts.extend(_gap_line(g, c.round_index))

    # ── §6 ROLE ─────────────────────────────────────────────────────────────
    from app.pipeline.v2.posture import Directive as _Dir
    # "No NAMED part yet" is the real condition, and it is the same one EVD-1
    # already selects on. select() can return CLOSE here -- the root gap has an
    # attempt, so by its lights there is something to close -- but the root is
    # the QUESTION, and a round whose only open gap is the question is a round
    # for finding out what the parts are. Deriving the role from the directive
    # ALONE made §6 say "close a named open part" directly above "name each
    # part still missing". Two authors, one round.
    _no_named_part = not gaps          # gaps already has the root filtered out
    # Evidence already in hand -> the round's job is to JUDGE it, and the role
    # must say so. Otherwise §6 reads "find evidence" directly above §10's
    # "here is the evidence", which is the same two-authors contradiction that
    # made §6 fight the review instruction earlier.
    if preloaded:
        role = ("judge what has already been retrieved below: does it answer "
                "the question, and if not, what is missing")
    else:
        role = (DISCOVER_ROLE
                if (directive is _Dir.DISCOVER or _no_named_part)
                else ROLE.get(posture))
    # THE ROLE STACK REPLACES THE SINGLE ROLE STRING, when the turn can supply
    # facts for it (blocks.Facts). One round has more than one job -- judging
    # what came back and writing what it supports are different instructions,
    # and a single §6 line can only ever carry one of them. blocks.py decides
    # WHICH roles this round has from what the round holds; see FRAME_SLOTS
    # there for why only these slots come from it.
    #
    # Falls back to the single string when facts are absent, so a caller that
    # has not been updated still renders a role rather than none.
    _role_lines: list[str] = []
    _memory_lines: list[str] = []
    if facts is not None:
        from app.pipeline.v2 import blocks as _BL
        _lines, _ids = _BL.frame_sections(facts)
        for _i, _ln in zip(_ids, _lines):
            (_role_lines if _i.startswith("role_") else _memory_lines).append(_ln)
    if _role_lines:
        parts.extend(_role_lines)
    elif role:
        parts.append(f"[§6 ROLE this round] {role}")

    # ── §8 COMPLETE? and the rest of the steering ───────────────────────────
    settle = [s for s in sel.statements if s.slot == ST.Slot.SETTLE]
    other = [s for s in sel.statements if s.slot != ST.Slot.SETTLE]
    if settle:
        parts.append("[§8 IS THIS COMPLETE?]")
        parts.extend(f"  - {ST.text_of(s, c)[0]}" for s in settle)
    if other:
        parts.append("[Governor — this round]")
        parts.extend(f"  - {ST.text_of(s, c)[0]}" for s in other)

    # §10 and §9 render LAST in the block but describe work that already
    # happened -- they are the round's inputs, not its instructions, and the
    # model reads instructions first then the evidence they apply to.
    # The cross-round memory: what was kept, and what was rejected getting
    # there. Placed with the evidence sections because that is what they are --
    # inputs describing work already done, not instructions for this round.
    parts.extend(_memory_lines)

    parts.extend(preload_sections(preloaded or [], suggest))

    if not parts:
        return None, sel

    # ── §11 THE V2 RESPONSE SHAPE ───────────────────────────────────────────
    #
    # Ananth, 2026-09-12: "okay lets build that" -- the contract asks for facts
    # with provenance, and until the PROMPT asks for them react keeps returning
    # v1's chunk numbers and the thread ledger stays empty. This is the block
    # that closes that loop.
    #
    # ADDITIVE, NOT A REPLACEMENT. react/prompts.py's response_shape is the LLM
    # seat's and still governs the base object; this asks for two extra keys
    # alongside it. Rewriting their shape from here would be two authors on one
    # object -- the defect this file's own header warns about.
    #
    # WHY FACTS AND NOT CHUNK NUMBERS: a chunk number is positional against the
    # last tool result and means nothing once the chunks are gone. A fact with
    # a document and page survives them, can be checked, and re-sends next turn
    # in ~100 characters where its passage costs ~9,000.
    # 🔴 A COMPLETE OBJECT THAT SUPERSEDES, NOT AN ADDENDUM.
    #
    # Ananth: "why llm seat.. because they own us the right output??" — and he
    # was right to push. I had proposed asking another seat to add facts[] to
    # react/prompts.py's response_shape. That file is SHARED BY BOTH ARMS, so
    # the fix would have started asking v1 for facts too: a prompt change to
    # the control arm, which is the contamination I reverted two hours ago.
    # v2's response shape is v2's, and it belongs here.
    #
    # WHY THE ADDENDUM FORM FAILED. §11 previously said "ALSO RETURN these two
    # keys, alongside your normal JSON" — a footnote to an authoritative
    # schema. Measured across runs of one question with identical code, react
    # returned facts[] sometimes and v1's shape other times. A model handed a
    # complete object and a footnote emits the complete object; that is a
    # prompt-design flaw of mine, not react disobeying.
    #
    # So this states the WHOLE object and says plainly that it replaces the
    # earlier one. It is the same object plus two keys -- never a different
    # contract -- because a second, genuinely different shape would be two
    # authors of one response.
    parts.append("[YOUR RESPONSE — this object REPLACES the JSON shape "
                 "described earlier in this prompt]")
    _n_keys = "THREE ADDITIONAL KEYS" if numbered_passages else "TWO ADDITIONAL KEYS"
    parts.append(f"  It is that same object with {_n_keys}. Keep "
                 "every field you were already returning; add these.")
    parts.append('  "facts": [{"fact": "<one thing you now know, in one '
                 'sentence>", "document": "<the document it came from>", '
                 '"page": <page number>}]')
    parts.append("     Only what THIS round's evidence supports. A fact with "
                 "no document is DROPPED — we cannot check it later, so it "
                 "must not be remembered as if we could.")
    # 🔴 kept SITS WITH facts, NOT AFTER not_useful.
    #
    # Measured live, cid 09cf4edf: react returned `kept` on ONE of six rounds
    # while the ask rendered in every posture. Same failure this block already
    # records for facts[] -- a key listed last in a sequence gets dropped, and
    # the fix that worked there was making it part of the object rather than an
    # addendum to it.
    #
    # So kept is stated as a PROPERTY OF THE FACTS ABOVE: every fact came from
    # a numbered passage, and this is those numbers. That makes it derivable
    # from work react has already done rather than a separate chore at the end
    # of a long block.
    #
    # ASKED ONLY WHEN THERE IS A LIST TO ADDRESS. Asking with nothing numbered
    # tells the model to address something it cannot see, and a model asked for
    # indices WILL produce some -- they parse as valid ints and resolve against
    # the wrong thing.
    if numbered_passages:
        parts.append(f'  "kept": [<the numbers of the passages those facts '
                     f'came from>]')
        parts.append(f"     The passages you were given are numbered [1] to "
                     f"[{numbered_passages}]. Every fact above came from one "
                     "of them — list those numbers. Nothing else: not what you "
                     "skimmed, not what you rejected.")
        parts.append(f"     ONLY numbers between 1 and {numbered_passages}. If "
                     "you are reading something that is not in that numbered "
                     "list, it does not belong here.")
        parts.append("     Just the numbers, e.g. [2, 5]. An empty list is a "
                     "real answer and means no numbered passage earned a fact.")
    parts.append('  "not_useful": ["<document or document p<page> you read '
                 'and are NOT using>"]')
    parts.append("     What you looked at and rejected. Recorded so no later "
                 "round retrieves or re-reads it — this is the only way that "
                 "knowledge survives the turn.")
    _missing = "facts[] and kept[]" if numbered_passages else "facts[]"
    parts.append(f"  Returning the earlier shape WITHOUT {_missing} means this "
                 "turn learns nothing: the evidence dies with the round and "
                 "the next round starts blind.")

    # ── the ack ─────────────────────────────────────────────────────────────
    parts.append("[ACK — return these in your JSON as \"ack\": {...}]")
    _ack_keys = ACK_KEYS + (NEXT_ROUND_ACK_KEYS if next_round_feasible else ())
    parts.extend(f"  {k}: {desc}" for k, desc in _ack_keys)
    parts.append("  Each is checked against what this round actually does. "
                 "A claim here that the round contradicts is worse than "
                 "leaving it out — say what you are doing, not what looks right.")

    return "\n".join(parts), sel
