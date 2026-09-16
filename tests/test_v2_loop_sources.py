

def test_a_preloaded_result_carrying_sources_upgrades_the_signal():
    """The previous gate asserted _upgrade_signal was CALLED on preload results.

    It was called. It could not do anything: preload.execute emits no `signal`
    key, so the function read a field that does not exist and returned the
    input. Thirteen sources shipped under a `no_sources` strip.

    So assert the EFFECT on a result shaped the way preload actually shapes
    one -- built from the real producer's keys, not from a guess at them.
    """
    from app.pipeline.v2.loop import _upgrade_signal
    from app.services.doc_assembly import RETRIEVAL_SIGNAL_NO_SOURCES

    preload_shaped = {
        "tool": "rag", "ok": True, "payload": "...", "summary": "s",
        "sources": [{"document": "FL Medicaid Handbook"}],
    }
    assert "signal" not in preload_shaped, "preload emits no signal key -- that is the whole bug"

    out = _upgrade_signal(RETRIEVAL_SIGNAL_NO_SOURCES, preload_shaped)
    assert out != RETRIEVAL_SIGNAL_NO_SOURCES, (
        "a result carrying sources must not leave the signal at no_sources"
    )


def test_sources_do_not_upgrade_when_the_tool_failed():
    """v1's rule (react_loop.py:9508): a failed tool does not upgrade."""
    from app.pipeline.v2.loop import _upgrade_signal
    from app.services.doc_assembly import RETRIEVAL_SIGNAL_NO_SOURCES

    failed = {"tool": "rag", "success": False, "ok": False,
              "sources": [{"document": "x"}]}
    assert _upgrade_signal(RETRIEVAL_SIGNAL_NO_SOURCES, failed) == RETRIEVAL_SIGNAL_NO_SOURCES
