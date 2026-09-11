

def test_divergence_rate_never_folds_unmapped_into_the_denominator():
    """`unmapped` answers a question about the MAPPING, not the decision.

    Counted as disagreement it inflates divergence and blames the posture
    machine for a hole in the translation table. 8 decided, 4 diverge => 50%,
    with 90 unmapped sitting beside it and changing nothing.
    """
    from app.pipeline.v2.shadow import divergence_rate
    r = divergence_rate(["agree"] * 4 + ["diverge"] * 4 + ["unmapped"] * 90)
    assert r["decided"] == 8
    assert r["pct_diverge"] == 50.0          # NOT 4/98 = 4.1%
    assert r["excluded_unmapped"] == 90


def test_a_rate_over_an_empty_population_is_None_not_zero():
    """Zero divergence and no measurement are different claims."""
    from app.pipeline.v2.shadow import divergence_rate
    assert divergence_rate(["unmapped"] * 5)["pct_diverge"] is None
    assert divergence_rate([])["pct_diverge"] is None


def test_the_payload_check_is_wired_and_is_not_the_success_flag():
    """`returned_payload` was hard-coded False, so converging() could never
    return True, so may_overrun() could never allow — the overrun Ananth asked
    for was STRUCTURALLY DEAD. Built, tested, reported, never once reachable.

    It must not be the tool's own success flag: a tool can succeed and return
    nothing, which is the exact case the check exists to catch.
    """
    import ast, pathlib
    src = pathlib.Path("app/pipeline/v2/shadow.py").read_text()
    tree = ast.parse(src)
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "state_from_ctx")
    # no literal False reaches returned_payload any more
    for n in ast.walk(fn):
        if isinstance(n, ast.keyword) and n.arg == "returned_payload":
            assert not (isinstance(n.value, ast.Constant) and n.value.value is False), \
                "returned_payload is hard-coded False again"
    # and it is derived from CLOSED GAPS / running answer, never from a
    # success/ok flag on the tool result
    body = ast.unparse(fn)
    assert "closed" in body and "running_answer" in body
    for flag in ('"success"', "'success'", '"ok"', "'ok'"):
        assert flag not in body, f"payload check reads {flag} — that is the success flag"


def test_the_budget_v2_decides_against_is_the_PROMISE():
    """It read _pp_contract.soft_target_s — copilot 12.0s — while the copilot
    PROMISE is 31.0s. v2 decided against under 40% of the contract it exists to
    keep, so `spendable` was False on virtually every round and every "nothing
    worth buying" was arithmetic wearing the clothes of a judgement."""
    from app.pipeline.v2.shadow import promise_seconds
    class P: latency_s = 31.0
    class Ctx: promise = P()
    class Contract: soft_target_s = 12.0
    assert promise_seconds(Ctx(), Contract()) == 31.0
    # no promise on the turn -> a DECLARED fallback, never the soft target
    class NoP: promise = None
    assert promise_seconds(NoP(), Contract()) != 12.0


def test_react_loop_no_longer_passes_the_soft_target():
    import pathlib
    src = pathlib.Path("app/pipeline/react_loop.py").read_text()
    i = src.index("_v2_state = _v2s.state_from_ctx")
    block = src[i:i + 1600]
    assert "promise_latency_s=_v2_promise_s(ctx" in block
    assert "soft_target_s)" not in block
