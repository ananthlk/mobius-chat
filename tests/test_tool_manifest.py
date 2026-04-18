"""Unit tests for ReAct tool manifest.

2026-04-18 disconnect: 7 credentialing/roster tools (lookup_npi,
find_org_locations, find_associated_providers_at_locations,
run_credentialing_report, validate_credentialing_step,
run_roster_reconciliation_report, ask_credentialing_npi) were removed
from the planner's visible tool list + from ENTITY_TOOLS +
FOLLOW_UP_CAPABLE. This test locks the post-disconnect shape.
"""
import pytest

from app.pipeline.tool_manifest import (
    TOOL_MANIFEST,
    ENTITY_TOOLS,
    FOLLOW_UP_CAPABLE,
)


def test_tool_manifest_renders_cleanly():
    """TOOL_MANIFEST is a non-empty string with expected remaining tools."""
    assert isinstance(TOOL_MANIFEST, str)
    assert len(TOOL_MANIFEST.strip()) > 100
    assert "search_corpus" in TOOL_MANIFEST
    assert "google_search" in TOOL_MANIFEST
    assert "web_scrape" in TOOL_MANIFEST
    assert "document_upload_skill" in TOOL_MANIFEST
    assert "list_thread_document_uploads" in TOOL_MANIFEST
    assert "refuse" in TOOL_MANIFEST
    assert "AVAILABLE TOOLS" in TOOL_MANIFEST


def test_removed_tools_absent_from_manifest():
    """Regression guard: if any of the disconnected tools reappear, the
    chat-side credentialing glue is partially back — investigate."""
    for tool_name in (
        "lookup_npi",
        "find_org_locations",
        "find_associated_providers_at_locations",
        "run_credentialing_report",
        "validate_credentialing_step",
        "run_roster_reconciliation_report",
        "ask_credentialing_npi",
    ):
        # The planner prompt must not advertise these. They'll come back
        # as clean skill integrations with their own envelope contracts.
        assert f"\n{tool_name}(" not in TOOL_MANIFEST, (
            f"Tool {tool_name!r} reappeared in TOOL_MANIFEST — this was "
            f"removed in the 2026-04-18 credentialing disconnect. If it's "
            f"genuinely back as a chat-integrated tool, update this test."
        )


def test_entity_tools_set():
    """ENTITY_TOOLS contains tools that never receive jurisdiction context."""
    # Post-disconnect, only these five remain.
    assert "document_upload_skill" in ENTITY_TOOLS
    assert "list_thread_document_uploads" in ENTITY_TOOLS
    assert "web_scrape" in ENTITY_TOOLS
    assert "healthcare_query" in ENTITY_TOOLS
    assert "healthcare_npi_lookup" in ENTITY_TOOLS
    # search_corpus is a jurisdiction-aware tool (gets payer/state filters).
    assert "search_corpus" not in ENTITY_TOOLS
    # Disconnected tools must not be in the set.
    for removed in (
        "lookup_npi",
        "find_org_locations",
        "find_associated_providers_at_locations",
        "run_credentialing_report",
        "validate_credentialing_step",
        "run_roster_reconciliation_report",
        "ask_credentialing_npi",
    ):
        assert removed not in ENTITY_TOOLS


def test_follow_up_capable_set():
    """FOLLOW_UP_CAPABLE post-disconnect: only list_thread_document_uploads.

    The credentialing/roster tools that kept a follow-up-aware context
    alive (a 'you just ran a credentialing report, so ask_credentialing_npi
    about it' pattern) are gone. When credentialing rebuilds as a skill,
    the follow-up capability becomes the skill's responsibility, not
    chat's.
    """
    assert "list_thread_document_uploads" in FOLLOW_UP_CAPABLE
    # Removed tools must not appear.
    for removed in (
        "run_credentialing_report",
        "validate_credentialing_step",
        "run_roster_reconciliation_report",
        "lookup_npi",
        "find_org_locations",
        "find_associated_providers_at_locations",
        "ask_credentialing_npi",
    ):
        assert removed not in FOLLOW_UP_CAPABLE
