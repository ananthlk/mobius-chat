"""MCPSkillAdapter — register remote MCP tools as registry skills.

This is the payoff for the skill-registry refactor (commits 17c12f2,
158e346, 66945de). Once chat's tool dispatch became a
``SkillSpec``-driven registry, making every MCP tool automatically
available became a single bridge:

  remote MCP ``list_tools`` → ``SkillSpec`` per tool → ``register()``.

After calling ``register_mcp_skills()`` at startup, any tool the
``mobius-skills-mcp`` server (or any future MCP server this chat
connects to) exposes is immediately dispatchable via
``answer_tool(..., tool_hint_override="<tool_name>")``, advertised in
the planner manifest (``registry.manifest_text()`` renders it), and
honored by ``ENTITY_TOOLS`` / ``FOLLOW_UP_CAPABLE`` derivation.

**Collision policy: builtins win.** If an MCP server exposes a tool
with the same name as one of the builtins registered at import
(``document_upload_skill``, ``list_thread_document_uploads``,
``healthcare_query``, ``web_scrape``, ``google_search``), the MCP
registration is skipped with a warning log. Rationale:

  - Builtins may wrap MCP calls with extra logic (e.g.
    ``healthcare_query`` does entity extraction first so active
    jurisdiction doesn't bleed into NPI lookups). An MCP-registered
    same-name would bypass that logic.
  - The ``register()`` duplicate-name guard is a loud failure by
    design; we don't want a misconfigured MCP server crashing chat
    startup. Skip + log is the graceful degradation.

**Failure mode: best-effort.** ``register_mcp_skills()`` never raises.
If the MCP server is down or ``list_mcp_tools()`` returns empty, we
log at WARNING and return an empty list — chat continues with just
the builtins. A future admin endpoint can trigger re-registration
once the MCP server comes back.

**Test surface:** inject ``tools`` explicitly to bypass the network.
The default behavior (``tools=None``) calls ``list_mcp_tools()``.
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.mcp_manager import call_mcp_tool, list_mcp_tools
from app.skills.registry import (
    SkillCall,
    SkillEnvelope,
    SkillSpec,
    SourceRef,
    has,
    register,
)

logger = logging.getLogger(__name__)


def _make_mcp_handler(tool_name: str):
    """Build a handler closure that forwards a ``SkillCall`` to the
    named MCP tool.

    Handler contract:
      - ``call.inputs`` is passed through to MCP as the arguments dict.
        The dispatcher has already populated inputs from the planner's
        tool_inputs, so the handler doesn't re-parse anything.
      - Success → envelope with the MCP text and one SourceRef naming
        the tool (so integrate stage has something to cite).
      - Failure → envelope with the error text, no sources,
        signal=no_sources. The MCP manager's ``call_mcp_tool`` already
        returns ``(text, False)`` for errors; we don't need to try/except
        around it here.
    """

    def _run(call: SkillCall) -> SkillEnvelope:
        # MCP tools are synchronous request/response: they don't use
        # the emitter, thread_id, active_context, etc. We pass only the
        # structured inputs. If a remote tool ever wants context, the
        # planner can forward it explicitly via tool_inputs.
        text, success = call_mcp_tool(tool_name, call.inputs or {})

        if success and text:
            return SkillEnvelope(
                text=text,
                sources=[
                    SourceRef(
                        document_name=f"MCP: {tool_name}",
                        index=1,
                        text=text[:300],
                        source_type="external",
                    )
                ],
                signal="no_sources",
            )
        # Graceful failure shape: caller sees a non-empty text and can
        # route to a fallback. Matches the shape builtins produce on
        # MCP error so downstream (integrate, react_loop retry) has one
        # shape to handle.
        return SkillEnvelope(
            text=text or f"MCP tool {tool_name!r} returned no content.",
            signal="no_sources",
        )

    # Name the closure for easier debugging — stack traces will show
    # ``_mcp_handler_<tool_name>`` instead of a generic ``_run``.
    _run.__name__ = f"_mcp_handler_{tool_name}"
    _run.__qualname__ = _run.__name__
    return _run


def _spec_from_mcp_tool(tool: dict[str, Any]) -> SkillSpec | None:
    """Build a ``SkillSpec`` from one MCP tool descriptor dict. Returns
    ``None`` if the descriptor is malformed (no name)."""
    name = (tool.get("name") or "").strip()
    if not name:
        return None

    description = (tool.get("description") or "").strip() or (
        f"Remote MCP tool {name!r}. See the MCP server's tool catalog."
    )
    schema = tool.get("inputSchema") or {}
    if not isinstance(schema, dict):
        schema = {}

    return SkillSpec(
        name=name,
        description=description,
        handler=_make_mcp_handler(name),
        inputs_schema=schema,
        # Conservative defaults for MCP-registered tools. If an MCP
        # tool genuinely needs jurisdiction (google_search-style), the
        # operator can write an in-process builtin that wraps it —
        # which is what ``google_search`` already does. The adapter
        # doesn't guess.
        requires_jurisdiction=False,
        follow_up_capable=False,
    )


def register_mcp_skills(
    *,
    tools: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Discover MCP tools and register each as a ``SkillSpec``.

    Args:
        tools: Optional pre-fetched tool list. When ``None`` (default),
            we call ``list_mcp_tools()`` against the configured MCP
            server. Tests inject this to avoid network.

    Returns:
        The list of skill names actually registered (excludes skipped
        builtins + malformed entries). Empty list when the MCP server
        is unreachable or returns nothing.

    This function never raises. Failure modes:
      - MCP server down → ``list_mcp_tools()`` returns []
      - Malformed tool (no name) → skipped with debug log
      - Name collides with existing registration → skipped with
        WARNING log ("builtins win")
    """
    discovered = tools if tools is not None else list_mcp_tools()
    if not discovered:
        logger.info("register_mcp_skills: no MCP tools discovered")
        return []

    registered: list[str] = []
    for t in discovered:
        if not isinstance(t, dict):
            logger.debug("register_mcp_skills: skipping non-dict tool entry: %r", t)
            continue

        spec = _spec_from_mcp_tool(t)
        if spec is None:
            logger.debug("register_mcp_skills: skipping malformed tool descriptor: %r", t)
            continue

        if has(spec.name):
            # Builtin (or prior MCP registration) already holds this
            # name. Builtins intentionally wrap MCP tools with extra
            # logic — silently overwriting would lose that.
            logger.warning(
                "register_mcp_skills: skipping MCP tool %r — "
                "a skill with that name is already registered. "
                "Builtins win; MCP tool not registered.",
                spec.name,
            )
            continue

        register(spec)
        registered.append(spec.name)

    if registered:
        logger.info(
            "register_mcp_skills: registered %d MCP tool(s) as skills: %s",
            len(registered),
            ", ".join(registered),
        )
    return registered
