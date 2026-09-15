"""A CARC the appeals library does not cover is an ANSWER, not a tool failure.

Measured live 2026-09-15. Asked "How do I appeal a CARC 24 denial?", the user
received:

    "I attempted to look up appeal rules and playbooks for CARC 24, but the
     tools failed to execute correctly."

A confession about our internals. The truth was simpler and actionable: the
appeals service holds 18 CARCs and 24 is not one of them. It said so —
404 {"detail": "CARC 24 not in library"} — and raise_for_status turned that
answer into an exception, which became "[tool] Error: ...", which react
reported as a broken tool.

This is the empty-versus-could_not_run distinction in chat's own branch: a 404
that NAMES the gap is the library answering; anything else is us failing.
"""
import inspect

from app.pipeline import react_loop

SRC = inspect.getsource(react_loop._execute_tool)


def _handler_block() -> str:
    """The _AppealsNotInLibrary handler, sliced to a REAL boundary.

    An earlier version took a fixed 900 characters and cut the block before
    its `signal` line, so the test failed on correct code. Slicing by char
    count is how a gate ends up asserting against half a statement.
    """
    i = SRC.index("except _AppealsNotInLibrary")
    nxt = SRC.index("except Exception", i)
    return SRC[i:nxt]


def test_a_named_library_gap_is_distinguished_from_a_failure():
    assert "_AppealsNotInLibrary" in SRC, (
        "no distinct type for a named library gap — it would be caught by the "
        "generic handler and reported as a tool error"
    )
    assert "not in library" in SRC


def test_a_bare_404_still_fails_loudly():
    """🔴 Do NOT widen this to all 404s. A bare 404 ('Not Found') is a wrong
    URL — our defect — and reporting it to the user as an absence in the
    library would hide a broken integration behind an honest-sounding answer.
    Both bare paths were measured returning exactly that on 2026-09-15."""
    i = SRC.index("_AppealsNotInLibrary(_detail)")
    window = SRC[max(0, i - 600):i]
    assert 'in _detail.lower()' in window, (
        "the gap must be recognised by the service NAMING it, not by the "
        "status code alone"
    )


def test_the_gap_carries_the_no_sources_signal():
    """An earned absence takes the same shape as any other, so the retry guard
    sees it and react does not re-ask a question the library cannot answer.
    Before this, react retried appeals_lookup_rules three rounds running."""
    block = _handler_block()
    assert "RETRIEVAL_SIGNAL_NO_SOURCES" in block, (
        "the gap must carry the absence signal or the retry guard cannot see it"
    )
    # _no_src() is chat's shared no-sources shape (success=False). Asserting
    # the helper rather than re-deriving its fields keeps this test pinned to
    # the contract instead of to a literal that can drift.
    assert "_no_src()" in block, (
        "the gap must use the shared no-sources shape, not a hand-built dict"
    )
    assert '"success": True' not in block, "an absence is not a success"


def test_the_result_text_tells_react_what_to_do_instead():
    """A bare absence leaves react with nothing; the payer's general appeal
    process is still a real answer to the person's question."""
    block = _handler_block()
    assert "general appeal process" in block, (
        "the absence must point react at the answer that IS available"
    )
