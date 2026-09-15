"""The promise flag must reach BOTH retry layers, not just chat's.

There are two retry mechanisms and one flag:

    chat  _execute_tool_with_retry   Layer A, logical retry   <- skip_retry steered
    toolreg  run_mcp(retries=2)      TRANSPORT retry          <- it did not

Quick mode promises 13s and sets skip_retry=True. Against a dead transport,
three attempts is 3x the timeout with nothing able to intervene -- and chat's
promise-fit guard only governs Layer A, so it could not see those attempts
either. A control that governs half the system while appearing to govern all
of it is the defect this fleet spent the session removing.

Tool Manifest threaded `transport_retries` and made `skip_retry` resolve it to
zero. That fix is INERT unless chat passes the flag: a parameter nobody passes
is a producer without a consumer, and `skip_retry` was already in scope at the
call site, unused.
"""
import ast
import inspect

from app.pipeline import react_loop


def _src(fn):
    return inspect.getsource(fn)


def test_the_toolreg_helper_accepts_the_flag():
    sig = inspect.signature(react_loop._execute_via_toolreg)
    assert "skip_retry" in sig.parameters, (
        "the helper cannot forward a flag it does not take"
    )


def test_the_flag_is_forwarded_to_the_executor():
    """🔴 The assertion that matters. toolreg's fix does nothing unless this
    call carries the flag."""
    src = _src(react_loop._execute_via_toolreg)
    tree = ast.parse(src.lstrip())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "_tr_exec"]
    assert calls, "no call to the toolreg executor found — re-read this file"
    for c in calls:
        kws = {k.arg for k in c.keywords}
        assert "skip_retry" in kws, (
            "the toolreg executor is called without skip_retry — quick mode "
            "would still get up to 3 transport attempts against a 13s promise"
        )


def test_the_call_site_passes_the_wrappers_own_flag():
    """It must forward the enclosing wrapper's parameter, not a literal.
    Hardcoding False here would re-create the defect one level down."""
    src = _src(react_loop._execute_tool_with_retry)
    i = src.index("_execute_via_toolreg(")
    window = src[i:i + 300]
    assert "skip_retry=skip_retry" in window, (
        "the call site must forward the wrapper's skip_retry, not a constant"
    )


def test_quick_mode_still_sets_the_flag_at_the_top():
    """The chain is only as good as its first link: react's per-round dispatch
    must still set skip_retry in quick mode."""
    src = inspect.getsource(react_loop)
    assert 'skip_retry=(mode_label == "quick")' in src, (
        "quick mode no longer requests skip_retry — the whole chain is moot"
    )
