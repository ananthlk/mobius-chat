"""The communicate round ends the turn: no blocking CRITIC after it.

Ananth, 2026-09-13: "the second call should have been communicate and direct
to our v2 deterministic module and no UX".

Measured on the three-payer turn before this gate: loop complete at +41.9s,
card out at +48.7s -- 6.8s of critic + next_steps that nothing was waiting for.

🔴 NARROWED 2026-09-15, BY A LATER RULING THAT PARTLY REVERSES THE ONE ABOVE.

Ananth, after seeing a live card with neither block: "we need a next steps and
follow up questions which is missing.. these are important.. USER first and
then we work towards the promise" and "lets start with producing this every
time".

So the gate now covers the CRITIC, which is what the 6.8s was really about and
what the standing ruling names. next_steps is no longer "nothing was waiting
for it" -- the person reads it, and a communicate round is exactly the turn
where the answer is finished and the next question is most useful.

THIS COSTS A MODEL CALL ON THE BLOCKING PATH and that is a deliberate, recorded
trade, not an oversight: the earlier decision was measured and this one
overrules it on the newer priority. If the latency proves worse than the block
is worth, the fix is to make next_steps non-blocking, NOT to quietly restore
the skip.

These assert the PROPERTY (the runner is never invoked FOR THE CRITIC) rather
than the decision's wording, so rephrasing `why` cannot make them pass
vacuously.
"""
from app.pipeline.v2 import enrich, integrator


class _Fact:
    def __init__(self, grounded=True):
        self.fact = "Molina uses an Integrated Care Management program."
        self.document, self.page, self.document_id = "molina.pdf", 11, "d1"
        self.grounded = grounded


def _runner_that_must_not_run(*a, **k):        # pragma: no cover
    raise AssertionError(
        "a model call was made on the blocking path after the communicate "
        "round -- that is the 6.8s this gate exists to stop")


def test_communicate_round_makes_no_CRITIC_model_call():
    """The critic is skipped and next_steps is not. Asserts the runner is
    invoked ONLY for the next-steps stage -- a critic call still trips it."""
    calls = []

    def _runner(system, user, **kw):
        stage = kw.get("stage") or ""
        if "next_steps" not in stage:
            raise AssertionError(
                f"a CRITIC model call was made on the blocking path "
                f"(stage={stage!r}) -- that is the 6.8s this gate exists to "
                "stop, and the standing ruling it enforces")
        calls.append(stage)
        return '{"next_steps":["s"],"follow_up_questions":["q?"]}'

    d = enrich.should_enrich(answer="Molina, Sunshine and UHC each...",
                             facts=(_Fact(),), open_gaps=("one open gap",),
                             is_complete=True, finalised_via_communicate=True)
    assert d.run_critic is False
    assert d.run_next_steps is True, (
        "the person gets no onward route on a clean turn")

    out = integrator.run(question="care management philosophy?",
                         answer="Molina, Sunshine and UHC each...",
                         facts=(_Fact(),), open_gaps=("one open gap",),
                         all_parts=("Molina", "Sunshine", "UHC"),
                         decision=d, runner=_runner)
    assert calls, "next_steps never called the model"
    # SKIPPED IS NOT UNCHECKED: the deterministic half still ran.
    assert out.ran["critique"] in ("skipped", "deterministic (verify_claims)",
                                   "deterministic (verify_claims) — nothing flagged")
    assert out.coverage, "deterministic coverage must survive the skip"
    assert out.next_steps, "next_steps produced nothing"
    assert out.follow_up_questions, "follow-up questions produced nothing"


def test_open_gap_alone_does_not_skip():
    """The gate is the communicate round, NOT 'complete'. A turn that ended
    any other way still gets the critic -- otherwise this silently becomes a
    blanket disable."""
    d = enrich.should_enrich(answer="an answer", facts=(_Fact(),),
                             open_gaps=("gap",), is_complete=True,
                             finalised_via_communicate=False)
    assert d.runs_anything is True


def test_criteria_records_the_signal():
    d = enrich.should_enrich(answer="a", facts=(), open_gaps=(),
                             is_complete=True, finalised_via_communicate=True)
    assert d.criteria["finalised_via_communicate"] is True


def test_a_turn_that_communicates_and_STOPS_also_skips_the_blocking_pair():
    """🔴 MEASURED, cid 3ee0fcdd — the regression that arrived WITH the win.

    The exact-tool posture communicated on round 1 and stopped, so no finalise
    round was ever bought, so `_v2_finalised` was False, so the critic fired:

        ran={'assemble':'ok', 'critique':'ok', 'next_steps':'ok'}

    — the 6.8s blocking tail removed in f8cbf2a, back on exactly the turns that
    had just got faster. The flag read HOW THE TURN ENDED instead of WHAT THE
    ROUND DID.

    Asserted on the source because the two signals are ORed at the call site,
    and the property is that communicating is sufficient on its own.
    """
    import ast
    import pathlib
    src = pathlib.Path("app/pipeline/react_loop.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg != "finalised_via_communicate":
            continue
        dumped = ast.dump(node.value)
        assert "_v2_round_communicates" in dumped, (
            "the integrator's skip still keys on how the turn ENDED — a turn "
            "that communicates on round 1 and stops pays the blocking critic")
        assert "_v2_finalised" in dumped, (
            "the finalise path must still count: a turn that bought a "
            "communicate round also communicated")
        return
    raise AssertionError("finalised_via_communicate is never passed")
