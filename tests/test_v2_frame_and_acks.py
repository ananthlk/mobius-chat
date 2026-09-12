"""The governor's frame sections, and the ack checkers that keep them honest.

Spec: docs/governor-prompt-frame-v1.md

An ack proves the section was READ. The checkers prove the round ACTED on it.
Disagreement is the signal; an ack that never contradicts the trace measures
nothing. No ack ships without its checker -- an unchecked ack reads as evidence
while proving nothing, which is worse than no ack.
"""
from app.pipeline.v2 import compliance as C
from app.pipeline.v2 import frame as F
from app.pipeline.v2 import statements as ST
from app.pipeline.v2.posture import (
    Attempt, Budget, Closure, Gap, Posture, RoundState,
)

Q = "care management philosophy for Molina, Sunshine Health and UnitedHealthcare"
MOL = "Molina care management philosophy"
SUN = "Sunshine Health care management philosophy"
UHC = "UnitedHealthcare care management philosophy"


def _A(rn, q, ok=True):
    return Attempt(round_index=rn, tool="rag", query=q,
                   returned_payload=ok, targeted=True)


SUNG = Gap(gap_id="S384280", text=SUN, opened_round=2,
           attempted_by=(_A(3, "sunshine health care management"),),
           closure_by=(Closure(3, 20), Closure(4, 65)))
UHCG = Gap(gap_id="S7c9bc3", text=UHC, opened_round=2)


def _ctx(round_index=5, proposes=True, gap=UHCG, gaps=(SUNG, UHCG)):
    st = RoundState(round_index=round_index, open_gaps=tuple(gaps),
                    gaps_open_history=(1, 3, 2),
                    budget=Budget(remaining_s=30.0, remaining_c=3.0, band_s=25.0),
                    next_round_cost_s=10.4, acting_cost_s=10.0,
                    validate_cost_s=9.6, question=Q)
    return ST.Ctx(state=st, round_index=round_index, tier="thinking", kept=11,
                  model_proposes_complete=proposes, gap=gap)


# ── the frame ───────────────────────────────────────────────────────────────

def test_the_ledger_says_which_parts_were_never_searched():
    """The distinction the whole ledger exists for. Today "never searched" and
    "searched and empty" produce the same sentence in the answer."""
    txt, _ = F.render(_ctx(), Posture.EXPLORE)
    assert "never been searched" in txt
    assert "S7c9bc3" in txt and "S384280" in txt


def test_the_role_is_in_reacts_language_not_the_machines():
    """A posture name the model cannot act on is a label that changes nothing."""
    txt, _ = F.render(_ctx(), Posture.COMMUNICATE)
    assert "communicate" not in txt.lower().split("[§6 role this round] ")[-1][:60]
    assert "write the answer from what is kept" in txt


def test_the_governor_renders_only_its_own_sections():
    """A section rendered by two authors is a contradiction waiting for whoever
    debugs it next."""
    txt, _ = F.render(_ctx(), Posture.EXPLORE)
    for foreign in ("[§1", "[§2", "[§3", "[§7", "[§9", "[§10"):
        assert foreign not in txt


def test_every_acked_key_has_a_checker():
    """An ack with no checker is a producer with no consumer wearing a schema."""
    checked = {"parts", "working_gap", "dissent", "complete", "complete_why"}
    for key, _desc in F.ACK_KEYS:
        assert key in checked, f"{key} is asked for and never checked"


# ── the checkers ────────────────────────────────────────────────────────────

def test_parts_ack_catches_named_three_queried_one():
    """THE 3/3 FAILURE, made explicit instead of inferred from two fields
    nothing compared."""
    r = C.check_parts({"parts": [MOL, SUN, UHC]},
                      {"inputs": {"query": "Molina care management philosophy"}},
                      [MOL, SUN, UHC])
    assert r.verdict is C.Verdict.IGNORED
    assert "1 of 3" in r.basis


def test_parts_ack_passes_when_the_query_covers_them():
    r = C.check_parts({"parts": [MOL, SUN, UHC]},
                      {"inputs": {"query": "care management philosophy for "
                                           "Molina, Sunshine Health and "
                                           "UnitedHealthcare"}},
                      [MOL, SUN, UHC])
    assert r.verdict is C.Verdict.FOLLOWED


def test_single_part_question_is_unobservable_not_a_pass():
    r = C.check_parts({"parts": [MOL]}, {"inputs": {"query": "anything"}}, [MOL])
    assert r.verdict is C.Verdict.UNOBSERVABLE


def test_working_gap_catches_says_sunshine_queries_molina():
    r = C.check_working_gap({"working_gap": "S384280"},
                            {"inputs": {"query": "molina care management"}},
                            {"S384280": SUN, "S7c9bc3": UHC})
    assert r.verdict is C.Verdict.IGNORED


def test_working_gap_acking_an_unknown_id_is_a_finding():
    """The ledger and the model disagreeing about what exists is itself news."""
    r = C.check_working_gap({"working_gap": "NOPE"},
                            {"inputs": {"query": "sunshine health"}},
                            {"S384280": SUN})
    assert r.verdict is C.Verdict.IGNORED
    assert "not in the open ledger" in r.basis


def test_declining_the_dissent_is_never_disobedience():
    """Ananth: "not to do so unilaterally". Both answers are legitimate."""
    r = C.check_dissent({"dissent": "declined — corpus has nothing for it"},
                        None, UHC, [SUN, UHC])
    assert r.verdict is C.Verdict.DECLINED


def test_dissent_catches_accepted_in_words_declined_in_fact():
    """The only failure mode here, and it is invisible without the ack."""
    r = C.check_dissent({"dissent": "accepted"},
                        {"inputs": {"query": "sunshine health care management"}},
                        UHC, [SUN, UHC])
    assert r.verdict is C.Verdict.IGNORED
    assert "searched something else" in r.basis


def test_dissent_followed_when_the_next_round_searches_it():
    r = C.check_dissent({"dissent": "accepted"},
                        {"inputs": {"query": "UnitedHealthcare care management"}},
                        UHC, [SUN, UHC])
    assert r.verdict is C.Verdict.FOLLOWED


def test_no_dissent_raised_is_unobservable():
    r = C.check_dissent({}, {"inputs": {"query": "x"}}, None, [SUN])
    assert r.verdict is C.Verdict.UNOBSERVABLE


# ── the root gap is the QUESTION, not a part ────────────────────────────────

def test_no_statement_speaks_about_the_root_gap():
    """Rendering the root into a gap-shaped sentence produced lines like "one
    more round is authorised if it closes <the entire question>" and "<the
    entire question> was searched once and is still open" -- true of a ledger
    row, nonsense to read.

    EVD-4 already excluded the root, but as ONE STATEMENT'S CONDITION rather
    than as the property it is, so five others did not. Found by rendering
    round 2 locally instead of deploying.
    """
    from app.pipeline.v2.posture import seed_root_gap
    root = seed_root_gap(Q)
    tried = Gap(gap_id=root.gap_id, text=root.text, opened_round=1,
                importance="high", attempted_by=(_A(1, "molina care management"),))
    st = RoundState(round_index=2, open_gaps=(tried,), gaps_open_history=(1,),
                    budget=Budget(remaining_s=80.0, remaining_c=3.0, band_s=25.0),
                    next_round_cost_s=10.4, acting_cost_s=10.0,
                    validate_cost_s=9.6, question=Q)
    c = ST.Ctx(state=st, round_index=2, max_rounds=10, tier="thinking",
               kept=17, gap=tried)
    txt, _ = F.render(c, Posture.EXPLORE)
    # The question must not appear as a quoted gap, nor under §5.
    assert "[§5" not in txt, "the root gap was listed as an open PART"
    assert Q not in txt, "a statement quoted the whole question as a gap"


def test_the_role_matches_the_round_when_no_part_is_named_yet():
    """§6 said "find evidence that closes a named open part" directly above a
    statement saying "name each part still missing". Two authors, one round.
    The condition is "no NAMED part yet" -- the same one EVD-1 selects on."""
    from app.pipeline.v2.posture import seed_root_gap
    root = seed_root_gap(Q)
    st = RoundState(round_index=2, open_gaps=(root,), gaps_open_history=(1,),
                    budget=Budget(remaining_s=80.0, remaining_c=3.0, band_s=25.0),
                    next_round_cost_s=10.4, acting_cost_s=10.0,
                    validate_cost_s=9.6, question=Q)
    c = ST.Ctx(state=st, round_index=2, max_rounds=10, tier="thinking",
               kept=17, gap=root)
    txt, _ = F.render(c, Posture.EXPLORE)
    assert F.DISCOVER_ROLE in txt
    assert "closes a named open part" not in txt


def test_a_narrowed_round_is_not_an_ignored_instruction():
    """THE FALSE POSITIVE. An honest, fully compliant round -- told to work one
    part, queried that part -- scored `parts: IGNORED` because the query named
    1 of 3. A follow-rate built on that is permanently red, and the "fix" would
    have been to correct a prompt that was working.

    "Cover every part in one query" is the ROUND-1 instruction; from round 2
    the governor names one part and says the others are not for this round.
    """
    r = C.check_parts({"parts": [MOL, SUN, UHC], "working_gap": "S384280"},
                      {"inputs": {"query": SUN}}, [MOL, SUN, UHC])
    assert r.verdict is C.Verdict.UNOBSERVABLE
    assert "instructed" in r.basis

    # And round 1 -- no working_gap -- must still catch the real failure.
    r1 = C.check_parts({"parts": [MOL, SUN, UHC], "working_gap": None},
                       {"inputs": {"query": MOL}}, [MOL, SUN, UHC])
    assert r1.verdict is C.Verdict.IGNORED, (
        "scoping the check must not disarm it on the round it exists for"
    )
