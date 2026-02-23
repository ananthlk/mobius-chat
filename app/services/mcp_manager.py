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
DEFAULT_MCP_URL = "http://localhost:8006/mcp"
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", DEFAULT_MCP_URL).strip() or DEFAULT_MCP_URL
MCP_CONNECT_TIMEOUT = float(os.environ.get("MCP_CONNECT_TIMEOUT", "10"))
MCP_READ_TIMEOUT = float(os.environ.get("MCP_READ_TIMEOUT", "60"))
MCP_MAX_RETRIES = 2
MCP_RETRY_DELAY = 1.0

_RETRIABLE_EXCEPTIONS = (ConnectionError, TimeoutError, OSError)


def _get_mcp_url() -> str:
    return MCP_SERVER_URL


def _create_http_client():
    """Create httpx AsyncClient with configurable timeouts for MCP."""
    import httpx
    return httpx.AsyncClient(
        timeout=httpx.Timeout(MCP_READ_TIMEOUT, connect=MCP_CONNECT_TIMEOUT),
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


async def _call_mcp_tool_async(tool_name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
    """Call an MCP tool. Returns (result_text, success)."""
    url = _get_mcp_url()
    last_error = None
    for attempt in range(MCP_MAX_RETRIES + 1):
        try:
            from mcp.client.session import ClientSession
            from mcp.client.streamable_http import streamable_http_client

            async with _create_http_client() as http_client:
                async with streamable_http_client(url, http_client=http_client) as (read_stream, write_stream, _):
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
            logger.warning("MCP call failed: %s (tool=%s)", e, tool_name, exc_info=True)
            return (f"MCP call failed: {e}", False)
    return (f"MCP call failed after retries: {last_error}", False)


def call_mcp_tool(tool_name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
    """Synchronous wrapper for MCP tool calls."""
    try:
        return _run_async(_call_mcp_tool_async(tool_name, arguments))
    except Exception as e:
        logger.warning("MCP tool call failed: %s (tool=%s)", e, tool_name, exc_info=True)
        return (f"Tool call failed: {e}", False)


async def _list_mcp_tools_async() -> list[dict[str, Any]]:
    """List tools from MCP server. Returns list of {name, description}."""
    url = _get_mcp_url()
    try:
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with _create_http_client() as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    tools = []
                    for t in result.tools:
                        tools.append({
                            "name": t.name,
                            "description": getattr(t, "description", "") or "",
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
