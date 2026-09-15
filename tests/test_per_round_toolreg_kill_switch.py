"""The per-round toolreg path must be revertible without a redeploy.

MOBIUS_V2_TOOLREG_EXEC gates preload and verify. The appeals cut added a THIRD
route into Tool Manifest's executor and was not gated by it — so when
appeals_find_carc came back "no executable route declared" from the deployed
catalogue on 2026-09-15, there was no way to turn the new path off short of a
redeploy.

I had spent a whole commit making that switch deployable (it was absent from
deploy.sh's allowlist, so the documented "~90-second revert" was wired to
nothing) and then reviewed a change that needed it without checking it applied.
"""
import ast
import inspect

from app.pipeline import react_loop


def _wrapper_src():
    return inspect.getsource(react_loop._execute_tool_with_retry)


def test_the_toolreg_branch_reads_the_kill_switch():
    """🔴 The property: the branch that routes to toolreg must be conditional
    on the same env var as every other toolreg path, so one flip reverts all
    of them."""
    src = _wrapper_src()
    i = src.index("_TOOLREG_OWNED")
    # the guard and the switch must be in the SAME condition, not merely both
    # present somewhere in the function
    window = src[max(0, i - 200): i + 400]
    assert "MOBIUS_V2_TOOLREG_EXEC" in window, (
        "the per-round toolreg branch is not gated by the kill switch — it "
        "cannot be reverted without a redeploy"
    )


def test_the_switch_is_read_as_a_condition_not_just_mentioned():
    """A comment naming the env var would satisfy a substring check. Parse it."""
    src = _wrapper_src()
    tree = ast.parse(src.lstrip())
    guarded = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.dump(node.test)
        if "_TOOLREG_OWNED" in test_src and "MOBIUS_V2_TOOLREG_EXEC" in test_src:
            guarded.append(node.lineno)
    assert guarded, (
        "no single `if` tests both membership and the kill switch — the two "
        "must be one condition or the switch does not govern the branch"
    )


def test_every_routed_tool_is_named_explicitly():
    """Not read from the catalogue at import: routing must not depend on a
    database being reachable, or a catalogue outage silently falls back to
    chat's branches and an A/B compares two undistinguished things."""
    assert isinstance(react_loop._TOOLREG_OWNED, frozenset)
    assert react_loop._TOOLREG_OWNED, "the routed set is empty"
    for t in react_loop._TOOLREG_OWNED:
        assert isinstance(t, str) and t.startswith("appeals_")
