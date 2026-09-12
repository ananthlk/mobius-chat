"""Drive a FULL round of the v2 loop with a stubbed model and tool.

Three live runs produced three failures, and every one was a signature or
return-shape error that the source-text tests structurally could not catch:

  1. `_call_llm_json` returns a STRING; I treated it as a dict, so every round
     parsed unusable and the loop published an EMPTY answer that everything
     downstream filled with an ungrounded one.
  2. `_execute_tool_with_retry` requires SIX arguments; I passed five, and my
     own except swallowed the TypeError into "tool failed".
  3. The prompt was the legacy builder with no tool manifest, so the model had
     nothing to call.

All three were four seconds of inspect.signature away, and the gates I had
written asserted that the imports RESOLVE. This file asserts that a ROUND
WORKS: the loop calls the real functions with argument shapes the real
functions accept, parses what they really return, and produces an answer.

The stubs deliberately mimic the REAL contracts:
  _call_llm_json          -> str   (react's parser handles fences/prose)
  _execute_tool_with_retry-> dict  (six required positional params)
  _finalize_response      -> None  (six positional params)
"""

import inspect
from types import SimpleNamespace

import pytest

from app.pipeline.v2 import loop as L


def _ctx(**kw):
    """A REAL PipelineContext, not a namespace.

    The first version used SimpleNamespace and the loop died on
    `ctx.last_turns` — an attribute build_reasoning_context reads and a stub
    does not have. That is a FIXTURE lying about the runtime: a namespace
    answers only the attributes the test author thought of, so it cannot catch
    the ones they did not. The real class is the only faithful stand-in, and
    it is what the loop actually receives.
    """
    from app.pipeline.context import PipelineContext

    c = PipelineContext(
        correlation_id="test-cid-0000",
        thread_id="t1",
        message="What is the timely filing deadline for Sunshine Health?",
    )
    c.chat_mode = "copilot"
    c.react_trace_rounds = []
    c.promise = SimpleNamespace(latency_s=31.0)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


@pytest.fixture
def harness(monkeypatch):
    """Patch the real functions where the loop imports them FROM, and record
    every call so the test can assert on argument shapes rather than outcomes
    alone."""
    calls = {"llm": [], "tool": [], "finalize": [], "v1_loop": []}

    import app.pipeline.react.prompts as prompts
    import app.pipeline.react_loop as rl

    real_tool_sig = inspect.signature(rl._execute_tool_with_retry)
    real_fin_sig = inspect.signature(rl._finalize_response)

    def fake_llm(system, user, **kw):
        calls["llm"].append({"system": system, "user": user, "kw": kw})
        # A REAL model response: fenced JSON, as gemini actually emits.
        if len(calls["llm"]) == 1:
            return ('```json\n{"thought":"searching","evidence_review":'
                    '{"running_answer":"partial","gaps_closed":[],'
                    '"gaps_open":["Sunshine filing deadline"]},'
                    '"tool":"rag","inputs":{"query":"sunshine filing"},'
                    '"is_complete":false}\n```')
        return ('{"thought":"done","evidence_review":{"running_answer":'
                '"Initial claims: 180 days.","gaps_closed":["Sunshine filing '
                'deadline"],"gaps_open":[]},"tool":null,'
                '"answer":"Initial claims: 180 days.","is_complete":true}')

    def fake_tool(*args, **kw):
        # BOUND to the real signature — a call the real function would reject
        # raises here too, which is the whole point.
        real_tool_sig.bind(*args, **kw)
        calls["tool"].append({"args": args, "kw": kw})
        return {"tool": args[0], "success": True, "result": "chunks…",
                "sources": [{"document": "Sunshine Provider Manual"}]}

    def fake_finalize(*args, **kw):
        real_fin_sig.bind(*args, **kw)
        calls["finalize"].append({"args": args, "kw": kw})

    def fake_v1_loop(ctx, emitter=None):
        calls["v1_loop"].append(True)

    monkeypatch.setattr(prompts, "_call_llm_json", fake_llm)
    monkeypatch.setattr(rl, "_execute_tool_with_retry", fake_tool)
    monkeypatch.setattr(rl, "_finalize_response", fake_finalize)
    monkeypatch.setattr(rl, "run_react", fake_v1_loop)
    return calls


def test_a_full_round_runs_and_produces_an_answer(harness):
    """The test that would have caught all three live failures."""
    L.run_react_v2(_ctx(), emitter=lambda m: None)
    assert harness["llm"], "the loop never called the model"
    assert harness["tool"], "THE LOOP NEVER CALLED A TOOL"
    assert harness["finalize"], "the loop never published"
    answer = harness["finalize"][0]["args"][1]
    assert answer and answer.strip(), "published an EMPTY answer"
    assert not harness["v1_loop"], "deferred to v1 despite producing an answer"


def test_the_tool_is_called_with_arguments_the_REAL_function_accepts(harness):
    """`_execute_tool_with_retry` requires six. The stub binds against the live
    signature, so passing five raises here exactly as it did in production —
    where my own except swallowed it into "tool failed"."""
    L.run_react_v2(_ctx(), emitter=lambda m: None)
    args = harness["tool"][0]["args"]
    bound = inspect.signature(
        __import__("app.pipeline.react_loop", fromlist=["x"])._execute_tool_with_retry
    )
    assert len(args) >= 6, f"only {len(args)} positional args"
    assert args[0] == "rag"


def test_the_model_STRING_is_parsed_not_assumed_to_be_a_dict(harness):
    """The stub returns FENCED JSON — a string, as _call_llm_json really does.
    The first implementation did `raw if isinstance(raw, dict) else {}` and
    therefore saw {} forever."""
    L.run_react_v2(_ctx(), emitter=lambda m: None)
    assert harness["tool"], "a fenced-JSON response produced no tool call"
    assert harness["tool"][0]["args"][0] == "rag"


def test_the_prompt_comes_from_REACTS_LIVE_COMPOSITION(harness):
    """A first version asserted only that the prompt MENTIONS a tool — and the
    legacy builder mentions tools too, so reintroducing the real defect (the
    museum-piece prompt without allowed_tools) did NOT fail this test. Three of
    four live defects were caught; this one walked through.

    The defect was never "no word rag" — it was the WRONG SOURCE. So assert
    the provenance the loop records, which is the thing that actually differs.
    """
    ctx = _ctx()
    L.run_react_v2(ctx, emitter=lambda m: None)
    source = getattr(ctx, "v2_prompt_source", {}).get("source")
    assert source in ("v2_composition", "v1_composition"), \
        f"the loop fell back to the legacy builder (source={source!r})"
    system = harness["llm"][0]["system"]
    assert len(system) > 2000, "the system prompt is a stub"
    assert "rag" in system.lower(), "no tool manifest in the prompt"


def test_a_loop_that_produces_nothing_DEFERS_and_does_not_publish(harness, monkeypatch):
    """A void does not stay a void; downstream fills it. Live, that produced a
    fluent three-payer comparison with zero sources."""
    import app.pipeline.react.prompts as prompts
    monkeypatch.setattr(prompts, "_call_llm_json", lambda *a, **k: "not json at all")
    L.run_react_v2(_ctx(), emitter=lambda m: None)
    assert harness["v1_loop"], "did not defer to v1's loop"
    assert not harness["finalize"], "published a void instead of deferring"


def test_the_wall_clock_stops_a_loop_that_overran(harness, monkeypatch):
    """One round took 205s against a 95s promise and nothing noticed until it
    returned. The backstop runs at the top of every iteration."""
    import time as _t
    ticks = iter([0.0] + [10_000.0] * 50)
    monkeypatch.setattr(L.time, "monotonic", lambda: next(ticks, 10_000.0))
    L.run_react_v2(_ctx(), emitter=lambda m: None)
    # it must not have spent a round, and must not have published a void
    assert not harness["tool"], "spent a tool call with the budget exhausted"
