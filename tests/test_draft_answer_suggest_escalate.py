"""append_draft_answer's suggest_escalate param (2026-08-07, Ananth,
directly, live UX finding).

The "Try with Think mode" button previously only appeared on the LATER
completed-card event (integrate.py's suggest_escalate field, computed
after the integrator's LLM call finishes). Ananth's point: on the
fast-mode thin-evidence hedge path, the answer text itself already
streams immediately via draft_ready -- the button that goes with it
shouldn't have to wait for the slower integrator pass when react
already knows at draft time that escalation is warranted.
"""

from __future__ import annotations

from unittest.mock import patch

from app.storage import progress


def test_suggest_escalate_true_included_in_draft_ready_event():
    progress.start_progress("cid-escalate")
    try:
        with patch("app.storage.progress._publish_progress_event") as mock_publish:
            progress.append_draft_answer("cid-escalate", "hedge text", suggest_escalate=True)
        _, ev = mock_publish.call_args[0]
        assert ev["event"] == "draft_ready"
        assert ev["data"]["text"] == "hedge text"
        assert ev["data"]["suggest_escalate"] is True
    finally:
        progress._progress.pop("cid-escalate", None)


def test_suggest_escalate_false_omitted_not_sent_as_false():
    """Default/unset case must not add noise to the event payload --
    existing consumers that don't check this field are unaffected."""
    progress.start_progress("cid-no-escalate")
    try:
        with patch("app.storage.progress._publish_progress_event") as mock_publish:
            progress.append_draft_answer("cid-no-escalate", "normal answer")
        _, ev = mock_publish.call_args[0]
        assert "suggest_escalate" not in ev["data"]
    finally:
        progress._progress.pop("cid-no-escalate", None)


def test_mode_hint_and_suggest_escalate_coexist():
    progress.start_progress("cid-both")
    try:
        with patch("app.storage.progress._publish_progress_event") as mock_publish:
            progress.append_draft_answer("cid-both", "text", mode_hint="RECITAL", suggest_escalate=True)
        _, ev = mock_publish.call_args[0]
        assert ev["data"]["mode_hint"] == "RECITAL"
        assert ev["data"]["suggest_escalate"] is True
    finally:
        progress._progress.pop("cid-both", None)


def test_draft_answer_text_still_stashed_durably():
    """The Task #29 durable-stash behavior must be unaffected by the new param."""
    progress.start_progress("cid-durable")
    try:
        with patch("app.storage.progress._publish_progress_event"):
            progress.append_draft_answer("cid-durable", "hedge text", suggest_escalate=True)
        assert progress._progress["cid-durable"]["draft_answer"] == "hedge text"
    finally:
        progress._progress.pop("cid-durable", None)
