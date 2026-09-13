"""emit_step must produce what on_thinking's structured branch actually accepts.

2026-09-13, live defect: emit_step passed the EmitEnvelope DATACLASS. on_thinking
gates on ``isinstance(chunk, dict) and is_envelope(chunk)``, so it failed that
check, fell to the bare-string elif, and every v2 step reached the user as
``str(envelope)`` -- the dataclass repr. Nothing raised; the structured-emit
except never fired; the trace was correct and discarded one isinstance short of
the renderer.

THESE TESTS ASSERT THROUGH THE REAL CONSUMER PREDICATE (is_envelope, imported
from the module on_thinking imports it from), never against ``dict`` directly.
A test that asserts ``isinstance(x, dict)`` passes on any dict and would not
have caught this; a test that re-implements the predicate is a second author of
the contract.
"""
from app.communication.emit_envelope import is_envelope
from app.pipeline.v2 import trace as v2tr


def _capture():
    seen = []
    return seen, seen.append


def test_emit_step_output_passes_on_thinkings_real_gate():
    seen, emitter = _capture()
    v2tr.emit_step(emitter, "cid-1234",
                   v2tr.Step("preload", "✓ Found 15 passage(s)",
                             (v2tr.kv("arms", 3),), {"n": 15},
                             source="rag", key="preload", state="done"))
    assert len(seen) == 1, "emit_step published nothing"
    chunk = seen[0]
    # The exact conjunction on_thinking evaluates. If this fails, the emit
    # takes the legacy string branch and the user sees a repr.
    assert isinstance(chunk, dict) and is_envelope(chunk), (
        f"emit_step output does NOT reach on_thinking's structured branch; "
        f"it would render as str(...) -> {str(chunk)[:120]!r}"
    )


def test_headline_rides_as_note_and_detail_is_expandable():
    """on_thinking shows chunk['note']; the FE expands chunk['data']['detail']."""
    seen, emitter = _capture()
    v2tr.emit_step(emitter, "cid-1234",
                   v2tr.Step("verify", "✓ Checked 5 claim(s)",
                             (v2tr.item("claim A", "✓"),), {},
                             source="tool manifest", key="verify"))
    d = seen[0]
    assert d["note"] == "✓ Checked 5 claim(s)"
    detail = d["data"]["detail"]
    # source is prepended as the first detail line -- attribution is part of
    # the contract, not decoration.
    assert detail[0] == v2tr.kv("from", "tool manifest")
    assert any("claim A" in line for line in detail)


def test_key_and_state_ride_so_running_collapses_to_done():
    """Same key emitted twice = one row replaced in place, not two rows."""
    seen, emitter = _capture()
    for state in ("running", "done"):
        v2tr.emit_step(emitter, "cid", v2tr.Step("preload", f"preload {state}",
                                                 key="preload", state=state))
    assert [c["data"]["key"] for c in seen] == ["preload", "preload"]
    assert [c["data"]["state"] for c in seen] == ["running", "done"]


def test_signal_is_v2_trace_so_a_dedicated_renderer_can_find_it():
    seen, emitter = _capture()
    v2tr.emit_step(emitter, "cid", v2tr.Step("plan", "planning"))
    assert seen[0]["signal"] == "v2_trace"


def test_preload_done_shows_the_query_it_asked():
    """The INPUT that decides whether 15 passages are the right 15.

    2026-09-13, Ananth: "i dont think it is reformatting the question enough".
    The query reached react (frame.py) but appeared in neither the emit nor the
    log, so "did we reformulate?" was unanswerable from telemetry.
    """
    step = v2tr.preload_done_step(
        [{"tool": "rag", "ok": True, "summary": "15 passage(s)",
          "asked": "care management reimbursement T1017 Sunshine Aetna UHC",
          "sources": [{"document_name": "Sunshine Provider Manual"}]}],
        elapsed_s=8.2)
    asked_lines = [d for d in step.detail if "asked" in d]
    assert asked_lines, f"preload step hides the query it sent: {step.detail}"
    assert "T1017" in asked_lines[0]
    # The input is stated BEFORE the outcome.
    assert step.detail.index(asked_lines[0]) < next(
        i for i, d in enumerate(step.detail) if "rag →" in d)


def test_preload_done_without_a_query_emits_no_empty_asked_line():
    step = v2tr.preload_done_step([{"tool": "rag", "ok": True,
                                    "summary": "3 passage(s)", "sources": []}])
    assert not [d for d in step.detail if "asked" in d]
