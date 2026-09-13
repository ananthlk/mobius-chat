"""A round with no evidence still has a job — and it is the most important one.

Ananth, 2026-09-13, reading a live 3-round trace:
    "WHY 3 ROUNDS .. FIRST ROUND DOES NOT FEEL LIKE IT WAS JUDING"

It was not judging. Measured on cid c1b560c6: preload logged NOTHING TO RUN,
and rounds 1 and 2 both logged roles=- — every drafting role keys off preload
output, so with nothing preloaded the whole governor role stack rendered EMPTY
and react reverted to bare v1 tool-picking. Three rounds instead of two.
"""
from app.pipeline.v2.blocks import MAX_ROLES, Facts, assemble


def roles_for(f):
    """The role ids assemble() actually rendered for these facts."""
    _, rendered, _ = assemble(f)
    return [i for i in rendered if i.startswith("role_")]


def _blind(**kw):
    """The shape of the defect: a question, and nothing else."""
    base = dict(question="timely filing deadline for Sunshine Health",
                preloaded=(), useful=(), suggest=(), gaps=(), answer="")
    base.update(kw)
    return Facts(**base)


def test_a_blind_round_is_never_roleless():
    """THE REGRESSION, as a property rather than a fingerprint: react is never
    sent into a drafting round with no instructions at all."""
    assert roles_for(_blind()), (
        "a round with no evidence rendered NO role — react gets no instruction "
        "and falls back to v1 tool-picking")


def test_the_blind_role_asks_for_specifics():
    ids = roles_for(_blind())
    assert "role_scope" in ids
    assert "role_judge" not in ids, "nothing to judge with no evidence"
    assert "role_summarise" not in ids, "nothing to summarise from"


def test_a_gap_with_no_offered_tool_still_gets_planned():
    """suggest was a GATE, so a gap nothing covers produced no plan role at
    all — silently, on exactly the turn where "nothing offered covers this" is
    the most useful thing react could tell us."""
    assert "role_plan" in roles_for(_blind(gaps=("what is the deadline",),
                                           suggest=()))


def test_evidence_turns_the_blind_role_off():
    """SCOPE IS FOR ABSENCE. Firing it with evidence present would tell react
    not to answer from material it was just handed."""
    for present in ({"preloaded": (("rag", True, "15 passages"),)}, {"useful": ("doc p1",)}):
        assert "role_scope" not in roles_for(_blind(**present)), present


def test_the_cap_still_holds_on_every_shape():
    for f in (_blind(),
              _blind(gaps=("g",)),
              _blind(gaps=("g",), suggest=("rag",)),
              _blind(preloaded=(("rag", True, "x"),), gaps=("g",), suggest=("rag",)),
              _blind(preloaded=(("rag", True, "x"),), useful=("d",), gaps=("g",))):
        got = roles_for(f)
        assert len(got) <= MAX_ROLES, got
