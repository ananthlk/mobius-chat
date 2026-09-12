"""One vocabulary for everything v2 emits, so the stream reads as a system.

Ananth, 2026-09-12: "please have tons of emits everywhere just like your system
where i can see through what is happening.. finally rag is emitting enough i
want you to emit everything .. the llm is a black box so i want to know
everything about what was happening".

THE RULE: emit the DECISION and its INPUTS, never just the outcome. "15
passages" is an outcome and tells you nothing about why; "asked rag for the
gap, got 15 across 3 docs, kept 13 after the per-arm cap, UHC's single chunk
survived" is a decision you can argue with.

SECOND RULE: say what did NOT happen. A silent skip and a step that ran and
found nothing produce the same silence, and they carry opposite advice. Every
helper here has a form for the empty case.

THIRD RULE: never emit a raw payload. Chunk text, prompts and answers are
already in the prompt or the answer; repeating them in the stream doubles the
cost of the thing being traced. Emit counts, names, sizes and reasons.

The marks match what react and rag already use, so one stream stays readable:
  ◌ starting   ✓ succeeded   ⊘ nothing/blocked   ↓ degraded   → decided
  🧠 memory    ✗ set aside   ? unknown
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Step:
    """ONE STEP: a headline that stands alone, and detail that expands.

    Ananth, 2026-09-12: "a top line but an expandable into everything
    underneath it" -- said after seeing a flat wall of emits. A stream where
    every step prints eight lines is not visibility; it is the same opacity
    with more scrolling.

    THE HEADLINE MUST STAND ALONE. If a reader has to expand it to know
    whether anything is wrong, it is a label, not a headline: put the number
    that would change their mind in it.
    """
    stage: str
    headline: str
    detail: tuple[str, ...] = ()
    data: dict = field(default_factory=dict)


def emit_step(emitter, correlation_id: str, step: Step, *, round=None,
              thread_id=None) -> None:
    """Send one step as a collapsible signal, with a text fallback.

    The FE renders `note` + expandable `data.detail` (same shape as
    retrieval_trace). A plain emitter that only takes strings still gets the
    headline -- a trace legible in one client and invisible in another gets
    debugged in neither.
    """
    if emitter is None:
        return
    try:
        from app.communication.emit_envelope import make_v2_trace
        emitter(make_v2_trace(correlation_id, stage=step.stage,
                              headline=step.headline,
                              detail=list(step.detail), data=step.data,
                              round=round, thread_id=thread_id))
    except Exception:
        try:
            emitter(step.headline)
        except Exception:
            pass


def memory_step(rc) -> Step:
    """HEADLINE CARRIES THE NUMBERS that change a reader's mind: how much we
    already know, and whether the store answered at all."""
    if rc.unavailable:
        head = "🧠 memory: UNAVAILABLE — " + rc.unavailable[0]
    elif rc.facts or rc.not_useful:
        head = (f"🧠 memory: {len(rc.facts)} fact(s) known, "
                f"{len(rc.not_useful)} source(s) already rejected"
                + (f", {rc.turn_chunks} chunk(s) held this turn"
                   if rc.turn_chunks else ""))
    else:
        # A NEW THREAD IS NOT A FAILURE, and must not read as one.
        head = "🧠 memory: new thread — nothing known yet"
    return Step("memory", head, tuple(memory(rc)),
                {"facts": len(rc.facts), "not_useful": len(rc.not_useful),
                 "turn_chunks": rc.turn_chunks,
                 "unavailable": list(rc.unavailable)})


def preload_step(execute, suggest, excluded) -> Step:
    head = (f"◌ preload: running {', '.join(execute)}" if execute
            else "⊘ preload: nothing to run — react searches blind this round")
    if suggest:
        head += f" (+{len(suggest)} offered to react)"
    return Step("preload", head, tuple(preload_plan(execute, suggest, excluded)),
                {"execute": list(execute), "suggest": list(suggest),
                 "excluded": [list(x) for x in (excluded or ())]})


def fair_share_step(tool, report, kept, total) -> Step:
    starved = [a for a, r in (report or {}).items() if r["kept"] == 0]
    if starved:
        # THE HEADLINE SAYS THE BAD NEWS. A starved arm means an entity will be
        # missing from the answer, and that cannot be one expand away.
        head = (f"↓ {tool}: kept {kept}/{total} — {len(starved)} fan-out arm(s) "
                f"got NOTHING: {', '.join(starved)}")
    else:
        head = f"✂ {tool}: kept {kept}/{total} passages across {len(report or {})} arm(s)"
    return Step("trim", head, tuple(fair_share(tool, report, kept, total)),
                {"kept": kept, "total": total,
                 "arms": {a: r for a, r in (report or {}).items()},
                 "starved": starved})


def preload_done_step(results, elapsed_s=None) -> Step:
    """What preload actually RETURNED — past tense, once.

    🔴 THIS REPLACES A SECOND "Pre-loading evidence before reasoning" LINE that
    fired AFTER execution in the present tense. With the intent headline
    already emitted before the tools ran, and rag's own chatter in between, the
    stream showed what looked like rag running TWICE. Ananth read it exactly
    that way and asked why. One call, two reports, and the second one phrased
    as if it were starting.

    Intent and result are both worth emitting -- a 12-second preload with no
    line looks frozen -- but they must be tensed so nobody has to count.
    """
    ok = [r for r in (results or []) if r.get("ok")]
    empty = [r for r in (results or []) if not r.get("ok")]
    took = f" in {elapsed_s:.1f}s" if elapsed_s is not None else ""
    if not results:
        head = "⊘ preload returned nothing — react starts with no evidence"
    elif ok:
        head = (f"✓ preloaded{took}: "
                + " · ".join(str(r.get("tool")) for r in ok)
                + (f" ({len(empty)} returned nothing)" if empty else ""))
    else:
        head = f"⊘ preload{took}: every tool ran and returned nothing"
    detail = [f"  ✓ {r.get('tool')} → {r.get('summary')}" for r in ok]
    # "Ran and found nothing" is a DIFFERENT fact from "was never run", and the
    # user is owed the distinction for the same reason react is.
    detail += [f"  ⊘ {r.get('tool')} → ran, returned nothing" for r in empty]
    if results:
        detail.append("  → Judging what came back before searching again.")
    return Step("preload_done", head, tuple(detail),
                {"ok": [r.get("tool") for r in ok],
                 "empty": [r.get("tool") for r in empty],
                 "elapsed_s": elapsed_s})


def reply_step(resp, elapsed_s=None) -> Step:
    took = f" in {elapsed_s:.1f}s" if elapsed_s is not None else ""
    ungrounded = sum(1 for f in resp.facts if not f.grounded)
    if resp.problems:
        head = (f"⚠ react replied{took}: shape={resp.shape_seen}, "
                f"{len(resp.problems)} problem(s) — {resp.problems[0][:80]}")
    else:
        head = (f"✓ react replied{took}: {len(resp.facts)} fact(s)"
                + (f" ({ungrounded} UNSOURCED)" if ungrounded else "")
                + f", {len(resp.gaps)} gap(s)"
                + (f", complete={str(resp.is_complete).lower()}"
                   if resp.is_complete is not None else ", complete not stated"))
    return Step("react", head, tuple(llm_reply(resp, elapsed_s)),
                {"shape_seen": resp.shape_seen, "facts": len(resp.facts),
                 "ungrounded": ungrounded, "gaps": len(resp.gaps),
                 "is_complete": resp.is_complete,
                 "next_round_worth_it": resp.next_round_worth_it,
                 "problems": list(resp.problems)})


def remembered_step(stored, refused, not_useful) -> Step:
    if not (stored or refused or not_useful):
        head = "⊘ nothing learned this round worth remembering"
    else:
        head = (f"💾 remembered {stored} fact(s), {not_useful} rejection(s)"
                + (f" — REFUSED {refused} unsourced" if refused else ""))
    return Step("memory_write", head,
                tuple(remembered(stored, refused, not_useful)),
                {"stored": stored, "refused": refused, "not_useful": not_useful})


def memory(rc) -> list[str]:
    """What this turn starts from. A cold thread and a broken store both have
    no facts and mean opposite things, so they read differently here."""
    out = []
    if rc.unavailable:
        out.append("  ↓ memory: " + "; ".join(rc.unavailable))
    if rc.facts:
        out.append(f"  🧠 memory: {len(rc.facts)} fact(s) already known — "
                   "re-sent as text, not re-retrieved")
        for f in rc.facts[:4]:
            out.append(f"       • {f[:150]}")
        if len(rc.facts) > 4:
            out.append(f"       … and {len(rc.facts) - 4} more")
    if rc.not_useful:
        out.append(f"  ✗ memory: {len(rc.not_useful)} source(s) already "
                   f"rejected — not retrieving again: "
                   + "; ".join(rc.not_useful[:3]))
    if rc.turn_chunks:
        out.append(f"  🧠 memory: {rc.turn_chunks} chunk(s) held this turn "
                   "(reachable via recall_evidence; not re-sent)")
    if not out:
        out.append("  🧠 memory: nothing known about this thread yet "
                   "(new thread — not an error)")
    return out


def preload_plan(execute, suggest, excluded) -> list[str]:
    out = [f"  ◌ preload: running {', '.join(execute) if execute else '(nothing)'}"]
    if suggest:
        out.append(f"       offering react next: {' · '.join(suggest)}")
    # WHY a tool was not run. Silently absent tools read as "not useful here",
    # which is a judgement nobody made.
    for key, why in (excluded or ())[:4]:
        out.append(f"       ⊘ {key}: {why}")
    return out


def fair_share(tool, report, kept, total) -> list[str]:
    """Per arm, always. "kept 13 of 15" cannot tell a trimmed arm from an arm
    that returned nothing, and it is the second that loses a payer."""
    if not report:
        return []
    out = [f"  ✂ {tool}: kept {kept} of {total} passages, per fan-out arm:"]
    for arm, r in report.items():
        mark = "✓" if r["kept"] == r["had"] else ("⊘" if r["kept"] == 0 else "✂")
        out.append(f"       {mark} {arm}: {r['kept']}/{r['had']}")
    starved = [a for a, r in report.items() if r["kept"] == 0]
    if starved:
        out.append(f"       ↓ starved by the budget: {', '.join(starved)} — "
                   "these entities will be missing from the answer")
    return out


def frame(rendered, skipped, chars) -> list[str]:
    """What the model is about to be told, and what it is NOT being told.

    The skipped list is the half nobody logs and the half that explains a
    strange answer: a round that never received the gap ledger cannot work a
    gap, and looks like disobedience.
    """
    roles = [r.replace("role_", "") for r in rendered if r.startswith("role_")]
    out = [f"  🧭 frame: {len(rendered)} block(s), {chars:,} chars"
           + (f" · roles: {' · '.join(roles)}" if roles else " · NO ROLE")]
    if skipped:
        out.append(f"       not sent: {', '.join(skipped[:8])}")
    return out


def llm_call(model, system_chars, user_chars, evidence_chars) -> list[str]:
    """The black box's inputs, measured. Ananth: "the llm is a black box so i
    want to know everything about what was happening"."""
    return [f"  🤖 asking {model}: {system_chars:,} system + {user_chars:,} user"
            f" chars ({evidence_chars:,} of it evidence)"]


def llm_reply(resp, elapsed_s=None) -> list[str]:
    """What came back, as claims — before anything acts on them."""
    took = f" in {elapsed_s:.1f}s" if elapsed_s is not None else ""
    out = [f"  ✓ replied{took}: shape={resp.shape_seen}"
           f" · {len(resp.facts)} fact(s)"
           f" · {len(resp.gaps)} gap(s)"
           + (f" · wants {resp.tool_request}" if resp.tool_request else "")]
    if resp.is_complete is not None:
        why = f" — {resp.complete_why}" if resp.complete_why else ""
        out.append(f"       complete={str(resp.is_complete).lower()}{why}")
    else:
        out.append("       complete: not stated")
    if resp.next_round_worth_it is not None:
        why = f" — {resp.next_round_why}" if resp.next_round_why else ""
        out.append(f"       another round worth it? "
                   f"{str(resp.next_round_worth_it).lower()}{why}"
                   "   (asked, not obeyed)")
    for f in resp.facts[:3]:
        src = f"[{f.document} p{f.page}]" if f.page is not None else f"[{f.document}]"
        out.append(f"       • {f.fact[:120]} {src if f.document else '⚠ NO SOURCE'}")
    # PROBLEMS ARE THE POINT. A round whose response could not be read is
    # exactly the round worth finding, and it must not scroll past silently.
    for p in resp.problems[:4]:
        out.append(f"       ⚠ {p}")
    return out


def remembered(stored, refused, not_useful) -> list[str]:
    out = []
    if stored:
        out.append(f"  💾 remembered {stored} fact(s) for this thread — "
                   "next turn re-sends them as text instead of retrieving")
    if refused:
        out.append(f"  ⚠ refused {refused} fact(s) with no source — "
                   "unverifiable later, so not stored")
    if not_useful:
        out.append(f"  ✗ recorded {not_useful} source(s) as not useful")
    if not out:
        out.append("  ⊘ nothing learned this round worth remembering")
    return out
