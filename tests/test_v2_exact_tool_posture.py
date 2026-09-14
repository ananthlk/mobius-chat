"""When Tool Manifest finds the exact tool and skips rag, deliver in one round.

Ananth, 2026-09-13: "when tool_manifest finds the perfect tool and skips rag,
then we are likely with the answer,, so we should suggest confirm >>
communicate >> plan.. emphasising more on communicate so that we can just get
done with one plan and with communicate means extended answer space exists".

The cost of not having this, measured: payor_fact answers the Sunshine
timely-filing question in 360ms from the certified fact store, and the same
turn took three rounds and 31.5s because nothing told react the answer was
already in front of it.
"""
from app.pipeline.v2 import prompts
from app.pipeline.v2.blocks import MAX_ROLES, Facts, assemble

PRELOADED = (("payor_fact", True, "180 days participating / 365 non-par"),)


def roles_for(f):
    _, rendered, _ = assemble(f)
    return [i for i in rendered if i.startswith("role_")]


def _exact(**kw):
    base = dict(question="timely filing deadline for Sunshine Health",
                preloaded=PRELOADED, exact_tool=True, answer="")
    base.update(kw)
    return Facts(**base)


def test_confirm_and_communicate_in_the_same_round():
    """The whole point: deliver now, do not spend a round re-deciding."""
    ids = roles_for(_exact())
    assert "role_confirm" in ids
    assert "role_communicate" in ids


def test_judge_steps_aside_so_the_round_has_one_job():
    """judge and confirm both key off preloaded. Both rendering would tell
    react to search-and-judge AND confirm-and-deliver in one round."""
    assert "role_judge" not in roles_for(_exact())


def test_without_the_signal_it_is_still_judge():
    """exact_tool is Tool Manifest's claim, not our optimism. Absent it, the
    ordinary posture is unchanged."""
    ids = roles_for(_exact(exact_tool=False))
    assert "role_judge" in ids and "role_confirm" not in ids


def test_a_suppression_whose_tool_returned_NOTHING_is_not_exact():
    """🔴 THE DANGEROUS CASE. rag skipped and the covering tool came back
    empty is the WORST outcome, not the best -- asking react to confirm an
    answer it does not have is could-not-check rendered as checked."""
    from app.pipeline.v2 import blocks

    class _Ctx:
        _v2_rag_suppressed = True
        merged_state = {}
        message = "q"
    empty = [{"tool": "payor_fact", "ok": False, "payload": ""}]
    f = blocks.facts_from(_Ctx(), None, preloaded=empty, suggest=())
    assert f.exact_tool is False


def test_the_cap_still_holds():
    for f in (_exact(), _exact(gaps=(("S1", "what about non-par"),)),
              _exact(exact_tool=False)):
        assert len(roles_for(f)) <= MAX_ROLES, roles_for(f)


def test_communicate_anywhere_opens_the_extended_answer_space():
    """"with communicate means extended answer space exists" -- keyed on the
    ROLE rendering, not on the round being the finalising one."""
    class _C:
        orchestrator_version = "v2"
        _v2_round_communicates = True
    assert "THAT CAP DOES NOT APPLY THIS ROUND" in prompts.system_suffix(_C())


def test_the_signal_has_a_PRODUCER():
    """🔴 CAUGHT BY A MUTATION CHECK THAT REFUSED TO FAIL.

    Every test above sets exact_tool (or _v2_rag_suppressed) itself, so all
    six passed with the producer in react_loop deliberately broken. A field
    can be perfectly consumed and never written — which is the defect class
    this module keeps finding elsewhere, reproduced here in the change written
    to avoid it.

    Asserts the wiring exists AND derives from Tool Manifest's field, so
    `= False`, `= True` or a constant fails.
    """
    import ast
    import pathlib

    src = pathlib.Path("app/pipeline/react_loop.py").read_text()
    tree = ast.parse(src)
    writes = [n for n in ast.walk(tree)
              if isinstance(n, ast.Assign)
              for t in n.targets
              if isinstance(t, ast.Attribute) and t.attr == "_v2_rag_suppressed"]
    assert writes, "_v2_rag_suppressed is read by blocks.facts_from and never written"
    assert any("rag_needed" in ast.dump(n.value) for n in writes), (
        "_v2_rag_suppressed is assigned from something other than the offer's "
        "rag_needed — the posture would fire on our own optimism rather than "
        "on Tool Manifest's claim")


def test_communicate_renders_even_with_a_gap_open():
    """🔴 THE ROUND THAT HAD THE ANSWER WAS TOLD TO PLAN INSTEAD.

    Measured, cid 21db20e1 on rev 01079-7f6: payor_fact preloaded (ok=1), and
    round 1 rendered confirm,plan,summarise — NOT communicate — because
    role_communicate required `not bool(f.gaps)` and a gap was open. So the
    round holding a 360ms authoritative answer was told to judge and plan, and
    the turn still took three rounds and 42s.

    An open gap is not a reason to withhold an authoritative answer. It is a
    reason to say what is still open ALONGSIDE it — which plan does, in the
    same round.
    """
    ids = roles_for(_exact(gaps=(("S1", "what about non-par"),)))
    assert "role_communicate" in ids, (
        "the exact-tool round cannot deliver — this is the 3-round turn")
    assert "role_plan" in ids, "what is still open must still be named"
    assert len(ids) <= MAX_ROLES, ids


def test_summarise_steps_aside_so_communicate_fits():
    """Three slots. summarise and communicate are the same job when one
    authoritative tool answered, and rendering both is what pushed communicate
    out of the round."""
    ids = roles_for(_exact(gaps=(("S1", "g"),)))
    assert "role_summarise" not in ids
    assert "role_confirm" in ids and "role_communicate" in ids


def test_communicate_needs_EVIDENCE_not_the_exact_tool_signal():
    """REWRITTEN 2026-09-14. This asserted that without the exact-tool signal
    an open gap still blocked communicate -- proving the relaxation was scoped.
    That premise is gone: gaps no longer block communicate in EITHER case,
    because open gaps are something to communicate about (Ananth: raise
    MAX_ROLES to 4; FINAL = SUMMARIZE + COMMUNICATE).

    So the scoping question is obsolete, but the real boundary is not: what
    communicate needs is something to SAY. A round with no evidence and no
    finalising signal still must not write the answer the user reads.
    """
    # open gap, no exact-tool signal -- communicates anyway, on its evidence
    assert "role_communicate" in roles_for(
        _exact(exact_tool=False, gaps=(("S1", "g"),)))
    # and the exact-tool signal is not what did it
    assert "role_communicate" in roles_for(
        _exact(exact_tool=True, gaps=(("S1", "g"),)))


def test_a_round_with_nothing_to_say_does_not_communicate():
    """The boundary that survives: evidence or finalising. Neither means there
    is no answer to write, and a COMMUNICATE instruction there would ask react
    to address the person from nothing."""
    import dataclasses

    from app.pipeline.v2.blocks import Facts, frame_sections

    bare = dataclasses.replace(Facts(), question="q", answer="")
    roles = [n for n in frame_sections(bare)[1] if n.startswith("role_")]
    assert "role_communicate" not in roles, roles
