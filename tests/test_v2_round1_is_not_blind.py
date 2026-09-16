"""Round 1 must receive the governor block. It did not, for a silent reason.

The pre-round hook reads _gap_status, which was bound ~370 lines BELOW that
read, at the end of the round body. On round 1 it was unbound, the hook raised
UnboundLocalError, and the `except Exception` that exists to stop a telemetry
failure from killing a turn swallowed it -- taking the whole governor block
with it. Round 1 got 1,902 characters and the question.

Measured before/after on the three-payer question, real rag, local:
    before   2 rounds, 187.8s, round 1 prompt 1,902 chars, no §10, no roles
    after    1 round,  100.3s, round 1 prompt 3,841 chars, §10 + roles present
"""
import ast


SRC = open("app/pipeline/react_loop.py").read()
TREE = ast.parse(SRC)


def _run_react():
    return next(n for n in ast.walk(TREE)
                if isinstance(n, ast.FunctionDef) and n.name == "run_react")


def _assign_lines(fn, name):
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    out.append(n.lineno)
    return sorted(out)


def _read_lines(fn, name):
    return sorted(n.lineno for n in ast.walk(fn)
                  if isinstance(n, ast.Name) and n.id == name
                  and isinstance(n.ctx, ast.Load))


def test_gap_status_is_bound_before_every_read():
    """AST, not text: a comment saying "bound before the loop" is not a
    binding, and this is the fourth UnboundLocalError of this shape today."""
    fn = _run_react()
    assigns = _assign_lines(fn, "_gap_status")
    reads = _read_lines(fn, "_gap_status")
    assert assigns and reads
    assert assigns[0] < reads[0], (
        f"_gap_status first assigned at {assigns[0]} but first read at "
        f"{reads[0]} — round 1 raises UnboundLocalError and the governor "
        f"block is silently dropped")


def test_the_swallowing_except_still_exists_and_still_logs():
    """The except is CORRECT -- a telemetry failure must not kill a turn. The
    defect was relying on it. It must keep logging, because a silent governor
    is indistinguishable from a governor that chose to say nothing."""
    assert "[v2.shadow] pre-round hook failed" in SRC


def test_preload_seeds_its_payload_as_a_virtual_tool_result():
    """The §10 summary tells react WHAT was retrieved; the seed is what it
    reads. Without it round 1 sees names and page numbers and must either
    search again or invent -- and it invented, with citation markers pointing
    at evidence that was never in the prompt."""
    assert "ctx.seed_tool_results.append({" in SRC
    i = SRC.index("ctx.seed_tool_results.append({")
    # SLICED TO A REAL BOUNDARY, not a char count. The first version took
    # SRC[i:i+500] and the block's own comments pushed the field past 500, so
    # the gate failed on code that was correct.
    block = SRC[i:SRC.index("})", i) + 2]
    assert '"round_virtual": 0' in block, "round 1 would be credited with the fetch"
    # ASSERT THE PROPERTY, NOT THE FINGERPRINT. This pinned the exact source
    # text `"result": _r["payload"]`, so it broke when the payload started
    # being coerced to text — while the thing it protects (the preloaded
    # payload reaches round 1 as evidence) was never in question.
    #
    # The coercion exists because `result` is a STRING by convention: nine
    # readers in react_loop call .strip() on it, and a raw dict payload killed
    # a live turn (cid aa6f582d, "'dict' object has no attribute 'strip'").
    assert '"result":' in block and '_r["payload"]' in block, (
        "the preloaded payload no longer reaches the seeded result — round 1 "
        "would be blind to what preload fetched")


def test_the_seed_is_written_before_the_loop_reads_it():
    """Written after the read, this is a no-op that looks wired."""
    w = SRC.index("ctx.seed_tool_results.append({")
    r = SRC.index('seed = list(getattr(ctx, "seed_tool_results"')
    assert w < r, "preload seeds evidence AFTER run_react snapshots the seed"
