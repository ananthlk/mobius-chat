"""Every skip path records its reason.

THE DEFECT, and it cost a false alarm. /diag/mcp reported

    {"discovered": 34, "registered": 0, "skipped": [], "error": null}

on six consecutive probes, and I read it as a total capability loss and told
Ananth chat had lost every MCP tool. It had not. All 34 were skipped
DELIBERATELY because a builtin already owns each name — the adapter's own
comment says builtins wrap MCP tools with extra logic and must win.

The registration loop has THREE skip paths and only ONE appended to `_skipped`:
non-dict entries recorded; malformed descriptors and name collisions returned
`continue` silently. So a deliberate policy and a silent failure produced the
same three numbers.

The function's own docstring had already named this exact hazard — "the listing
returning zero and the listing returning 29 that were all skipped are opposite
problems with opposite fixes, and the same three words" — and then implemented
it for one path of three.
"""

import ast
import inspect

from app.skills import mcp_adapter as A


def _register_fn():
    tree = ast.parse(inspect.getsource(A))
    return next(f for f in ast.walk(tree)
                if isinstance(f, ast.FunctionDef) and f.name == "register_mcp_skills")


def test_every_continue_in_the_loop_records_a_skip():
    """Asserts the PROPERTY over the AST: no `continue` inside the registration
    loop is reachable without appending to _skipped first. Naming the two paths
    I happened to fix would pass again the day a fourth is added."""
    fn = _register_fn()
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.For)]
    assert loops, "registration loop not found — did it move?"

    unrecorded: list[int] = []
    for loop in loops:
        for node in ast.walk(loop):
            if not isinstance(node, ast.If):
                continue
            body = node.body
            if not any(isinstance(x, ast.Continue) for x in body):
                continue
            appends = [
                x for x in ast.walk(ast.Module(body=body, type_ignores=[]))
                if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)
                and x.func.attr == "append"
                and isinstance(x.func.value, ast.Name)
                and x.func.value.id == "_skipped"
            ]
            if not appends:
                unrecorded.append(node.lineno)
    assert not unrecorded, (
        f"skip path(s) at line(s) {unrecorded} `continue` without recording a "
        "reason — /diag/mcp will report them as though nothing happened")


def test_the_report_carries_an_outcome_and_a_total():
    """`skipped` is truncated to 40 for the payload, so a count that disagrees
    with the list length is the only way to know it was truncated — and
    `outcome` is what stops a reader inferring an outage from three numbers."""
    src = inspect.getsource(A)
    for key in ('"skipped_total"', '"outcome"'):
        assert key in src, f"{key} missing from the discovery report"


def test_reasons_are_prefixed_so_they_can_be_counted():
    """`builtin_wins:get_top_orgs` — a reader (and a counter) can tell WHICH
    policy skipped it. A bare tool name could not."""
    src = inspect.getsource(A)
    for reason in ("builtin_wins:", "malformed:", "non-dict:"):
        assert reason in src, f"skip reason {reason!r} not recorded"


def test_registering_nothing_is_explained_not_just_logged():
    """A boot that registers zero is worth one line even when it is correct —
    and the line must say it is not necessarily an outage, because that is the
    reading it produced."""
    src = inspect.getsource(A)
    assert "0 registered" in src
    assert "NOT an outage" in src
