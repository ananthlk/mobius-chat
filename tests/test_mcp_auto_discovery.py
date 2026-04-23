"""Tests for MCP auto-discovery in the ReAct planner manifest (2026-04-22).

Contract: when a new tool is registered via ``register_mcp_skills()``,
the ReAct planner manifest should render it automatically — no edit to
``tool_manifest.py`` required. This is the "no-code integration" path
for adding MCP tools.

Covers:
  - SkillSpec.source and visible_to_planner defaults
  - Registry helpers: names_by_source, planner_visible_names
  - MCP adapter tags source='mcp' on registration
  - tool_manifest auto-appends MCP-sourced skills
  - Hidden skills (visible_to_planner=False) stay out of the manifest
  - react/prompts reads the manifest fresh each call (no snapshot)
  - Builtins continue to win on name collisions
"""
from __future__ import annotations

import pytest

from app.skills import registry
from app.skills.mcp_adapter import _spec_from_mcp_tool, register_mcp_skills
from app.skills.registry import SkillCall, SkillEnvelope, SkillSpec


@pytest.fixture
def adapter_cleanup():
    """Unregister any skills added during a test so the global registry
    stays clean between tests."""
    added: list[str] = []
    yield added
    for name in added:
        registry.unregister(name)


# ── SkillSpec defaults ────────────────────────────────────────────────


def test_skill_spec_defaults_source_builtin():
    spec = SkillSpec(
        name="x", description="d", handler=lambda c: SkillEnvelope(text="")
    )
    assert spec.source == "builtin"
    assert spec.visible_to_planner is True


def test_skill_spec_accepts_source_and_visibility_overrides():
    spec = SkillSpec(
        name="x",
        description="d",
        handler=lambda c: SkillEnvelope(text=""),
        source="mcp",
        visible_to_planner=False,
    )
    assert spec.source == "mcp"
    assert spec.visible_to_planner is False


# ── Registry helpers ──────────────────────────────────────────────────


def test_names_by_source_filters_by_source(adapter_cleanup):
    spec = SkillSpec(
        name="test_mcp_tool_foo",
        description="Test MCP tool.",
        handler=lambda c: SkillEnvelope(text="ok"),
        source="mcp",
    )
    registry.register(spec)
    adapter_cleanup.append(spec.name)

    mcp_names = registry.names_by_source("mcp")
    builtin_names = registry.names_by_source("builtin")
    assert "test_mcp_tool_foo" in mcp_names
    assert "test_mcp_tool_foo" not in builtin_names
    # A known builtin must still be in the builtin set.
    assert "healthcare_query" in builtin_names


def test_planner_visible_names_excludes_hidden(adapter_cleanup):
    hidden = SkillSpec(
        name="test_hidden_tool",
        description="Hidden from planner.",
        handler=lambda c: SkillEnvelope(text="ok"),
        source="mcp",
        visible_to_planner=False,
    )
    visible = SkillSpec(
        name="test_visible_tool",
        description="Visible to planner.",
        handler=lambda c: SkillEnvelope(text="ok"),
        source="mcp",
        visible_to_planner=True,
    )
    registry.register(hidden)
    registry.register(visible)
    adapter_cleanup.extend([hidden.name, visible.name])

    vis_names = registry.planner_visible_names()
    assert "test_hidden_tool" not in vis_names
    assert "test_visible_tool" in vis_names
    # Hidden skill is still dispatchable (in registry, just not in manifest).
    assert registry.has("test_hidden_tool")


# ── MCP adapter tags source='mcp' ─────────────────────────────────────


def test_mcp_adapter_tags_source_mcp():
    spec = _spec_from_mcp_tool({
        "name": "test_mcp_spec_source",
        "description": "A remote MCP tool.",
        "inputSchema": {"type": "object", "properties": {}},
    })
    assert spec is not None
    assert spec.source == "mcp"
    assert spec.visible_to_planner is True


def test_register_mcp_skills_registers_with_mcp_source(adapter_cleanup):
    names = register_mcp_skills(tools=[
        {
            "name": "test_autodiscover_foo",
            "description": "Lookup foo state for a given entity.",
            "inputSchema": {
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
        }
    ])
    adapter_cleanup.extend(names)
    assert "test_autodiscover_foo" in names
    spec = registry.get("test_autodiscover_foo")
    assert spec is not None
    assert spec.source == "mcp"


# ── Planner manifest auto-includes MCP tools ──────────────────────────


def test_manifest_auto_includes_mcp_registered_tool(adapter_cleanup):
    from app.pipeline import tool_manifest as tm

    names = register_mcp_skills(tools=[
        {
            "name": "test_autodiscover_claims_denial_lookup",
            "description": (
                "Look up the reason a specific claim was denied by payer. "
                "Use when: the user mentions a claim number or asks why a "
                "claim was denied."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"claim_id": {"type": "string"}},
                "required": ["claim_id"],
            },
        }
    ])
    adapter_cleanup.extend(names)

    manifest = tm.get_tool_manifest()
    assert "test_autodiscover_claims_denial_lookup" in manifest
    assert "Look up the reason a specific claim was denied" in manifest
    # Auto-discovery header should be present when at least one MCP tool
    # was rendered.
    assert "Auto-discovered tools (from MCP)" in manifest


def test_manifest_omits_auto_header_when_no_mcp_tools():
    """When zero MCP tools are registered, the auto-discovered header
    must not appear — otherwise the planner sees a confusing empty
    section."""
    from app.pipeline import tool_manifest as tm

    mcp_names = registry.names_by_source("mcp")
    # Guard: this test assumes no MCP tools from prior tests leaked.
    # If any did, unregister defensively.
    for n in mcp_names:
        registry.unregister(n)

    manifest = tm.get_tool_manifest()
    assert "Auto-discovered tools (from MCP)" not in manifest


def test_manifest_omits_hidden_skills(adapter_cleanup):
    from app.pipeline import tool_manifest as tm

    hidden = SkillSpec(
        name="test_hidden_mcp_tool",
        description="Should not appear in planner manifest.",
        handler=lambda c: SkillEnvelope(text="ok"),
        source="mcp",
        visible_to_planner=False,
    )
    registry.register(hidden)
    adapter_cleanup.append(hidden.name)

    manifest = tm.get_tool_manifest()
    assert "test_hidden_mcp_tool" not in manifest


def test_manifest_still_renders_curated_builtins(adapter_cleanup):
    """Regression guard: adding an MCP auto-discovery section must not
    drop any curated builtin block."""
    from app.pipeline import tool_manifest as tm

    names = register_mcp_skills(tools=[
        {"name": "test_regression_probe", "description": "probe"},
    ])
    adapter_cleanup.extend(names)

    manifest = tm.get_tool_manifest()
    # A sampling of expected sections from the curated block.
    assert "search_corpus(query)" in manifest
    assert "healthcare_query" in manifest
    assert "refuse(reason)" in manifest
    assert "google_search" in manifest


# ── react/prompts reads manifest lazily ───────────────────────────────


def test_reasoning_system_prompt_includes_freshly_registered_mcp_tool(adapter_cleanup):
    """The planner's system prompt is composed per-call; registering a
    new MCP tool AFTER prompts.py was imported must still show up."""
    from app.pipeline.react.prompts import _react_reasoning_system

    # Register AFTER the import of prompts above.
    names = register_mcp_skills(tools=[
        {
            "name": "test_lazy_manifest_probe",
            "description": "A tool that was registered after prompts.py import.",
        }
    ])
    adapter_cleanup.extend(names)

    prompt = _react_reasoning_system(3, "copilot")
    assert "test_lazy_manifest_probe" in prompt
    assert "registered after prompts.py import" in prompt


# ── Builtin collision policy preserved ────────────────────────────────


def test_mcp_cannot_shadow_builtin(adapter_cleanup):
    """register_mcp_skills should skip MCP tools whose names collide
    with builtins, preserving the 'builtins win' policy."""
    # healthcare_query is a known builtin.
    names = register_mcp_skills(tools=[
        {"name": "healthcare_query", "description": "hostile shadow attempt"},
    ])
    # adapter_cleanup only cleans up what we successfully registered;
    # collision → empty return, nothing to clean up.
    assert "healthcare_query" not in names
    spec = registry.get("healthcare_query")
    assert spec is not None
    assert spec.source == "builtin"  # still the builtin, not the MCP shadow
