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
    Trend, alternatives_worth_it, complied, converging, distinct_levers, exit_mode,
    jaccard, may_overrun, offers_continuation,
    select, spendable, stuck, trend, validate_worth_it, worth_spending,
)


def _budget(s=100.0, c=50.0):
    return Budget(remaining_s=s, remaining_c=c)


def _state(**kw):
    base = dict(
        round_index=3, open_gaps=(), gaps_open_history=(1, 1),
        budget=_budget(), next_round_cost_s=10.0, acting_cost_s=10.0,
        validate_cost_s=9.6, alternatives_cost_s=8.0,
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


def test_select_never_returns_FRAME_it_has_no_signal_yet():
    """FRAME's job is real -- a wrong frame makes every later round waste money.
    Its TRIGGER was positional (round_index <= 1), which is not a posture but a
    label for a position, and it produced 57% of all shadow divergences at
    round 1 against a v1 that has no FRAME concept.

    Declared-unreachable, not accidentally unreachable: this test is what keeps
    the difference. If someone builds a real frame signal, they must delete this
    test deliberately -- which is the point.
    """
    for rn in (1, 2, 5):
        for gaps in ((), (_gap(),)):
            for hist in ((), (0,), (0, 1), (2, 1)):
                d = select(_state(round_index=rn, open_gaps=gaps,
                                  gaps_open_history=hist))
                assert d.posture is not Posture.FRAME, (rn, gaps, hist, d)


def test_round_one_with_no_gaps_is_discovery():
    """What v1 calls `search`, and what it actually is: we are gathering."""
    d = select(_state(round_index=1, open_gaps=(), gaps_open_history=(0,)))
    assert d.posture in (Posture.EXPLORE, Posture.COMMUNICATE)


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
    # The invariant is "does not buy another CLOSE round on a gap two levers
    # have already failed". The DESTINATION changed on 2026-09-11 when Ananth
    # added ALTERNATIVES -- a stuck gap now routes to offering the user a route
    # rather than straight to naming the shortfall. Asserted as "not CLOSE"
    # plus the new destination, so the invariant survives the next change to
    # where stuck gaps go.
    assert d.directive is not Directive.CLOSE
    assert d.posture is Posture.ALTERNATIVES


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
    """The v1 vocabulary from governor.py: search, extend, consolidate,
    finalize, complete. 'extend' is resolved by reason, not by the table."""
    from app.pipeline.v2.shadow import map_directive
    assert set(DIRECTIVE_TO_POSTURE) == {
        "search", "consolidate", "finalize", "complete"}
    assert map_directive("extend", "quality issue flagged — going deeper") is not None
    assert map_directive("extend", "confidence bar not yet met and round budget "
                                   "exhausted — extending") is not None


# ── the mapping vs v1's own live mapping ────────────────────────────────────

def test_extend_splits_on_its_reason_not_its_name():
    """governor.py:197 appends the critique to tool_results, so the next round
    is aimed at NAMED unsupported claims -- remediation, not open search.
    governor.py:209 genuinely is 'keep gathering, just out of base rounds'."""
    from app.pipeline.v2.shadow import map_directive
    assert map_directive("extend", "quality issue flagged — going deeper on "
                                   "the unsupported claim(s)") is Posture.NARROW
    assert map_directive("extend", "confidence bar not yet met and round budget "
                                   "exhausted — extending") is Posture.EXPLORE


def test_an_unrecognised_extend_is_UNMAPPED_not_bucketed():
    """Silently bucketing it would make the shadow agree for the wrong reason --
    green by construction, on the one directive we know is ambiguous."""
    from app.pipeline.v2.shadow import map_directive
    assert map_directive("extend", "some new reason nobody wrote down") is None


def test_reason_substrings_still_exist_in_governor_py():
    """DRIFT TEST. The mapping matches substrings of governor.py's own reason
    prose. If that prose changes, this fails loudly -- instead of map_directive
    silently returning None and every extend becoming UNMAPPED."""
    from pathlib import Path
    from app.pipeline.v2.shadow import _EXTEND_OUT_OF_ROUNDS, _EXTEND_REMEDIATION
    src = Path("app/pipeline/react/governor.py").read_text()
    for needle in (_EXTEND_REMEDIATION, _EXTEND_OUT_OF_ROUNDS):
        assert needle in src, f"reason prose moved: {needle!r}"


def test_divergence_from_v1s_live_mapping_is_declared_and_exhaustive():
    """v1's _DIRECTIVE_TO_AGENT_ROLE is LIVE (react_loop.py:4761 selects the
    prompt composition with it). Where we disagree it must be DECLARED, and the
    declaration must cover every disagreement -- so a new one cannot appear
    silently, which is how two mappings drift apart in the first place."""
    from app.pipeline.react.governor import _DIRECTIVE_TO_AGENT_ROLE
    from app.pipeline.v2.shadow import (
        DELIBERATE_DIVERGENCE, _AGENT_ROLE_TO_POSTURE, map_directive,
    )
    undeclared = []
    for directive, role in _DIRECTIVE_TO_AGENT_ROLE.items():
        v1_posture = _AGENT_ROLE_TO_POSTURE.get(role)
        v2_posture = map_directive(directive)          # no reason: table only
        if v2_posture is None:                          # reason-resolved
            assert directive in DELIBERATE_DIVERGENCE, directive
            continue
        if v1_posture is not v2_posture:
            undeclared.append(f"{directive}: v1->{v1_posture} v2->{v2_posture}")
    assert undeclared == [], f"undeclared divergence: {undeclared}"


def test_compare_passes_the_reason_through():
    st = _state(open_gaps=())                            # v2 -> COMMUNICATE
    c = compare("extend", st, v1_reason="quality issue flagged — going deeper")
    assert c["v1_reason"].startswith("quality issue")
    assert c["v1_maps_to"] == "narrow"


def test_the_197_extend_path_is_unreachable_pre_round():
    """Chat seat's finding, encoded so it cannot silently stop being true.

    _pp_pre_state hardcodes proposes_complete=False, and governor.py's
    'quality issue flagged' extend sits inside `if state.proposes_complete`.
    So EVERY pre-round extend is the ':209' path -- still gathering evidence.

    If someone later makes the pre-round state propose completion, this fails,
    and the pre-round mapping below stops being safe to assume.
    """
    from pathlib import Path
    loop = Path("app/pipeline/react_loop.py").read_text()
    gov = Path("app/pipeline/react/governor.py").read_text()

    assert "proposes_complete=False, self_reported_confidence=None" in loop, \
        "pre-round state no longer hardcodes proposes_complete=False"

    # The claim is positional: the remediation reason lives INSIDE the
    # proposes_complete guard. Checked against the LAST occurrence of each
    # needle, because both phrases also appear in the module docstring above
    # evaluate() -- a first-occurrence check reads the prose, not the code.
    # (My first version of this test asserted an ordering between the two
    # reasons and failed on exactly that: it found the docstring copy.)
    guard = gov.rindex("if state.proposes_complete:")
    remediation = gov.rindex("quality issue flagged")
    assert guard < remediation, "the :197 extend left the proposes_complete guard"

    # Textual proxy, and a limited one: it shows the remediation reason follows
    # the guard, not that it is lexically nested under it. A control-flow proof
    # would need the AST. Stated so the next reader does not over-trust it.
    assert "round budget exhausted" in gov


def test_ledger_uses_the_logical_db_key_not_the_physical_name():
    """db_client takes a LOGICAL key. Passing the physical database name fails
    with 'No fallback URL for database ...' -- swallowed, producing zero rows
    while every other signal reads healthy.

    promise.py already carries this constant with a comment warning against the
    exact mistake. The comment did not stop it; this test will.
    """
    from app.pipeline.react import promise as _promise
    from app.pipeline.v2 import ledger as _ledger
    assert _ledger._DB == _promise._DB, (
        f"ledger._DB={_ledger._DB!r} disagrees with the known-good "
        f"promise._DB={_promise._DB!r}"
    )
    assert _ledger._DB == "chat"


def test_compare_authors_the_verdict_string_not_just_booleans():
    """The persisted column reads `verdict`. An earlier version returned only
    `agrees`/`unmapped` and let emit() derive the string as a local -- so every
    stored row was NULL while the log looked correct.

    MUTATION-CHECKED by the fact that it is asserted at all: the write path and
    the log path must read the SAME key, authored in one place.
    """
    st = _state(open_gaps=(_gap(),))
    for v1, want in (("search", "agree"), ("finalize", "diverge"),
                     ("nonsense_directive", "unmapped")):
        c = compare(v1, st)
        assert c["verdict"] == want, (v1, c["verdict"])
    # the booleans must agree with the string -- two representations of one
    # fact, and they must not be able to disagree
    c = compare("search", _state(open_gaps=(_gap(),)))
    assert (c["verdict"] == "agree") is c["agrees"]
    assert (c["verdict"] == "unmapped") is c["unmapped"]


# ── ALTERNATIVES — a route, not a shortfall ─────────────────────────────────

def _tried_nothing(gid="G1", opened=0):
    return _gap(gid, opened=opened, attempts=[
        Attempt(1, tool="rag", returned_payload=False),
        Attempt(2, tool="healthcare_query", returned_payload=False),
    ])


def test_stuck_gap_routes_to_ALTERNATIVES_not_straight_to_narrow():
    """Ananth: 'this leaves the user with something they can get without saying
    I don't know.' NARROW names the shortfall; ALTERNATIVES makes it actionable."""
    d = select(_state(round_index=4, open_gaps=(_tried_nothing(),),
                      gaps_open_history=(1, 1)))
    assert d.posture is Posture.ALTERNATIVES


def test_an_UNTRIED_gap_does_not_get_alternatives():
    """MUTATION-CHECKED distinction, and the flattering-direction one: a gap
    nobody attempted is UNFUNDED, not unreachable. Offering alternatives there
    tells the user to go elsewhere for something we could have answered."""
    st = _state(round_index=4, open_gaps=(_gap(attempts=[]),),
                gaps_open_history=(1, 1), budget=_budget(s=5.0),
                next_round_cost_s=10.0, acting_cost_s=10.0)
    assert alternatives_worth_it(st) is False
    assert select(st).posture is Posture.NARROW


def test_alternatives_needs_budget_for_the_round():
    st = _state(round_index=4, open_gaps=(_tried_nothing(),),
                gaps_open_history=(1, 1), budget=_budget(s=2.0),
                next_round_cost_s=10.0, acting_cost_s=10.0,
                alternatives_cost_s=8.0)
    assert alternatives_worth_it(st) is False
    assert select(st).posture is Posture.NARROW


def test_a_gap_that_RETURNED_something_is_not_unreachable():
    """Evidence came back; it is a synthesis or budget problem, not a source
    problem. Suggesting the user look elsewhere would be wrong."""
    got_something = _gap(attempts=[
        Attempt(1, tool="rag", returned_payload=True),
        Attempt(2, tool="rag", returned_payload=False)])
    st = _state(round_index=5, open_gaps=(got_something,), gaps_open_history=(1, 1),
                budget=_budget(s=3.0), next_round_cost_s=10.0, acting_cost_s=10.0)
    assert alternatives_worth_it(st) is False


def test_alternatives_never_fires_with_no_open_gaps():
    assert alternatives_worth_it(_state(open_gaps=())) is False
    assert select(_state(open_gaps=())).posture is Posture.COMMUNICATE


# ── measured round costs ────────────────────────────────────────────────────

def test_every_posture_has_a_measured_cost_with_a_stated_basis():
    """A posture with no cost cannot be budgeted. Adding one without a cost must
    fail loudly rather than inherit someone else's number silently."""
    from app.pipeline.v2.posture import _ROUND_COST, round_cost
    for p in Posture:
        rc = round_cost(p)
        assert rc.p50_s > 0 and rc.p90_s >= rc.p50_s, (p, rc)
        assert rc.basis, f"{p} has a cost with no stated basis"
        assert rc.basis.startswith(("BOOTSTRAP", "MEASURED")), rc.basis
    assert set(_ROUND_COST) == {p.value for p in Posture}


def test_unknown_posture_raises_rather_than_defaulting():
    """A default would price a new posture at another posture's number and never
    say so -- the silent-default shape."""
    from app.pipeline.v2.posture import round_cost
    with pytest.raises(KeyError, match="no measured round cost"):
        round_cost("teleport")


def test_bootstrap_costs_are_labelled_as_v1_priors_not_v2_predictions():
    """The claim these once encoded -- "a rag round is cheaper than a no-tool
    round" -- was an off-by-one artifact and is withdrawn. What this asserts now
    is only that nobody has quietly re-labelled a v1 prior as a measurement of
    v2."""
    from app.pipeline.v2.posture import _ROUND_COST
    v1_priors = [k for k, v in _ROUND_COST.items() if v.basis.startswith("BOOTSTRAP")]
    assert len(v1_priors) >= 4, "postures silently promoted from prior to measurement"


def test_spendable_falls_back_to_the_measured_table():
    """A caller that supplies no per-round cost still gets a real number, not 0."""
    st = RoundState(round_index=3, open_gaps=(_gap(),), gaps_open_history=(1, 1),
                    budget=Budget(remaining_s=100.0, remaining_c=10.0),
                    next_round_cost_s=0.0, acting_cost_s=0.0, validate_cost_s=9.6)
    assert spendable(st) is True
    broke = RoundState(round_index=3, open_gaps=(_gap(),), gaps_open_history=(1, 1),
                       budget=Budget(remaining_s=5.0, remaining_c=10.0),
                       next_round_cost_s=0.0, acting_cost_s=0.0, validate_cost_s=9.6)
    assert spendable(broke) is False   # 9.2 + 10.0 > 5.0, from the table


# ── the overrun: relaxing a constraint when close and converging ────────────

def _converging_gap():
    """One gap, something came back on the last attempt."""
    return _gap(opened=2, attempts=[
        Attempt(2, tool="rag", query="unit definition", returned_payload=False),
        Attempt(3, tool="rag", query="H2019 HR units per recipient", returned_payload=True),
    ])


def _broke(band=8.0, drawn=0.0):
    return Budget(remaining_s=4.0, remaining_c=5.0, band_s=band, band_drawn_s=drawn)


def test_overrun_fires_when_close_and_converging():
    """The whole point: out of promise budget, one gap, evidence returning ->
    buy the round from the band and SAY so."""
    st = _state(round_index=4, open_gaps=(_converging_gap(),),
                gaps_open_history=(2, 1), budget=_broke())
    assert spendable(st) is False           # promise budget is gone
    d = select(st)
    assert d.overran is True
    assert d.directive is Directive.CLOSE
    assert d.because.startswith("OVERRUN:")


def test_overrun_refuses_on_a_feeling_with_no_evidence():
    """MUTATION-CHECKED: nothing came back, so there is no evidence the next
    round closes it. Self-reported confidence must never authorise spend -- it
    is the one signal produced by the thing being judged."""
    nothing_back = _gap(opened=2, attempts=[
        Attempt(2, tool="rag", returned_payload=False),
        Attempt(3, tool="rag", returned_payload=False)])
    st = _state(round_index=4, open_gaps=(nothing_back,),
                gaps_open_history=(1, 1), budget=_broke())
    ok, why = may_overrun(st)
    assert ok is False and "not converging" in why
    assert select(st).overran is False


def test_overrun_refuses_when_gaps_are_still_RISING():
    """Rising gaps means discovery, not convergence -- measured at 25.4% closure
    versus ~41% flat. A turn still finding new holes is not one round from done."""
    st = _state(round_index=4, open_gaps=(_converging_gap(),),
                gaps_open_history=(1, 2), budget=_broke())
    ok, why = may_overrun(st)
    assert ok is False and "not converging" in why


def test_overrun_refuses_when_the_turn_is_wide_open():
    g1, g2, g3 = _converging_gap(), _gap("G2"), _gap("G3")
    st = _state(round_index=4, open_gaps=(g1, g2, g3),
                gaps_open_history=(3, 3), budget=_broke())
    assert may_overrun(st)[0] is False


def test_overrun_is_BOUNDED_by_the_band_and_by_what_is_already_drawn():
    """The band is a budget, not a licence. Once drawn, it is gone."""
    st_ok = _state(round_index=4, open_gaps=(_converging_gap(),),
                   gaps_open_history=(2, 1), budget=_broke(band=20.0, drawn=0.0))
    assert may_overrun(st_ok)[0] is True
    st_spent = _state(round_index=4, open_gaps=(_converging_gap(),),
                      gaps_open_history=(2, 1), budget=_broke(band=20.0, drawn=19.0))
    ok, why = may_overrun(st_spent)
    assert ok is False and "band cannot fund it" in why


def test_the_promise_itself_is_never_edited_by_an_overrun():
    """An overrun draws on the BAND. The promise stays frozen, so the
    attestation still records kept=False / in_band=True -- we exceeded what we
    promised, stayed inside the stated tolerance, and said why. A governor that
    could edit the promise would resolve every breach by moving the target."""
    st = _state(round_index=4, open_gaps=(_converging_gap(),),
                gaps_open_history=(2, 1), budget=_broke())
    before = st.budget.remaining_s
    select(st)
    assert st.budget.remaining_s == before      # frozen: nothing was mutated


def test_overran_survives_the_whole_chain_decision_to_row():
    """END-TO-END on the fields, not just the decision. `overran` existed in
    Decision and nowhere else -- decided and discarded, the defect this module
    was built to hunt. This asserts every hop carries it."""
    import inspect
    from app.pipeline.v2 import ledger as _ledger
    st = _state(round_index=4, open_gaps=(_converging_gap(),),
                gaps_open_history=(2, 1), budget=_broke())
    d = select(st)
    assert d.overran is True                                  # 1. decided
    c = compare("search", st)
    assert c["v2_overran"] is True                            # 2. carried
    src = inspect.getsource(_ledger.write_rounds)
    assert "overran" in src and "v2_overran" in src           # 3. written


# ── the question is the gap ─────────────────────────────────────────────────

def test_round_one_explores_because_the_question_is_the_gap():
    """An empty ledger means nothing has been NAMED, not that nothing is needed.
    Before this, v2 said COMMUNICATE on round 1 before looking at anything --
    50 of 87 divergences."""
    d = select(_state(round_index=1, open_gaps=(), gaps_open_history=(0,),
                      question="what is the timely filing deadline?"))
    assert d.posture is Posture.EXPLORE
    assert d.gap_targeted == "G0"


def test_the_root_gap_is_not_invented_when_gaps_already_exist():
    """react has named something specific; the root gap must not displace it."""
    d = select(_state(round_index=3, open_gaps=(_gap("G7"),),
                      gaps_open_history=(1, 1), question="a question"))
    assert d.gap_targeted == "G7"


def test_no_question_means_no_root_gap():
    """MUTATION-CHECKED: the seed must depend on there BEING a question, not fire
    unconditionally -- otherwise it manufactures work on a turn with nothing to
    do and COMMUNICATE becomes unreachable."""
    d = select(_state(round_index=1, open_gaps=(), gaps_open_history=(0,), question=""))
    assert d.posture is Posture.COMMUNICATE


def test_the_root_gap_carries_the_question_text_and_high_importance():
    from app.pipeline.v2.posture import ROOT_GAP_ID, seed_root_gap
    g = seed_root_gap("  how do i appeal a carc 24 denial?  ")
    assert g.gap_id == ROOT_GAP_ID
    assert g.text == "how do i appeal a carc 24 denial?"
    assert g.importance == "high"      # it is the whole turn
    assert g.attempted_by == ()


def test_tools_offered_and_tool_called_reach_the_write():
    """END-TO-END on the fields. tools_offered was specified and never written --
    a column decided and discarded, the third such field in one night. This
    asserts the write actually binds all four."""
    import inspect
    from app.pipeline.v2 import ledger as _ledger
    src = inspect.getsource(_ledger.write_rounds)
    for col in ("tools_offered", "tool_called", "declared_latency_ms", "declared_version"):
        assert col in src, f"{col} is in the table and not in the INSERT"
    for param in ('"offered"', '"tool_called"', '"decl_ms"', '"decl_ver"'):
        assert param in src, f"{param} bound in SQL but not supplied"


def test_the_settle_enrichment_joins_the_trace_by_round():
    """tool_called is knowable only AFTER the round runs, so the pre-round hook
    cannot supply it. The join happens at settle, from ctx.react_trace_rounds."""
    import inspect
    from app.pipeline import orchestrator as _orch
    src = inspect.getsource(_orch.run_pipeline)
    assert "react_trace_rounds" in src and "tool_called" in src


# ── 2026-09-11: three defects the persisted decision_inputs exposed ─────────

def test_zero_to_one_gap_is_discovery_not_a_rising_trend():
    """history [0,1] fired trend_increasing on essentially every two-round
    turn in dev — asking for another round on turns that had only just named
    their own question. A trend needs something to have been a trend FROM,
    and zero is not that."""
    from app.pipeline.v2.posture import trend, Trend
    assert trend((0, 1)) is Trend.FLAT
    assert trend((0, 3)) is Trend.FLAT
    # a real rise still reads as one
    assert trend((1, 2)) is Trend.INCREASING
    assert trend((2, 1)) is Trend.DECREASING


def test_the_increasing_branch_cannot_spend_what_it_cannot_afford():
    """It returned "buy another round" BEFORE the budget was consulted. Live
    on q12/ab-c311023248 it asked for a round with 0.0s remaining and a 20.4s
    shortfall; only the exit mode stopped it, and the exit mode doing all the
    work is what kept it invisible.

    Discovering that you are lost is not a reason you can afford to keep
    walking."""
    from dataclasses import replace
    from app.pipeline.v2.posture import (
        Budget, Gap, Posture, RoundState, select, spendable)
    broke = RoundState(
        round_index=3, open_gaps=(Gap(gap_id="S1", text="a", opened_round=1),
                                  Gap(gap_id="S2", text="b", opened_round=2)),
        gaps_open_history=(1, 2), budget=Budget(remaining_s=0.0, remaining_c=0.0),
        next_round_cost_s=10.4, acting_cost_s=10.0, validate_cost_s=9.6,
        question="q")
    assert not spendable(broke)
    assert select(broke).posture is not Posture.EXPLORE
    # ...and with budget, the same rising trend still explores
    rich = replace(broke, budget=Budget(remaining_s=120.0, remaining_c=0.0))
    d = select(rich)
    assert d.posture is Posture.EXPLORE
    assert d.branch in ("trend_increasing", "gap_affordable")


def test_gap_age_accumulates_and_a_fresh_gap_is_age_zero():
    """WITHDRAWN as a defect after checking. age=0 on every observed row was a
    gap genuinely opened that round, not a broken clock. Pinned so the next
    reader does not re-raise it."""
    from app.pipeline.v2.posture import Gap
    assert Gap(gap_id="S1", text="x", opened_round=1).age(3) == 2
    assert Gap(gap_id="S2", text="y", opened_round=2).age(2) == 0


def test_explain_reports_the_state_the_decision_was_MADE_on():
    """select() seeded the root gap on a LOCAL variable, so explain() reported
    the unseeded state it was handed. Rows showed `open gaps 0` and
    `worth_spending None` beside `branch=gap_affordable` and
    `because="closing G0"` — a gap the gap list did not contain.

    An emit faithful to its input and wrong about the decision is worse than no
    emit: it invites you to argue with a state that never decided anything.
    """
    from app.pipeline.v2.posture import (
        ROOT_GAP_ID, Budget, RoundState, explain, select)
    empty = RoundState(
        round_index=1, open_gaps=(), gaps_open_history=(),
        budget=Budget(remaining_s=31.0, remaining_c=0.0),
        next_round_cost_s=10.4, acting_cost_s=10.0, validate_cost_s=9.6,
        question="what are the timely filing deadlines")
    d = select(empty)
    e = explain(empty, d)
    assert e["open_gaps"], "explain reports zero gaps on a decision made with G0"
    assert e["open_gaps"][0]["id"] == ROOT_GAP_ID
    # the gate it reports must match the branch that was taken
    if d.branch == "gap_affordable":
        assert e["worth_spending"] is not None, (d.branch, e["worth_spending"])


def test_alternatives_never_announces_zero_unreachable_gaps():
    """ALTERNATIVES fired on q12/ab-e491c9a27a saying "0 gap(s) unreachable:
    offer a route rather than a shortfall" — a posture reporting zero instances
    of the thing it exists for.

    _stuck_count() counted only stuck(); alternatives_worth_it() also admitted
    attempted-and-returned-nothing. Two populations of one list, counted by two
    functions, disagreeing about the same state."""
    from app.pipeline.v2.posture import (
        Attempt, Budget, Gap, Posture, RoundState, explain, select)
    tried_nothing_back = Gap(
        gap_id="S1", text="a", opened_round=1,
        attempted_by=(Attempt(round_index=1, tool="rag", query="q",
                              returned_payload=False),))
    st = RoundState(
        round_index=2, open_gaps=(tried_nothing_back,), gaps_open_history=(1, 1),
        budget=Budget(remaining_s=15.0, remaining_c=0.0),
        next_round_cost_s=10.4, acting_cost_s=10.0, validate_cost_s=9.6,
        alternatives_cost_s=5.0, question="q")
    d = select(st)
    assert d.posture is Posture.ALTERNATIVES, d.posture
    assert "0 gap(s)" not in d.because, d.because
    assert explain(st, d)["unreachable_gaps"] >= 1


def test_the_overrun_names_where_the_money_actually_came_from():
    """It read "drawing 10.4s from a 0.0s band" on q09/ab-82c6105465 — a round
    funded from 17.6s of UNRESERVED PROMISE TIME. spendable() had refused only
    because it also reserves the cost of ACTING on what the round finds, and an
    overrun deliberately does not.

    A reason that names the wrong source is a reason you cannot audit."""
    from dataclasses import replace
    from app.pipeline.v2.posture import (
        Attempt, Budget, Gap, RoundState, may_overrun)
    g = Gap(gap_id="S1", text="a", opened_round=1,
            attempted_by=(Attempt(round_index=2, tool="rag", query="q",
                                  returned_payload=True),))
    st = RoundState(
        round_index=3, open_gaps=(g,), gaps_open_history=(2, 1),
        budget=Budget(remaining_s=17.56, remaining_c=0.0, band_s=8.0),
        next_round_cost_s=10.4, acting_cost_s=10.0, validate_cost_s=9.6,
        question="q")
    ok, why = may_overrun(st)
    assert ok
    assert "unreserved promise time" in why, why
    assert "0.0s band" not in why
    # and when the promise really is spent, it must name the BAND
    # 4.0s of promise left + an 8.0s band funds a 10.4s round; 2.0 + 8.0 does
    # NOT, and the machine correctly refuses that one — checked both ways so
    # this test cannot pass by picking a friendly number.
    partly = replace(st, budget=Budget(remaining_s=4.0, remaining_c=0.0, band_s=8.0))
    ok2, why2 = may_overrun(partly)
    assert ok2 and "of the 8.0s band" in why2, why2
    spent = replace(st, budget=Budget(remaining_s=2.0, remaining_c=0.0, band_s=8.0))
    ok3, why3 = may_overrun(spent)
    assert not ok3 and "even the band cannot fund it" in why3, why3
