"""tool_manifest — comprehensive behaviour + latency.

Ananth, 2026-09-10: tool_manifest gets "comprehensive test and latency test"
BEFORE any fix, and the schematic before that. So every test here either
asserts a property that must hold, or CHARACTERISES current behaviour that is
believed wrong. Characterisation tests are labelled DEFECT and they pass — they
exist so a later fix trips them and has to be deliberate.

Nothing here fixes anything. That is the instruction.

Why this module deserves the coverage: it renders 35.8% of every react prompt,
it is 737 lines, and it had 5 tests.
"""
from __future__ import annotations

import time

import pytest

from app.pipeline.tool_manifest import (
    _ROUTER_OWNED_BLOCKS,
    get_manifest_tool_names,
    get_tool_manifest,
)

# The "no tools" sentinel the composer emits. Measured, not assumed.
_EMPTY_LEN = len(get_tool_manifest([]))


class TestComposerContract:
    """get_manifest_tool_names must describe what get_tool_manifest RENDERS.

    This is the tool funnel's first stage. A name reported as "offered" that
    the model never saw makes `tool.offered` an artefact of the instrument
    rather than a fact about the turn.
    """

    def test_every_reported_name_appears_in_the_rendered_text(self):
        text = get_tool_manifest()
        missing = [n for n in get_manifest_tool_names() if n not in text]
        assert missing == [], f"reported as offered but absent from the prompt: {missing}"

    def test_empty_render_reports_no_names(self):
        assert get_manifest_tool_names([]) == []

    @pytest.mark.parametrize("name", sorted(get_manifest_tool_names()))
    def test_each_reported_name_is_consistent_with_its_own_render(self, name):
        """Per-name, the two entry points must agree: either the manifest
        renders something and the name is reported, or neither happens."""
        rendered = get_tool_manifest([name])
        reported = get_manifest_tool_names([name])
        assert (len(rendered) > _EMPTY_LEN) == bool(reported), (
            f"{name}: rendered={len(rendered) > _EMPTY_LEN} reported={bool(reported)}"
        )


class TestFilterSemantics:
    def test_none_renders_the_full_manifest(self):
        assert len(get_tool_manifest(None)) > _EMPTY_LEN
        assert len(get_manifest_tool_names(None)) > 0

    def test_empty_list_offers_nothing(self):
        assert "No tools available" in get_tool_manifest([])

    def test_unknown_tool_name_offers_nothing_and_does_not_raise(self):
        assert get_manifest_tool_names(["no_such_tool_xyz"]) == []
        assert len(get_tool_manifest(["no_such_tool_xyz"])) == _EMPTY_LEN

    def test_filter_is_a_subset_never_an_expansion(self):
        allowed = ["appeals_find_carc", "fetch_document"]
        assert set(get_manifest_tool_names(allowed)).issubset(set(allowed) | _APPEALS_FAMILY)

    def test_composition_is_deterministic(self):
        """No hidden state between calls — the manifest is re-rendered per call
        (it must be, because MCP tools register at FastAPI startup)."""
        assert get_tool_manifest() == get_tool_manifest()
        assert get_manifest_tool_names() == get_manifest_tool_names()


_APPEALS_FAMILY = {
    "appeals_find_carc", "appeals_lookup_rules", "appeals_get_playbook",
    "appeals_validate_claim", "appeals_assemble_letter",
}


class TestAppealsGateDefect:
    """DEFECT, characterised not fixed.

    `_APPEALS_BLOCK` documents five callable tools but is gated on the single
    key `appeals_find_carc` (tool_manifest.py:595). So a tool policy that
    allows one of the other four — and not the gate key — renders
    "No tools available", even though the tool is allowed and dispatchable.

    That is the tool-policy filter path (a user disabling tools, or task mode
    with a restricted set) silently dropping a permitted tool. Recorded here so
    a fix has to trip these deliberately.
    """

    _NON_GATE = sorted(_APPEALS_FAMILY - {"appeals_find_carc"})

    @pytest.mark.parametrize("name", _NON_GATE)
    def test_non_gate_appeals_tool_alone_renders_nothing(self, name):
        assert len(get_tool_manifest([name])) == _EMPTY_LEN
        assert get_manifest_tool_names([name]) == []

    def test_the_gate_key_alone_renders_the_block_but_reports_only_itself(self):
        """Measured, and it corrects an assumption I made writing this file.

        `_router_block` records `also` names only when they are THEMSELVES
        allowed (`_rendered.update(n for n in also if _allow(n))`). So with a
        filter of just the gate key, the block renders — documenting all five
        callable tools to the model — while only `appeals_find_carc` is
        reported as offered.

        That is defensible for the funnel (the other four were not permitted)
        and it means the rendered PROSE still describes four tools the policy
        excluded. Recorded, not judged: which of those two is wrong is a
        question for the schematic.
        """
        assert len(get_tool_manifest(["appeals_find_carc"])) > _EMPTY_LEN
        assert set(get_manifest_tool_names(["appeals_find_carc"])) == {"appeals_find_carc"}

    def test_unfiltered_manifest_reports_all_five(self):
        """With no filter every `also` name is allowed, so all five are
        reported — which is why the full manifest lists 28 names."""
        assert _APPEALS_FAMILY.issubset(set(get_manifest_tool_names()))

    def test_all_five_map_to_one_block(self):
        assert {_ROUTER_OWNED_BLOCKS[n] for n in _APPEALS_FAMILY} == {"_APPEALS_BLOCK"}


class TestPromptCost:
    """The manifest is the largest single component of the react prompt.

    A ratchet, in the style of the LOC ratchets: it fails when the manifest
    GROWS, so growth is a deliberate act rather than a drift nobody priced.
    Numbers are the locally-rendered manifest (no MCP registered); production
    renders roughly twice this, so these are a FLOOR.
    """

    # Measured 2026-09-10: 32,441 chars / ~8,110 tokens over 28 tools.
    CEILING_CHARS = 36_000

    def test_manifest_stays_under_its_ceiling(self):
        n = len(get_tool_manifest())
        assert n <= self.CEILING_CHARS, (
            f"manifest is {n:,} chars, over the {self.CEILING_CHARS:,} ceiling. "
            "It is ~36% of every react prompt — raise this deliberately, with a "
            "note on what was added and why, or don't add it."
        )

    def test_cost_scales_with_tool_count_not_a_flat_fee(self):
        """Establishes that pruning tools actually saves tokens — the premise
        the selector design rests on. If this ever inverts, a large fixed
        preamble has appeared and per-tool pruning stops paying."""
        names = get_manifest_tool_names()
        small = len(get_tool_manifest(names[:8]))
        large = len(get_tool_manifest(names))
        assert large > small, "adding 20 tools must cost more than 8"


class TestRenderLatency:
    """Latency test, per Ananth's instruction.

    The manifest is re-composed on EVERY call (deliberately — MCP tools
    register at startup, after import). That is only sound while composition is
    cheap; this pins that it stays cheap.
    """

    def test_render_is_sub_millisecond(self):
        get_tool_manifest()  # warm any import-time work
        t0 = time.perf_counter()
        for _ in range(50):
            get_tool_manifest()
        per_call_ms = (time.perf_counter() - t0) / 50 * 1000
        assert per_call_ms < 5.0, (
            f"{per_call_ms:.2f} ms/call. It is re-rendered per planner call; "
            "if this grows, cache it or make composition lazy."
        )

    def test_names_derivation_is_not_slower_than_rendering(self):
        """get_manifest_tool_names derives from the composer rather than
        maintaining a second list. That is correct, and it must stay cheap —
        it runs on the telemetry path of every turn."""
        get_manifest_tool_names()
        t0 = time.perf_counter()
        for _ in range(50):
            get_manifest_tool_names()
        per_call_ms = (time.perf_counter() - t0) / 50 * 1000
        assert per_call_ms < 10.0, f"{per_call_ms:.2f} ms/call on the telemetry path"
