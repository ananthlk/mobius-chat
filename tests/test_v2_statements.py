"""The statement registry: which statement, which posture, in what order.

Spec: docs/governor-profile-statements.md

The three orderings are the design (TURN / EXECUTION / PRECEDENCE), and
conflating them is the bug these tests exist to catch.
"""
import pytest

from app.pipeline.v2 import statements as S
from app.pipeline.v2.posture import (
    Attempt, Budget, Gap, Posture, RoundState, seed_root_gap,
)

Q = "care management philosophy for Molina, Sunshine Health and UnitedHealthcare"
SUN = "Sunshine Health care management philosophy"


def _state(gaps, hist=(1,), rem=88.0):
    return RoundState(
        round_index=2, open_gaps=tuple(gaps), gaps_open_history=tuple(hist),
        budget=Budget(remaining_s=rem, remaining_c=3.0, band_s=25.0),
        next_round_cost_s=10.4, acting_cost_s=10.0, validate_cost_s=9.6,
        question=Q,
    )


def _gap(text, gid, attempts=()):
    return Gap(gap_id=gid, text=text, opened_round=1,
               attempted_by=tuple(attempts))


MOLINA = _gap("Molina care management philosophy", "S1",
              [Attempt(round_index=1, tool="rag", query="molina care management",
                       returned_payload=True, targeted=True)])
SUNSHINE = _gap(SUN, "S2")
UHC = _gap("UnitedHealthcare care management philosophy", "S3")


# ── registry hygiene ────────────────────────────────────────────────────────

def test_ids_are_unique():
    ids = [s.id for s in S.REGISTRY]
    assert len(ids) == len(set(ids)), [i for i in ids if ids.count(i) > 1]


def test_every_statement_has_at_least_one_posture():
    """A statement no posture can reach is a statement that never fires."""
    for s in S.REGISTRY:
        assert s.postures, s.id


def test_conflicts_name_real_statements():
    ids = {s.id for s in S.REGISTRY}
    for s in S.REGISTRY:
        assert s.conflicts <= ids, (s.id, s.conflicts - ids)


# ── THE REGRESSION: precedence must not remove a capability ─────────────────

def test_the_governors_dissent_is_reachable():
    """CTL-1 -- "you said complete, but this part was never searched" -- is the
    whole dual mode. A one-per-group precedence rule made it STRUCTURALLY
    UNREACHABLE for an hour, because CTL-3 is also CONTROL and sorted first.
    Invisible until the block was rendered for a round where both applied.
    """
    c = S.Ctx(state=_state([SUNSHINE, UHC, MOLINA], (1, 3, 3)), round_index=4,
              tier="thinking", kept=9, model_proposes_complete=True, gap=SUNSHINE)
    fired = {s.id for s in S.select(c, Posture.EXPLORE).statements}
    assert "CTL-1" in fired, "the dissent cannot be sent; the handshake is dead"
    assert "CTL-3" in fired, "the satisfaction question must also survive"


def test_complementary_framing_statements_both_fire():
    """FRM-1 (name the parts) and FRM-2 (ask as asked) are the whole round-1
    instruction. Keeping only the first is how a question gets decomposed and
    then asked one part at a time -- the behaviour this was built to stop."""
    root = seed_root_gap(Q)
    c = S.Ctx(state=_state([root]), round_index=1, tier="thinking", gap=root)
    fired = {s.id for s in S.select(c, Posture.EXPLORE).statements}
    assert {"FRM-1", "FRM-2"} <= fired


# ── EXECUTION order == render order ─────────────────────────────────────────

def test_render_order_is_execution_order():
    """E1 BOUND first: "this is the final round" invalidates everything below
    it, and rendering it last means the model has planned a tool call before it
    learns it cannot make one."""
    c = S.Ctx(state=_state([SUNSHINE, UHC, MOLINA], (1, 3, 3)), round_index=4,
              tier="thinking", kept=9, model_proposes_complete=True, gap=SUNSHINE)
    slots = [s.slot for s in S.select(c, Posture.EXPLORE).statements]
    assert slots == sorted(slots)


def test_settle_renders_before_target():
    """E4 before E5 IS the dual mode: ask whether we are done BEFORE deciding
    what to spend on. Reversed, the governor picks a gap and the completion
    question is never put -- which is today's failure."""
    c = S.Ctx(state=_state([SUNSHINE, UHC, MOLINA], (1, 3, 3)), round_index=4,
              tier="thinking", kept=9, model_proposes_complete=True, gap=SUNSHINE)
    order = [s.id for s in S.select(c, Posture.EXPLORE).statements]
    assert order.index("CTL-3") < order.index("EVD-4")
    assert order.index("CTL-3") < order.index("CTL-1"), (
        "the satisfaction question must be asked before the challenge, so react "
        "answers on its own evidence rather than reacting to being challenged"
    )


# ── TURN order ──────────────────────────────────────────────────────────────

def test_round_one_gets_no_synthesis_instruction():
    """Nothing has been tried yet, so "say why you could not answer this part"
    is a question about work that has not happened."""
    root = seed_root_gap(Q)
    c = S.Ctx(state=_state([root]), round_index=1, tier="thinking", gap=root)
    assert "FRM-7" not in {s.id for s in S.select(c, Posture.EXPLORE).statements}


def test_round_one_gets_no_targeting():
    root = seed_root_gap(Q)
    c = S.Ctx(state=_state([root]), round_index=1, tier="thinking", gap=root)
    fired = {s.id for s in S.select(c, Posture.EXPLORE).statements}
    assert "EVD-4" not in fired and "EVD-1" not in fired


# ── conflicts and the cap ───────────────────────────────────────────────────

def test_final_round_suppresses_go_search_again():
    """react_loop already uses `elif` for this hazard: "you're not required to
    stop" directly contradicts "do NOT request another tool call"."""
    c = S.Ctx(state=_state([SUNSHINE, UHC], (1, 3, 2), rem=2.0), round_index=10,
              max_rounds=10, tier="thinking", kept=3, gap=None)
    sel = S.select(c, Posture.COMMUNICATE)
    fired = {s.id for s in sel.statements}
    assert "SAF-1" in fired
    assert "EVD-2" not in fired
    assert "EVD-2" in sel.dropped_by_conflict, "a dropped statement must be RECORDED"


def test_the_cap_holds_and_records_what_it_dropped():
    """Silently dropping guidance is how a rule stops being sent without
    anyone noticing."""
    c = S.Ctx(state=_state([SUNSHINE, UHC, MOLINA], (1, 3, 3)), round_index=4,
              tier="thinking", kept=0, model_proposes_complete=True, gap=SUNSHINE)
    sel = S.select(c, Posture.EXPLORE)
    assert len(sel.statements) <= S.MAX_STATEMENTS
    if sel.dropped_by_cap:
        kept_slots = [s.slot for s in sel.statements]
        assert max(kept_slots) <= S.Slot.FORM


# ── failure containment ─────────────────────────────────────────────────────

def test_a_raising_selector_does_not_take_the_turn():
    """Absent guidance, never a crashed round -- a deliberate fail-quiet on the
    prompt path only."""
    boom = S.Statement("BOOM", S.Slot.ACT, S.Group.EVIDENCE, S.ANY_POSTURE,
                       when=lambda c: (_ for _ in ()).throw(RuntimeError("x")),
                       block_key="governor.never")
    original = S.REGISTRY
    try:
        S.REGISTRY = original + (boom,)
        root = seed_root_gap(Q)
        c = S.Ctx(state=_state([root]), round_index=1, gap=root)
        assert "BOOM" not in {s.id for s in S.select(c, Posture.EXPLORE).statements}
    finally:
        S.REGISTRY = original


@pytest.mark.parametrize("posture", list(Posture))
def test_every_posture_produces_a_selection_without_raising(posture):
    c = S.Ctx(state=_state([SUNSHINE, UHC, MOLINA], (1, 3, 3)), round_index=3,
              tier="thinking", kept=4, gap=SUNSHINE)
    S.select(c, posture)


# ── wording lives in the prompt DB, not in this module ──────────────────────

def test_every_statement_names_a_prompt_block():
    """Selection is code; wording is content. A statement with no block key is
    prose hardcoded in Python -- which is how react_loop reached 6,900 lines
    nobody can edit without a deploy."""
    for s in S.REGISTRY:
        assert s.block_key and s.block_key.startswith("governor."), s.id


def test_every_block_key_has_a_fallback():
    """The fallback is the FLOOR, not the source of truth. Without it a missing
    DB block deletes the statement from the prompt silently -- a producer with
    no consumer arriving through the prompt store."""
    from app.pipeline.v2.statement_text import FALLBACK
    missing = [s.block_key for s in S.REGISTRY if s.block_key not in FALLBACK]
    assert not missing, missing


def test_fallback_templates_interpolate_the_params_they_are_given():
    """A template naming {gap} whose statement supplies no gap renders the
    brace literally into the prompt."""
    from app.pipeline.v2.statement_text import FALLBACK
    import re
    root = seed_root_gap(Q)
    c = S.Ctx(state=_state([SUNSHINE, UHC, MOLINA], (1, 3, 3)), round_index=4,
              tier="thinking", kept=9, model_proposes_complete=True, gap=SUNSHINE)
    for s in S.REGISTRY:
        needed = set(re.findall(r"\{(\w+)", FALLBACK[s.block_key]))
        try:
            got = set((s.params(c) or {}).keys())
        except Exception:
            got = set()
        assert needed <= got, (s.id, needed - got)


def test_the_source_of_every_line_is_recorded():
    """A turn worded from the DB and a turn worded from the fallback are
    different experiments; a comparison that cannot tell them apart is
    measuring two things at once."""
    c = S.Ctx(state=_state([SUNSHINE, UHC, MOLINA], (1, 3, 3)), round_index=4,
              tier="thinking", kept=9, model_proposes_complete=True, gap=SUNSHINE)
    for s in S.select(c, Posture.EXPLORE).statements:
        text, source = S.text_of(s, c)
        assert text and source in {"db", "fallback", "missing"}
        assert not text.startswith("[missing prompt block")
