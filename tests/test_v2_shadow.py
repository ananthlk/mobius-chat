

def test_divergence_rate_never_folds_unmapped_into_the_denominator():
    """`unmapped` answers a question about the MAPPING, not the decision.

    Counted as disagreement it inflates divergence and blames the posture
    machine for a hole in the translation table. 8 decided, 4 diverge => 50%,
    with 90 unmapped sitting beside it and changing nothing.
    """
    from app.pipeline.v2.shadow import divergence_rate
    r = divergence_rate(["agree"] * 4 + ["diverge"] * 4 + ["unmapped"] * 90)
    assert r["decided"] == 8
    assert r["pct_diverge"] == 50.0          # NOT 4/98 = 4.1%
    assert r["excluded_unmapped"] == 90


def test_a_rate_over_an_empty_population_is_None_not_zero():
    """Zero divergence and no measurement are different claims."""
    from app.pipeline.v2.shadow import divergence_rate
    assert divergence_rate(["unmapped"] * 5)["pct_diverge"] is None
    assert divergence_rate([])["pct_diverge"] is None
