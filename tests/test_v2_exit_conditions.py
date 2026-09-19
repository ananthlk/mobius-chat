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


class TestTheUnusableFuseAsksAboutCost:
    """Live turn 98f8a6e1 stopped on `unusable_rounds` after 2 rounds with
    84.8s of a 95s promise UNSPENT, on an agentic question whose ceiling was
    6 rounds. The governor said the gap was affordable on both rounds and
    said EXTEND. v1 answered the same question in 6 rounds, scoring 0.969
    against v2's 0.384.

    The fuse's own rationale was budget reasoning -- "at v2's ceilings a
    third wasted round is most of the budget" -- written as a round count.
    With 89% of the budget left that inference is simply false.
    """

    def fuse(self, unusable, left_s, round_cost_s):
        """The decision, reproduced: stop, or keep going?"""
        from app.pipeline.v2.loop import (MAX_UNUSABLE_ROUNDS,
                                          MAX_UNUSABLE_ROUNDS_HARD)
        if unusable < MAX_UNUSABLE_ROUNDS:
            return "continue"
        affordable = round_cost_s > 0.0 and left_s >= round_cost_s
        if affordable and unusable < MAX_UNUSABLE_ROUNDS_HARD:
            return "continue"
        return "stop"

    def test_it_no_longer_stops_with_the_budget_barely_touched(self):
        """98f8a6e1's numbers exactly."""
        assert self.fuse(unusable=2, left_s=84.8, round_cost_s=9.0) == "continue"

    def test_it_still_stops_when_a_round_is_unaffordable(self):
        """The case the fuse was written for: nearly spent, still learning
        nothing."""
        assert self.fuse(unusable=2, left_s=3.0, round_cost_s=9.0) == "stop"

    def test_the_hard_cap_is_absolute_however_much_budget_remains(self):
        """🔴 THE RUNAWAY MUST STILL BE CAUGHT. No amount of remaining budget
        may buy an unbounded number of rounds that learn nothing."""
        assert self.fuse(unusable=4, left_s=900.0, round_cost_s=1.0) == "stop"

    def test_an_unknown_round_cost_stops_rather_than_continues(self):
        """Could-not-check is not a licence to spend. If the cost of a round
        is unknown, affordability is UNKNOWN, and the fuse must not read that
        as 'affordable' -- the same measured-zero/not-measured line, on the
        input that decides whether to keep going."""
        assert self.fuse(unusable=2, left_s=900.0, round_cost_s=0.0) == "stop"

    def test_the_LOOP_actually_consults_the_budget_in_that_branch(self):
        """🔴 THE TESTS ABOVE REPRODUCE THE DECISION; THIS ONE CHECKS THE CODE.

        A table of cases can agree with itself forever while the loop stops
        asking the question.

        My first version searched the branch text for "_left" and PASSED
        while the decision was mutated to `_affordable = False` — because
        "_left" still appeared in the log line below it. Matching prose that
        happens to sit nearby, in the gate written to prevent drift. So:
        parse, find the assignment that decides affordability, and assert
        ITS expression reads the remaining budget.
        """
        import ast
        import pathlib

        import app.pipeline.v2.loop as L

        tree = ast.parse(pathlib.Path(L.__file__).read_text())
        decided = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "_affordable"
                            for t in node.targets)):
                names = {n.id for n in ast.walk(node.value)
                         if isinstance(n, ast.Name)}
                decided.append(names)
        assert decided, "nothing decides `_affordable` any more"
        assert any("_left" in names for names in decided), (
            f"affordability is decided without reading the remaining budget "
            f"(saw {decided}) — the fuse is a round count again")
        assert any("_round_cost" in names for names in decided), (
            "affordability is decided without the cost of a round")
