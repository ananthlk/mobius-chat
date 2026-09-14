"""The rag call ceiling is lifted for v2 and unchanged for v1.

Ananth, 2026-09-14: "we are passing the exhaust_rag field to react because we
have the 3 round limit, remove that limit for now because i think rag is
struggling as it thinks rag is doine and there is nothing more to share".

Two things were true and both are his point: react stops asking well before the
corpus is exhausted, and the terminal "budget exhausted" signal reads to it as
"there is nothing more to share" rather than "you spent your allowance".
"""
import types

import app.pipeline.react_loop as RL
from app.pipeline.react.prompts import _react_reasoning_system


def _ctx(arm, mode="copilot"):
    return types.SimpleNamespace(orchestrator_version=arm, chat_mode=mode)


def test_v1_keeps_the_shared_per_mode_ceiling():
    """v1 is the control arm. Changing its ceiling makes the A/B measure this
    instead of the orchestrator."""
    assert RL._rag_ceiling_for(_ctx("v1", "copilot")) == 3
    assert RL._rag_ceiling_for(_ctx("v1", "agentic")) == 6


def test_v2_is_effectively_unlimited():
    assert RL._rag_ceiling_for(_ctx("v2", "copilot")) >= 99
    assert RL._rag_ceiling_for(_ctx("v2", "agentic")) >= 99


def test_v2_is_NOT_literally_unbounded():
    """The turn is already bounded by max_rounds and the time budget. This
    removes a second, tighter cap -- not every backstop. A literal None would
    leave a retrieval loop with none if either of those regressed."""
    v = RL._rag_ceiling_for(_ctx("v2"))
    assert isinstance(v, int) and v > 0


def test_react_is_TOLD_the_ceiling_that_applies_to_it():
    """rule 1b renders {{ rag_call_ceiling }} as a hard limit. Leaving it at 3
    while the loop allows 99 would have react stop at a cap that no longer
    exists -- a prompt asserting a constraint the code does not enforce."""
    lifted = _react_reasoning_system(6, "copilot", rag_call_ceiling=99)
    assert "99 rag calls" in lifted, "react was not told the lifted ceiling"
    assert "3 rag calls" not in lifted

    # and with no override, v1's wording is byte-identical to before
    default = _react_reasoning_system(6, "copilot")
    assert "3 rag calls" in default


def test_the_loop_and_the_prompt_use_the_SAME_source():
    """Two callers deriving the ceiling separately is how they drift -- the
    communicate gate did exactly that tonight, fixed in a helper the block did
    not call."""
    import ast
    import inspect

    src = inspect.getsource(RL)
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_rag_ceiling_for"]
    assert len(calls) >= 2, "the prompt and the loop do not share the helper"
