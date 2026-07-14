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

import json
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

        # Structured response: {"text": "...", "extra": {...}}
        # MCP tools can optionally return this JSON shape to pass out-of-band
        # data (e.g. credentialing_card) without embedding it in the markdown.
        # Non-JSON and plain-text responses pass through unchanged.
        parsed_extra: dict[str, Any] | None = None
        if success and text and text.strip().startswith("{"):
            try:
                _parsed = json.loads(text)
                if isinstance(_parsed, dict) and isinstance(_parsed.get("text"), str):
                    text = _parsed["text"]
                    _e = _parsed.get("extra")
                    parsed_extra = _e if isinstance(_e, dict) else None
            except (json.JSONDecodeError, ValueError):
                pass  # plain-text response — use as-is

        # Propagate extra to pipeline context so _sync_extra_out_to_context
        # can pick up credentialing_card and similar out-of-band fields.
        if parsed_extra:
            _pctx = getattr(call, "pipeline_ctx", None)
            if _pctx is not None:
                _current = getattr(_pctx, "extra_out", None) or {}
                setattr(_pctx, "extra_out", {**_current, **parsed_extra})

        if success and text:
            return SkillEnvelope(
                text=text,
                extra=parsed_extra or None,
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

    # Infer category from name conventions so MCP analytics tools
    # (get_top_orgs, get_org_profile, get_rate_benchmarks, etc.) group
    # correctly in the UI without manual tagging.
    cat = "analytics"
    if name.startswith(("lookup_", "search_", "ingest_")):
        cat = "web"
    elif name.startswith(("npi_", "healthcare_")):
        cat = "healthcare"

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
        # Tag the origin so the planner manifest can render MCP-sourced
        # tools in a dedicated auto-discovery section, separate from
        # the curated builtin block. See
        # ``app.pipeline.tool_manifest._compose_manifest`` for the
        # rendering contract.
        source="mcp",
        category=cat,
        display_name=name.replace("_", " ").title(),
    )


def _list_tools_from_url(url: str) -> list[dict[str, Any]]:
    """Fetch tool list from an arbitrary MCP endpoint URL.

    Strategy (fastest first, graceful fallback):
    1. Try GET <base>/tools — a plain REST shortcut that some Mobius
       skill servers expose (appeals-agent, future skills). No session
       handshake, works even on cold start.
    2. Fall back to full MCP StreamableHTTP session handshake
       (initialize → list_tools). Required for standard MCP servers
       that don't expose the REST shortcut.

    Returns [] on any failure (never raises).
    """
    import asyncio
    import os
    from concurrent.futures import ThreadPoolExecutor
    import httpx

    connect_timeout = float(os.environ.get("MCP_CONNECT_TIMEOUT", "10"))
    read_timeout    = float(os.environ.get("MCP_READ_TIMEOUT", "60"))

    # ── Strategy 1: REST shortcut GET <base>/tools ─────────────────────
    # url is the /mcp endpoint; strip to get the base.
    base_url = url[:-4] if url.endswith("/mcp") else url
    try:
        with httpx.Client(timeout=httpx.Timeout(read_timeout, connect=connect_timeout)) as c:
            r = c.get(f"{base_url}/mcp/tools")
            if r.status_code == 200:
                data = r.json()
                tools = data.get("tools", data) if isinstance(data, dict) else data
                if isinstance(tools, list) and tools:
                    logger.debug("_list_tools_from_url(%s): REST shortcut returned %d tools", url, len(tools))
                    return tools
    except Exception as exc:
        logger.debug("_list_tools_from_url(%s) REST shortcut failed: %s", url, exc)

    # ── Strategy 2: Full MCP session handshake ─────────────────────────
    async def _async_fetch() -> list[dict[str, Any]]:
        try:
            from mcp.client.session import ClientSession
            from mcp.client.streamable_http import streamable_http_client
            timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http_client:
                async with streamable_http_client(url, http_client=http_client) as (r, w, _):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
                        result = await session.list_tools()
                        return [
                            {
                                "name":        t.name,
                                "description": getattr(t, "description", ""),
                                "inputSchema": getattr(t, "inputSchema", {}) or {},
                            }
                            for t in (result.tools or [])
                        ]
        except Exception as exc:
            logger.debug("_list_tools_from_url(%s) MCP session failed: %s", url, exc)
            return []

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, _async_fetch()).result()
    return asyncio.run(_async_fetch())


def _extra_mcp_urls() -> list[str]:
    """Return additional MCP server URLs from EXTRA_MCP_URLS env var.

    EXTRA_MCP_URLS is a comma-separated list of base URLs.  Each entry
    has /mcp appended if it doesn't already end with /mcp.

    Example:
        EXTRA_MCP_URLS=https://mobius-appeals-prototype-xxx.run.app,https://other.run.app
    """
    import os
    raw = (os.environ.get("EXTRA_MCP_URLS") or "").strip()
    if not raw:
        return []
    urls = []
    for entry in raw.split(","):
        u = entry.strip().rstrip("/")
        if u:
            urls.append(u if u.endswith("/mcp") else u + "/mcp")
    return urls


def register_mcp_skills(
    *,
    tools: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Discover MCP tools and register each as a ``SkillSpec``.

    Polls the primary MCP server (CHAT_SKILLS_MCP_URL) **plus** any
    additional servers listed in EXTRA_MCP_URLS (comma-separated base
    URLs).  Each server's tools are registered independently; name
    collisions across servers follow the same "first registration wins"
    rule as builtins.

    Args:
        tools: Optional pre-fetched tool list. When ``None`` (default),
            we call ``list_mcp_tools()`` against the primary server.
            Tests inject this to avoid network.

    Returns:
        The list of skill names actually registered (excludes skipped
        builtins + malformed entries). Empty list when all servers are
        unreachable or return nothing.

    This function never raises.
    """
    if tools is not None:
        discovered = tools
    else:
        discovered = list_mcp_tools()
        # Poll extra servers and merge
        for extra_url in _extra_mcp_urls():
            extra_tools = _list_tools_from_url(extra_url)
            if extra_tools:
                logger.info("register_mcp_skills: %d tool(s) from extra MCP server %s", len(extra_tools), extra_url)
                discovered = discovered + extra_tools
            else:
                logger.info("register_mcp_skills: no tools from extra MCP server %s (down or empty)", extra_url)

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
