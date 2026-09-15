"""The speculative fan-out cap, enforced where it can actually bite.

Tool Manifest's catalogue declares `tools.speculative_fanout` with
enforced_by = 'CALLER (preload planner / Governor) — ADVISORY UNTIL THE
GOVERNOR IMPLEMENTS IT'. The cap is 3 concurrent speculative calls, derived
from mobius-payor's connection ceiling (containerConcurrency 80 x maxScale 2 =
160 fresh asyncpg connections against 171 Postgres headroom, no pool).

TODAY THE CAP CANNOT BIND, because preload executes SEQUENTIALLY -- a plain
`for` loop, no executor, no gather (preload.py, and the header above execute()
explains why: _execute_tool ASSIGNS fifteen ctx attributes, so two tools in
flight clobber each other's sources). Concurrency is 1, so a cap of 3 is
satisfied trivially.

So a runtime semaphore would be a gate that can never fire -- exactly the
defect this fleet spent the night removing. The requirement is real at exactly
one moment: when someone makes preload concurrent. This gate fires THEN, and
points at the cap rather than leaving the next author to rediscover it.
"""
import ast
import inspect
import pathlib

from app.pipeline.v2 import preload

# Tool Manifest's declared value. Read from tools.speculative_fanout, not
# invented here -- recorded so a drift between their column and this file is
# visible rather than silent.
DECLARED_CAP = 3


def _execute_fn():
    src = inspect.getsource(preload.execute)
    return ast.parse(src.lstrip()).body[0]


def test_preload_is_still_sequential_or_the_cap_must_be_enforced():
    """🔴 IF THIS FAILS, YOU MADE PRELOAD CONCURRENT. Read this before "fixing" it.

    Making preload concurrent does two things at once, and the second is easy
    to miss: it multiplies one user turn into N simultaneous requests against
    mobius-payor, whose connections are unpooled and whose headroom is 171.
    Preload is what turns 1 connection per in-flight turn into N.

    If you are introducing concurrency deliberately: enforce a ceiling of
    DECLARED_CAP simultaneous speculative calls, confirm the cap with Tool
    Manifest (it is derived from a capacity figure that may have moved), give
    each concurrent tool its OWN context rather than sharing one, and then
    rewrite this test to assert the ceiling instead of the absence.
    """
    fn = _execute_fn()
    concurrent = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and node.attr in (
            "ThreadPoolExecutor", "ProcessPoolExecutor", "gather", "submit",
            "map_async", "run_in_executor",
        ):
            concurrent.append(node.attr)
        if isinstance(node, ast.Name) and node.id in ("ThreadPoolExecutor", "gather"):
            concurrent.append(node.id)
        if isinstance(node, (ast.AsyncFor, ast.Await)):
            concurrent.append(type(node).__name__)
    assert not concurrent, (
        f"preload.execute now uses {sorted(set(concurrent))} — it is no longer "
        f"sequential. Enforce the speculative fan-out cap of {DECLARED_CAP} "
        f"and isolate each tool's context before removing this gate."
    )


def test_the_gate_is_not_vacuous():
    """The test above passes if preload.execute stops existing, or if its
    source cannot be read. Assert we actually parsed the real loop."""
    fn = _execute_fn()
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.For)]
    assert loops, "no for-loop found in preload.execute — the gate above is vacuous"
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "runner"]
    assert calls, "preload.execute no longer calls runner() — re-read this file"


def test_the_capacity_basis_is_recorded_not_folklore():
    """The cap is 3 because of a measured ceiling, not a round number. If the
    basis disappears from this file, the next person cannot tell whether 3 is
    still right — which is how a derived limit becomes folklore."""
    doc = pathlib.Path(__file__).read_text()
    for fact in ("171", "160", "speculative_fanout"):
        assert fact in doc, f"the basis for the cap no longer records {fact}"
