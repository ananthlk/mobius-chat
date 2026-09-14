"""v2 sends the turn's real rag call number; v1 keeps the turn floor.

Ananth, 2026-09-14: "we need to pass the round information to rag so that it
uses the google search and llm more effectively".

That is precisely what call_number gates. Retriever confirmed from their own
code and live: portfolio.py's _STRATEGY_MIN_TURN = {"d": 2, "c": 3} -- d is the
web/google arm, c is reverse-RAG -- per QUERY, unconditional on slot
confidence. Their measured lift on a comparable fan-out: lb95 0.474 -> 0.697
between call 1 and call 3.

Hardcoding 1 meant every v2 corpus call asked at the turn floor, so the
strongest arms were reachable and almost never reached.
"""
import ast
import inspect

import app.pipeline.react_loop as RL


def _call_number_expr():
    """The expression assigned to the "call_number" key on the first dispatch."""
    tree = ast.parse(inspect.getsource(RL))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if (isinstance(k, ast.Constant) and k.value == "call_number"
                    and isinstance(v, ast.IfExp)):
                return ast.unparse(v)
    return None


def test_v2_sends_the_turns_real_call_number():
    expr = _call_number_expr()
    assert expr, "the first dispatch no longer branches call_number by arm"
    assert "_rag_call_number" in expr, expr


def test_v1_still_sends_the_turn_floor():
    """v1 is the control arm. Escalating it would make the A/B measure RAG's
    strategy ladder instead of the orchestrator."""
    expr = _call_number_expr()
    assert "orchestrator_version" in expr, expr
    assert expr.rstrip().endswith("else 1"), expr


def test_the_counter_is_bound_before_the_dispatch_uses_it():
    """react_loop is 8,900 lines and a name used above its assignment raises
    only when the line RUNS -- inside a fail-soft. That killed preload tonight
    (`rn` read 314 lines above its binding) and the trace still said the tool
    had run."""
    src = inspect.getsource(RL)
    tree = ast.parse(src)
    binds = [n.lineno for n in ast.walk(tree)
             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
             and n.id == "_rag_call_number"]
    reads = [n.lineno for n in ast.walk(tree)
             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
             and n.id == "_rag_call_number"]
    assert binds and reads
    assert min(binds) < min(reads), (
        f"_rag_call_number is read at {min(reads)} before its first "
        f"assignment at {min(binds)}")
