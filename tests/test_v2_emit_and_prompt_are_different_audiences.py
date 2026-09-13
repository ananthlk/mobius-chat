"""Our observations go to the TRACE. The evidence goes to react.

Ananth asked for "a real good summary from rag.. its role, what it is trying to
solve, what it found and the new gap it is trying to close" — FOR THE EMIT, the
trace a person reads. I built it and wired the same string into react's PROMPT,
where the judgement half steered the model.

🔴 MEASURED, cid bd509bed. preload retrieved 14 chunks covering ALL THREE
payers (Aetna abhfl_*, Sunshine, UHC FL-Care-Provider-Manual). My summary said:

    not mentioned in anything returned: reimburse, activities, need

and react rejected every one, echoing it back: "didn't directly address
reimbursement for providers". It then re-retrieved per payer. 13.3s of
retrieval discarded because of a note I attached to it.

The check is a SUBSTRING test on the user's own words. Manuals write payment as
billing, claims, compensation, fee schedule — so "reimburse" is absent as a
WORD while the material is present. `activities` and `need` are ordinary
English carrying no retrieval meaning at all.
"""
from app.pipeline.v2 import blocks, preload


def _result(summary, prompt_summary=None):
    r = {"ok": True, "summary": summary, "payload": "p", "sources": []}
    if prompt_summary is not None:
        r["prompt_summary"] = prompt_summary
    return r


def test_the_uncovered_terms_note_never_reaches_the_prompt():
    """THE REGRESSION, as a property: react is not told our opinion of the
    evidence before it reads it."""
    class _Ctx:
        merged_state = {}
        message = "q"
    f = blocks.facts_from(_Ctx(), None, preloaded=[{
        "tool": "rag", "ok": True,
        "summary": "15 passage(s) across 3 doc(s): a; b | not mentioned in "
                   "anything returned: reimburse, activities, need",
        "prompt_summary": "15 passage(s) across 3 doc(s): a; b",
    }], suggest=())
    rendered = "\n".join(str(x) for x in f.preloaded)
    assert "not mentioned in anything" not in rendered
    assert "15 passage(s)" in rendered


def test_the_trace_still_gets_everything():
    """The observation is not deleted — it moves. A person reading the trace
    should still see what the ask asked for that nothing returned."""
    out = preload.execute(
        preload.PreloadPlan(execute=["rag"], suggest=(), excluded=()),
        lambda t, i: _result("15 passage(s) | not mentioned in anything "
                             "returned: reimburse",
                             "15 passage(s)"),
        "q")
    assert "not mentioned in anything" in out[0]["summary"]
    assert "not mentioned in anything" not in out[0]["prompt_summary"]


def test_what_we_did_NOT_search_still_reaches_the_prompt():
    """Narrowing is not an opinion about the evidence — it is a fact about OUR
    spending, and react cannot know it any other way. It must survive the
    split."""
    out = preload.execute(
        preload.PreloadPlan(execute=["rag"], suggest=(), excluded=()),
        lambda t, i: _result(
            "9 passage(s) | NARROWED BY BUDGET: 2 retrieval arm(s) were not run",
            "9 passage(s) | NARROWED BY BUDGET: 2 retrieval arm(s) were not run"),
        "q")
    assert "NARROWED BY BUDGET" in out[0]["prompt_summary"]


def test_a_runner_that_does_not_split_is_unchanged():
    """Fallback: prompt_summary absent means the prompt sees `summary`, exactly
    as before. No tool regresses because its runner has not been updated."""
    out = preload.execute(
        preload.PreloadPlan(execute=["payor_fact"], suggest=(), excluded=()),
        lambda t, i: _result("180 days participating"), "q")
    assert out[0]["prompt_summary"] == "180 days participating"


def test_it_is_carried_not_rebuilt():
    """execute() once dropped a 141,074-char payload by reconstructing its dict
    field by field. Losing prompt_summary the same way would silently send the
    emit's judgements back into the prompt."""
    out = preload.execute(
        preload.PreloadPlan(execute=["rag"], suggest=(), excluded=()),
        lambda t, i: _result("rich | not mentioned in anything returned: x",
                             "neutral"), "q")
    assert out[0]["prompt_summary"] == "neutral"


def test_the_RUNNER_itself_splits_them(monkeypatch):
    """🔴 CAUGHT BY A MUTATION CHECK THAT REFUSED TO FAIL.

    Deleting the filter in _preload_runner left every test above green: they
    construct the result dicts by hand and never run the producer. Testing the
    consumer and calling it covered — the third time today.

    This drives the real function with a stubbed dispatch, so the split is
    asserted where it is actually made.
    """
    from app.pipeline import react_loop as R

    chunk = {"text": ("Sunshine Health case management program coordinates "
                      "care. Aetna Better Health care management staff "
                      "coordinate services."),
             "document_name": "m.pdf", "page_number": 1}
    monkeypatch.setattr(R, "_execute_tool",
                        lambda tool, inputs, ctx, emitter=None: {
                            "chunks": [chunk], "sources": [chunk]})

    class _Ctx:
        correlation_id = "t"
        merged_state = {}
        sources = []

    out = R._preload_runner(
        "rag",
        {"query": "Does sunshine health and aetna reimburse providers for "
                  "care management activities?"},
        _Ctx())

    # The emit carries the observation...
    assert "not mentioned in anything returned" in out["summary"]
    assert "reimburse" in out["summary"]
    # ...and the prompt does not.
    assert "not mentioned in anything returned" not in out["prompt_summary"]
    # Both still say what actually came back.
    assert "passage(s)" in out["prompt_summary"]
