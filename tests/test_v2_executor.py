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
    assert '"inputs": json.dumps(r.get("v2_decision_inputs")' in ledg, "not written"
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


def test_the_framing_hook_records_and_does_NOT_act():
    """The first instant round N's gaps exist — and BEFORE round N's tool runs.

    Every other governor hook is blind to them: the pre-round hook fires before
    the model has spoken, and round N's enrichment is written at :5316. That is
    why the LLM seat's round-1 decomposition changed 17 of 20 branch sequences
    not at all — nine gaps produced correctly, and the governor's round-1
    decision already made before they existed.

    OBSERVATION ONLY. Acting here means refusing a tool call the model has
    already chosen — a far larger behaviour change than substituting a
    directive, and it has earned no evidence yet.
    """
    block = _framing_block()
    assert "_v2fp.select(" in block and "_v2fp.explain(" in block, "records nothing"
    # It must not reassign the loop's control variables. Word-bounded: a plain
    # substring test matched my own local `_v2f_inputs` on "inputs =" — the
    # same prose-for-program error this program has now made five times, in a
    # new dress. `(?<![\w])` is what separates the loop's `inputs` from a
    # variable that merely ends in it.
    import re
    for control in ("_pp_directive", "max_it", "tool", "inputs", "is_complete"):
        assert not re.search(rf"(?<![\w])(?<!\.){control}\s*(?:=|\+=)(?!=)", block), \
            f"the framing hook assigns {control!r} — it acts"
    for stmt in ("continue", "return", "break"):
        assert not re.search(rf"(?<![\w]){stmt}(?![\w])", block), \
            f"the framing hook does {stmt!r} — it acts"


def test_framing_never_overwrites_the_pre_round_decision():
    """Both on one row, deliberately: the DIFFERENCE between what the governor
    decided before the model spoke and what it would decide once the gaps exist
    IS the value of moving the hook, and it cannot be read if one replaces the
    other."""
    block = _framing_block()
    assert '_v2f_row["v2_framing_inputs"]' in block
    assert '"v2_decision_inputs"' not in block, "the framing hook clobbers the pre-round row"
    ledg = pathlib.Path("app/pipeline/v2/ledger.py").read_text()
    assert '"framing": json.dumps(r.get("v2_framing_inputs")' in ledg
    api = pathlib.Path("app/api/ab_harness.py").read_text()
    assert '"framing_inputs": r["framing_inputs"]' in api


def test_the_framing_hook_shares_the_other_hooks_clock():
    """A third clock would make the three rows incomparable — the whole point
    is to diff two decisions about the same moment."""
    assert "_pp_time_mod.monotonic() - _pp_turn_start" in _framing_block()
