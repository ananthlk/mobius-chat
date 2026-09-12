"""The narrow v2 executor — the decision varies, nothing else does.

These assert the two properties that make an arm comparable: every posture
produces an action (an executor may not decline), and the executor's vocabulary
is exactly react_loop's — verified against react_loop.py itself, not against a
copy of the strings.
"""

import ast
import pathlib

import pytest

from app.pipeline.v2 import executor as ex
from app.pipeline.v2.posture import Decision, ExitMode, Posture


# ── purity, the same bar posture.py is held to ──────────────────────────────

def test_the_executor_is_pure():
    """No clock, no DB, no env. An arm that reads a global cannot be replayed
    from its stored row, which is the only way a disputed result is settled
    without re-running traffic that no longer exists."""
    tree = ast.parse(pathlib.Path("app/pipeline/v2/executor.py").read_text())
    banned = {"time", "datetime", "os", "random", "requests", "psycopg2"}
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    assert not (imported & banned), imported & banned


# ── totality: an executor may not decline ───────────────────────────────────

@pytest.mark.parametrize("posture", list(Posture))
def test_every_posture_produces_a_real_directive(posture):
    """shadow.map_directive may return None; this may not. The turn is running
    and something has to happen next."""
    a = ex.decide(Decision(posture, "because"))
    assert a.directive in ex.V1_DIRECTIVES, (posture, a.directive)


def test_an_unmapped_posture_ships_and_SAYS_SO():
    """A posture the table doesn't cover must not be a silent `complete`.
    Shipping the turn is right; hiding that the table is behind the machine is
    not."""
    class Fake(str):
        value = "reframe_and_pivot"
    a = ex.decide(Decision(Fake("x"), "because"))
    assert a.directive == ex.COMPLETE
    assert "UNMAPPED POSTURE" in a.because


# ── the COMMUNICATE split ───────────────────────────────────────────────────

def test_budget_exit_ships_with_the_notice_and_complete_ships_clean():
    d = Decision(Posture.COMMUNICATE, "done")
    assert ex.decide(d, ExitMode.COMPLETE).directive == ex.COMPLETE
    assert ex.decide(d, ExitMode.BUDGET).directive == ex.FINALIZE


def test_CAPABILITY_never_takes_the_finalize_path():
    """THE load-bearing test of this module.

    v1's finalize path appends "these claims could not be verified" AND is the
    path that offers continuation. A CAPABILITY exit means no amount of waiting
    produces the answer. Routing it through finalize would attach a quality
    warning to a scope limit and then invite the person to wait for it.

    It is one enum row away at all times, which is why this is a test and not
    a comment.
    """
    a = ex.decide(Decision(Posture.COMMUNICATE, "no tool reaches this"),
                  ExitMode.CAPABILITY)
    assert a.directive == ex.COMPLETE
    assert a.directive != ex.FINALIZE


# ── the known-wrong prompt, declared rather than discovered ─────────────────

def test_the_prompt_mismatch_is_RECORDED_not_hidden():
    """EXPLORE extends into v1's REMEDIATION prompt because v1 has no
    extend-to-gather prompt at this branch. Reproduced deliberately — fixing it
    would vary the prompt, and a divergence could no longer be attributed to
    the decision. It must arrive on the row."""
    assert ex.decide(Decision(Posture.EXPLORE, "thin")).prompt_mismatch
    # NARROW is a WRAP-UP and no longer extends at all — nothing to mismatch.
    assert ex.decide(Decision(Posture.NARROW, "nothing worth buying")).prompt_mismatch is None
    assert ex.decide(Decision(Posture.COMMUNICATE, "done")).prompt_mismatch is None


def test_ALTERNATIVES_records_that_its_output_is_never_generated():
    """v2 chooses to offer routes; v1 has no alternatives prompt, so they are
    decided and never rendered — the person saw a normal answer.

    An instruction whose output nothing reads is the worst shape in this
    program's catalogue. The comparison must not be able to credit v2 with an
    answer nobody saw.
    """
    a = ex.decide(Decision(Posture.ALTERNATIVES, "3 gaps unreachable"))
    assert a.directive == ex.COMPLETE
    assert a.prompt_mismatch == ex.ALTERNATIVES_NOT_RENDERED


# ── the vocabulary is react_loop's, checked against react_loop ──────────────

def test_the_three_directives_are_the_ones_react_loop_actually_branches_on():
    """Read from react_loop.py's own comparisons, over the AST.

    A copy of three strings in a test proves the copy matches itself. If the
    loop grows a fourth branch or renames one, this fails loudly instead of the
    executor emitting a directive nothing acts on — a producer with no
    consumer, dressed as a decision.
    """
    tree = ast.parse(pathlib.Path("app/pipeline/react_loop.py").read_text())
    compared = set()
    for n in ast.walk(tree):
        if not isinstance(n, ast.Compare) or not isinstance(n.left, ast.Name):
            continue
        if n.left.id != "_pp_directive":
            continue
        for c in n.comparators:
            if isinstance(c, ast.Constant) and isinstance(c.value, str):
                compared.add(c.value)
    assert compared, "no _pp_directive comparisons found — did the branch move?"
    assert compared <= set(ex.V1_DIRECTIVES), compared - set(ex.V1_DIRECTIVES)


def test_extend_is_the_only_directive_that_continues():
    assert ex.decide(Decision(Posture.EXPLORE, "x")).continues
    assert not ex.decide(Decision(Posture.COMMUNICATE, "x")).continues
    assert not ex.decide(Decision(Posture.COMMUNICATE, "x"), ExitMode.BUDGET).continues


def test_overran_and_gap_ride_through_to_the_action():
    """Decided-and-discarded is a defect this program has already shipped once:
    `overran` was computed and never reached a column."""
    d = Decision(Posture.EXPLORE, "one more", gap_targeted="G3", overran=True)
    a = ex.decide(d)
    assert a.overran is True and a.gap_targeted == "G3"


# ── the exit mode dominates EVERY posture ───────────────────────────────────

@pytest.mark.parametrize("posture", [Posture.EXPLORE, Posture.NARROW,
                                     Posture.VALIDATE, Posture.ALTERNATIVES])
def test_no_posture_can_extend_past_a_budget_or_capability_exit(posture):
    """Found by printing the whole table, not by reading the code.

    `EXPLORE + BUDGET -> extend` spends a round the budget says is not there.
    `EXPLORE + CAPABILITY -> extend` chases an answer no tool can reach. Both
    were live until the table was printed.

    "select() would never return EXPLORE when the budget is gone" is true today
    and is exactly the reasoning that produced FRAME.
    """
    d = Decision(posture, "one more would close it")
    for mode in (ExitMode.BUDGET, ExitMode.ERROR, ExitMode.CAPABILITY):
        a = ex.decide(d, mode)
        assert not a.continues, (posture, mode, a.directive)


@pytest.mark.parametrize("posture", list(Posture))
def test_capability_ships_clean_from_every_posture(posture):
    """The scope limit must never take the path that appends a quality warning
    and offers continuation — from ANY posture, not just COMMUNICATE."""
    a = ex.decide(Decision(posture, "no tool reaches this"), ExitMode.CAPABILITY)
    assert a.directive == ex.COMPLETE, (posture, a.directive)


def test_a_posture_exit_contradiction_is_recorded_on_the_row():
    """A disagreement inside my own module. Stopping wins, and the loser is
    written down — an overridden decision that leaves no trace is
    indistinguishable from one that was never made."""
    a = ex.decide(Decision(Posture.EXPLORE, "one more"), ExitMode.BUDGET)
    assert "overrode it" in a.because and "explore" in a.because


def test_the_two_branches_react_loop_names_are_both_still_there():
    """`complete` is react_loop's fall-through and has no comparison; extend
    and finalize are explicit. A rename of either must fail here rather than
    leave the executor emitting a directive nothing acts on."""
    tree = ast.parse(pathlib.Path("app/pipeline/react_loop.py").read_text())
    compared = {c.value for n in ast.walk(tree)
                if isinstance(n, ast.Compare) and isinstance(n.left, ast.Name)
                and n.left.id == "_pp_directive"
                for c in n.comparators
                if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    assert {ex.EXTEND, ex.FINALIZE} <= compared, compared


# ── the wiring: what the pure module cannot assert about itself ─────────────

def _react_src():
    return pathlib.Path("app/pipeline/react_loop.py").read_text()


def test_the_executor_branch_is_gated_on_the_ROUTED_arm_not_on_sampling():
    """A turn that flipped arms mid-flight would be in both populations and in
    neither. The gate must read the arm assigned at POST — never a coin flip,
    a round index, or a rate."""
    tree = ast.parse(_react_src())
    gates = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "getattr"
             and any(isinstance(a, ast.Constant) and a.value == "orchestrator_version"
                     for a in n.args)]
    assert gates, "the executor is not gated on the routed arm"


def test_v1s_directive_is_never_erased_on_a_v2_turn():
    """The row must still record what v1 WOULD have done. An arm that erases
    its counterfactual cannot be compared with anything — and the whole point
    of routing one turn to one arm is that the other arm's answer is a
    prediction, not a second execution."""
    src = _react_src()
    i = src.index("STEP 2: v2 DECIDES")
    block = src[i:i + 5000]
    assert "_pp_directive = _v2_act.directive" in block
    # v1_directive is written by compare() BEFORE this block and must not be
    # reassigned inside it
    assert 'v1_directive"] =' not in block


def test_applied_is_set_at_the_substitution_not_at_the_end_of_the_block():
    """A raise in the recording leaves v2's directive already running. Marking
    that turn 'degraded to v1' would file a v2 turn in v1's population, which
    is worse than the crash. Ordering, not existence — a hardened branch below
    an earlier return is dead code."""
    src = _react_src()
    i = src.index("STEP 2: v2 DECIDES")
    block = src[i:i + 5000]
    assert block.index("_v2_applied = True") < block.index('"v2_applied"')


def test_the_arm_reaches_every_round_row():
    """Without it every row lands as 'v1', the two populations become one, and
    the comparison is v1 against itself — which agrees 100% of the time and
    reads like a success."""
    orch = pathlib.Path("app/pipeline/orchestrator.py").read_text()
    assert '_row["orchestrator_version"] = _arm' in orch


def test_the_split_is_in_the_deploy_allowlist():
    """SET_ENV_VARS is an allowlist, not a passthrough. A var absent from it is
    simply not in the container, silently — the defect that made the shadow
    flag a no-op on its first deploy."""
    assert "MOBIUS_V2_PCT=" in pathlib.Path("scripts/deploy.sh").read_text()


def test_the_default_split_is_zero():
    """Merging the executor must not move a single turn. Turning it on is one
    number; turning it off is the same number."""
    orch = pathlib.Path("app/pipeline/orchestrator.py").read_text()
    assert 'os.environ.get("MOBIUS_V2_PCT", "0")' in orch
    from app.pipeline.v2.routing import assign
    assert all(assign(f"cid-{i}", 0) == "v1" for i in range(200))


def test_the_substitution_is_WRITTEN_and_READ_not_decided_and_discarded():
    """Three fields were set on the comparison row and written nowhere — the
    third instance in this program and the second in my own module (`overran`
    was the first). A decision that leaves no row is indistinguishable from
    one that was never made.

    Producer, write, reader — all three, or it is the same defect.
    """
    react = _react_src()
    ledg = pathlib.Path("app/pipeline/v2/ledger.py").read_text()
    api = pathlib.Path("app/api/ab_harness.py").read_text()
    for producer, column, reader in (
        ('"v2_directive_applied"', "applied_directive", '"applied_directive"'),
        ('"v2_applied"', "v2_applied", '"v2_applied"'),
        ('"v2_prompt_mismatch"', "prompt_mismatch", '"prompt_mismatch"'),
    ):
        assert producer in react, f"no producer for {column}"
        assert column in ledg, f"{column} not written"
        assert reader in api, f"no reader for {column}"


# ── the runaway, as a regression ────────────────────────────────────────────

def test_wrapup_postures_never_extend():
    """THE runaway, 2026-09-11: 98 rounds on one turn, 420s against a 31s
    promise, v1 saying `finalize` every round and v2 overriding it to `extend`.

    posture.py returns NARROW and ALTERNATIVES from exactly one branch — the
    one where worth_spending() ALREADY returned None. Mapping either to "buy
    another round" inverts the decision that was just made. The first version
    of this table mapped the NOUN; this asserts the BRANCH.
    """
    for posture in (Posture.NARROW, Posture.ALTERNATIVES):
        a = ex.decide(Decision(posture, "gaps open but none worth buying"))
        assert not a.continues, posture
        assert a.directive == ex.COMPLETE, (posture, a.directive)


def test_only_the_two_affordability_checked_postures_can_extend():
    """EXPLORE and VALIDATE are the only postures select() returns after an
    affordability check. Anything else extending means the fuse is the only
    thing between a decision and an unbounded spend."""
    can = {p for p in Posture if ex.decide(Decision(p, "x")).continues}
    assert can == {Posture.EXPLORE, Posture.VALIDATE}, can


def test_the_ceiling_stops_an_extend_even_when_every_other_signal_says_go():
    """The fuse, checked BEFORE the exit mode so it holds when the exit mode is
    WRONG — which is precisely the case that produced the runaway: select()
    counted every open gap and said NARROW, exit_mode() counted only material
    gaps and said COMPLETE, and nothing stopped the loop.

    v1's max_it grows by one on every extend, so react_loop cannot stop me —
    I am the one telling it to continue. The bound lives with the decision.
    """
    d = Decision(Posture.EXPLORE, "one more would close it")
    assert ex.decide(d, ExitMode.COMPLETE, extensions_used=0).continues
    at_ceiling = ex.decide(d, ExitMode.COMPLETE,
                           extensions_used=ex.MAX_V2_EXTENSIONS)
    assert not at_ceiling.continues
    assert "CEILING" in at_ceiling.because
    assert "fuse, not by the decision" in at_ceiling.because


def test_the_population_mismatch_is_filed_in_code_not_only_in_a_doc():
    """select() counts all open_gaps; exit_mode() counts only material ones.
    Same state, same round, two answers — a decision-core defect, deliberately
    not fixed in the change that stopped the runaway, because the right fix
    changes what every recorded exit mode has meant.

    An objection you agree with and do not act on is worse than one you argue
    with. This is the least: making it impossible to rediscover."""
    assert "min_importance" in ex.POPULATION_MISMATCH
    assert "two answers" in ex.POPULATION_MISMATCH


def test_the_ceiling_is_actually_WIRED_at_the_call_site():
    """A ceiling the caller never supplies defaults to 0 forever and the guard
    is decorative — the 'gate with no caller' shape. v1's own
    _pp_extension_rounds_used is the counter; it must be passed."""
    src = _react_src()
    i = src.index("STEP 2: v2 DECIDES")
    assert "extensions_used=_pp_extension_rounds_used" in src[i:i + 5000]


def test_the_second_hook_of_a_round_is_MERGED_not_dropped():
    """TWO hooks write one round: pre-round records the posture, post-round
    records what the round did and what the executor substituted. Same
    round_index — so ON CONFLICT DO NOTHING silently dropped the second, and
    the executor's fields never reached the table while the logs showed it
    firing every turn.

    COALESCE(EXCLUDED, existing) on every nullable column so the later write
    fills in what it knows and cannot erase what the earlier one knew;
    booleans OR rather than overwrite — a round that was executed by v2 cannot
    become one that wasn't.
    """
    # Over the SQL with its -- comments stripped. My own comment here names
    # DO NOTHING to explain it, and a text search matched that instead of the
    # statement — the FOURTH time in this program a test has read prose and
    # called it a program.
    import re
    ledg = pathlib.Path("app/pipeline/v2/ledger.py").read_text()
    sql = "\n".join(re.sub(r"--.*$", "", ln) for ln in ledg.splitlines())
    assert "DO NOTHING" not in sql, "the second hook's row is still dropped"
    assert "DO UPDATE SET" in sql
    assert "applied_directive = COALESCE(EXCLUDED.applied_directive" in sql
    assert "v2_applied        = turn_rounds.v2_applied OR EXCLUDED.v2_applied" in sql


def test_the_decision_inputs_are_emitted_written_and_read():
    """Ananth, 2026-09-11: "there is no way in the AI world for anyone to
    understand what the model is doing, and the thinking is really the only
    way."

    The row carried `rationale` — one sentence, a CONCLUSION — and asserted
    "gaps open but none worth buying" while its own gaps_opened column read []
    because compare() never emitted it. All three ends, or it is the same
    defect the other four were.
    """
    shadow = pathlib.Path("app/pipeline/v2/shadow.py").read_text()
    ledg = pathlib.Path("app/pipeline/v2/ledger.py").read_text()
    api = pathlib.Path("app/api/ab_harness.py").read_text()
    assert '"v2_decision_inputs": explain(state, d)' in shadow, "no producer"
    assert '"v2_decision_inputs"]' in ledg and '"inputs":' in ledg, "not written"
    assert '"decision_inputs": r["decision_inputs"]' in api, "no reader"
    # and the two the ledger had been reading from nobody
    assert '"gaps_opened": [g.gap_id for g in state.open_gaps]' in shadow


def test_explain_calls_the_predicates_rather_than_reimplementing_them():
    """A second implementation of the decision's own logic would be a second
    AUTHOR of the decision it claims to report — and it would drift first
    exactly where the decision is most interesting."""
    import ast
    tree = ast.parse(pathlib.Path("app/pipeline/v2/posture.py").read_text())
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "explain")
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    for predicate in ("spendable", "worth_spending", "converging", "may_overrun",
                      "alternatives_worth_it", "validate_worth_it", "trend",
                      "exit_mode", "stuck", "distinct_levers"):
        assert predicate in called, f"explain() does not call {predicate}"


def test_every_select_branch_is_named():
    """The same posture comes out of different branches for opposite reasons.
    A row saying only "explore" cannot be argued with, and a decision you
    cannot argue with cannot be tuned."""
    import ast
    from app.pipeline.v2 import posture as P
    tree = ast.parse(pathlib.Path("app/pipeline/v2/posture.py").read_text())
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "select")
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    for r in returns:
        kws = {k.arg for k in getattr(r.value, "keywords", [])}
        assert "branch" in kws, ast.unparse(r)[:90]
    assert P.BRANCH_SKIPS_BUDGET


def _framing_block():
    """The framing hook's source, sliced to its OWN end rather than to a magic
    character count. A fixed 3200-char window stopped inside the comment and
    silently tested nothing — an assertion over an empty region passes."""
    src = _react_src()
    i = src.index("FRAMING DECISION POINT")
    j = src.index('logger.warning("[v2.frame] hook failed', i)
    block = src[i:j]
    assert len(block) > 800, "the slice collapsed — the hook moved or was renamed"
    # COMMENTS STRIPPED. Twice in this one test the assertion matched my own
    # prose: first `_v2f_inputs` on "inputs =", then the word "break" inside
    # the comment explaining why there is no break. That is the sixth time in
    # this program a test has read prose and called it a program, and the
    # general fix — not the sixth special case — is to stop handing it prose.
    import re as _re
    code = "\n".join(_re.sub(r"#.*$", "", ln) for ln in block.splitlines())
    assert "_v2fp.select(" in code, "the slice lost the hook body"
    return code


def test_the_framing_hook_acts_ONLY_through_the_gated_stop():
    """CONTRACT CHANGED 2026-09-11 — the hook was observation-only and now
    decides. This test was `..._does_NOT_act` and is rewritten rather than
    deleted, because a gate quietly removed when its subject changes is how a
    rule stops existing without anyone deciding to remove it.

    The new contract: the hook's ONLY effect is _finalize_response()+return,
    inside the three-constraint gate. It still may not touch the loop's other
    control variables — no max_it, no directive substitution, no continue.
    """
    block = _framing_block()
    assert "_v2fp.select(" in block and "_v2fp.explain(" in block, "records nothing"
    import re
    # the ONE sanctioned effect
    assert "_finalize_response(" in block and "return" in block
    # ...and nothing else that steers the loop
    for control in ("_pp_directive", "max_it", "tool", "inputs", "is_complete"):
        assert not re.search(rf"(?<![\w])(?<!\.){control}\s*(?:=|\+=)(?!=)", block), \
            f"the framing hook assigns {control!r}"
    assert not re.search(r"(?<![\w])continue(?![\w])", block), \
        "the framing hook continues the loop"


def test_framing_never_overwrites_the_pre_round_decision():
    """Both on one row, deliberately: the DIFFERENCE between what the governor
    decided before the model spoke and what it would decide once the gaps exist
    IS the value of moving the hook, and it cannot be read if one replaces the
    other."""
    block = _framing_block()
    assert '_v2f_row["v2_framing_inputs"]' in block
    assert '"v2_decision_inputs"' not in block, "the framing hook clobbers the pre-round row"
    ledg = pathlib.Path("app/pipeline/v2/ledger.py").read_text()
    assert '"v2_framing_inputs"]' in ledg and '"framing":' in ledg
    api = pathlib.Path("app/api/ab_harness.py").read_text()
    assert '"framing_inputs": r["framing_inputs"]' in api


def test_the_framing_hook_shares_the_other_hooks_clock():
    """A third clock would make the three rows incomparable — the whole point
    is to diff two decisions about the same moment."""
    assert "_pp_time_mod.monotonic() - _pp_turn_start" in _framing_block()


def test_absent_json_is_SQL_NULL_not_a_jsonb_null():
    """json.dumps(None) is the STRING "null", which CASTs to a JSONB null — a
    legal value that count() counts. It reported all 52 rounds as having
    framing data when 17 had none.

    A JSONB null and a missing row are indistinguishable to every aggregate,
    which is the "could-not-check reported as checked-false" shape this
    program has found eight times. In the ledger that exists to prevent it.
    """
    import json as _json
    import app.pipeline.v2.ledger as L
    captured = {}

    def fake_execute(sql, db, params=None):
        captured.update(params or {})
        return {}

    import app.db_client as dbc
    orig = dbc.db_execute
    dbc.db_execute = fake_execute
    try:
        L.write_rounds("cid", [{"round": 1}])          # nothing computed
    finally:
        dbc.db_execute = orig
    assert captured.get("inputs") is None, captured.get("inputs")
    assert captured.get("framing") is None, captured.get("framing")
    assert captured.get("inputs") != _json.dumps(None)


def test_framing_stop_is_gated_on_all_three_constraints():
    """Graduated to DECIDING. Three constraints, each earned today:

    1. Never on round 1 — stopping before any tool returned answers from zero
       evidence, and this afternoon's budget bug made the machine say NARROW
       at round 1 on every turn. Live then, it would have ended every turn
       before it started.
    2. Only the routed v2 arm.
    3. Revertible without a deploy — an env var, ~90 seconds.
    """
    block = _framing_block()
    assert 'os.environ.get("MOBIUS_V2_FRAME_DECIDES"' in block
    assert 'getattr(ctx, "orchestrator_version", "v1") == "v2"' in block
    assert "rn > 1" in block, "the round-1 floor is gone"


def test_framing_stop_uses_the_SAME_inverse_map_as_the_executor():
    """One posture -> directive table, not a second. A second would disagree
    with the first exactly where the decision is most interesting."""
    block = _framing_block()
    assert "_v2fx.decide(" in block
    assert "_v2f_act.continues" in block
    # and it carries the fuse
    assert "extensions_used=_pp_extension_rounds_used" in block


def test_framing_stop_never_finalises_an_empty_answer():
    """Finalising an empty answer turns a governor decision into a blank
    screen — worse than the round it is trying to save. The governor decides
    WHEN to stop; it never decides WHAT to say."""
    block = _framing_block()
    i = block.index("if not _v2f_act.continues:")
    tail = block[i:]
    assert "if _v2f_answer:" in tail, "no empty-answer guard"
    assert tail.index("if _v2f_answer:") < tail.index("_finalize_response("), \
        "the guard is after the finalise — it cannot guard anything"
    assert "_running_answer or thought" in tail, \
        "the answer is not the model's own running answer"


def test_a_governor_stop_is_RECORDED_on_the_row():
    """Without it the row is indistinguishable from a turn that simply ran out
    of rounds — the could-not-check-vs-checked-false shape, and this time it
    would hide the only behaviour change v2 makes outside its one branch."""
    block = _framing_block()
    assert '_v2f_row["v2_framing_stopped"] = True' in block
    assert '_v2f_row["v2_framing_suppressed_tool"] = tool' in block


def test_the_frame_decides_flag_is_in_the_deploy_allowlist():
    """SET_ENV_VARS is an allowlist, not a passthrough — a var absent from it
    is simply not in the container, silently. That defect made the shadow flag
    a no-op on its first deploy, and it would make the revert lever fail open
    here: the hook would keep deciding with no way to turn it off short of a
    redeploy."""
    assert "MOBIUS_V2_FRAME_DECIDES=" in pathlib.Path("scripts/deploy.sh").read_text()


def test_the_v2_flags_have_a_home_ON_DISK_not_in_a_shell():
    """INCIDENT 2026-09-11, reported by the Chat FE seat.

    deploy.sh builds SET_ENV_VARS as "MOBIUS_V2_PCT=${MOBIUS_V2_PCT:-}" — read
    from whatever shell runs the deploy. I wrote those lines knowing the array
    is an ALLOWLIST, commented on it three times, and still made the VALUES
    depend on my own shell history.

    Consequence: every agent who deployed mobius-chat reset v2 to empty without
    touching anything of mine. Chat FE deployed 3x and v2 routing, the SHADOW,
    and the fork were silently off from their first deploy — discovered only
    because their /fork call started returning 409.

    A default that exists only in one person's shell is not a default. deploy.sh
    sources dev.env with `set -a` BEFORE building the array, so a value here is
    the default and an exported value still overrides it for a one-off.
    """
    env = pathlib.Path("deploy/dev.env").read_text()
    for var in ("MOBIUS_V2_SHADOW", "MOBIUS_V2_PCT", "MOBIUS_V2_FRAME_DECIDES",
                "MOBIUS_V2_AB_FORK", "MOBIUS_SELF_URL"):
        assert f"\n{var}=" in env, f"{var} has no on-disk default — a bare deploy erases it"
    sh = pathlib.Path("scripts/deploy.sh").read_text()
    # ...and the file must still be sourced BEFORE the array is built, or the
    # defaults are read after the values they are meant to supply.
    assert sh.index('source "${ENV_FILE}"') < sh.index("SET_ENV_VARS=("), \
        "dev.env is sourced after SET_ENV_VARS is built — the defaults cannot apply"


# ── the accelerator ─────────────────────────────────────────────────────────

def test_the_governor_can_OVERRULE_an_early_finish():
    """Until now it could stop a turn and not extend one — a brake with no
    accelerator.

    Ananth's three-payer question: the model proposed complete after round 2
    with two gaps open and 62.6s of a 95s promise left, and the answer shipped
    saying it could not find two of the three payers. v1, on the same question
    in the same seconds, ran to round 6 and found Sunshine Health. The
    governor's only capability was the one that made that worse.
    """
    d = Decision(Posture.EXPLORE, "closing S1 (open 1 rounds, 1 levers spent)",
                 gap_targeted="S1")
    a = ex.decide(d, ExitMode.BUDGET, extensions_used=0,
                  model_proposes_complete=True)
    assert a.continues, "the model's completion still ends the turn"
    assert "OVERRULING" in a.because


def test_an_overrule_needs_a_NAMED_gap():
    """"More might exist" is not a reason to spend a round. Only a gap the
    machine can name and intends to close."""
    d = Decision(Posture.EXPLORE, "something is missing", gap_targeted=None)
    a = ex.decide(d, ExitMode.BUDGET, model_proposes_complete=True)
    assert not a.continues


def test_CAPABILITY_is_never_overruled():
    """A gap nothing can reach is not bought by another round. Overruling here
    would spend the person's time on an answer no amount of waiting produces —
    the cruelty the exit modes exist to prevent."""
    d = Decision(Posture.EXPLORE, "one more", gap_targeted="S1")
    a = ex.decide(d, ExitMode.CAPABILITY, model_proposes_complete=True)
    assert not a.continues, "CAPABILITY was overruled"


def test_the_overrule_still_respects_the_extension_fuse():
    """It is how a 98-round runaway starts. The fuse is checked for an
    overrule exactly as for any other extend."""
    d = Decision(Posture.EXPLORE, "one more", gap_targeted="S1")
    a = ex.decide(d, ExitMode.BUDGET,
                  extensions_used=ex.MAX_V2_EXTENSIONS,
                  model_proposes_complete=True)
    assert not a.continues
    assert "CEILING" in a.because


def test_a_wrapup_posture_is_not_turned_into_an_overrule():
    """NARROW/ALTERNATIVES are reached only when nothing is worth buying.
    model_proposes_complete must not resurrect them into another round —
    that would invert the decision they just made."""
    for posture in (Posture.NARROW, Posture.ALTERNATIVES, Posture.COMMUNICATE):
        a = ex.decide(Decision(posture, "nothing worth buying", gap_targeted="S1"),
                      ExitMode.BUDGET, model_proposes_complete=True)
        assert not a.continues, posture


def test_the_call_site_TELLS_decide_the_model_proposed_complete():
    """The branch only runs when the model proposed complete — which is exactly
    the moment the governor needs to say "not yet". A default of False here
    would leave the accelerator permanently unreachable: built, tested, and
    never once able to fire. That shape has appeared three times today."""
    src = _react_src()
    i = src.index("_v2x.decide(")
    assert "model_proposes_complete=True" in src[i:i + 400]


# ── the self-report gate ────────────────────────────────────────────────────

def test_an_answer_that_contradicts_its_own_confidence_note_is_flagged():
    """cid 403d0e59: the SAME JSON said "No sources were provided to answer the
    question" and then answered it in full, including a fabricated Centene
    claim. confidence_note is carried through six places in this codebase and
    gated by none — the fourteenth producer-without-a-consumer here, and the
    most consequential, because the missing consumer would have stopped an
    ungrounded answer reaching a person."""
    bad, why = ex.self_report_contradicts_answer({
        "confidence_note": "No sources were provided to answer the question.",
        "direct_answer": "Molina, Sunshine Health and United Healthcare differ in…",
    })
    assert bad and "confidence_note" in why


def test_an_HONEST_no_answer_is_NOT_flagged():
    """"I could not find this" with zero sources is CORRECT behaviour. Flagging
    it would punish the one response we most want — and would push the system
    back toward answering anyway, which is the defect."""
    ok, _ = ex.self_report_contradicts_answer({
        "confidence_note": "No sources were provided to answer the question.",
        "direct_answer": "",
    }, source_count=0)
    assert not ok


def test_zero_sources_alone_never_triggers_it():
    """Corroboration, never the sole tell. Plenty of legitimate answers have no
    corpus sources — the product-identity path answers from a different
    knowledge base, and the clarifying-question path answers with no evidence
    by design."""
    ok, _ = ex.self_report_contradicts_answer(
        {"direct_answer": "Short answer."}, source_count=0)
    assert not ok


def test_unknown_source_count_is_not_treated_as_zero():
    """None means UNKNOWN. A caller that does not know must not be forced to
    guess, and could-not-check reported as checked-false is a shape this
    program has found eight times."""
    ok, _ = ex.self_report_contradicts_answer(
        {"direct_answer": "x" * 900}, source_count=None)
    assert not ok


def test_the_gate_REPORTS_and_never_rewrites():
    """A function that silently blanked an answer would be a worse failure than
    the one it fixes. What to do about a contradicted answer is the caller's
    decision."""
    import ast, inspect
    tree = ast.parse(inspect.getsource(ex.self_report_contradicts_answer))
    for n in ast.walk(tree):
        assert not isinstance(n, ast.Assign) or not any(
            isinstance(t, ast.Subscript) for t in n.targets), \
            "the gate mutates the envelope"


def test_the_self_report_gate_runs_on_BOTH_arms():
    """It is a property of the ANSWER, not of the orchestrator. An ungrounded
    v1 answer matters exactly as much as an ungrounded v2 one, and gating only
    v2 would make the comparison look like v2 has a problem v1 does not."""
    orch = pathlib.Path("app/pipeline/orchestrator.py").read_text()
    i = orch.index("SELF-REPORT GATE")
    block = orch[i:i + 1600]
    assert "self_report_contradicts_answer" in block
    assert 'orchestrator_version' in block, "the log does not say which arm"
    # it must NOT be gated on the arm
    assert '== "v2"' not in block, "the gate only runs on one arm"
