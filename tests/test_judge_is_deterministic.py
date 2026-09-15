"""The judge must not sample.

The adjudicator's score gates every quality claim in this repo. Measured
2026-09-14: re-scoring IDENTICAL text with the same judge and the same
pipeline inputs gives mean |delta| 0.241, max 0.621 (n=8) — wider than the
v1-vs-v2 difference the score is used to decide, and wider than any effect
measured in a night of work. Every provider adapter defaults to temperature
0.1 and `generate()` had no parameter to ask for anything else, so the judge
had always graded with sampling on.

These tests pin the PROPERTY (the judge asks for deterministic decoding and
generation stages do not) rather than any measured delta, which would make
them a flaky re-measurement of the thing they exist to protect.
"""
import inspect

from app.services import llm_manager
from app.services.adjudication import full as adj_full


def test_generate_can_be_asked_for_deterministic_decoding():
    assert "temperature" in inspect.signature(llm_manager.generate).parameters


def test_temperature_is_opt_in_not_a_new_global_default():
    """🔴 Do NOT default this to 0. Prose written at temperature 0 is a
    different and worse trade — this is for grading, not composing."""
    p = inspect.signature(llm_manager.generate).parameters["temperature"]
    assert p.default is None, (
        "temperature must default to None so generation stages keep their own"
    )
    src = inspect.getsource(llm_manager.generate)
    assert "if temperature is not None:" in src, (
        "an unconditional pass-through would override every stage's default"
    )


def test_the_adjudicator_asks_for_zero():
    src = inspect.getsource(adj_full)
    i = src.index("stage=\"adjudicator\"")
    window = src[max(0, i - 400): i + 700]
    assert "temperature=0.0" in window, (
        "the judge must request deterministic decoding at its own call site"
    )


def test_the_provider_would_honour_it():
    """Guards the assumption the rest rests on: the adapter reads the kwarg
    rather than ignoring it. If a provider stops honouring `temperature`,
    pinning the judge becomes a comment again."""
    from app.services import llm_provider
    src = inspect.getsource(llm_provider)
    assert 'kwargs.get("temperature"' in src, (
        "no provider reads a temperature kwarg — the judge's request would "
        "be silently discarded"
    )
