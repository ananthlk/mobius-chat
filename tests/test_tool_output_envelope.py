"""Tests for Mobius tool output envelope (summary vs user detail)."""
from __future__ import annotations

from app.communication.tool_output_envelope import (
    MARKER_DETAIL,
    MARKER_SUMMARY,
    MOBIUS_TOOL_OUTPUT_VERSION,
    compose_mobius_tool_envelope,
    split_mobius_tool_envelope,
)


def test_compose_includes_version_and_headings() -> None:
    out = compose_mobius_tool_envelope("One line.", "# Big\n\nBody.")
    assert MOBIUS_TOOL_OUTPUT_VERSION in out
    assert MARKER_SUMMARY in out
    assert MARKER_DETAIL in out
    assert "One line." in out
    assert "# Big" in out


def test_split_roundtrip() -> None:
    s, d = "Summary here.", "## Doc\n\nMore."
    full = compose_mobius_tool_envelope(s, d, include_preamble=False)
    got_s, got_d = split_mobius_tool_envelope(full)
    assert got_s == s
    assert got_d == d


def test_split_fallback_no_markers() -> None:
    plain = "just prose"
    assert split_mobius_tool_envelope(plain) == ("", plain)


def test_compose_without_preamble() -> None:
    out = compose_mobius_tool_envelope("a", "b", include_preamble=False)
    assert "Mobius tool output" not in out
    assert split_mobius_tool_envelope(out) == ("a", "b")
