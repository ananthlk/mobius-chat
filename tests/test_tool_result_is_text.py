"""A tool result is TEXT, and a structured payload must not kill the turn.

LIVE FAILURE, 2026-09-16, cids aa6f582d and c71a31be. Ananth's screen:

    ✗ Turn failed at orchestrator: 'dict' object has no attribute 'strip'
    Something went wrong — trying another path. Please try rephrasing your
    question.

    Traceback:
      react_loop.py:9554  run_react -> _checkpoint_best_evidence(...)
      react_loop.py:5620  text = (best.get("result") or "").strip()
      AttributeError: 'dict' object has no attribute 'strip'

NINE places in react_loop read `(tr.get("result") or "").strip()`. Every
producer writes text — the appeals branches json.dumps their payload before
returning — except the preload seed mapping, which passed toolreg's payload
through raw. One structured MCP result, and the first consumer to touch it
ended the turn.

Fixed at the PRODUCER. Patching the one consumer that happened to crash first
would have left eight more holding the same assumption.
"""

import ast
import inspect

import app.pipeline.react_loop as R


def test_a_structured_payload_becomes_text_not_a_crash():
    assert R._payload_text({"deadline_appeal_days": 90}) == '{"deadline_appeal_days": 90}'
    assert R._payload_text(["a", "b"]) == '["a", "b"]'
    assert R._payload_text("already text") == "already text"
    assert R._payload_text(None) == ""


def test_the_payload_is_serialized_not_dropped():
    """Returning "" for a dict would trade a crash for a silent loss of the
    evidence the round just paid to fetch."""
    out = R._payload_text({"payor": "Sunshine Health", "deadline_appeal_days": 90})
    assert "Sunshine Health" in out and "90" in out


def test_unserializable_payloads_still_produce_text():
    class Odd:
        def __repr__(self): return "<Odd>"
    assert R._payload_text({"x": Odd()})          # must not raise
    assert R._payload_text(Odd())


def test_the_seed_mapping_coerces_before_storing():
    """THE PRODUCER. Asserts over the AST that the preload seed writes
    _payload_text(...) into `result`, not the raw payload."""
    tree = ast.parse(inspect.getsource(R))
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "run_react")
    bad = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if (isinstance(k, ast.Constant) and k.value == "result"
                    and "_payload_text" not in ast.unparse(v)
                    and "payload" in ast.unparse(v)):
                bad.append(node.lineno)
    assert not bad, (
        f"line(s) {bad}: a raw payload is stored as `result` — nine readers "
        "in this module call .strip() on it")


def test_checkpointing_cannot_kill_the_turn_it_protects():
    """Its docstring promises exactly this and the guard covered only the
    write, while the SELECTION above it raised. A promise the code does not
    keep is worse than no promise: it stops the next reader looking."""
    src = inspect.getsource(R._checkpoint_best_evidence)
    tree = ast.parse(src.strip())
    tries = [n for n in ast.walk(tree) if isinstance(n, ast.Try)]
    assert tries, "no guard at all"
    guarded = ast.unparse(tries[0])
    for must in ("tool_results", "result_summary", "append_evidence_checkpoint"):
        assert must in guarded, (
            f"{must!r} sits OUTSIDE the guard — it can still end the turn")
