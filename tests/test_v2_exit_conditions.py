"""A queued attempt is not a failed attempt.

Live turn 4d97dad2 (agentic, 15-question bank): declared CAPABILITY — "I
have no source for this", which is TERMINAL and tells the person not to
expect more — with 4 gaps open, 60.3s of budget remaining, and
`service_line_search` sitting in the queue. It dropped the tool on the way
out and reported the gap unreachable without running the source it had
already chosen.

    round=4 branch=gap_affordable posture=explore -> complete gaps=4 remaining=60.3s
    pending tools dropped at exit: service_line_search

The executor's overrule, which would have extended the turn, is skipped on
CAPABILITY by design. So a wrong exit mode did not merely mislabel the
turn, it ENDED it.
"""
from app.pipeline.v2 import posture as P


def _state(pending=(), **kw):
    """A state with one material gap that was attempted and returned nothing —
    the shape that yields CAPABILITY."""
    g = P.Gap(gap_id="g1", text="rate for H0036", opened_round=1,
              importance="normal",
              attempted_by=(P.Attempt(round_index=1, tool="rag", model="m",
                                      query="h0036 rate",
                                      returned_payload=False, targeted=True),))
    return P.RoundState(
        round_index=4,
        open_gaps=(g,),
        gaps_open_history=(1, 1, 1),
        budget=P.Budget(remaining_s=60.3, remaining_c=5.0, cost_coverage=1.0,
                        band_s=95.0, band_drawn_s=34.7),
        next_round_cost_s=9.0, acting_cost_s=3.0, validate_cost_s=5.0,
        pending_tools=tuple(pending), **kw)


def test_capability_is_reachable_when_nothing_is_queued():
    """The baseline: this state is exactly the CAPABILITY shape."""
    assert P.exit_mode(_state()) is P.ExitMode.CAPABILITY


def test_a_queued_tool_blocks_the_capability_exit():
    """🔴 THE FIX. Same state, one tool queued."""
    assert P.exit_mode(_state(pending=("service_line_search",))) is P.ExitMode.BUDGET


def test_budget_is_the_honest_reading_not_merely_a_softer_one():
    """CAPABILITY is terminal — offers_continuation is False for it — so the
    label decides whether the person is told to expect more. BUDGET says we
    had somewhere left to look and stopped, which is what happened."""
    assert P.offers_continuation(P.ExitMode.BUDGET)
    assert not P.offers_continuation(P.ExitMode.CAPABILITY)


def test_the_queue_reaches_the_state_through_EVERY_hop():
    """A field nothing writes is a consumer with no producer.

    🔴 AND ONE HOP IS NOT THE CHAIN. My first version of this test asserted
    that SOME call passed `pending_tools`, and passed while the second hop
    was deleted — `_round_state` still took it and then dropped it on the
    floor before `state_from_ctx`. Branch-exists-is-not-branch-reached,
    committed in the gate written to prevent it.

    The value travels loop -> _round_state -> state_from_ctx -> RoundState,
    so assert it is passed at BOTH call sites.
    """
    import ast
    import pathlib

    import app.pipeline.v2.loop as L

    tree = ast.parse(pathlib.Path(L.__file__).read_text())
    passing = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and any(k.arg == "pending_tools" for k in n.keywords)
    ]
    assert len(passing) >= 2, (
        f"pending_tools is passed at {len(passing)} call site(s); it must "
        f"travel loop -> _round_state -> state_from_ctx, so a hop is missing "
        f"and the queue never reaches the exit-mode decision")


def test_the_state_builder_actually_stores_it():
    """The last hop, checked on the builder rather than the caller."""
    import ast
    import pathlib

    import app.pipeline.v2.shadow as S

    src = pathlib.Path(S.__file__).read_text()
    tree = ast.parse(src)
    ok = False
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "RoundState"
                and any(k.arg == "pending_tools" for k in n.keywords)):
            ok = True
    assert ok, "state_from_ctx builds a RoundState without pending_tools"
