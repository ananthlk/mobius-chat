"""A retrieval narrowed for budget must say so, in our voice not the corpus's.

Retriever, verified in production on the three-payer question:

    max_arms=1    9 chunks, ALL from molina_fl_provider_manual_2026.pdf
                  fanout_arms_dropped = 2
    (omitted)    17 chunks, Molina + Sunshine + UnitedHealthcare
                  fanout_arms_dropped = 0

So `fast` retrieves ONE payer for a three-payer question. If react is not told,
it writes a confident answer about Molina and the omission is invisible — the
filter making the answer look complete. That is the defect we fixed earlier
tonight, and the tier budget policy would have reintroduced it.
"""
from app.pipeline.react_loop import _arms_dropped


def test_finds_it_nested_wherever_it_arrives():
    """rag puts it in contract.traces; the skill wraps telemetry under extra.
    Pinning the depth from this side would break the moment either moves."""
    assert _arms_dropped({"fanout_arms_dropped": 2}) == 2
    assert _arms_dropped({"extra": {"pipeline_trace": {
        "contract": {"traces": {"fanout_arms_dropped": 2}}}}}) == 2
    assert _arms_dropped({"traces": [{"fanout_arms_dropped": 1}]}) == 1


def test_zero_and_absent_are_both_no_narrowing():
    """Zero is Retriever's honest 'the cap did not bite'. Absent is an older
    rag that never reports it. Neither may read as narrowing."""
    assert _arms_dropped({"fanout_arms_dropped": 0}) == 0
    assert _arms_dropped({}) == 0
    assert _arms_dropped(None) == 0


def test_junk_does_not_become_a_narrowing_claim():
    """Claiming arms were dropped when they were not would tell react the
    answer is incomplete when it is whole — the same error mirrored."""
    for junk in ({"fanout_arms_dropped": None}, {"fanout_arms_dropped": "two"},
                 {"fanout_arms_dropped": -1}):
        assert _arms_dropped(junk) == 0


def test_it_terminates_on_hostile_input():
    deep = cur = {}
    for _ in range(50):
        cur["x"] = {}
        cur = cur["x"]
    cur["fanout_arms_dropped"] = 3
    assert _arms_dropped(deep) == 0, "depth bound must hold"
    assert _arms_dropped({"a": [{"b": {}} for _ in range(500)]}) == 0
