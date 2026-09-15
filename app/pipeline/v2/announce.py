"""Every major step says what it decided AND WHY.

Ananth, 2026-09-15: "we want every major step emitting their think.. every
major decision (e.g. rag, tools, prompts, llm) explaining their decision.. we
need every module kind of announcing so that we know how this runs."

WHAT WAS WRONG. The loop emitted OUTCOMES as bare strings:

    preloaded 3 tool(s)
    round 2: search_corpus
    governor: EXPLORE — gap worth buying

Each is true and none is followable. "preloaded 3" does not say which three,
what else was offered, or why those three won. "round 2: search_corpus" does
not say which gap it is aimed at. The reader cannot tell a good decision from
a bad one, which means they cannot catch a bad one — and that is the entire
purpose of a trace.

THE RULE HERE: a step names the INPUTS it saw, the CHOICE it made, and the
REASON, in that order. The reason is not decoration. A decision published
without it cannot be argued with, and an unarguable trace gets read once.

Bare strings also could not carry structure, so everything rendered flat.
These build `trace.Step`, which the FE renders as a headline with expandable
detail — and which the loop was already importing for nothing.
"""

from __future__ import annotations

from app.pipeline.v2.trace import Step, item, kv

#: Attribution. trace.Step renders this as the first detail line, so a reader
#: can tell OUR judgement from a peer's without reading the code that produced
#: it. Ananth: "i want to know that the thing was from tools manifest".
SRC_TOOLREG = "tools manifest (toolreg.estimate)"
SRC_GOVERNOR = "v2 governor (posture.select)"
SRC_PROMPT = "v2 prompt build"
SRC_MODEL = "llm manager"
SRC_LOOP = "v2 loop"


def _clip(text, n: int = 100) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _prompt_source(source) -> str:
    """Prompt provenance in one line.

    `prompt_source` is a DICT (composition id, hash, posture, failed). Rendered
    with str() it dumped a 64-char hash into the trace and buried the one fact
    that matters: whether this came from the prompt DB or the in-code fallback.
    A turn silently running the fallback is how a prompt change appears to have
    no effect, so that fact gets its own words.
    """
    if not isinstance(source, dict):
        return str(source or "—")
    where = source.get("source") or "?"
    bits = [where]
    if source.get("composition_id") is not None:
        bits.append(f"composition {source['composition_id']}")
    blk = source.get("posture_block")
    if blk:
        bits.append(f"posture block {blk}")
    if source.get("failed"):
        bits.append("⚠ FELL BACK — the prompt DB did not answer")
    return " · ".join(bits)


# ── TOOLS: selection ────────────────────────────────────────────────────────

def tools_selected(*, offered, chosen, excluded=(), suggest=(), floor=False) -> Step:
    """What the manifest offered, what we run, and what we passed over.

    `excluded` is the field that makes this followable. A list of what ran
    cannot distinguish "never offered" from "offered and rejected" — the same
    two states the FRAME prompt asks the model to separate. The trace should
    hold itself to what it asks of the model.
    """
    detail = [kv("offered", f"{len(offered)} tool(s)")]
    detail += [item(t) for t in offered]
    if chosen:
        detail.append(kv("running", f"{len(chosen)} now"))
        detail += [item(t) for t in chosen]
    if suggest:
        detail.append(kv("held back", "offered, not run this round"))
        detail += [item(t) for t in suggest]
    if excluded:
        detail.append(kv("excluded", "ruled out by policy"))
        detail += [item(f"{t} — {why}") for t, why in excluded]
    if floor:
        detail.append(kv("why rag", "the manifest returned nothing, and rag is "
                                    "the floor — a turn with no retrieval is "
                                    "worse than one with unranked retrieval"))
    return Step(
        stage="tools",
        headline=(f"{len(chosen)} of {len(offered)} offered tools running"
                  if not floor else "no tools offered — falling back to rag"),
        detail=tuple(detail), source=SRC_TOOLREG,
        key="v2.preload", state="done",
        data={"offered": list(offered), "chosen": list(chosen)},
    )


def tools_running(query: str) -> Step:
    """The open half of the preload row. Emitted with the SAME key as
    tools_selected, so the renderer replaces it rather than stacking a second
    row — the collapse Ananth asked for, and the structural reason a single
    call cannot be read as two."""
    return Step(stage="tools", headline="asking the manifest what to run…",
                detail=(kv("for", _clip(query, 80)),),
                source=SRC_TOOLREG, key="v2.preload", state="running")


def tool_call(*, round: int, tool: str, inputs, closing=None) -> Step:
    """One tool call, aimed at a named gap.

    `closing` is why this call exists. Without it a tool row says the loop did
    something, not that it did the right thing.
    """
    detail = [kv("tool", tool)]
    if inputs:
        detail += [item(f"{k} = {_clip(v, 70)}") for k, v in inputs.items()]
    detail.append(kv("to close", _clip(closing) if closing
                     else "not stated by the model — it named a tool without a gap"))
    return Step(stage="tools", headline=f"round {round}: {tool}",
                detail=tuple(detail), source=SRC_LOOP,
                key=f"v2.tool.{round}.{tool}", state="running",
                data={"round": round, "tool": tool})


def tool_result(*, round: int, tool: str, ok: bool, outcome: str,
                sources: int = 0, chars: int = 0) -> Step:
    """Collapsed result. `outcome` is one of the five shared words — never a
    sentence, and never "not found": no tool outcome licenses a world claim."""
    return Step(stage="tools",
                headline=f"round {round}: {tool} — {outcome}"
                         + (f", {sources} source(s)" if sources else ""),
                detail=(kv("outcome", outcome), kv("evidence", f"{chars} chars"),
                        kv("sources", sources)),
                source=SRC_LOOP, key=f"v2.tool.{round}.{tool}", state="done",
                data={"round": round, "tool": tool, "outcome": outcome,
                      "success": ok})


# ── GOVERNOR: the posture decision ──────────────────────────────────────────

def posture(*, round: int, decision, action, spent_s=None, budget_s=None,
            rounds_left=None, gaps_open=(), has_answer=False,
            targeting=None) -> Step:
    """THE decision this loop exists to make.

    Every input that moved it is named, because a posture published without
    its inputs is indistinguishable from a coin flip — and for four days I
    could not tell whether ours was one.
    """
    detail = [
        kv("posture", getattr(decision.posture, "value", decision.posture)),
        kv("because", _clip(getattr(decision, "because", ""), 160)),
        kv("branch", getattr(decision, "branch", "") or "—"),
    ]
    # THE GAP IN WORDS, not its id. `because` reads "closing Sba95e6", which
    # is a handle for correlating logs and tells a reader nothing about
    # whether the round is aimed at the right thing.
    if targeting:
        detail.append(kv("targeting", _clip(targeting, 140)))
    if spent_s is not None and budget_s:
        detail.append(kv("clock", f"{spent_s:.0f}s of {budget_s:.0f}s promised"))
    if rounds_left is not None:
        detail.append(kv("rounds left", rounds_left))
    detail.append(kv("answer so far", "written" if has_answer else "none yet"))
    if gaps_open:
        detail.append(kv("gaps open", len(gaps_open)))
        detail += [item(_clip(g, 90)) for g in gaps_open]
    if action is not None and not getattr(action, "continues", True):
        detail.append(kv("stopping", _clip(getattr(action, "because", ""), 160)))
    return Step(stage="governor",
                headline=f"round {round}: "
                         f"{getattr(decision.posture, 'value', decision.posture)}"
                         f" — {_clip(targeting or getattr(decision, 'because', ''), 70)}",
                detail=tuple(detail), source=SRC_GOVERNOR,
                key=f"v2.governor.{round}", state="done",
                data={"round": round,
                      "posture": getattr(decision.posture, "value",
                                         str(decision.posture))})


# ── PROMPT: what was assembled ──────────────────────────────────────────────

def prompt_built(*, round: int, posture, source: str, system_chars: int,
                 user_chars: int, evidence_tools=()) -> Step:
    """Which prompt the model is about to read, and where it came from.

    `source` matters more than its size: it distinguishes the prompt DB from
    the in-code fallback, and a turn silently running the fallback is how a
    prompt change appears to have no effect.
    """
    return Step(stage="prompt",
                headline=f"round {round}: {getattr(posture, 'value', posture)} "
                         f"prompt ({system_chars + user_chars:,} chars)",
                detail=(kv("posture block", getattr(posture, "value", posture)),
                        kv("built by", _prompt_source(source)),
                        kv("system", f"{system_chars:,} chars"),
                        kv("evidence", f"{user_chars:,} chars"),
                        kv("tools in hand", ", ".join(evidence_tools) or "none")),
                source=SRC_PROMPT, key=f"v2.prompt.{round}", state="done",
                data={"round": round, "prompt_source": source})


# ── LLM + parse ─────────────────────────────────────────────────────────────

def model_replied(*, round: int, elapsed_s=None, proposes_complete=False,
                  tool=None, tools=(), gaps_closed=(), gaps_open=(),
                  answer_chars=0) -> Step:
    """What the model said, read as a DECISION rather than as text."""
    detail = [kv("elapsed", f"{elapsed_s:.1f}s" if elapsed_s else "—"),
              kv("answer", f"{answer_chars:,} chars" if answer_chars else "none")]
    if proposes_complete:
        detail.append(kv("proposes", "complete — the governor still decides"))
    queued = list(tools) or ([tool] if tool else [])
    if queued:
        detail.append(kv("asks next", ", ".join(queued)))
        if len(queued) > 1:
            detail.append(item("named together, so they run in ONE round"))
    if gaps_closed:
        detail.append(kv("closed", len(gaps_closed)))
        detail += [item(_clip(g, 90)) for g in gaps_closed]
    if gaps_open:
        detail.append(kv("still open", len(gaps_open)))
        detail += [item(_clip(g, 90)) for g in gaps_open]
    return Step(stage="llm",
                headline=f"round {round}: model "
                         + ("proposes complete" if proposes_complete
                            else f"asks for {', '.join(queued)}" if queued
                            else "returned nothing usable"),
                detail=tuple(detail), source=SRC_MODEL,
                key=f"v2.llm.{round}", state="done",
                data={"round": round, "proposes_complete": proposes_complete})


def dropped(*, tools) -> Step:
    """Work the model asked for that the turn ended before running.

    Silently dropping a request is the defect this fleet spent the week
    removing in every other form.
    """
    return Step(stage="tools",
                headline=f"{len(tools)} requested tool(s) never ran — "
                         "the turn ended first",
                detail=tuple([kv("why", "the governor did not fund another "
                                        "round; a tool already requested does "
                                        "not become affordable")]
                             + [item(t) for t in tools]),
                source=SRC_LOOP, key="v2.dropped", state="done",
                data={"dropped": list(tools)})


def exited(*, stopped_by: str, exit_mode, rounds: int, spent_s=None,
           budget_s=None, answer_chars=0) -> Step:
    """The turn's own account of how it ended."""
    return Step(stage="exit",
                headline=f"turn ended after {rounds} round(s): {stopped_by}",
                detail=(kv("stopped by", stopped_by),
                        kv("mode", getattr(exit_mode, "value", exit_mode) or "—"),
                        kv("clock", f"{spent_s:.0f}s of {budget_s:.0f}s"
                           if spent_s is not None and budget_s else "—"),
                        kv("answer", f"{answer_chars:,} chars" if answer_chars
                           else "NONE — publishing a failure")),
                source=SRC_LOOP, key="v2.exit", state="done",
                data={"stopped_by": stopped_by, "rounds": rounds})
