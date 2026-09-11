"""Posture machine — the decision core of orchestrator v2.

Two kinds of test here, and the second kind is the point:

  1. behaviour — does select() return what the spec says
  2. MUTATION-CHECKED invariants — tests shaped so that breaking the rule they
     protect makes them fail. A test that passes whether or not the rule holds
     is the shape this program spent a week finding (3 green tests on 209
     unreachable lines under a "directly-testable" header).
"""

import pytest

from app.pipeline.v2.posture import (
    Attempt, Budget, Decision, Directive, ExitMode, Gap, Posture, RoundState,
    Trend, complied, distinct_levers, exit_mode, jaccard, offers_continuation,
    select, spendable, stuck, trend, validate_worth_it, worth_spending,
)


def _budget(s=100.0, c=50.0):
    return Budget(remaining_s=s, remaining_c=c)


def _state(**kw):
    base = dict(
        round_index=3, open_gaps=(), gaps_open_history=(1, 1),
        budget=_budget(), next_round_cost_s=10.0, acting_cost_s=10.0,
        validate_cost_s=9.6,
    )
    base.update(kw)
    return RoundState(**base)


def _gap(gid="G1", opened=1, attempts=(), importance="normal"):
    return Gap(gap_id=gid, text=f"gap {gid}", opened_round=opened,
               importance=importance, attempted_by=tuple(attempts))


# ── purity: the property the whole shadow-mode plan rests on ────────────────

def test_module_reads_no_clock_no_db_no_globals():
    """R0 shadow compares two runs. A module that reads a clock cannot be
    compared -- the runs differ by noise and the comparison proves nothing."""
    import ast
    from pathlib import Path

    # AST, not regex. A regex over source matches the word "random" in a
    # docstring that says the module uses no randomness -- which is exactly the
    # false positive this program keeps finding (a swallow detector that counted
    # syntax; an assignment sweep that called every mutated container dead).
    # The question is what the code CALLS, not what the text contains.
    banned_calls = {"monotonic", "perf_counter", "time", "now", "getenv",
                    "random", "uniform", "choice", "db_execute", "db_query"}
    banned_imports = {"time", "random", "datetime", "os"}

    hits = []
    for mod in ("posture", "routing"):
        tree = ast.parse(Path(f"app/pipeline/v2/{mod}.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                if name in banned_calls:
                    hits.append(f"{mod}:{node.lineno} {name}()")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names]
                if any(m in banned_imports for m in [getattr(node, "module", None) or ""] + names):
                    hits.append(f"{mod}:{node.lineno} import")
    assert hits == [], f"impure: {hits}"


def test_select_is_deterministic_across_repeat_calls():
    st = _state(open_gaps=(_gap(),))
    assert select(st) == select(st)


# ── trend: only INCREASING informs ──────────────────────────────────────────

@pytest.mark.parametrize("hist,want", [
    ((1, 2), Trend.INCREASING),
    ((2, 1), Trend.DECREASING),
    ((2, 2), Trend.FLAT),
    ((3,), Trend.FLAT),          # too short to classify
    ((), Trend.FLAT),
])
def test_trend(hist, want):
    assert trend(hist) is want


def test_increasing_gaps_buys_discovery_not_closure():
    """Measured: closure odds are 25.4% when gaps are increasing vs ~41% flat.
    Buying CLOSE here is buying a losing ticket."""
    d = select(_state(open_gaps=(_gap(),), gaps_open_history=(1, 3)))
    assert d.posture is Posture.EXPLORE
    assert d.directive is Directive.DISCOVER


# ── the stuck rule ──────────────────────────────────────────────────────────

def test_stuck_requires_age_AND_levers_AND_no_payload():
    """MUTATION-CHECKED: each clause is load-bearing. Drop any one and a gap
    that should still be bought gets abandoned, or vice versa."""
    old_two_levers_nothing = _gap(opened=0, attempts=[
        Attempt(1, tool="rag", returned_payload=False),
        Attempt(2, tool="healthcare_query", returned_payload=False),
    ])
    assert stuck(old_two_levers_nothing, current_round=3) is True

    # young -> not stuck
    assert stuck(_gap(opened=2, attempts=[
        Attempt(2, tool="rag"), Attempt(3, tool="x")]), current_round=3) is False
    # one lever twice -> not stuck (the lever has not been varied)
    assert stuck(_gap(opened=0, attempts=[
        Attempt(1, tool="rag"), Attempt(2, tool="rag")]), current_round=4) is False
    # something came back -> not stuck
    assert stuck(_gap(opened=0, attempts=[
        Attempt(1, tool="rag", returned_payload=True),
        Attempt(2, tool="y", returned_payload=False)]), current_round=4) is False


def test_stuck_gap_is_not_bought_again():
    """The real turn this rule was written for ran the identical gap for seven
    consecutive rounds, 193.7s against a 95s promise."""
    g = _gap(opened=0, attempts=[
        Attempt(1, tool="rag", returned_payload=False),
        Attempt(2, tool="healthcare_query", returned_payload=False),
    ])
    d = select(_state(round_index=4, open_gaps=(g,), gaps_open_history=(1, 1)))
    assert d.posture is Posture.NARROW


# ── reserve the cost of ACTING ──────────────────────────────────────────────

def test_round_refused_when_only_the_LOOK_is_affordable():
    """Buying the last round to DISCOVER a problem leaves nothing to fix it."""
    st = _state(open_gaps=(_gap(),), budget=_budget(s=12.0),
                next_round_cost_s=10.0, acting_cost_s=10.0)
    assert spendable(st) is False
    assert select(st).posture is Posture.NARROW

    st_ok = _state(open_gaps=(_gap(),), budget=_budget(s=25.0),
                   next_round_cost_s=10.0, acting_cost_s=10.0)
    assert spendable(st_ok) is True
    assert select(st_ok).directive is Directive.CLOSE


def test_cost_does_not_decide():
    """react_1 reports cost on 686/1,236 calls -- understated spend FAILS OPEN.
    Cost may inform; it must not refuse a round on its own."""
    st = _state(open_gaps=(_gap(),), budget=Budget(remaining_s=100.0, remaining_c=0.0))
    assert select(st).directive is Directive.CLOSE


# ── CLOSE carries the attempt history; jaccard checks compliance ────────────

def test_close_injects_prior_queries():
    """REFORMULATE is not a directive. The prompt gets the history and the
    materiality requirement follows from the data."""
    g = _gap(attempts=[Attempt(2, tool="rag", query="florida medicaid filing deadline")])
    d = select(_state(open_gaps=(g,)))
    assert d.directive is Directive.CLOSE
    assert d.prior_queries == ("florida medicaid filing deadline",)


def test_complied_rejects_a_cosmetic_rewording():
    """H0036: 'the model's only real lever was cosmetic query rewording'.
    24.5% of consecutive rag pairs are still near-duplicate."""
    g = _gap(attempts=[Attempt(2, query="what is the timely filing deadline for sunshine")])
    assert complied(g, "what is the timely filing deadline for sunshine health") is False
    assert complied(g, "sunshine health provider manual claims submission window") is True


def test_jaccard_bounds():
    assert jaccard("a b", "a b") == 1.0
    assert jaccard("a b", "c d") == 0.0


# ── exit modes ──────────────────────────────────────────────────────────────

def test_exit_complete_when_nothing_material_is_open():
    assert exit_mode(_state()) is ExitMode.COMPLETE


def test_exit_capability_only_when_every_attempt_RAN_and_returned_nothing():
    tried_nothing = _gap(attempts=[Attempt(1, tool="rag", returned_payload=False)])
    assert exit_mode(_state(open_gaps=(tried_nothing,))) is ExitMode.CAPABILITY


def test_a_gap_never_attempted_is_BUDGET_not_CAPABILITY():
    """We ran out before trying it. Different fact, opposite advice about
    continuing -- and the flattering default is the wrong one here."""
    assert exit_mode(_state(open_gaps=(_gap(attempts=[]),))) is ExitMode.BUDGET


def test_payload_check_not_the_tools_success_flag():
    """Port hazard 9: the MCP adapter attaches a self-citing SourceRef on
    success, so exhaustion is structurally unreachable for ~34 tools if you
    trust the tool's own flag. returned_payload is the payload check."""
    self_citing = _gap(attempts=[Attempt(1, tool="mcp_x", returned_payload=False)])
    assert exit_mode(_state(open_gaps=(self_citing,))) is ExitMode.CAPABILITY


def test_capability_never_offers_continuation():
    """MUTATION-CHECKED: flip this and the user is asked to retry something that
    cannot succeed -- the worst available outcome."""
    assert offers_continuation(ExitMode.CAPABILITY) is False
    assert offers_continuation(ExitMode.BUDGET) is True
    assert offers_continuation(ExitMode.ERROR) is True
    assert offers_continuation(ExitMode.COMPLETE) is False


def test_error_still_reaches_communicate():
    """ONE way out, even on early exit. An ERROR that skips COMMUNICATE skips
    the diagnostics that say it errored."""
    d = select(_state(errored=True, open_gaps=(_gap(),)))
    assert d.posture is Posture.COMMUNICATE
    assert exit_mode(_state(errored=True)) is ExitMode.ERROR


# ── validate: value of information ──────────────────────────────────────────

def test_validate_needs_budget_for_the_FIX_not_just_the_look():
    st = _state(quality_uncertain=True, budget=_budget(s=10.0), validate_cost_s=9.6,
                acting_cost_s=10.0)
    assert validate_worth_it(st) is False
    st2 = _state(quality_uncertain=True, budget=_budget(s=30.0), validate_cost_s=9.6,
                 acting_cost_s=10.0)
    assert validate_worth_it(st2) is True


def test_validate_skipped_when_quality_is_not_uncertain():
    assert validate_worth_it(_state(quality_uncertain=False, budget=_budget(s=100.0))) is False


# ── A/B routing ─────────────────────────────────────────────────────────────

from app.pipeline.v2.routing import Banner, V1, V2, assign, decision_line


def test_assignment_is_stable_for_the_same_turn():
    """A turn must not flip arms mid-flight, and a retry must land on the same
    arm, or the comparison is over a moving population."""
    cid = "d50c4353-1032-4af8-85cd-fea3052693f7"
    assert len({assign(cid, 25) for _ in range(50)}) == 1


def test_zero_and_hundred_are_absolute():
    assert assign("anything", 0) == V1        # R0 shadow: nothing routed
    assert assign("anything", 100) == V2


def test_split_is_roughly_the_requested_share():
    ids = [f"turn-{i}" for i in range(4000)]
    share = sum(assign(i, 25) == V2 for i in ids) / len(ids)
    assert 0.22 < share < 0.28, share


def test_banner_names_the_arm_first():
    b = Banner(V2, "normal", 31, 8, ("frame", "explore", "narrow", "communicate"))
    line = b.line()
    assert line.startswith("▣ v2"), line
    assert "31s ±8" in line
    assert "FRAME → EXPLORE" in line


def test_every_decision_emit_carries_the_version():
    assert decision_line(V2, 3, "closing G3").startswith("v2 · round 3")


# ── R0 shadow ───────────────────────────────────────────────────────────────

from app.pipeline.v2.shadow import DIRECTIVE_TO_POSTURE, compare, emit


def test_shadow_never_raises_on_a_broken_state():
    """An observer that can break the thing it observes is not an observer."""
    assert compare("search", None) is None          # garbage state
    emit("cid", None)                                # must not raise
    emit("cid", {"agrees": True})                    # missing keys, must not raise


def test_shadow_reports_agreement_when_v1_and_v2_line_up():
    st = _state(open_gaps=(_gap(),))                 # v2 -> EXPLORE/CLOSE
    c = compare("search", st)
    assert c["v2_posture"] == "explore"
    assert c["agrees"] is True
    assert c["unmapped"] is False


def test_shadow_reports_divergence_rather_than_hiding_it():
    st = _state(open_gaps=(_gap(),))                 # v2 -> EXPLORE
    c = compare("finalize", st)                      # v1 -> COMMUNICATE
    assert c["agrees"] is False
    assert c["v1_maps_to"] == "communicate"


def test_unmapped_is_distinct_from_disagreement():
    """v1 saying something the mapping does not cover is a finding ABOUT THE
    MAPPING -- not evidence that v2 is wrong."""
    c = compare("some_new_directive", _state(open_gaps=(_gap(),)))
    assert c["unmapped"] is True
    assert c["agrees"] is False


def test_v1s_answer_is_not_fed_into_v2s_decision():
    """If it were, agreement would be guaranteed and the comparison would be
    green by construction."""
    st = _state(open_gaps=(_gap(),))
    assert compare("search", st)["v2_posture"] == compare("finalize", st)["v2_posture"]


def test_every_v1_directive_is_mapped():
    """The v1 vocabulary, read from governor.py: search, extend, consolidate,
    finalize, complete. An unmapped one would silently inflate UNMAPPED."""
    assert set(DIRECTIVE_TO_POSTURE) == {
        "search", "extend", "consolidate", "finalize", "complete"}
