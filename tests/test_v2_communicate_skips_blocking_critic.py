"""The communicate round ends the turn: no blocking model call after it.

Ananth, 2026-09-13: "the second call should have been communicate and direct
to our v2 deterministic module and no UX".

Measured on the three-payer turn before this gate: loop complete at +41.9s,
card out at +48.7s -- 6.8s of critic + next_steps that nothing was waiting for.

These assert the PROPERTY (the runner is never invoked) rather than the
decision's wording, so rephrasing `why` cannot make them pass vacuously.
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


def test_communicate_round_makes_no_model_call():
    d = enrich.should_enrich(answer="Molina, Sunshine and UHC each...",
                             facts=(_Fact(),), open_gaps=("one open gap",),
                             is_complete=True, finalised_via_communicate=True)
    assert d.runs_anything is False
    out = integrator.run(question="care management philosophy?",
                         answer="Molina, Sunshine and UHC each...",
                         facts=(_Fact(),), open_gaps=("one open gap",),
                         all_parts=("Molina", "Sunshine", "UHC"),
                         decision=d, runner=_runner_that_must_not_run)
    # SKIPPED IS NOT UNCHECKED: the deterministic half still ran.
    assert out.ran["critique"] == "skipped"
    assert out.ran["next_steps"] == "skipped"
    assert out.coverage, "deterministic coverage must survive the skip"


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
