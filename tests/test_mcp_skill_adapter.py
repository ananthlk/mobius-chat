"""MCPSkillAdapter — bridge MCP ``list_tools`` into the skill registry.

This is the follow-up commit to the three-commit skill-registry series.
The refactor's payoff: any MCP tool a remote server exposes becomes a
dispatchable chat skill automatically. This test file locks the
adapter's contract so that automatic bridging can't silently drift.

What's guarded here:

1. **Registration contract.** A well-formed MCP tool descriptor
   produces a SkillSpec with the tool's name, description, and JSON
   Schema. Malformed descriptors are skipped silently (logged at
   debug) instead of crashing startup — an MCP server returning junk
   shouldn't block chat from booting.

2. **Collision policy: builtins win.** If the MCP server exposes a
   tool whose name matches an already-registered builtin (google_search,
   healthcare_query, …), the adapter MUST skip it with a warning log.
   Builtins wrap MCP calls with extra logic (entity extraction,
   jurisdiction isolation, etc.) that an overwriting MCP registration
   would bypass.

3. **Handler shape.** A registered MCP skill dispatches to the same
   ``call_mcp_tool`` helper the builtins use, forwards ``SkillCall.inputs``
   as the MCP arguments, returns a ``SkillEnvelope`` with a SourceRef
   naming the MCP tool. Success/failure shapes match what integrate
   and react_loop expect.

4. **Graceful degradation.** MCP server down → ``list_mcp_tools()``
   returns ``[]`` → ``register_mcp_skills()`` returns ``[]`` and logs at
   INFO. Never raises. Chat continues with just the builtins.

5. **The planner sees the new skill.** After registration, the skill
   appears in ``registry.manifest_text()`` output — the full
   round-trip from MCP discovery to planner prompt works without any
   code edit per skill.

Test isolation: every test that registers a new skill unregisters it
in a ``finally`` (via a helper fixture) so the module-level registry
stays clean across tests. Without this, tests that run after an
MCP-registration test would see spurious skills.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.skills import registry
from app.skills.mcp_adapter import (
    _spec_from_mcp_tool,
    register_mcp_skills,
)
from app.skills.registry import SkillCall, SkillEnvelope


# ── Fixture: clean up adapter-registered skills after each test ──────


@pytest.fixture
def adapter_cleanup():
    """Track adapter-registered skill names and unregister them after
    the test. Without this, a test that registers ``claims_denial_lookup``
    would leave it in the global registry and a subsequent test's
    ``registry.all_names()`` assertion could fail unpredictably."""
    registered_names: list[str] = []
    yield registered_names
    for name in registered_names:
        registry.unregister(name)


# ── Spec construction from MCP tool descriptor ──────────────────────


class TestSpecFromMcpTool:
    def test_well_formed_tool_produces_spec(self):
        spec = _spec_from_mcp_tool({
            "name": "claims_denial_lookup",
            "description": "Look up a claim denial reason.",
            "inputSchema": {"type": "object", "properties": {"claim_id": {"type": "string"}}},
        })
        assert spec is not None
        assert spec.name == "claims_denial_lookup"
        assert "denial" in spec.description.lower()
        # Adapter MUST pass through the MCP inputSchema unchanged —
        # it's the contract the planner reads to build tool_inputs.
        assert spec.inputs_schema == {
            "type": "object",
            "properties": {"claim_id": {"type": "string"}},
        }

    def test_missing_description_gets_placeholder(self):
        """MCP tool descriptors don't always carry a description. The
        adapter still needs to produce something the planner manifest
        can render, so it fills with a deterministic placeholder."""
        spec = _spec_from_mcp_tool({"name": "silent_tool"})
        assert spec is not None
        assert spec.name == "silent_tool"
        assert spec.description  # non-empty fallback
        assert "silent_tool" in spec.description

    def test_missing_name_returns_none(self):
        """No name → can't register. Adapter returns None instead of
        raising so a misbehaving MCP entry doesn't crash startup."""
        assert _spec_from_mcp_tool({"description": "no name here"}) is None
        assert _spec_from_mcp_tool({"name": ""}) is None
        assert _spec_from_mcp_tool({"name": "   "}) is None

    def test_non_dict_schema_falls_back_to_empty(self):
        """Defensive: if ``inputSchema`` is a string or list (broken
        MCP server), we replace it with ``{}`` rather than passing
        garbage into SkillSpec."""
        spec = _spec_from_mcp_tool({
            "name": "borked",
            "inputSchema": "not-a-dict",
        })
        assert spec is not None
        assert spec.inputs_schema == {}

    def test_spec_defaults_are_conservative(self):
        """Adapter-registered skills default to
        ``requires_jurisdiction=False``, ``follow_up_capable=False``.
        If an MCP tool genuinely needs jurisdiction, the operator
        wraps it with an in-process builtin (like google_search does)
        — the adapter doesn't guess semantic properties."""
        spec = _spec_from_mcp_tool({"name": "any_tool"})
        assert spec.requires_jurisdiction is False
        assert spec.follow_up_capable is False


# ── End-to-end registration ─────────────────────────────────────────


class TestRegisterMcpSkills:
    def test_registers_well_formed_tools(self, adapter_cleanup):
        registered = register_mcp_skills(tools=[
            {"name": "mcp_test_tool_a", "description": "Tool A"},
            {"name": "mcp_test_tool_b", "description": "Tool B"},
        ])
        adapter_cleanup.extend(registered)
        assert set(registered) == {"mcp_test_tool_a", "mcp_test_tool_b"}
        assert registry.has("mcp_test_tool_a")
        assert registry.has("mcp_test_tool_b")

    def test_skips_builtins_with_warning(self, adapter_cleanup, caplog):
        """Collision test. If the MCP server exposes ``google_search``
        (which it does today, since that's the builtin's backing MCP
        call), adapter MUST skip and log WARNING. Otherwise the MCP
        version would shadow the builtin's entity-extraction /
        jurisdiction-aware logic."""
        import logging

        caplog.set_level(logging.WARNING, logger="app.skills.mcp_adapter")
        registered = register_mcp_skills(tools=[
            {"name": "google_search", "description": "MCP version — must be skipped"},
            {"name": "healthcare_query", "description": "MCP version — must be skipped"},
            {"name": "mcp_only_tool", "description": "This one registers."},
        ])
        adapter_cleanup.extend(registered)
        assert registered == ["mcp_only_tool"]
        # Warnings logged for the two collisions:
        msgs = [r.message for r in caplog.records]
        assert any("google_search" in m for m in msgs)
        assert any("healthcare_query" in m for m in msgs)
        assert any("Builtins win" in m for m in msgs)

        # Builtins' handlers are unchanged:
        from app.skills.builtin.healthcare import _run as builtin_healthcare_run
        assert registry.get("healthcare_query").handler is builtin_healthcare_run

    def test_empty_tool_list_is_no_op(self, caplog):
        """MCP down / no tools exposed → return []. This is the
        graceful-degradation path that lets chat boot without MCP."""
        import logging

        caplog.set_level(logging.INFO, logger="app.skills.mcp_adapter")
        assert register_mcp_skills(tools=[]) == []
        assert any(
            "no MCP tools discovered" in r.message for r in caplog.records
        )

    def test_malformed_entries_skipped_silently(self, adapter_cleanup):
        """Mix of good + malformed descriptors → adapter picks out the
        good ones and logs the rest at debug. Defensive: a buggy MCP
        server returning garbage shouldn't crash startup."""
        registered = register_mcp_skills(tools=[
            {"name": "mcp_test_good", "description": "ok"},
            {"description": "no name — skip"},
            "not a dict — skip",
            None,
            {"name": "", "description": "empty name — skip"},
        ])
        adapter_cleanup.extend(registered)
        assert registered == ["mcp_test_good"]

    def test_calls_list_mcp_tools_when_tools_none(self):
        """Default behavior — no explicit tools arg → call the MCP
        manager's list_mcp_tools(). Patched here to avoid network."""
        with patch("app.skills.mcp_adapter.list_mcp_tools") as mock_list:
            mock_list.return_value = []
            register_mcp_skills()  # no tools= arg
            mock_list.assert_called_once_with()


# ── Handler dispatch ────────────────────────────────────────────────


class TestMcpHandlerDispatch:
    def test_success_returns_envelope_with_mcp_source(self, adapter_cleanup):
        """Happy path: MCP returns (text, True) → envelope carries the
        text + a SourceRef naming the MCP tool. The "MCP: <name>"
        convention lets integrate distinguish MCP-sourced answers from
        RAG or web."""
        registered = register_mcp_skills(tools=[
            {"name": "mcp_test_claims", "description": "Lookup claims."},
        ])
        adapter_cleanup.extend(registered)

        with patch("app.skills.mcp_adapter.call_mcp_tool") as mock_mcp:
            mock_mcp.return_value = ("Claim C-123: denied, missing auth", True)
            env = registry.dispatch(SkillCall(
                name="mcp_test_claims",
                inputs={"claim_id": "C-123"},
                question="why was my claim denied?",
            ))
        assert isinstance(env, SkillEnvelope)
        assert "Claim C-123" in env.text
        assert env.signal == "no_sources"
        assert len(env.sources) == 1
        assert env.sources[0].document_name == "MCP: mcp_test_claims"
        assert env.sources[0].source_type == "external"
        # The handler passes inputs through unchanged to MCP:
        mock_mcp.assert_called_once_with("mcp_test_claims", {"claim_id": "C-123"})

    def test_failure_returns_error_envelope_not_raise(self, adapter_cleanup):
        """MCP returned ``(text, False)`` → envelope carries the error
        text with empty sources. The contract is 'never raise' so the
        react_loop's retry + fallback machinery sees a consistent
        shape whether the tool succeeded or failed."""
        registered = register_mcp_skills(tools=[
            {"name": "mcp_test_fail", "description": "Always fails."},
        ])
        adapter_cleanup.extend(registered)

        with patch("app.skills.mcp_adapter.call_mcp_tool") as mock_mcp:
            mock_mcp.return_value = ("Tool call failed: connection refused", False)
            env = registry.dispatch(SkillCall(
                name="mcp_test_fail",
                inputs={},
                question="x",
            ))
        assert env.sources == []
        assert env.signal == "no_sources"
        assert "failed" in env.text.lower()

    def test_empty_input_dict_passes_through(self, adapter_cleanup):
        """Skill called with no structured inputs → adapter forwards
        ``{}`` to MCP, not ``None`` (which some MCP servers would
        reject as 'missing required arguments object')."""
        registered = register_mcp_skills(tools=[
            {"name": "mcp_test_empty", "description": "No args."},
        ])
        adapter_cleanup.extend(registered)

        with patch("app.skills.mcp_adapter.call_mcp_tool") as mock_mcp:
            mock_mcp.return_value = ("ok", True)
            registry.dispatch(SkillCall(name="mcp_test_empty", inputs={}, question=""))
            # Handler called with args={} not args=None:
            args_passed = mock_mcp.call_args[0][1]
            assert args_passed == {}

    def test_handler_name_is_debuggable(self, adapter_cleanup):
        """Stack traces are easier to read when the handler's
        ``__name__`` mentions the tool. Adapter renames the closure
        to ``_mcp_handler_<tool_name>``."""
        registered = register_mcp_skills(tools=[
            {"name": "mcp_test_named", "description": "."},
        ])
        adapter_cleanup.extend(registered)
        spec = registry.get("mcp_test_named")
        assert spec.handler.__name__ == "_mcp_handler_mcp_test_named"


# ── Integration: planner manifest picks up adapter-registered skills ─


class TestPlannerManifestIntegration:
    def test_adapter_registered_skill_appears_in_manifest_text(self, adapter_cleanup):
        """The whole point of the adapter is: register an MCP tool →
        planner can pick it. ``registry.manifest_text()`` is what the
        planner reads, so an adapter-registered tool MUST render there
        with no extra wiring."""
        registered = register_mcp_skills(tools=[
            {
                "name": "mcp_test_plan",
                "description": "Use this tool when the user needs an X lookup.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"subject": {"type": "string"}},
                    "required": ["subject"],
                },
            },
        ])
        adapter_cleanup.extend(registered)
        body = registry.manifest_text(names=("mcp_test_plan",))
        assert "mcp_test_plan(subject)" in body
        assert "X lookup" in body

    def test_adapter_skill_in_entity_tools_view(self, adapter_cleanup):
        """Conservative default: adapter-registered tools have
        ``requires_jurisdiction=False``, so they appear in
        ``entity_tools()``. Good default — the planner treats them
        like any other entity-lookup tool, no jurisdiction injection."""
        registered = register_mcp_skills(tools=[
            {"name": "mcp_test_entity", "description": "."},
        ])
        adapter_cleanup.extend(registered)
        assert "mcp_test_entity" in registry.entity_tools()
