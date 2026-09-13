"""An unknown ceiling refused 46 of 49 tools, including a 360ms fact lookup.

Measured, cid 7973c25d: once Tool Manifest made payor_fact fillable, THIS rule
was what blocked it — "worst case unknown (no declared ceiling)" — on a lookup
the fact store answers in 360ms, on a turn that then took 3 rounds and 31.5s.

The original concern stays valid and stays asserted: healthcare_query declared
no ceiling and spent 30s timing out on the critical path.
"""
import time

from app.pipeline.v2 import preload


def test_unknown_ceiling_still_refused_when_nothing_backs_it():
    """THE ORIGINAL PROTECTION. A tool nobody ranked, with arguments we would
    have to invent, is exactly healthcare_query."""
    ok, why = preload.affordable_to_preload(None)
    assert ok is False and "unknown" in why


def test_unknown_ceiling_allowed_when_claim_backed_AND_arg_exact():
    ok, _ = preload.affordable_to_preload(None, claim_backed=True, args_exact=True)
    assert ok is True


def test_one_half_is_not_enough():
    """BOTH conditions, not either. A ranked tool we would call with invented
    arguments is the healthcare_query failure (it took `question`, was sent
    `query`); exact arguments for a tool that never claimed relevance is
    speculative spend."""
    assert preload.affordable_to_preload(None, claim_backed=True)[0] is False
    assert preload.affordable_to_preload(None, args_exact=True)[0] is False


def test_a_declared_ceiling_still_wins():
    """The exception must not become the rule: a DECLARED oversized ceiling is
    still refused, however well-backed the tool is. verify_claims declares
    144000ms and must never be preloaded speculatively."""
    ok, why = preload.affordable_to_preload(
        144000, claim_backed=True, args_exact=True)
    assert ok is False and "exceeds" in why


def test_p50_is_never_used_as_a_proxy_for_worst_case():
    """The docstring's whole point: `ceiling or p50` substitutes typical for
    worst, which is how a 30s tool passed a check priced at 800ms."""
    import ast
    import inspect
    import textwrap

    fn = ast.parse(textwrap.dedent(
        inspect.getsource(preload.affordable_to_preload))).body[0]
    # DROP THE DOCSTRING, not just the # comments. The first version of this
    # test stripped comments and matched the docstring, which EXPLAINS why p50
    # is unsafe — a test reading the prose that describes the code instead of
    # the code. Same shape as the six gates this repo caught doing it.
    body = fn.body[1:] if (isinstance(fn.body[0], ast.Expr)
                           and isinstance(fn.body[0].value, ast.Constant)
                           and isinstance(fn.body[0].value.value, str)) else fn.body
    executable = "\n".join(ast.dump(n) for n in body)
    assert "p50" not in executable, "p50 leaked into the affordability decision"


def test_the_wall_clock_stops_starting_new_work():
    """🔴 THE REAL GUARANTEE. Unknown cost is now BOUNDED rather than
    predicted — so a slow tool cannot run the sequence past the budget."""
    started = []

    def slow_runner(tool, inputs):
        started.append(tool)
        if tool == "slow":
            time.sleep(0.25)
        return {"ok": True, "summary": "s", "payload": "p", "sources": []}

    plan = preload.PreloadPlan(execute=["slow", "after"], suggest=(), excluded=())
    old = preload.PRELOAD_WALL_MS
    preload.PRELOAD_WALL_MS = 100          # ms
    try:
        out = preload.execute(plan, slow_runner, "q")
    finally:
        preload.PRELOAD_WALL_MS = old

    assert started == ["slow"], f"ran past the wall clock: {started}"
    # NOT MISSING: a tool absent from the result reads as never-attempted.
    after = [r for r in out if r["tool"] == "after"]
    assert after and after[0].get("not_started"), (
        "the skipped tool must say it was not started, not vanish")
