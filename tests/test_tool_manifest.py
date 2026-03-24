"""Unit tests for ReAct tool manifest."""
import pytest

from app.pipeline.tool_manifest import (
    TOOL_MANIFEST,
    ENTITY_TOOLS,
    FOLLOW_UP_CAPABLE,
)


def test_tool_manifest_renders_cleanly():
    """TOOL_MANIFEST is a non-empty string with expected tools."""
    assert isinstance(TOOL_MANIFEST, str)
    assert len(TOOL_MANIFEST.strip()) > 100
    assert "search_corpus" in TOOL_MANIFEST
    assert "google_search" in TOOL_MANIFEST
    assert "web_scrape" in TOOL_MANIFEST
    assert "lookup_npi" in TOOL_MANIFEST
    assert "run_credentialing_report" in TOOL_MANIFEST
    assert "run_roster_reconciliation_report" in TOOL_MANIFEST
    assert "document_upload_skill" in TOOL_MANIFEST
    assert "list_thread_document_uploads" in TOOL_MANIFEST
    assert "refuse" in TOOL_MANIFEST
    assert "AVAILABLE TOOLS" in TOOL_MANIFEST


def test_entity_tools_set():
    """ENTITY_TOOLS contains tools that never receive jurisdiction context."""
    assert "lookup_npi" in ENTITY_TOOLS
    assert "run_credentialing_report" in ENTITY_TOOLS
    assert "run_roster_reconciliation_report" in ENTITY_TOOLS
    assert "document_upload_skill" in ENTITY_TOOLS
    assert "list_thread_document_uploads" in ENTITY_TOOLS
    assert "web_scrape" in ENTITY_TOOLS
    assert "search_corpus" not in ENTITY_TOOLS


def test_follow_up_capable_set():
    """FOLLOW_UP_CAPABLE contains tools that can answer follow-up questions."""
    assert "run_credentialing_report" in FOLLOW_UP_CAPABLE
    assert "run_roster_reconciliation_report" in FOLLOW_UP_CAPABLE
    assert "lookup_npi" in FOLLOW_UP_CAPABLE
    assert "list_thread_document_uploads" in FOLLOW_UP_CAPABLE
