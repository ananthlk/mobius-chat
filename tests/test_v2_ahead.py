"""Ask for the next steps while the answer is still forming.

Ananth, 2026-09-15: "the trick is to predict when we are near an answer and ask
the next steps and asks prompt right then .. there is always intelligence which
is better than just throwing things in the kitchen table".

The prediction already existed — posture.converging(), computed every round and
read by ONE caller, to decide budget overrun. Restoring next_steps had put a
blocking model call back on the tail (part of the 6.8s Ananth cut on 09-13);
firing it on the prediction overlaps it with the rounds that remain instead.
"""

import ast
import inspect
from concurrent.futures import Future
from types import SimpleNamespace

from app.pipeline.v2 import ahead, integrator


def _ctx():
    return SimpleNamespace(correlation_id="c", message="a question?")


def test_the_prediction_is_not_a_second_predicate():
    """Two answers to "are we nearly done" that can disagree is worse than
    none. `predicted` must delegate to posture.converging, not reimplement it."""
    src = inspect.getsource(ahead.predicted)
    assert "converging" in src
    tree = ast.parse(inspect.getsource(ahead))
    # no arithmetic on gap counts here — that logic belongs to posture.py
    assert "gaps_open_history" not in inspect.getsource(ahead), (
        "ahead.py is re-deriving convergence instead of asking posture")


def test_it_starts_once_per_turn():
    """A later round must not open a second call: the lists would be
    near-identical and we would pay twice."""
    ctx = _ctx()
    calls = []

    def runner(system, user, **kw):
        calls.append(1)
        return '{"next_steps":["s"],"follow_up_questions":["q?"]}'

    assert ahead.start(ctx, question="q", answer_so_far="draft",
                       open_gaps=(), runner=runner) is True
    assert ahead.start(ctx, question="q", answer_so_far="draft",
                       open_gaps=(), runner=runner) is False
    ahead.take(ctx).result(timeout=5)
    assert len(calls) == 1
    ahead.shutdown(ctx)


def test_it_does_not_fire_without_an_answer_to_write_next_steps_ABOUT():
    """Firing on an empty running answer asks the model what follows an answer
    that does not exist."""
    ctx = _ctx()
    assert ahead.start(ctx, question="q", answer_so_far="",
                       open_gaps=(), runner=lambda *a, **k: "{}") is False
    assert ahead.take(ctx) is None


def test_take_hands_it_over_exactly_once():
    ctx = _ctx()
    ahead.start(ctx, question="q", answer_so_far="draft", open_gaps=(),
                runner=lambda *a, **k: '{"next_steps":["s"],'
                                       '"follow_up_questions":["q?"]}')
    first = ahead.take(ctx)
    assert first is not None
    assert ahead.take(ctx) is None, "a second consumer would await a dead slot"
    first.result(timeout=5)
    ahead.shutdown(ctx)


def test_the_integrator_uses_the_prefetched_call_instead_of_opening_one():
    """The whole point: no round trip on the blocking path when the prediction
    fired. Asserts the runner is NEVER invoked at finalisation."""
    fut = Future()
    fut.set_result('{"next_steps":["Go to the portal"],'
                   '"follow_up_questions":["What about corrected claims?"]}')

    def must_not_run(*a, **k):        # pragma: no cover
        raise AssertionError(
            "the integrator opened a next_steps call although one was already "
            "in flight — the prefetch bought nothing")

    out = integrator.run(question="q", answer="an answer", facts=(),
                         open_gaps=("g",), decision=_AlwaysRun(),
                         runner=must_not_run, prefetched=fut)
    assert out.next_steps == ("Go to the portal",)
    assert out.follow_up_questions == ("What about corrected claims?",)
    assert "early" in out.ran.get("next_steps_started", "")


def test_a_wrong_prediction_degrades_to_todays_behaviour():
    """If the prefetch failed or was never started, finalisation calls exactly
    as it does now. A wrong prediction costs one discarded call, never a wrong
    answer and never a missing block."""
    calls = []

    def runner(system, user, **kw):
        calls.append(kw.get("stage"))
        return '{"next_steps":["s"],"follow_up_questions":["q?"]}'

    out = integrator.run(question="q", answer="an answer", facts=(),
                         open_gaps=("g",), decision=_AlwaysRun(),
                         runner=runner, prefetched=None)
    assert calls, "no prefetch and no call — the blocks would be empty"
    assert out.next_steps and out.follow_up_questions
    assert "finalisation" in out.ran.get("next_steps_started", "")


def test_a_prefetch_that_RAISES_does_not_kill_the_turn():
    """Degrade, never fabricate — and never fail."""
    fut = Future()
    fut.set_exception(RuntimeError("model unavailable"))
    out = integrator.run(question="q", answer="an answer", facts=(),
                         open_gaps=("g",), decision=_AlwaysRun(),
                         runner=lambda *a, **k: "{}", prefetched=fut)
    assert out.next_steps == ()
    assert out.problems, "a failed prefetch must be recorded, not silent"


class _AlwaysRun:
    runs_anything = True
    run_critic = False
    run_next_steps = True
    why = ""


def test_the_loop_actually_fires_the_prediction():
    """The gate my mutation run showed was MISSING: I removed the call from
    run_react_v2 and every test still passed, which means nothing tied the
    prediction to the loop. A prefetch nothing starts is a producer with no
    producer — the exact shape this codebase keeps finding."""
    from app.pipeline.v2 import loop as L2

    tree = ast.parse(inspect.getsource(L2))
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "run_react_v2")
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_start_next_steps_ahead" in called, (
        "run_react_v2 never starts the early next-steps call — the prediction "
        "is computed and discarded, and the model call stays on the tail")

    # and it must sit INSIDE the round loop, not after it: firing after the
    # loop has finished is the blocking tail it exists to remove.
    loops = [n for n in ast.walk(fn) if isinstance(n, (ast.While, ast.For))]
    inside = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "_start_next_steps_ahead"
        for lp in loops for n in ast.walk(lp))
    assert inside, (
        "the prediction fires outside the round loop — that is the tail call "
        "again, just renamed")


def test_finalisation_hands_the_running_call_to_the_integrator():
    """The other end of the same wire."""
    import app.pipeline.react_loop as R

    src = inspect.getsource(R)
    assert "prefetched=_v2_ahead.take(ctx)" in src, (
        "finalisation never collects the prefetched call — it would start a "
        "second one while the first is still running")


def test_every_loop_that_TAKES_a_prefetch_also_STARTS_one():
    """THE DEFECT I SHIPPED. _v2_integrate reads `prefetched=_v2_ahead.take(ctx)`,
    but the only code that STARTED a prefetch lived in run_react_v2 — v2's own
    loop, which is OFF. So on the live path take() returned None every turn, the
    integrator opened a blocking call, and the turn paid for it:

        "promised 31s · delivered 40.6s · MISSED"   (2026-09-15, live card)

    A consumer with no producer, on exactly the seam this fleet keeps finding
    them — and I built it while fixing one.

    The gate is general on purpose: any module that calls ahead.take() must
    also, somewhere, call ahead.start(). Naming one function would pass the day
    someone adds a third loop.
    """
    import app.pipeline.react_loop as R
    from app.pipeline.v2 import loop as L2

    for mod in (R, L2):
        src = inspect.getsource(mod)
        takes = ".take(ctx)" in src or "ahead.take" in src
        if not takes:
            continue
        starts = ("ahead.start(" in src or "_ahd.start(" in src
                  or "_ahead.start(" in src)
        assert starts, (
            f"{mod.__name__} consumes a prefetched next-steps call but never "
            "starts one — take() will return None and the model call goes back "
            "on the blocking tail")
