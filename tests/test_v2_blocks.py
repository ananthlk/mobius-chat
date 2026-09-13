"""The modular round prompt: blocks assembled from what we know.

Ananth, 2026-09-12:
  "tool identity : Mobius ; user identity and preferences : Role identity :
   Judge (existing answers, identify gaps) and Plan (identify best tools) ...
   we also need a role as a summarizer. this is critical ... no more than 2 or
   3 roles judge, plan, summarise"
"""
from app.pipeline.v2.blocks import MAX_ROLES, Facts, REGISTRY, Slot, assemble

Q = "What is the care management philosophy of United Healthcare in FL?"


def _roles(ids):
    return [i for i in ids if i.startswith("role_")]


# ── a block renders only when its fact exists ───────────────────────────────

def test_absent_facts_render_no_block():
    """An empty section asserts there is nothing there, which react cannot
    tell apart from a section nobody filled in."""
    _, rendered, skipped = assemble(Facts(question=Q))
    assert "user_identity" in skipped, "rendered a user block with no user"
    assert "preloaded" in skipped and "not_useful" in skipped
    assert "question" in rendered


def test_skipped_blocks_are_returned_not_discarded():
    """"We did not know this" and "we chose not to say it" are different, and
    a prompt that cannot say what it omitted cannot be debugged from output."""
    _, rendered, skipped = assemble(Facts())
    assert skipped, "assemble() reports nothing about what it left out"
    assert set(rendered) & {"tool_identity"}


# ── the three roles ─────────────────────────────────────────────────────────

def test_never_more_than_three_roles():
    f = Facts(question=Q, user_name="G", gaps=(("S1", "a gap"),),
              targeted_gap="a gap", preloaded=(("rag", True, "15 passages"),),
              suggest=("web_scrape",), useful=("doc p1",), discarded=("other.pdf",))
    _, rendered, _ = assemble(f)
    assert len(_roles(rendered)) <= MAX_ROLES == 3


def test_round_one_judges_and_summarises_but_does_not_plan():
    """PLAN with nothing to plan for asks react to choose a tool for nothing --
    the round-1 contradiction that had it searching again after preload."""
    _, rendered, _ = assemble(Facts(question=Q, preloaded=(("rag", True, "6 passages"),)))
    assert "role_plan" not in rendered
    # Preload that already answers IS allowed to deliver -- that is the whole
    # point of preloading ("what is the timely filing limit" needs no round 2).
    assert _roles(rendered) == ["role_judge", "role_summarise", "role_communicate"]


def test_plan_requires_a_gap_but_not_a_tool_to_suggest():
    """SUPERSEDES test_plan_requires_both_a_gap_and_a_tool_to_suggest.

    The gap half still holds: planning with nothing to plan FOR asks react to
    choose a tool for nothing. The SUGGEST half was wrong and cost a round.

    Measured, cid c1b560c6: gaps=1 and suggest empty (every offered tool was
    excluded for unmet preconditions), so plan did not render — on exactly the
    turn where "nothing offered to me covers this" is the single most useful
    thing react could say. An empty tool list is a FINDING to report, not a
    reason to stop asking for the report.
    """
    tools_only = assemble(Facts(question=Q, suggest=("web_scrape",)))[1]
    assert "role_plan" not in tools_only, "planning with no gap plans for nothing"

    gaps_only = assemble(Facts(question=Q, gaps=(("S1", "a gap"),)))[1]
    assert "role_plan" in gaps_only, "a gap nothing covers still needs planning"

    both = assemble(Facts(question=Q, gaps=(("S1", "a gap"),),
                          suggest=("web_scrape",)))[1]
    assert "role_plan" in both


def test_summarise_is_the_role_that_produces_the_deliverable():
    """Judge and Plan can both succeed and leave nothing written: one names
    gaps, the other names tools, and neither answers the question."""
    _, rendered, _ = assemble(Facts(question=Q, useful=("doc p5",)))
    assert "role_summarise" in rendered and "role_judge" not in rendered


def test_no_evidence_means_no_summarise():
    """Asking react to write an answer from nothing is how a confident,
    ungrounded answer gets produced."""
    _, rendered, _ = assemble(Facts(question=Q))
    assert "role_summarise" not in rendered


def test_a_round_with_evidence_always_has_a_role():
    """Material with no role is a prompt that hands react evidence and never
    says what to do with it."""
    for f in (Facts(question=Q, preloaded=(("rag", True, "x"),)),
              Facts(question=Q, useful=("doc p1",))):
        _, rendered, _ = assemble(f)
        assert _roles(rendered), "evidence rendered with no role"


# ── the memory of rejection ─────────────────────────────────────────────────

def test_rejected_documents_are_named_not_counted():
    """"20 passages were read and not kept" is an instruction react cannot
    follow -- it never learns which 20."""
    f = Facts(question=Q, discarded=("Exhibit_II-A_MMA_Program.pdf (1 passage(s), none useful)",))
    txt, rendered, _ = assemble(f)
    assert "not_useful" in rendered
    assert "Exhibit_II-A_MMA_Program.pdf" in txt


def test_ordering_is_explicit_not_alphabetical():
    """Three blocks once shared one slot and the tiebreak was alphabetical, so
    "tools you may request" rendered BEFORE "work this gap and no other".
    Prompt order decided by variable naming is an accident, not an ordering."""
    slots = {b.id: b.slot for b in REGISTRY}
    assert slots["preloaded"] < slots["this_round"] < slots["suggest"]
    assert slots["useful"] < slots["not_useful"] < slots["complete"]
    assert slots["role_judge"] == Slot.ROLE


def test_every_block_declares_an_owner():
    """Wording ownership is the thing that decides who may edit it without a
    deploy. A block with no owner is prose nobody is responsible for."""
    for b in REGISTRY:
        assert b.owner in {"governor", "llm_seat", "chat"}, (b.id, b.owner)


# ── COMMUNICATE: the role that addresses the asker, not the evidence ────────
# Ananth, 2026-09-12: "FINAL = SUMMARIZE + COMMUNICATE".

def test_final_round_is_summarise_plus_communicate():
    f = Facts(question=Q, useful=("FL_manual.pdf p5",))
    _, rendered, _ = assemble(f)
    assert _roles(rendered) == ["role_summarise", "role_communicate"]


def test_a_round_still_choosing_tools_does_not_communicate():
    """COMMUNICATE is the answer the user reads. A round whose job is to pick
    the next tool has not finished looking, and an answer written there reads
    as final while the gap is still open."""
    f = Facts(question=Q, preloaded=(("rag", True, "6 passages"),),
              gaps=(("S1", "UHC not covered"),), suggest=("web_scrape",))
    assert "role_communicate" not in _roles(assemble(f)[1])


def test_plan_and_communicate_are_mutually_exclusive_by_construction():
    """This, not a truncation, is what keeps any round at or under the cap.
    A cap enforced by slicing would silently drop whichever role sorted last."""
    for f in (Facts(question=Q, preloaded=(("rag", True, "x"),),
                    gaps=(("S1", "g"),), suggest=("web_scrape",)),
              Facts(question=Q, useful=("d p1",)),
              Facts(question=Q, preloaded=(("rag", True, "x"),)),
              Facts(question=Q, gaps=(("S1", "g"),), useful=("d p1",))):
        r = _roles(assemble(f)[1])
        assert not ("role_plan" in r and "role_communicate" in r), r
        assert len(r) <= MAX_ROLES, r


def test_communicate_needs_evidence_like_summarise():
    assert "role_communicate" not in assemble(Facts(question=Q))[1]


def test_roles_render_in_the_order_the_work_runs():
    """Sorted by id, COMMUNICATE would print FIRST -- before the judging it
    depends on -- because "c" precedes "j". Prompt order decided by a name is
    the accident this module already documents one slot up."""
    f = Facts(question=Q, preloaded=(("rag", True, "x"),), useful=("d p1",))
    r = _roles(assemble(f)[1])
    assert r == ["role_judge", "role_summarise", "role_communicate"]
    txt = assemble(f)[0]
    assert txt.index("— JUDGE") < txt.index("— SUMMARISE") < txt.index("— COMMUNICATE")


# ── the critic round: the enricher, as a role ───────────────────────────────
# Ananth, 2026-09-12: "the round after = next steps + critic .. this allows us
# to bypass the integrate enricher .. its just another loop"

def _draft():
    return Facts(question=Q, preloaded=(("rag", True, "17 passages"),),
                 useful=("Molina_manual.pdf p5",),
                 discarded=("Exhibit_II-A.pdf (none useful)",))


def test_an_answer_turns_the_next_round_into_critic_plus_next_steps():
    f = Facts(**{**_draft().__dict__, "answer": "Molina emphasises whole-person care."})
    assert _roles(assemble(f)[1]) == ["role_critic", "role_next_steps"]


def test_the_critic_round_carries_the_answer_and_the_evidence_it_came_from():
    """The enrichment module critiqued a finished answer WITHOUT the evidence
    behind it, so it could only judge tone. As a role it holds both."""
    f = Facts(**{**_draft().__dict__, "answer": "Molina emphasises whole-person care."})
    txt, rendered, _ = assemble(f)
    assert "answer" in rendered and "preloaded" in rendered and "useful" in rendered
    assert "Molina emphasises whole-person care." in txt
    assert txt.index("[THE QUESTION]") < txt.index("[THE ANSWER GIVEN") \
           < txt.index("[ALREADY RETRIEVED")


def test_no_round_both_drafts_and_critiques():
    """Asking one round to write the answer and review it is how a model
    rubber-stamps its own text -- and it is also what would breach the cap."""
    drafting = {"role_judge", "role_plan", "role_summarise", "role_communicate"}
    critiquing = {"role_critic", "role_next_steps"}
    for ans in ("", "some answer"):
        for f in (Facts(question=Q, answer=ans, preloaded=(("rag", True, "x"),)),
                  Facts(question=Q, answer=ans, useful=("d p1",)),
                  Facts(question=Q, answer=ans, gaps=(("S1", "g"),),
                        suggest=("web_scrape",), preloaded=(("rag", True, "x"),))):
            r = set(_roles(assemble(f)[1]))
            assert not (r & drafting and r & critiquing), (ans, r)
            assert len(r) <= MAX_ROLES, (ans, r)


def test_whitespace_is_not_an_answer():
    """`answer` is the round selector; a blank string that is not falsy would
    put the loop into critic mode with nothing to critique."""
    f = Facts(**{**_draft().__dict__, "answer": "   \n  "})
    assert "role_critic" not in _roles(assemble(f)[1])


# ── WIRED: frame.render must actually consume this registry ─────────────────

def _ctx_for(round_index=2):
    from app.pipeline.v2 import statements as ST
    from app.pipeline.v2.posture import Budget, Gap, RoundState
    g = Gap(gap_id="S1", text="UHC care management philosophy", opened_round=1)
    st = RoundState(round_index=round_index, open_gaps=(g,),
                    gaps_open_history=(1,),
                    budget=Budget(remaining_s=30.0, remaining_c=3.0, band_s=25.0),
                    next_round_cost_s=10.4, acting_cost_s=10.0,
                    validate_cost_s=9.6, question=Q)
    return ST.Ctx(state=st, round_index=round_index, max_rounds=6,
                  tier="normal", model_proposes_complete=False, kept=0,
                  gap_status="", preloaded=True, extensions_used=0, gap=g), st


def test_frame_renders_the_role_stack_not_its_own_single_role():
    """The consumer check. "[YOUR ROLE — JUDGE]" exists ONLY in blocks.py, so
    frame emitting it proves the delegation happened -- unlike asserting the
    text is non-empty, which frame's own §6 line would satisfy."""
    from app.pipeline.v2 import frame as FR
    from app.pipeline.v2.posture import Posture
    c, _ = _ctx_for()
    f = Facts(question=Q, preloaded=(("rag", True, "17 passages"),),
              useful=("Molina_manual.pdf p5",),
              discarded=("Exhibit_II-A.pdf (none useful)",))
    txt, _ = FR.render(c, Posture.EXPLORE, preloaded=[{"tool": "rag", "ok": True,
                       "summary": "17 passages"}], facts=f)
    assert "[YOUR ROLE — JUDGE]" in txt
    assert "[§6 ROLE this round]" not in txt, "frame kept its own role line too"


def test_frame_carries_the_rejected_documents():
    """The whole point of `discarded`: nothing has ever told react what it
    already looked at and threw away, so each round re-retrieves it."""
    from app.pipeline.v2 import frame as FR
    from app.pipeline.v2.posture import Posture
    c, _ = _ctx_for()
    f = Facts(question=Q, preloaded=(("rag", True, "x"),),
              discarded=("Exhibit_II-A_MMA.pdf (1 passage, none useful)",))
    txt, _ = FR.render(c, Posture.EXPLORE,
                       preloaded=[{"tool": "rag", "ok": True, "summary": "x"}],
                       facts=f)
    assert "Exhibit_II-A_MMA.pdf" in txt
    assert "REJECTED" in txt


def test_frame_without_facts_still_renders_a_role():
    """A caller not yet passing facts must not lose §6 entirely -- that would
    be a round with evidence and no instruction."""
    from app.pipeline.v2 import frame as FR
    from app.pipeline.v2.posture import Posture
    c, _ = _ctx_for()
    txt, _ = FR.render(c, Posture.EXPLORE,
                       preloaded=[{"tool": "rag", "ok": True, "summary": "x"}])
    assert "[§6 ROLE this round]" in txt


def test_frame_sections_cannot_diverge_from_assemble():
    """frame_sections is a PROJECTION of assemble, not a second selection. A
    role assemble renders and the frame does not would only surface in a live
    prompt."""
    for f in (Facts(question=Q, preloaded=(("rag", True, "x"),)),
              Facts(question=Q, useful=("d p1",)),
              Facts(question=Q, answer="an answer", useful=("d p1",)),
              Facts(question=Q, gaps=(("S1", "g"),), suggest=("web_scrape",),
                    preloaded=(("rag", True, "x"),))):
        from app.pipeline.v2.blocks import frame_sections
        want = [i for i in assemble(f)[1] if i.startswith("role_")]
        got = [i for i in frame_sections(f)[1] if i.startswith("role_")]
        assert want == got, (want, got)


# ── the reframe instruction must carry its material ────────────────────────

def test_the_target_block_names_the_query_s_material_not_just_the_gap():
    """Ananth, 2026-09-12: "when asking to reframe .. it should state the full
    gap and question with the right payor all the details so that we can use
    it. it said ask a targeted question, but how".

    It said "Work this gap and no other: '<gap>'" and left react to invent the
    rest — which entity, what the last query already returned. An instruction
    that names a goal without its material is a request to guess, and the guess
    is what produced a repeat query."""
    f = Facts(question="care management philosophy for Molina, Sunshine, UHC",
              targeted_gap="Sunshine Health's general care management philosophy",
              discarded=("SH-PRO-BH-PSR.pdf p5",),
              preloaded=(("rag", True, "15"),))
    txt, _, _ = assemble(f)
    blk = txt[txt.index("[THIS ROUND]"):]
    assert "Sunshine Health's general care management philosophy" in blk
    assert "care management philosophy for Molina" in blk, "the question is missing"
    assert "SH-PRO-BH-PSR.pdf p5" in blk, "already-rejected source not named"
    assert "names the specific entity" in blk


def test_the_target_block_degrades_when_there_is_less_to_say():
    """A first round has no rejections and may have no question text. The block
    must not render empty labels — an empty field asserts there is nothing
    there, which react cannot tell from a field nobody filled."""
    f = Facts(targeted_gap="UHC philosophy", preloaded=(("rag", True, "1"),))
    blk = assemble(f)[0]
    blk = blk[blk.index("[THIS ROUND]"):]
    assert "avoid:" not in blk and "asked:" not in blk
    assert "UHC philosophy" in blk


# ── the finalising round exists to WRITE THE ANSWER ────────────────────────

def test_a_finalising_round_communicates_and_does_not_plan():
    """Ananth, 2026-09-12: "if the answer is complete then the next round
    should have communicate with the extended answer.. i think this is
    missing".

    It was. Measured: react said complete=true on a round whose roles were
    judge · plan · summarise — COMMUNICATE was in the NOT-SENT list. The answer
    the user reads was written by a round asked to summarise the evidence,
    never to answer the person. Those are different jobs, which is the whole
    reason the roles are separate."""
    kw = dict(question=Q, preloaded=(("rag", True, "15"),),
              gaps=(("S1", "g"),), suggest=("rag",))
    normal = _roles(assemble(Facts(**kw))[1])
    final = _roles(assemble(Facts(**kw, finalising=True))[1])
    assert "role_plan" in normal and "role_communicate" not in normal
    assert "role_communicate" in final
    # Planning the next tool while writing the final answer is the two-jobs
    # contradiction this stack exists to prevent.
    assert "role_plan" not in final
    assert len(final) <= MAX_ROLES


def test_the_finalising_flag_is_cleared_after_the_round_that_uses_it():
    """🔴 MEASURED LIVE. _v2_finalising was set and never cleared, so EVERY
    round after the communicate round also rendered communicate with PLAN
    suppressed — and when react reconsidered and said NOT complete, it could no
    longer choose a tool. Rounds 2 and 3 both came back
    judge → summarise → communicate.

    Same shape as _v2_proposed_complete in the same block, which carries the
    same comment for the same reason.

    ORDER MATTERS AND IS ASSERTED AS PROGRAM, NOT PROSE. This test used to
    slice 700 characters before the clear and look for the words "FIRES ONCE".
    Adding an eleven-line comment above the clear pushed the phrase out of the
    window and turned it red while the invariant was untouched -- a test
    reading its own author's prose inside a char count. The positions below
    are facts about what executes.
    """
    src = open("app/pipeline/react_loop.py").read()
    set_at = src.index("ctx._v2_finalising = True")
    clear_at = src.index("ctx._v2_finalising = False")
    assert clear_at != set_at, "the flag is set but never cleared"

    # Cleared once per round, where the facts are built -- not at loop exit.
    facts_at = src.index("_v2bl.facts_from(")
    assert facts_at < clear_at, (
        "the clear moved above facts_from: the round that should communicate "
        "would no longer see the flag it is gated on")

    # AND the capture for the prompt is taken while the flag is still true.
    # system_suffix() runs ~390 lines below the clear, so a prompt gate
    # reading _v2_finalising directly fires never.
    capture_at = src.index("ctx._v2_round_communicates = bool(")
    suffix_at = src.index("_v2pr.system_suffix(ctx)")
    assert capture_at < clear_at < suffix_at, (
        "the communicate capture must be taken before the flag is cleared "
        "and read after it")


# ── the finalising round's composition, per Ananth ─────────────────────────

def test_a_clean_finalising_round_is_communicate_then_validate():
    """Ananth, 2026-09-12: "if complete = true then round 2 is not judge it is
    communicate > validate"."""
    kw = dict(question=Q, preloaded=(("rag", True, "15"),),
              gaps=(("S1", "g"),), suggest=("rag",))
    r = _roles(assemble(Facts(**kw, finalising=True))[1])
    assert r == ["role_communicate", "role_validate"]


def test_findings_turn_it_into_incorporate_then_communicate():
    """"if there was critic errors then incorporate >> communicate" — and
    incorporate comes FIRST: an answer written and corrected after is two
    answers, and the reader gets whichever one the renderer picked."""
    kw = dict(question=Q, preloaded=(("rag", True, "15"),),
              gaps=(("S1", "g"),), suggest=("rag",))
    r = _roles(assemble(Facts(**kw, finalising=True,
                              critic_findings=('"optimal outcomes" unsupported',)))[1])
    assert r == ["role_incorporate", "role_communicate"]


def test_judging_is_not_repeated_on_the_finalising_round():
    """The earlier rounds judged. Asking the finalising round to judge again
    invites it to re-open a question it just closed — measured live: a
    communicate round that also judged came back complete=false, 0 facts."""
    kw = dict(question=Q, preloaded=(("rag", True, "15"),))
    assert "role_judge" not in _roles(assemble(Facts(**kw, finalising=True))[1])
    assert "role_judge" in _roles(assemble(Facts(**kw))[1])


def test_the_findings_are_named_in_the_prompt_not_just_counted():
    """"fix the claims" without saying WHICH is an instruction react cannot
    follow — the same defect as telling it 20 passages were rejected."""
    txt = assemble(Facts(question=Q, preloaded=(("rag", True, "x"),),
                         finalising=True,
                         critic_findings=('"optimal outcomes" is not in the evidence',
                                          "UHC goals unsupported")))[0]
    assert '"optimal outcomes" is not in the evidence' in txt
    assert "UHC goals unsupported" in txt


def test_incorporate_forbids_silent_deletion():
    """Dropping an unverified claim without saying so leaves the reader with a
    shorter answer and no idea something was removed."""
    txt = assemble(Facts(question=Q, preloaded=(("rag", True, "x"),),
                         finalising=True, critic_findings=("x",)))[0]
    assert "do not silently delete" in txt


# ── the finalising round's composition, per Ananth ─────────────────────────

def test_a_clean_finalising_round_is_communicate_then_validate():
    """Ananth, 2026-09-12: "if complete = true then round 2 is not judge it is
    communicate > validate"."""
    kw = dict(question=Q, preloaded=(("rag", True, "15"),),
              gaps=(("S1", "g"),), suggest=("rag",))
    assert _roles(assemble(Facts(**kw, finalising=True))[1]) == [
        "role_communicate", "role_validate"]


def test_findings_turn_it_into_incorporate_then_communicate():
    """"if there was critic errors then incorporate >> communicate" — and
    incorporate comes FIRST: an answer written and corrected after is two
    answers, and the reader gets whichever the renderer picked."""
    kw = dict(question=Q, preloaded=(("rag", True, "15"),),
              gaps=(("S1", "g"),), suggest=("rag",))
    assert _roles(assemble(Facts(**kw, finalising=True,
                                 critic_findings=("x unsupported",)))[1]) == [
        "role_incorporate", "role_communicate"]


def test_judging_is_not_repeated_on_the_finalising_round():
    """Measured live: a communicate round that ALSO judged came back
    complete=false with zero facts — it talked itself out of an answer it had
    already decided was ready."""
    kw = dict(question=Q, preloaded=(("rag", True, "15"),))
    assert "role_judge" not in _roles(assemble(Facts(**kw, finalising=True))[1])
    assert "role_judge" in _roles(assemble(Facts(**kw))[1])


def test_the_findings_are_named_not_counted():
    """"fix the claims" without saying WHICH is an instruction react cannot
    follow — the same defect as telling it twenty passages were rejected."""
    txt = assemble(Facts(question=Q, preloaded=(("rag", True, "x"),),
                         finalising=True,
                         critic_findings=('"optimal outcomes" not in evidence',)))[0]
    assert '"optimal outcomes" not in evidence' in txt


def test_incorporate_forbids_silent_deletion():
    txt = assemble(Facts(question=Q, preloaded=(("rag", True, "x"),),
                         finalising=True, critic_findings=("x",)))[0]
    assert "do not silently delete" in txt
