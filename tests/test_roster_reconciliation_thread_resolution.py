"""Roster reconciliation: thread state + multi-part / decomposed subquestions."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.context import PipelineContext
from app.pipeline.react_loop import _execute_tool


def test_execute_tool_fills_reconciliation_from_single_uploaded_file():
    """ReAct: empty tool inputs still resolve when thread has one roster row in uploaded_files."""
    ctx = PipelineContext(
        correlation_id="cid",
        thread_id="t1",
        message="Run reconciliation and also explain timely filing",
        merged_state={
            "active": {
                "uploaded_files": [
                    {
                        "upload_id": "up-99",
                        "org_id": "1234567890",
                        "org_name": "David Lawrence Center",
                        "purpose": "roster_reconciliation",
                        "filename": "roster.csv",
                        "row_count": 12,
                    }
                ],
            }
        },
    )
    emitter = MagicMock()
    with patch("app.pipeline.react_loop.answer_tool") as mock_at:
        mock_at.return_value = ("x" * 120, [], None, "roster_complete")
        out = _execute_tool(
            "run_roster_reconciliation_report",
            {"org_name": "", "upload_id": "", "org_id": ""},
            ctx,
            emitter,
        )
    assert out["success"] is True
    mock_at.assert_called_once()
    _args, kwargs = mock_at.call_args
    assert kwargs["reconciliation_upload_id"] == "up-99"
    assert kwargs["reconciliation_org_id"] == "1234567890"
    assert kwargs.get("user_message") == ctx.message
    assert _args[0] == "David Lawrence Center"


def test_execute_tool_uses_most_recent_roster_when_multiple_uploads():
    """Newest roster row (first in uploaded_files) supplies upload_id/org_id when pointers are empty."""
    ctx = PipelineContext(
        correlation_id="cid",
        thread_id="t1",
        message="Run reconciliation report for David Lawrence Center",
        merged_state={
            "active": {
                "uploaded_files": [
                    {
                        "upload_id": "up-new",
                        "org_id": "9999999999",
                        "org_name": "David Lawrence Center",
                        "purpose": "roster_reconciliation",
                        "filename": "latest.xlsx",
                        "row_count": 5,
                    },
                    {
                        "upload_id": "up-old",
                        "org_id": "1111111111",
                        "org_name": "David Lawrence Center",
                        "purpose": "roster_reconciliation",
                        "filename": "old.csv",
                        "row_count": 3,
                    },
                ],
            }
        },
    )
    emitter = MagicMock()
    with patch("app.pipeline.react_loop.answer_tool") as mock_at:
        mock_at.return_value = ("x" * 120, [], None, "roster_complete")
        out = _execute_tool(
            "run_roster_reconciliation_report",
            {"org_name": "David Lawrence Center", "upload_id": "", "org_id": ""},
            ctx,
            emitter,
        )
    assert out["success"] is True
    mock_at.assert_called_once()
    kwargs = mock_at.call_args[1]
    assert kwargs["reconciliation_upload_id"] == "up-new"
    assert kwargs["reconciliation_org_id"] == "9999999999"


def test_resolve_passes_reconciliation_ids_from_merged_active():
    """Legacy resolve path: answer_tool receives upload_id/org_id from active (defense in depth)."""
    from app.stages.resolve import _answer_for_subquestion

    with patch("app.stages.resolve.answer_tool") as mock_at:
        mock_at.return_value = ("short", [], None, "no_sources")
        _answer_for_subquestion(
            "cid",
            "sq1",
            "tool",
            "non_patient",
            "Run reconciliation report",
            user_message="Timely filing and reconciliation please",
            tool_hint="roster_reconciliation",
            active_context={
                "reconciliation_upload_id": "u1",
                "reconciliation_org_id": "1111111111",
                "reconciliation_org_name": "Acme Health",
            },
            thread_id="t1",
        )
    mock_at.assert_called_once()
    _args, kwargs = mock_at.call_args
    assert kwargs["reconciliation_upload_id"] == "u1"
    assert kwargs["reconciliation_org_id"] == "1111111111"
    assert _args[0] == "Acme Health"


def test_resolve_generic_subquestion_uses_state_org_name():
    """Decomposed subquestion text can be generic; org comes from upload metadata."""
    from app.stages.resolve import _answer_for_subquestion

    with patch("app.stages.resolve.answer_tool") as mock_at:
        mock_at.return_value = ("ok", [], None, "no_sources")
        _answer_for_subquestion(
            "cid",
            "sq1",
            "tool",
            "non_patient",
            "run reconciliation report",
            user_message="Part 1: Sunshine timely filing. Part 2: run reconciliation report.",
            tool_hint="roster_reconciliation",
            active_context={
                "reconciliation_upload_id": "u1",
                "reconciliation_org_id": "2222222222",
                "reconciliation_org_name": "Beta Clinic",
            },
            thread_id="t1",
        )
    mock_at.assert_called_once()
    assert mock_at.call_args[0][0] == "Beta Clinic"
