"""Tests for MCP manager (tool agent uses this to call skills)."""
import asyncio
import os

import pytest
from unittest.mock import AsyncMock, patch

from app.services.mcp_manager import call_mcp_tool, list_mcp_tools


def test_list_tools_returns_list():
    """list_mcp_tools returns a list (empty if MCP server unreachable)."""
    tools = list_mcp_tools()
    assert isinstance(tools, list)
    for t in tools:
        assert "name" in t
        assert "description" in t or "description" in str(t)


def test_call_tool_google_search_integration():
    """Integration: call google_search when MCP server and google-search API are running."""
    tools = list_mcp_tools()
    if not tools:
        pytest.skip("MCP server not reachable (mobius-skills-mcp on port 8006)")
    txt, ok = call_mcp_tool("google_search", {"query": "test", "max_results": 2})
    assert isinstance(txt, str)
    assert isinstance(ok, bool)
    if ok:
        assert len(txt) > 0


def test_mcp_manager_retry_on_connection_error():
    """Retries on ConnectionError; returns error message after exhausting retries."""
    call_count = [0]

    class _RaiseConnectionError:
        async def __aenter__(self):
            call_count[0] += 1
            raise ConnectionError("connection refused")

        async def __aexit__(self, *args):
            pass

    def _mock_streamable(*args, **kwargs):
        return _RaiseConnectionError()

    with patch("mcp.client.streamable_http.streamable_http_client", side_effect=_mock_streamable):
        txt, ok = call_mcp_tool("google_search", {"query": "test", "max_results": 1})
    assert ok is False
    assert "MCP call failed after retries" in txt
    assert "connection refused" in txt
    assert call_count[0] >= 2  # At least 2 attempts (initial + 1 retry)


def test_mcp_manager_event_loop_safety():
    """Call from async context: no RuntimeError (uses ThreadPoolExecutor)."""
    with patch("app.services.mcp_manager._call_mcp_tool_async", new_callable=AsyncMock) as mock:
        mock.return_value = ("test result", True)

        async def from_async_context():
            return call_mcp_tool("google_search", {"query": "x", "max_results": 1})

        result = asyncio.run(from_async_context())
    assert result == ("test result", True)
