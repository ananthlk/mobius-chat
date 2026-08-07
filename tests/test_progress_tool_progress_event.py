"""publish_tool_progress_event (2026-08-07, Chat Architecture spec).

A dedicated SSE event for appeals discovery tools (appeals_find_carc/
appeals_lookup_rules/appeals_get_playbook) whose raw result carries
structured data the frontend formats itself, rather than a pre-formatted
string. Mirrors publish_bandit_reward_event's dedicated-event pattern
exactly -- deliberately NOT folded into the plain `thinking` line list,
since append_thinking reduces every entry to {line, ts} and would drop
the structured inputs/result fields.
"""

from __future__ import annotations

from unittest.mock import patch

from app.storage import progress


def test_event_shape_matches_spec():
    progress.start_progress("cid-shape")
    try:
        with patch("app.storage.progress._publish_progress_event") as mock_publish:
            progress.publish_tool_progress_event(
                "cid-shape", "appeals_lookup_rules", "after",
                success=True,
                inputs={"carc": "16"},
                result={"rules_found": 2, "carc_title": "Timely Filing"},
                note="✓ 2 rules for Timely Filing",
            )
        mock_publish.assert_called_once()
        cid_arg, ev = mock_publish.call_args[0]
        assert cid_arg == "cid-shape"
        assert ev["event"] == "tool_progress"
        data = ev["data"]
        assert data["tool_name"] == "appeals_lookup_rules"
        assert data["phase"] == "after"
        assert data["success"] is True
        assert data["inputs"] == {"carc": "16"}
        assert data["result"] == {"rules_found": 2, "carc_title": "Timely Filing"}
        assert data["note"] == "✓ 2 rules for Timely Filing"
        assert "ts" in data and "ts_readable" in data
    finally:
        progress._progress.pop("cid-shape", None)


def test_event_appended_to_events_list_not_thinking():
    """Dedicated-event pattern: lands in _progress[cid]["events"], never
    in ["thinking"] -- the plain-line list that would silently drop the
    structured fields."""
    progress.start_progress("cid-events")
    try:
        with patch("app.storage.progress._publish_progress_event"):
            progress.publish_tool_progress_event(
                "cid-events", "appeals_find_carc", "before", inputs={"description": "denied"},
            )
        assert progress._progress["cid-events"]["thinking"] == []
        assert len(progress._progress["cid-events"]["events"]) == 1
        assert progress._progress["cid-events"]["events"][0]["event"] == "tool_progress"
    finally:
        progress._progress.pop("cid-events", None)


def test_defaults_when_optional_fields_omitted():
    progress.start_progress("cid-defaults")
    try:
        with patch("app.storage.progress._publish_progress_event") as mock_publish:
            progress.publish_tool_progress_event("cid-defaults", "appeals_get_playbook", "before")
        _, ev = mock_publish.call_args[0]
        assert ev["data"]["success"] is None
        assert ev["data"]["inputs"] == {}
        assert ev["data"]["result"] == {}
        assert ev["data"]["note"] == ""
    finally:
        progress._progress.pop("cid-defaults", None)


def test_unknown_correlation_id_still_publishes_but_does_not_crash():
    """No start_progress() call for this cid (e.g. a race, or a caller
    that publishes after the turn's progress entry was cleaned up) --
    must not raise. _publish_progress_event still fires (SSE/Redis
    delivery is independent of the in-memory _progress dict)."""
    assert "cid-unknown" not in progress._progress
    with patch("app.storage.progress._publish_progress_event") as mock_publish:
        progress.publish_tool_progress_event("cid-unknown", "appeals_lookup_rules", "after", success=False)
    mock_publish.assert_called_once()
