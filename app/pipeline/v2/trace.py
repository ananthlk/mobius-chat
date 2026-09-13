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

import logging

from dataclasses import dataclass, field


# ── ONE DETAIL STRUCTURE, USED BY EVERY STEP ────────────────────────────────
#
# Ananth, 2026-09-12: "your structure on detail is inconsistent".
#
# It was: some steps emitted "  ✓ tool → summary", some "  stage:  react_1",
# some "       • fact [doc p12]". Three shapes in one stream means a reader
# re-learns the layout at every step, which is the cost the headline/expand
# split was meant to remove.
#
# Two forms, and nothing else:
#   kv(label, value)   a named field of this step
#   item(text, mark)   one of a list under the field above it
#
# And every step names WHERE ITS FACTS CAME FROM. Ananth: "i want to know that
# the thing was from tools manifest". A trace that shows a decision without its
# author cannot be argued with — you cannot tell our judgement from a peer's.
_LABEL_W = 12


logger = logging.getLogger(__name__)


def kv(label: str, value) -> str:
    return f"  {label + ':':<{_LABEL_W}} {value}"


def item(text, mark: str = "•") -> str:
    return f"    {mark} {text}"


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
    # WHOSE FACTS THESE ARE. Rendered as the first detail line so every step
    # can be attributed without reading the code that produced it.
    source: str = ""
    # ── PROGRESSIVE DISCLOSURE ──────────────────────────────────────────
    # Ananth, 2026-09-12: "i want for instance preload to be shown and when
    # done collapse to a line like you do".
    #
    # A step is emitted TWICE with the SAME `key`: once as `running` (open,
    # so the reader watches a 27-second preload happen) and once as `done`
    # (collapsed to its headline). The renderer keys on `key` and REPLACES.
    #
    # This is also the structural fix for the defect Ananth caught earlier —
    # "rag was run twice before we called react". It was one call reported by
    # two separate steps, the second phrased as if starting. One step with two
    # states cannot produce that reading: there is only ever one row for it.
    key: str = ""
    state: str = "done"          # running | done


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
        detail = list(step.detail)
        if step.source:
            detail = [kv("from", step.source)] + detail
        emitter(make_v2_trace(correlation_id, stage=step.stage,
                              headline=step.headline,
                              detail=detail,
                              data={**step.data, "source": step.source,
                                    # The renderer keys on this pair: same key
                                    # = same row, replaced in place.
                                    "key": step.key or step.stage,
                                    "state": step.state},
                              round=round, thread_id=thread_id))
    except Exception as e:
        # 🔴 THE FALLBACK MUST NOT BE SILENT.
        #
        # Measured on dev, cid 2a2556c3: every v2 step reached the user as a
        # bare string on the legacy path ([thinking:legacy]) and NOT one
        # v2_trace signal was published. The stages all ran. The structured
        # emit raised, this except caught it, the headline still went out --
        # so the trace looked present, the FE had nothing to expand, and the
        # exception that explained it was discarded here.
        #
        # A degraded emit is a real outcome and is worth keeping; a degraded
        # emit nobody can see the cause of is how a broken trace survives a
        # green deploy. Log WHICH stage and WHY, once per step.
        logger.warning(
            "[v2.trace] structured emit FAILED stage=%s key=%s cid=%s -- fell "
            "back to a bare-string headline, so this step carries no "
            "expandable detail: %r",
            step.stage, step.key or step.stage,
            (correlation_id or "")[:8], e)
        try:
            emitter(step.headline)
        except Exception as e2:
            logger.warning("[v2.trace] even the text fallback failed "
                           "stage=%s: %r", step.stage, e2)


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
    det = []
    for u in rc.unavailable:
        det.append(kv("degraded", u))
    det.append(kv("facts", f"{len(rc.facts)} known — re-sent as text, not re-retrieved"))
    for f in rc.facts[:4]:
        det.append(item(str(f)[:150]))
    det.append(kv("rejected", f"{len(rc.not_useful)} source(s) — not retrieving again"))
    for n in rc.not_useful[:3]:
        det.append(item(str(n)[:110], "✗"))
    det.append(kv("this turn", f"{rc.turn_chunks} chunk(s) held (reachable via "
                               "recall_evidence; not re-sent)"))
    return Step("memory", head, tuple(det),
                {"facts": len(rc.facts), "not_useful": len(rc.not_useful),
                 "turn_chunks": rc.turn_chunks,
                 "unavailable": list(rc.unavailable)},
                source="governor memory (thread_evidence)")


def preload_step(execute, suggest, excluded) -> Step:
    head = (f"◌ preload: running {', '.join(execute)}" if execute
            else "⊘ preload: nothing to run — react searches blind this round")
    if suggest:
        head += f" (+{len(suggest)} offered to react)"
    det = [kv("running", ", ".join(execute) or "(nothing)"),
           kv("offering", " · ".join(suggest) or "(none)")]
    if excluded:
        det.append(kv("not run", f"{len(excluded)} tool(s), each with a reason"))
        for key, why in (excluded or ())[:6]:
            det.append(item(f"{key}: {why}", "⊘"))
    return Step("preload", head, tuple(det),
                key="preload", state="running",
                data={"execute": list(execute), "suggest": list(suggest),
                      "excluded": [list(x) for x in (excluded or ())]},
                source="Tool Manifest — estimate() ranked these and named the "
                       "call (ToolOffer.inputs); reasons are theirs verbatim")


def fair_share_step(tool, report, kept, total) -> Step:
    starved = [a for a, r in (report or {}).items() if r["kept"] == 0]
    if starved:
        # THE HEADLINE SAYS THE BAD NEWS. A starved arm means an entity will be
        # missing from the answer, and that cannot be one expand away.
        head = (f"↓ {tool}: kept {kept}/{total} — {len(starved)} fan-out arm(s) "
                f"got NOTHING: {', '.join(starved)}")
    else:
        head = f"✂ {tool}: kept {kept}/{total} passages across {len(report or {})} arm(s)"
    det = [kv("kept", f"{kept} of {total} passages")]
    for arm, r in (report or {}).items():
        mark = "✓" if r["kept"] == r["had"] else ("⊘" if r["kept"] == 0 else "✂")
        det.append(item(f"{arm}: {r['kept']}/{r['had']}", mark))
    if starved:
        det.append(kv("starved", ", ".join(starved)
                      + " — these entities will be missing from the answer"))
    return Step("trim", head, tuple(det),
                {"kept": kept, "total": total,
                 "arms": {a: r for a, r in (report or {}).items()},
                 "starved": starved},
                source="governor per-arm budget, over rag's own fan-out slots")


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
    det = []
    for r in ok:
        det.append(item(f"{r.get('tool')} → {r.get('summary')}", "✓"))
    for r in empty:
        det.append(item(f"{r.get('tool')} → ran, returned nothing", "⊘"))
    if results:
        det.append(kv("next", "judging what came back before searching again"))
    return Step("preload", head, tuple(det),
                key="preload", state="done",
                data={"ok": [r.get("tool") for r in ok],
                      "empty": [r.get("tool") for r in empty],
                      "elapsed_s": elapsed_s},
                source="the tools themselves, executed by the governor")


def prompt_step(*, blocks, skipped=(), statements=(), dropped=(),
                chars=0, round_index=None) -> Step:
    """WHICH PROMPT BLOCKS WERE LOADED, IN WHICH ORDER.

    Ananth, 2026-09-12: "i want to see a emit from prompt to say what actual
    blocks of prompt was loaded in which order".

    ORDER IS THE POINT, not just membership. This module has already shipped
    two ordering defects that were invisible in output: three blocks sharing a
    slot with an alphabetical tiebreak put "tools you may request" BEFORE "work
    this gap and no other", and the four roles sorted by id would have printed
    COMMUNICATE first, before the judging it depends on. Neither shows up in an
    answer; both show up here.

    SKIPPED IS SHOWN TOO. "We did not know this" and "we chose not to say it"
    are different, and a prompt that cannot say what it omitted cannot be
    debugged from its output.
    """
    det = [kv("order", " → ".join(blocks) if blocks else "(no blocks)")]
    for i, b in enumerate(blocks, 1):
        det.append(item(f"{i}. {b}"))
    if skipped:
        det.append(kv("not sent", ", ".join(skipped)))
    if statements:
        det.append(kv("statements", ", ".join(statements)))
    if dropped:
        # A statement the selector dropped is a thing react was NOT told, and
        # it is invisible everywhere else.
        det.append(kv("dropped", ", ".join(dropped) + " — selected then cut"))
    if chars:
        det.append(kv("size", f"{chars:,} chars of governor block"))
    roles = [b.replace("role_", "") for b in blocks if b.startswith("role_")]
    head = (f"🧭 prompt: {len(blocks)} block(s)"
            + (f" · roles in order: {' → '.join(roles)}" if roles
               else " · NO ROLE")
            + (f" · {len(skipped)} not sent" if skipped else ""))
    return Step("prompt", head, tuple(det),
                {"blocks": list(blocks), "skipped": list(skipped),
                 "statements": list(statements), "dropped": list(dropped),
                 "chars": chars},
                source="governor — v2/blocks.py (roles, memory) + "
                       "v2/frame.py (§5 gaps, §8 complete, §9 tools, "
                       "§10 evidence, §11 response shape)")


def invoke_step(*, stage, system_chars, user_chars, evidence_chars,
                max_tokens, reasoning_depth=None, latency_budget_ms=None,
                composition_id=None, composition_hash=None,
                round_index=None, roles=()) -> Step:
    """HOW REACT WAS INVOKED — everything decided BEFORE the model answers.

    Ananth, 2026-09-12: "i want to know how react was invoked.. a whole series
    of which model etc."

    The model is NOT here: llm_manager's roster and bandit choose it inside the
    call, so naming one now would be a guess. It is reported in reply_step from
    ctx.usages, which records what actually answered. A predicted model printed
    as fact is how a trace starts lying.
    """
    det = [
        f"  stage:      {stage}" + (f"  (round {round_index})" if round_index else ""),
        f"  prompt:     {system_chars:,} system + {user_chars:,} user chars"
        + (f"  ({evidence_chars:,} of it evidence)" if evidence_chars else ""),
        f"  max output: {max_tokens:,} tokens (INCLUDES thinking — a ceiling "
        f"sized to the visible reply truncates)",
    ]
    if roles:
        det.append("  asked for:  " + " · ".join(r.replace("role_", "") for r in roles))
    if reasoning_depth:
        det.append(f"  depth:      {reasoning_depth}")
    if latency_budget_ms:
        det.append(f"  latency budget: {latency_budget_ms}ms")
    if composition_id or composition_hash:
        det.append(f"  prompt block: id={composition_id} hash={str(composition_hash)[:12]}")
    head = (f"🤖 asking react ({stage}): {system_chars + user_chars:,} chars in, "
            f"up to {max_tokens:,} out")
    return Step("invoke", head, tuple(det),
                key=f"react_{round_index or ''}", state="running",
                source="governor — prompt assembled here; the MODEL is chosen "
                       "inside the call by the roster/bandit",
                data={"stage": stage, "system_chars": system_chars,
                 "user_chars": user_chars, "evidence_chars": evidence_chars,
                 "max_tokens": max_tokens, "reasoning_depth": reasoning_depth,
                 "latency_budget_ms": latency_budget_ms,
                 "composition_id": composition_id})


def shared_step(resp, usage=None, elapsed_s=None, round_index=None) -> Step:
    """EVERYTHING REACT SHARED — except the expanded answer.

    Ananth: "i want to know what react shared everything except the expanded
    answer".

    The expanded answer is the deliverable and is rendered as the answer;
    repeating it here would double the longest thing in the stream. Everything
    else react said is its REASONING, and that is what a reader needs to judge
    the answer by.
    """
    model = (usage or {}).get("model") or "?"
    provider = (usage or {}).get("provider") or "?"
    intok = (usage or {}).get("input_tokens")
    outtok = (usage or {}).get("output_tokens")
    took = f" in {elapsed_s:.1f}s" if elapsed_s is not None else ""

    det = [f"  answered by: {provider}/{model}"
           + (f"  ({intok:,} in / {outtok:,} out tokens)"
              if isinstance(intok, int) and isinstance(outtok, int) else "")]
    if resp.thought:
        det.append(f"  thought:    {resp.thought[:300]}")
    if resp.roles_assumed:
        det.append("  roles it took: " + " · ".join(resp.roles_assumed))
    if resp.running_answer:
        # The SUMMARY, not the expanded answer -- this is what changed.
        det.append(f"  running answer: {resp.running_answer[:400]}")
    for f in resp.facts:
        src = (f"[{f.document} p{f.page}]" if f.page is not None
               else f"[{f.document}]") if f.document else "⚠ NO SOURCE"
        det.append(f"  fact:       {f.fact[:150]} {src}")
    for g in resp.gaps:
        det.append(f"  gap:        [{g.status}] {g.text[:120]}")
    if resp.not_useful:
        det.append("  set aside:  " + "; ".join(resp.not_useful[:4]))
    if resp.tool_request:
        det.append(f"  wants next: {resp.tool_request}"
                   + (f" — {resp.tool_reason[:120]}" if resp.tool_reason else ""))
    det.append(f"  complete:   {'not stated' if resp.is_complete is None else str(resp.is_complete).lower()}"
               + (f" — {resp.complete_why[:140]}" if resp.complete_why else ""))
    if resp.next_round_worth_it is not None:
        det.append(f"  another round worth it? {str(resp.next_round_worth_it).lower()}"
                   + (f" — {resp.next_round_why[:120]}" if resp.next_round_why else "")
                   + "   (asked, not obeyed)")
    det.append(f"  shape:      {resp.shape_seen}")
    for p in resp.problems:
        det.append(f"  ⚠ {p}")
    # NOT INCLUDED, DELIBERATELY: resp.answer. It is the deliverable and is
    # rendered as the answer; repeating the longest thing in the turn here
    # would bury the reasoning this step exists to show.

    ungrounded = sum(1 for f in resp.facts if not f.grounded)
    if resp.problems:
        head = (f"⚠ react{took} via {model}: shape={resp.shape_seen}, "
                f"{len(resp.problems)} problem(s) — {resp.problems[0][:70]}")
    else:
        head = (f"✓ react{took} via {model}: {len(resp.facts)} fact(s)"
                + (f" ({ungrounded} UNSOURCED)" if ungrounded else "")
                + f", {len(resp.gaps)} gap(s)"
                + (f", complete={str(resp.is_complete).lower()}"
                   if resp.is_complete is not None else ", complete not stated"))
    return Step("react", head, tuple(det),
                key=f"react_{round_index or ''}", state="done",
                source=f"react (the model: {provider}/{model})",
                data={"model": model, "provider": provider,
                 "input_tokens": intok, "output_tokens": outtok,
                 "shape_seen": resp.shape_seen, "facts": len(resp.facts),
                 "ungrounded": ungrounded, "gaps": len(resp.gaps),
                 "is_complete": resp.is_complete,
                 "next_round_worth_it": resp.next_round_worth_it,
                 "problems": list(resp.problems)})


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
    det = [kv("stored", f"{stored} fact(s) — next turn re-sends them as text "
                        "instead of retrieving"),
           kv("refused", f"{refused} fact(s) with no source — unverifiable later"),
           kv("rejected", f"{not_useful} source(s) recorded as not useful")]
    return Step("memory_write", head, tuple(det),
                {"stored": stored, "refused": refused, "not_useful": not_useful},
                source="governor memory (thread_evidence)")


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
