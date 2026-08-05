"""MCP manager: connect to Mobius skills MCP server, list tools, call tools.

Replaces direct HTTP calls to google-search and web-scraper. As we add skills
to mobius-skills-mcp, they are discovered via list_tools—no code changes here.
"""
import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

# Default: mobius-skills-mcp runs on port 8006, path /mcp
# CHAT_SKILLS_MCP_URL is the canonical env var (set in deploy/dev.env).
# MCP_SERVER_URL is kept for backward-compat with local-dev overrides.
DEFAULT_MCP_URL = "http://localhost:8006/mcp"
MCP_SERVER_URL = (
    os.environ.get("CHAT_SKILLS_MCP_URL")
    or os.environ.get("MCP_SERVER_URL")
    or DEFAULT_MCP_URL
).strip() or DEFAULT_MCP_URL
MCP_CONNECT_TIMEOUT = float(os.environ.get("MCP_CONNECT_TIMEOUT", "10"))
MCP_READ_TIMEOUT = float(os.environ.get("MCP_READ_TIMEOUT", "60"))
MCP_MAX_RETRIES = 2
MCP_RETRY_DELAY = 1.0

_RETRIABLE_EXCEPTIONS = (ConnectionError, TimeoutError, OSError)

# JSON-RPC "Invalid Request" — the MCP server uses this for a session-id
# mismatch (e.g. right after the server cold-starts and doesn't recognize a
# session id it never actually issued, or issued just before restarting).
# Every call already gets a brand-new ClientSession + initialize() handshake
# (see _call_mcp_tool_async below) — there's no stored session id in this
# file to go "stale" — but before this fix, an McpError of ANY kind fell
# through to the generic `except Exception` and returned failure with zero
# retry, even though the very next attempt (a fresh session, same as any
# other attempt) is exactly what would resolve a transient cold-start
# mismatch. Confirmed live: a fresh curl call succeeds immediately after a
# cold-started credentialing server rejects one with -32600.
_MCP_SESSION_ERROR_CODE = -32600


def _is_session_error(exc: Exception) -> bool:
    """Duck-typed (not isinstance-checked) so this helper doesn't need its
    own ``mcp`` import — ``mcp.shared.exceptions.McpError`` is imported
    lazily alongside the other mcp.client classes, matching the existing
    ImportError-graceful-degradation pattern in this file."""
    err = getattr(exc, "error", None)
    code = getattr(err, "code", None)
    message = (getattr(err, "message", None) or str(exc) or "").lower()
    return code == _MCP_SESSION_ERROR_CODE or "session id" in message


def _get_mcp_url() -> str:
    return MCP_SERVER_URL


def _create_http_client(*, read_timeout: float | None = None):
    """Create httpx AsyncClient with configurable timeouts for MCP."""
    import httpx
    read = float(read_timeout) if read_timeout is not None else float(MCP_READ_TIMEOUT)
    return httpx.AsyncClient(
        timeout=httpx.Timeout(read, connect=MCP_CONNECT_TIMEOUT),
        follow_redirects=True,
    )


def _run_async(coro):
    """Run coroutine; safe when called from sync or from inside an event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result()
    return asyncio.run(coro)


async def _call_mcp_tool_async(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    read_timeout: float | None = None,
) -> tuple[str, bool]:
    """Call an MCP tool. Returns (result_text, success).

    read_timeout: optional longer read timeout (e.g. multi-page web scrape); defaults to MCP_READ_TIMEOUT.
    """
    url = _get_mcp_url()
    last_error = None
    for attempt in range(MCP_MAX_RETRIES + 1):
        try:
            from mcp.client.session import ClientSession
            from mcp.client.streamable_http import streamable_http_client

            async with _create_http_client(read_timeout=read_timeout) as http_client:
                # Tolerant unpack: the installed `mcp` SDK version determines
                # whether streamable_http_client yields (read, write) or
                # (read, write, get_session_id) — requirements.txt pins
                # mcp>=1.0.0 (unpinned ceiling) and Docker layer caching can
                # keep an older resolution baked into the image even when a
                # fresh install would pull a newer one. Only the first two
                # elements are ever used here, so grab those regardless of
                # arity instead of hard-failing on a 3-tuple assumption.
                async with streamable_http_client(url, http_client=http_client) as _streams:
                    read_stream, write_stream = _streams[0], _streams[1]
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        result = await session.call_tool(tool_name, arguments)
                        content = getattr(result, "content", None) or []
                        if isinstance(content, list):
                            parts = []
                            for item in content:
                                if hasattr(item, "text"):
                                    parts.append(item.text)
                                elif isinstance(item, dict) and "text" in item:
                                    parts.append(item["text"])
                                else:
                                    parts.append(str(item))
                            text = "\n\n".join(parts) if parts else ""
                        else:
                            text = str(content)
                        if getattr(result, "isError", False):
                            logger.warning("MCP tool %s returned error", tool_name)
                            return (text or "Tool returned an error", False)
                        logger.info("MCP tool %s completed", tool_name)
                        return (text, True)
        except ImportError as e:
            logger.warning("MCP client not available: %s. Install mcp[cli] and ensure Python 3.11+.", e)
            return ("MCP client not available. Install mcp package.", False)
        except _RETRIABLE_EXCEPTIONS as e:
            last_error = e
            if attempt < MCP_MAX_RETRIES:
                logger.warning("MCP call failed (attempt %s/%s): %s; retrying in %ss", attempt + 1, MCP_MAX_RETRIES + 1, e, MCP_RETRY_DELAY)
                await asyncio.sleep(MCP_RETRY_DELAY)
            else:
                logger.warning("MCP call failed after %s retries: %s", MCP_MAX_RETRIES + 1, e, exc_info=True)
                return (f"MCP call failed after retries: {e}", False)
        except Exception as e:
            last_error = e
            if _is_session_error(e) and attempt < MCP_MAX_RETRIES:
                # Session-id mismatch (typically a cold-started server that
                # doesn't yet recognize a session it never actually issued).
                # The next attempt already gets a brand-new ClientSession +
                # fresh initialize() handshake (see the async-with above) —
                # that's the full "drop the stale session and re-init" fix,
                # no separate session cache to clear.
                logger.warning(
                    "MCP tool %s hit a session error (attempt %s/%s): %s; retrying with a fresh session",
                    tool_name, attempt + 1, MCP_MAX_RETRIES + 1, e,
                )
                await asyncio.sleep(MCP_RETRY_DELAY)
                continue
            logger.warning("MCP call failed: %s (tool=%s)", e, tool_name, exc_info=True)
            return (f"MCP call failed: {e}", False)
    return (f"MCP call failed after retries: {last_error}", False)


def call_mcp_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    read_timeout: float | None = None,
) -> tuple[str, bool]:
    """Synchronous wrapper for MCP tool calls.

    read_timeout: optional seconds for HTTP read (large / slow tools).
    """
    try:
        return _run_async(_call_mcp_tool_async(tool_name, arguments, read_timeout=read_timeout))
    except Exception as e:
        logger.warning("MCP tool call failed: %s (tool=%s)", e, tool_name, exc_info=True)
        return (f"Tool call failed: {e}", False)


async def _list_mcp_tools_async() -> list[dict[str, Any]]:
    """List tools from MCP server.

    Returns a list of ``{name, description, inputSchema}`` dicts.
    ``inputSchema`` is the JSON-Schema object the MCP tool declares for
    its arguments; an empty ``{}`` when the MCP object doesn't carry
    one. Added so ``app.skills.mcp_adapter`` can build ``SkillSpec``s
    with typed input schemas; earlier callers that only read ``name``
    and ``description`` are unaffected (dict keys are additive).
    """
    url = _get_mcp_url()
    try:
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with _create_http_client(read_timeout=None) as http_client:
            # Tolerant unpack — see _call_mcp_tool_async for why.
            async with streamable_http_client(url, http_client=http_client) as _streams:
                read_stream, write_stream = _streams[0], _streams[1]
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    tools = []
                    for t in result.tools:
                        schema = getattr(t, "inputSchema", None) or {}
                        if not isinstance(schema, dict):
                            schema = {}
                        tools.append({
                            "name": t.name,
                            "description": getattr(t, "description", "") or "",
                            "inputSchema": schema,
                        })
                    return tools
    except ImportError:
        return []
    except Exception as e:
        logger.warning("MCP list_tools failed: %s", e, exc_info=True)
        return []


def list_mcp_tools() -> list[dict[str, Any]]:
    """Synchronous wrapper for list_tools."""
    try:
        return _run_async(_list_mcp_tools_async())
    except Exception as e:
        logger.warning("MCP list_tools failed: %s", e, exc_info=True)
        return []
