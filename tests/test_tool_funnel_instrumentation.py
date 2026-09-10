"""The tool-selection funnel — P2 instrumentation from the Stage 0 report.

Stage 0 found two of four funnel stages unmeasurable and a third ambiguous, so
the layer behind the sporadic-tool-selection bug could not be named. These are
the three counts that make it nameable. They are instrumentation, not a fix —
no behaviour changes.

Per the P2 gate as amended: a count must be ATTRIBUTED, not merely present. Each
test below asserts that the count distinguishes two states that previously
produced the identical record — that is the property, not "a number appeared".
"""
from __future__ import annotations

import pytest


# ── stage 1: what was OFFERED, by name ───────────────────────────────
# `tool.offered` recorded a single `__unfiltered__` sentinel; over 76 turns it
# was the only value ever written. It recorded a fact about the FILTER, never
# about the manifest, so "was tool X offered?" had no answer.

def test_manifest_names_come_from_the_composer_not_a_parallel_list():
    """A second list of tool names would drift from the manifest the model sees,
    and a drifted answer to 'what was offered' is worse than none."""
    from app.pipeline.tool_manifest import get_manifest_tool_names, get_tool_manifest
    names = get_manifest_tool_names(None)
    assert names, "unfiltered manifest reported no tools"
    manifest = get_tool_manifest(None)
    # every reported name must actually appear in the rendered manifest text
    missing = [n for n in names if n not in manifest]
    assert not missing, f"reported as offered but absent from the manifest: {missing}"


def test_multi_tool_block_reports_every_tool_it_offers():
    """_APPEALS_BLOCK is gated on one key but documents five callable tools.
    Recording only the gate key would show appeals_get_playbook as never offered
    on turns where the model called it — a funnel artefact, not a fact."""
    from app.pipeline.tool_manifest import get_manifest_tool_names
    names = set(get_manifest_tool_names(None))
    for t in ("appeals_find_carc", "appeals_lookup_rules", "appeals_get_playbook"):
        assert t in names, f"{t} is callable and offered but not reported as offered"


def test_filtering_is_still_reflected():
    """The names must track the filter, or the count stops meaning 'offered'."""
    from app.pipeline.tool_manifest import get_manifest_tool_names
    assert get_manifest_tool_names(["rag"]) == ["rag"]
    assert get_manifest_tool_names([]) == []


# ── stage 2: emitted vs unparseable ──────────────────────────────────

def test_unparseable_and_none_are_distinct_targets():
    """Previously both landed on `__none__`. One is the planner choosing not to
    act; the other is the planner acting and us dropping it. Opposite
    conclusions from the same number — and the reason the json-formatting
    hypothesis could not be tested."""
    import inspect
    from app.pipeline import react_loop
    src = inspect.getsource(react_loop)
    assert '"__unparseable__"' in src
    assert '"__unparseable_recovered__"' in src
    assert '"__none__"' in src


def test_recovered_parse_failure_is_recorded_at_all():
    """A retry that SUCCEEDS previously called emit() only — which goes to the
    turn's thinking chunks, not to logs. So it cost a whole extra LLM round and
    left nothing queryable, and '0 parse-failure warnings' never meant '0 parse
    failures'."""
    import inspect
    from app.pipeline import react_loop
    src = inspect.getsource(react_loop)
    i_flag = src.index("_parse_recovered = True")
    i_emit = src.index('emit("  Format-correction retry succeeded.")')
    assert i_flag < i_emit, "the recovery must be recorded, not just emitted"


# ── stage 3: returned-nonempty ───────────────────────────────────────

@pytest.mark.parametrize("result,expected_empty", [
    ({"success": True, "result": "a real playbook", "sources": []}, False),
    ({"success": True, "result": "", "sources": []}, True),
    ({"success": True, "result": "   ", "sources": []}, True),
    ({"success": True, "result": None, "sources": []}, True),
    ({"success": True, "result": None, "sources": [{"id": 1}]}, False),
    ({"success": True, "result": [], "sources": []}, True),
    ({"success": True, "result": [{"x": 1}], "sources": []}, False),
    ({"success": False, "result": "", "sources": []}, True),
    (None, True),
])
def test_emptiness_is_independent_of_the_success_flag(result, expected_empty):
    """`success` says the call did not error; it does not say anything came
    back. Imports the REAL function — reimplementing it here would only assert
    that a copy behaves like itself."""
    from app.pipeline.react_loop import tool_result_is_empty
    assert tool_result_is_empty(result) is expected_empty


def test_success_and_emptiness_are_recorded_as_separate_kinds():
    """Folded into tool.dispatched's target, the change would have broken
    comparability with the history the Stage 0 funnel was built from."""
    from app.telemetry.spans import KIND_TOOL_DISPATCHED, KIND_TOOL_RESULT, _KINDS
    assert KIND_TOOL_DISPATCHED != KIND_TOOL_RESULT
    assert KIND_TOOL_RESULT in _KINDS
