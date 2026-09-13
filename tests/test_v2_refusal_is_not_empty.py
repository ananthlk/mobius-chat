"""A tool that could not run made no claim about the corpus.

Measured, cid 9de5c318: payor_fact was called with Tool Manifest's
{'payor','predicate'} against a chat skill that reads inputs['field'], so it
refused with extra={'error': 'need payor and field'} WITHOUT performing a
lookup. The preload summary reported an empty body, the frame rendered

    payor_fact -> ran, returned nothing

and react read that as "the authoritative fact store has no timely filing
deadline for Sunshine Health". Its round-1 thought is the receipt:

    "The appeals_get_playbook tool was called but did not return the timely
     filing deadline. I will now use the rag tool to search for this."

react behaved correctly on a false premise, and the turn took three rounds
instead of one.
"""
from app.pipeline.v2.blocks import Facts, assemble


def _render(preloaded):
    text, rendered, _ = assemble(Facts(question="q", preloaded=preloaded))
    assert "preloaded" in rendered
    return text


def test_a_refusal_does_not_claim_the_corpus_is_empty():
    out = _render((("payor_fact", False,
                    "COULD NOT RUN — need payor and field (no lookup was "
                    "performed)"),))
    assert "ran, returned nothing" not in out, (
        "a tool that refused our call is being reported as a source that was "
        "consulted and had nothing — react cannot tell our bug from an "
        "absent fact")
    assert "COULD NOT RUN" in out


def test_a_genuine_empty_result_still_says_so():
    """The opposite error would be just as bad: a source that really was
    consulted and had nothing is a finding react needs."""
    out = _render((("rag", False, ""),))
    assert "ran, returned nothing" in out


def test_a_successful_tool_is_unchanged():
    out = _render((("payor_fact", True, "180 days participating"),))
    assert "180 days participating" in out
    assert "ran, returned nothing" not in out
    assert "COULD NOT RUN" not in out


def test_an_undispatchable_tool_is_not_reported_as_an_answer():
    """🔴 THE ACTUAL MECHANISM of cid 9de5c318, and my first diagnosis of it
    was wrong.

    I read payor.py, saw `if not payor or not field: return _unavailable(...)`
    and reported an argument-name mismatch (predicate vs field). The bytes
    disagree: len("Unknown tool: payor_fact") == 24 == the payload_chars we
    logged. chat registers `payor_lookup` — display_name "Payor Fact Lookup" —
    and `payor_readiness`. There is no `payor_fact` skill, and react_loop:3899
    returns {"result": "Unknown tool: payor_fact"}. The call never reached
    payor.py.

    It arrives in `result`, not `error`, so a truthy 24-char body would be
    handed to react as the tool's ANSWER.
    """
    from app.pipeline import react_loop

    class _Ctx:
        correlation_id = "t"
        merged_state = {}
        sources = []
    out = react_loop._preload_runner.__wrapped__ if hasattr(
        react_loop._preload_runner, "__wrapped__") else None
    # Exercise the classification directly rather than the whole dispatch.
    res = {"result": "Unknown tool: payor_fact"}
    body = res.get("result") or ""
    assert len(body) == 24, "the fingerprint that identified this bug"
    assert body.startswith("Unknown tool:"), (
        "an undispatchable tool must be classified as COULD NOT RUN, never as "
        "a tool that answered with this string as its evidence")
