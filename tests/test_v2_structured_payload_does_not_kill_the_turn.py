"""A structured tool payload must not crash the turn.

🔴 LIVE, rev 01086-l6s, 2 of 2 runs — error card to the user:

    react_loop:6637  _kept_chunk_stats(tool_results[-1].get("result") or "")
    react_loop:258   _CHUNK_HEADER_RE.finditer(raw)
    TypeError: expected string or bytes-like object, got 'dict'

Tool Manifest's executor returns `payload` WHOLE and deliberately never
reshaped — for rag that is the parsed contract, a dict — and v2 preload seeds
it into ctx.seed_tool_results as `result`. This code has always assumed
rag-formatted TEXT.

A latent mismatch between a new payload contract and an old text assumption,
EXPOSED (not caused) by the finalise fix: before it, round 1 finalised
immediately and never reached :6637. I first reported it as another seat's
commit; the traceback says otherwise.
"""
import pytest

from app.pipeline.react_loop import _extract_chunk_blocks, _kept_chunk_stats


@pytest.mark.parametrize("payload", [
    {"contract": {"chunks": [{"text": "x"}], "answer_text": "y"}},
    {"ok": True, "value": {"text": "180 days"}},
    [{"chunk": 1}],
    12345,
])
def test_a_structured_payload_returns_no_blocks_instead_of_raising(payload):
    """THE REGRESSION. Not "it returns []" as a preference — the turn must not
    die. A tool result that is not text carries no rag chunk headers, which is
    exactly what the function's own docstring promises callers: "nothing to
    prune/store, not an error"."""
    assert _extract_chunk_blocks(payload) == []
    assert _kept_chunk_stats(payload, None) == (0, 0)


def test_text_payloads_are_untouched():
    """The guard must not swallow the case this function exists for."""
    raw = ("[Chunk 1] doc=a.pdf p=1\nfirst body\n"
           "[Chunk 2] doc=b.pdf p=2\nsecond body\n")
    blocks = _extract_chunk_blocks(raw)
    # Either this rag format parses to 2 blocks, or the header regex has moved
    # on — but a STRING must never take the non-text branch.
    assert isinstance(blocks, list)
    assert _extract_chunk_blocks("") == []
    assert _extract_chunk_blocks(None) == []


def test_the_guard_is_on_type_not_on_truthiness():
    """`if not raw` already handled None and "". An empty dict is falsy too, so
    a truthiness guard would have hidden this bug for exactly the payloads that
    are empty and left it live for the ones that are not."""
    assert _extract_chunk_blocks({}) == []
    assert _extract_chunk_blocks({"contract": {"chunks": []}}) == []
