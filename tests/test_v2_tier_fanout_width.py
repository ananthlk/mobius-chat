"""Fan-out width is priced per tier, and the cap actually reaches rag.

Ananth, 2026-09-13: "price the fan out width per tier", then "yes get both
fields" when I reported I could neither know the width nor set it.

Retriever measured the cost (tag_select alone, concurrency controlled,
post-warmup, same query):

    width=1   4.6-5.1s        1 -> 2   +30-40%
    width=2   5.9-6.9s        2 -> 3   +15-20%
    width=3   6.8-7.6s
    width=1   4.6s   -- back to baseline the moment concurrency drops

and then shipped `max_arms` (d7d84e0) so the price can be acted on rather than
just known. Mechanism was NOT the obvious one: 3 of 15 pool connections is
nowhere near exhaustion, so it is DB-side CPU in tag_select's
jsonb_object_keys() iteration.
"""
from app.pipeline.v2 import preload


def test_each_tier_buys_what_it_can_afford():
    assert preload.max_arms_for_tier("fast") == 1
    assert preload.max_arms_for_tier("normal") == 3
    assert preload.max_arms_for_tier("thinking") is None, (
        "thinking must OMIT the cap — Retriever's no-op guarantee is stated "
        "for an omitted parameter, not a null one")


def test_an_unknown_tier_is_not_treated_as_cheap():
    assert preload.max_arms_for_tier(None) == 3
    assert preload.max_arms_for_tier("nonsense") == 3


def test_fast_can_now_afford_one_arm():
    """🔴 THE REGRESSION THIS REPLACES. Before max_arms existed the lever was
    0-or-N, so `fast` refused preload ENTIRELY — priced at the 3-arm worst
    case it could never have narrowed. A single-entity question was charged
    for a fan-out it would not have run."""
    ok, why = preload.affordable_for_tier("fast", 13.0)
    assert ok, why
    assert preload.expected_preload_ms(1) < preload.expected_preload_ms(3)


def test_the_cap_reaches_the_rag_call():
    """A tier policy nothing sends is a gate with no caller."""
    sent = {}

    def runner(tool, inputs):
        sent[tool] = dict(inputs)
        return {"ok": True, "summary": "s", "payload": "p", "sources": []}

    plan = preload.PreloadPlan(execute=["rag"], suggest=(), excluded=())
    preload.execute(plan, runner, "q", max_arms=1)
    assert sent["rag"].get("max_arms") == 1


def test_omitted_when_uncapped():
    """thinking sends NO max_arms key at all, not max_arms=None."""
    sent = {}

    def runner(tool, inputs):
        sent[tool] = dict(inputs)
        return {"ok": True, "summary": "s", "payload": "p", "sources": []}

    plan = preload.PreloadPlan(execute=["rag"], suggest=(), excluded=())
    preload.execute(plan, runner, "q", max_arms=None)
    assert "max_arms" not in sent["rag"]


def test_only_rag_gets_it():
    """max_arms means nothing to a fact lookup; sending it to every tool is a
    parameter that means nothing to most of them."""
    sent = {}

    def runner(tool, inputs):
        sent[tool] = dict(inputs)
        return {"ok": True, "summary": "s", "payload": "p", "sources": []}

    plan = preload.PreloadPlan(execute=["rag", "payor_fact"], suggest=(), excluded=())
    preload.execute(plan, runner, "q", max_arms=1)
    assert sent["rag"].get("max_arms") == 1
    assert "max_arms" not in sent["payor_fact"]
