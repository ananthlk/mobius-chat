"""A model that returned nothing must not be counted as a model that answered.

`_call_llm_json` ends `return (raw or "").strip()`. A provider answering
HTTP 200 with zero output tokens raises no exception, so the loop's
`except` branch -- and every recovery rung hanging off it -- is skipped.
The empty string then parses to {} and is spent against MAX_UNUSABLE_ROUNDS.

Deep Research measured the same shape on their side: 21,823 tokens in,
output_tokens 0 back, surfaced to the user as "response did not parse" --
a claim about the request, when nobody had answered it.

These tests DRIVE THE LOOP. Asserting the branch exists would not show it
is reached.
"""
import pytest

from app.pipeline.v2 import loop as L
from tests.test_v2_loop_integration import _ctx  # the real context builder


@pytest.fixture
def empty_model(monkeypatch):
    """The provider returns "" -- successfully. No exception, as in life."""
    import app.pipeline.react.prompts as prompts
    import app.pipeline.react_loop as rl

    calls = {"llm": 0, "finalize": []}

    def silent_llm(system, user, **kw):
        calls["llm"] += 1
        return ""                      # 200 OK, zero output tokens

    monkeypatch.setattr(prompts, "_call_llm_json", silent_llm)
    monkeypatch.setattr(rl, "_execute_tool_with_retry",
                        lambda *a, **k: {"tool": a[0], "success": True,
                                         "result": "x", "sources": []})
    monkeypatch.setattr(rl, "_finalize_response",
                        lambda *a, **k: calls["finalize"].append(a))
    monkeypatch.setattr(rl, "run_react", lambda ctx, emitter=None: None)
    return calls


def test_an_empty_model_reply_is_named_model_empty(empty_model):
    ctx = _ctx()
    L.run_react_v2(ctx, emitter=lambda m: None)

    stopped = getattr(ctx, "v2_stopped_by", None)
    assert stopped == "model_empty", (
        f"an unanswered call must be named for what it was; got {stopped!r}. "
        "'unusable_rounds' would blame the model for output it never produced."
    )


def test_an_unanswered_call_is_not_spent_as_an_unusable_round(empty_model):
    """The cost of the confusion, not just the label.

    Each empty reply used to burn a round against MAX_UNUSABLE_ROUNDS, so a
    provider returning nothing consumed the whole budget before the turn
    ended. One call is enough to learn the model is not answering.
    """
    ctx = _ctx()
    L.run_react_v2(ctx, emitter=lambda m: None)

    assert empty_model["llm"] == 1, (
        f"called the model {empty_model['llm']}x after it returned nothing; "
        "an empty reply is being spent as an unusable round"
    )
