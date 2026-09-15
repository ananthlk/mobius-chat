"""The bridge from v2 to Tool Manifest's executor — reached, not assumed.

🔴 THIS FILE EXISTS BECAUSE THE 81 EXISTING v2 TESTS DO NOT TOUCH THE BRIDGE.

Measured before writing it: making `_preload_runner_toolreg` raise
AssertionError on its first line leaves test_v2_preload, test_v2_round_report
and test_v2_kept_ordinals all green — 81 passed. So every claim about this
function that rests on those tests is unfounded, including "the toolreg
integration is fine, the suite is green".

react_loop.py's own comment predicted it: "Tool Manifest's executor is the
DEFAULT runner, so on every real turn the passages were never numbered ... The
gate worked perfectly and the feature was inert. Branch exists is not branch
reached, and a test of the other runner passes either way."
"""
import ast
import pathlib
import types

import pytest

from app.pipeline import react_loop


class _Ctx:
    correlation_id = "testcid0"


def _fake_v2_result(**kw):
    """Stands in for toolreg.v2.V2Result — attribute access, like the real one."""
    base = dict(tool="t", outcome="evidence", payload={"value": "v"},
                sources=[], effects={}, notes=[], duration_ms=12,
                route="POST http://x", reason="", ok=True, auto_retried=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


# ─────────────────────────── the guard that was disarmed

def test_speculative_has_no_default():
    """A parameter that decides WHETHER a guard runs must not have a default.

    The old bridge hardcoded speculative=True for BOTH call sites — including
    the verify path whose own comment says "DIRECTED ... never speculative".
    A default here is what let one literal misreport the other call site.
    """
    with pytest.raises(TypeError):
        react_loop._preload_runner_toolreg("rag", {}, _Ctx())


@pytest.mark.parametrize("speculative", [True, False])
def test_the_flag_reaches_the_executor_unchanged(monkeypatch, speculative):
    """Steering the guard is worthless if the value never reaches the wire."""
    seen = {}

    def _spy(tool, inputs, *, speculative, **kw):
        seen["speculative"] = speculative
        return _fake_v2_result()

    import toolreg.v2 as _v2
    monkeypatch.setattr(_v2, "execute_tool", _spy)
    react_loop._preload_runner_toolreg("rag", {"query": "q"}, _Ctx(),
                                       speculative=speculative)
    assert seen["speculative"] is speculative


def test_every_call_site_states_its_intent():
    """🔴 A STATIC GATE, BECAUSE THE TWO CALL SITES ARE INLINE LAMBDAS.

    The defect was not in the bridge — it was that two callers with opposite
    intent shared one hardcoded literal. A runtime test of the bridge cannot
    see that. This reads react_loop.py and requires every call to pass
    `speculative=` explicitly, so a third caller cannot inherit someone else's
    risk posture by saying nothing.
    """
    src = pathlib.Path(react_loop.__file__).read_text()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "_preload_runner_toolreg"]
    assert calls, "no call sites found — this gate would pass vacuously"
    for c in calls:
        kws = {k.arg for k in c.keywords}
        assert "speculative" in kws, (
            f"call at line {c.lineno} does not state whether it is speculative")


# ─────────────────────────── what toolreg now returns, and what chat still does

def test_sources_from_the_executor_reach_the_caller(monkeypatch):
    """Tool Manifest returns rag's chunks and the fact store's singular source
    now. The bridge must pass them through, not re-derive them."""
    rows = [{"document_id": "d1", "page_number": 3, "text": "a"},
            {"document_id": "d2", "page_number": 9, "text": "b"}]
    import toolreg.v2 as _v2
    monkeypatch.setattr(_v2, "execute_tool",
                        lambda *a, **k: _fake_v2_result(sources=rows))
    out = react_loop._preload_runner_toolreg("rag", {"query": "q"}, _Ctx(),
                                             speculative=True)
    assert out["ok"] is True
    assert [s["document_id"] for s in out["sources"]] == ["d1", "d2"]


def test_the_ratchet_is_silent_when_toolreg_supplies_sources(monkeypatch, caplog):
    """REVERSE RATCHET. The hand-recovery is kept but must be silent."""
    import toolreg.v2 as _v2
    monkeypatch.setattr(_v2, "execute_tool", lambda *a, **k: _fake_v2_result(
        sources=[{"document_id": "d1", "text": "a"}],
        payload={"contract": {"chunks": [{"document_id": "d1"}]}}))
    with caplog.at_level("WARNING"):
        react_loop._preload_runner_toolreg("rag", {"query": "q"}, _Ctx(),
                                           speculative=True)
    assert "RECOVERY FIRED" not in caplog.text


def test_a_tool_with_no_passages_is_not_reported_as_a_regression(monkeypatch, caplog):
    """🔴 THE FIRST LIVE CALL AFTER THIS CHANGE CRIED WOLF.

    appeals_lookup_rules returns RULES, not passages, and correctly has no
    provenance rows. The ratchet warned anyway, because it fired on `sources`
    being empty rather than on having recovered anything. A warning that fires
    for every sourceless tool trains the next reader to skip it — which is the
    one outcome that makes the ratchet worse than nothing.
    """
    import toolreg.v2 as _v2
    monkeypatch.setattr(_v2, "execute_tool", lambda *a, **k: _fake_v2_result(
        sources=[], payload={"rules": [{"triggers_when": "…"}], "found": True}))
    with caplog.at_level("WARNING"):
        react_loop._preload_runner_toolreg("appeals_lookup_rules", {}, _Ctx(),
                                           speculative=False)
    assert "RECOVERY FIRED" not in caplog.text


def test_the_ratchet_fires_when_the_recovery_finds_what_toolreg_missed(monkeypatch, caplog):
    """The one fact that means toolreg stopped honouring its promise."""
    import toolreg.v2 as _v2
    monkeypatch.setattr(_v2, "execute_tool", lambda *a, **k: _fake_v2_result(
        sources=[],
        payload={"contract": {"chunks": [{"document_id": "d1", "text": "a"}]}}))
    with caplog.at_level("WARNING"):
        out = react_loop._preload_runner_toolreg("rag", {"query": "q"}, _Ctx(),
                                                 speculative=True)
    assert "RECOVERY FIRED" in caplog.text
    assert len(out["sources"]) == 1, "the turn must still get its provenance"


def test_could_not_run_never_reads_as_an_empty_corpus(monkeypatch):
    """`empty` is a claim about the CORPUS; could_not_run is a claim about our
    code. The summary a person sees must not confuse them."""
    import toolreg.v2 as _v2
    monkeypatch.setattr(_v2, "execute_tool", lambda *a, **k: _fake_v2_result(
        outcome="could_not_run", payload=None, sources=[],
        reason="contract.status=timeout"))
    out = react_loop._preload_runner_toolreg("rag", {"query": "q"}, _Ctx(),
                                             speculative=True)
    assert out["ok"] is False
    assert "COULD NOT RUN" in out["summary"]
    assert "no lookup was performed" in out["summary"]


def test_effects_are_allow_listed_not_setattr_ed_blindly(monkeypatch):
    """Two tools in flight on one ctx race. A new effect name must be a
    deliberate change here, never something toolreg can invent."""
    import toolreg.v2 as _v2
    monkeypatch.setattr(_v2, "execute_tool", lambda *a, **k: _fake_v2_result(
        effects={"ctx_effects": {"plan": ["p"], "arbitrary_new_key": "BOOM"}}))
    ctx = _Ctx()
    react_loop._preload_runner_toolreg("rag", {"query": "q"}, ctx,
                                       speculative=True)
    assert getattr(ctx, "plan", None) == ["p"]
    assert not hasattr(ctx, "arbitrary_new_key")
