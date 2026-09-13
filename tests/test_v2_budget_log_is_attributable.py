"""A cid-less diagnostic in a two-arm system is actively misleading.

2026-09-13: `[v2] no promise on ctx` logged with no correlation_id. In an A/B
turn both arms run at the same instant and BOTH reach this line -- the served
arm, and the v1 shadow whose promise chat.py:563 pops deliberately. I read a
cid-less line next to v2's logs and reported "the promise never reaches the v2
arm" to Ananth twice as a confirmed defect. It was the v1 arm working as
designed.

Asserted on the LOG RECORD, not on the source text: a test that greps for
"cid=%s" in the module passes on a line that never executes, and one that reads
the comment passes on a comment.
"""
import logging
import types

import pytest

from app.pipeline.v2 import shadow


class _Ctx(types.SimpleNamespace):
    pass


def _emit(caplog, ctx):
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=shadow.logger.name):
        out = shadow.promise_seconds(ctx, None)
    recs = [r for r in caplog.records if "no promise on ctx" in r.getMessage()]
    return out, recs


def test_budget_falls_back_and_names_the_turn(caplog):
    ctx = _Ctx(correlation_id="452f4cc1-dead-beef-0000-000000000000",
               promise=None, ab_arm="v1", ab_shadow=True)
    out, recs = _emit(caplog, ctx)
    assert out == shadow._NO_PROMISE_FALLBACK_S
    assert len(recs) == 1, "the fallback must say so exactly once"
    msg = recs[0].getMessage()
    # The three facts that make the line readable at all.
    assert "452f4cc1" in msg, f"log cannot be attributed to a turn: {msg!r}"
    assert "v1" in msg, f"log does not say which arm: {msg!r}"
    assert "shadow=True" in msg, f"log does not say if it was served: {msg!r}"


def test_a_real_promise_logs_nothing_and_returns_its_latency(caplog):
    ctx = _Ctx(correlation_id="abcd1234", ab_arm="v2", ab_shadow=False,
               promise=types.SimpleNamespace(latency_s=95.0))
    out, recs = _emit(caplog, ctx)
    assert out == 95.0
    assert recs == [], "a turn WITH a promise must not log the fallback"


@pytest.mark.parametrize("missing", [{}, {"correlation_id": None}])
def test_missing_identity_degrades_to_a_marker_not_a_crash(caplog, missing):
    """A turn with no cid must still log -- and must not silently look like a
    named one."""
    ctx = _Ctx(promise=None, **missing)
    out, recs = _emit(caplog, ctx)
    assert out == shadow._NO_PROMISE_FALLBACK_S
    assert "?" in recs[0].getMessage()
