

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
    assert "promise_latency_s=_v2s.promise_seconds(ctx" in block
    assert "soft_target_s)" not in block


def test_NO_call_site_passes_the_soft_target():
    """There are TWO state_from_ctx call sites — pre-round and post-round — and
    fixing one left the other reading soft_target_s. The fix then verified
    clean on the round that reaches the executor and changed nothing on every
    round before it.

    Asserted over ALL call sites, so a third one cannot reintroduce it."""
    import ast, pathlib
    tree = ast.parse(pathlib.Path("app/pipeline/react_loop.py").read_text())
    sites = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "state_from_ctx"]
    assert len(sites) >= 2, f"expected both hooks, found {len(sites)}"
    for call in sites:
        kw = {k.arg: ast.unparse(k.value) for k in call.keywords}
        src = kw.get("promise_latency_s", "")
        assert "soft_target" not in src, f"call site still passes: {src}"
        assert "promise_seconds" in src, f"call site does not use the promise: {src}"


def test_the_band_is_wired_from_the_promise():
    """Budget.band_s defaulted to 0.0 and state_from_ctx never set it, so
    may_overrun's band arm was inert — a turn genuinely out of promise time
    could never overrun even when converging, and the one that DID overrun drew
    on unreserved promise time while reporting "from a 0.0s band"."""
    from app.pipeline.v2.shadow import band_seconds
    assert band_seconds(13.0) == 5.0
    assert band_seconds(31.0) == 8.0
    assert band_seconds(95.0) == 25.0
    # an unrecognised promise gets NO band — a synthesised tolerance is the
    # same defect as a synthesised promise
    assert band_seconds(42.0) == 0.0


def test_a_gap_id_means_the_same_sub_question_every_round():
    """It was f"S{i+1}" over the LATEST round's list — a POSITION, not an
    identity. Measured across 35 turns: 7 (20%) had an id change meaning
    mid-turn. On one, S1 was "timely filing deadline for Sunshine Health" at
    one round and "…for Humana" at the next.

    The stored VALUES survived (age/levers/attempts are keyed on text), but a
    label that lies is worse than no label — it invites the false continuity I
    read into my own reasoning dump.
    """
    from app.pipeline.v2.posture import gap_id_for
    a = gap_id_for("timely filing deadline for Sunshine Health")
    b = gap_id_for("  Timely Filing Deadline for   Sunshine Health ")
    c = gap_id_for("timely filing deadline for Humana")
    assert a == b, "whitespace/case must not mint a new gap"
    assert a != c, "different sub-questions must not share an id"
    # and it is NOT positional: order cannot change the id
    assert gap_id_for("x") == gap_id_for("x")


def test_a_reworded_gap_keeps_its_id_and_its_age():
    """react restates the same sub-question in slightly different words between
    rounds. A pure hash would mint a fresh gap and RESET the age and lever
    counts — which is what makes `stuck` reachable at all, so resetting them
    silently disables the stuck rule.

    Reuses REPEAT_JACCARD, the threshold posture.py already applies to the same
    question: is this the same thing restated?
    """
    import app.pipeline.v2.shadow as S

    class Ctx:
        message = "q"
        react_trace_rounds = [
            {"round": 1, "tool": "rag", "inputs": {"query": "q"},
             "enrichment": {"gaps_open": ["fax number for LTC waiver appeals coordinator at Sunshine"],
                            "gaps_closed": [], "running_answer": ""}},
            {"round": 2, "tool": "rag", "inputs": {"query": "q"},
             "enrichment": {"gaps_open": ["fax number for the LTC waiver appeals coordinator"],
                            "gaps_closed": [], "running_answer": ""}},
        ]
    st = S.state_from_ctx(Ctx(), round_index=2, elapsed_s=1.0,
                          promise_latency_s=31.0, round_cost_s=0.0,
                          acting_cost_s=0.0)
    g = st.open_gaps[0]
    assert g.opened_round == 1, "a reworded gap restarted its clock"
    assert g.age(2) == 1


def test_state_from_ctx_ids_are_CONTENT_addressed_not_positional():
    """Guards the CALL SITE, not just gap_id_for().

    A first pass at this tested the helper and left state_from_ctx free to go
    back to f"S{i+1}" — reverting the call site kept the suite green. The
    property that matters is that REORDERING the same gaps does not rename
    them, which is exactly what positional ids fail.
    """
    import app.pipeline.v2.shadow as S
    A = "timely filing deadline for Sunshine Health"
    B = "timely filing deadline for Humana"

    def ids(order):
        class Ctx:
            message = "q"
            react_trace_rounds = [
                {"round": 1, "tool": "rag", "inputs": {"query": "q"},
                 "enrichment": {"gaps_open": list(order), "gaps_closed": [],
                                "running_answer": ""}}]
        st = S.state_from_ctx(Ctx(), round_index=1, elapsed_s=1.0,
                              promise_latency_s=31.0, round_cost_s=0.0,
                              acting_cost_s=0.0)
        return {g.text: g.gap_id for g in st.open_gaps}

    first, flipped = ids([A, B]), ids([B, A])
    assert first[A] == flipped[A], "reordering renamed a gap — ids are positional"
    assert first[B] == flipped[B]
    assert first[A] != first[B]
