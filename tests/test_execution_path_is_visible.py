"""Which path served a tool must be visible in the turn.

Ananth, 2026-09-15: "when we flip to tools_manifest i want that to go through
tools_manifest.. put emits so that i can track".

Until this, chat's own branch and Tool Manifest's executor produced the same
result shape for the same tool and nothing said which had run. That is what
made the appeals outage take three hours: appeals_get_playbook worked while
appeals_lookup_rules did not, and no line in any trace named the path either
had taken — so the evidence was equally consistent with a missing catalogue
row, a dead server, and a stale cache. All three were investigated. None was
the cause.
"""
import inspect

from app.pipeline import react_loop

SRC = inspect.getsource(react_loop._execute_tool_with_retry)


def test_both_paths_announce_themselves():
    """🔴 BOTH, not just the new one. A trace that names only the toolreg path
    means silence is ambiguous: it could be chat's branch, or it could be a
    tool that never dispatched at all."""
    assert "via tool-manifest" in SRC
    assert "via chat" in SRC


def test_the_emit_sits_inside_the_branch_it_describes():
    """An emit before the `if` would claim a path the code may not take."""
    i_if = SRC.index("_TOOLREG_OWNED")
    i_tm = SRC.index("via tool-manifest")
    i_chat = SRC.index("via chat")
    assert i_tm > i_if, "the toolreg emit must be inside the guarded branch"
    assert i_chat > i_tm, "the chat emit must be in the else branch"


def test_the_emit_names_the_tool():
    """A bare 'via tool-manifest' is useless on a turn that runs three tools."""
    for marker in ("via tool-manifest", "via chat"):
        i = SRC.index(marker)
        line_start = SRC.rindex("\n", 0, i)
        assert "{tool}" in SRC[line_start:i + len(marker) + 5], (
            f"the {marker!r} emit does not interpolate the tool name"
        )
