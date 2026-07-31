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


@pytest.mark.integration
@pytest.mark.requires_skills
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


def _mock_call_tool_ctx(call_tool_side_effect):
    """Build the (http_client, streamable_http_client, ClientSession) mock
    trio for a single _call_mcp_tool_async attempt, wired so ``session.
    call_tool`` uses the given side_effect. Mirrors the fresh-session-per-
    attempt shape _call_mcp_tool_async actually uses."""
    from unittest.mock import AsyncMock, MagicMock

    session = MagicMock()
    session.initialize = AsyncMock()
    session.call_tool = AsyncMock(side_effect=call_tool_side_effect)

    class _Ctx:
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *a):
            return False

    return session, _Ctx


def test_mcp_manager_retries_and_recovers_on_session_error():
    """A JSON-RPC -32600 'Missing session ID' error (the class a server
    cold-start can produce) must retry — the next attempt already gets a
    brand-new ClientSession + fresh initialize() per the existing loop
    structure, so a transient session mismatch self-heals on retry."""
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    call_count = {"n": 0}

    async def flaky_then_ok(tool_name, arguments):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise McpError(ErrorData(code=-32600, message="Missing session ID"))
        result = type("R", (), {})()
        result.isError = False
        result.content = [type("C", (), {"text": "org found"})()]
        return result

    session, Ctx = _mock_call_tool_ctx(flaky_then_ok)

    with patch("app.services.mcp_manager._create_http_client", return_value=Ctx(object())), \
         patch("mcp.client.streamable_http.streamable_http_client", return_value=Ctx((None, None, None))), \
         patch("mcp.client.session.ClientSession", return_value=Ctx(session)):
        txt, ok = call_mcp_tool("search_orgs", {"query": "david lawrence center"})

    assert ok is True
    assert "org found" in txt
    assert call_count["n"] == 2, "expected exactly one retry after the session error"


def test_mcp_manager_gives_up_gracefully_when_session_error_persists():
    """If the session error never clears (all retries exhausted), fail
    cleanly — no crash, no infinite retry."""
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    async def always_session_error(tool_name, arguments):
        raise McpError(ErrorData(code=-32600, message="Missing session ID"))

    session, Ctx = _mock_call_tool_ctx(always_session_error)

    with patch("app.services.mcp_manager._create_http_client", return_value=Ctx(object())), \
         patch("mcp.client.streamable_http.streamable_http_client", return_value=Ctx((None, None, None))), \
         patch("mcp.client.session.ClientSession", return_value=Ctx(session)):
        txt, ok = call_mcp_tool("search_orgs", {"query": "x"})

    assert ok is False
    assert "Missing session ID" in txt


def test_mcp_manager_does_not_retry_non_session_mcp_errors():
    """A DIFFERENT McpError (not a session mismatch) must fail immediately
    — retrying it would mask a real error as a transient one."""
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    call_count = {"n": 0}

    async def other_error(tool_name, arguments):
        call_count["n"] += 1
        raise McpError(ErrorData(code=-32601, message="Method not found"))

    session, Ctx = _mock_call_tool_ctx(other_error)

    with patch("app.services.mcp_manager._create_http_client", return_value=Ctx(object())), \
         patch("mcp.client.streamable_http.streamable_http_client", return_value=Ctx((None, None, None))), \
         patch("mcp.client.session.ClientSession", return_value=Ctx(session)):
        txt, ok = call_mcp_tool("search_orgs", {"query": "x"})

    assert ok is False
    assert call_count["n"] == 1, "a non-session MCP error must not be retried"


def test_mcp_manager_event_loop_safety():
    """Call from async context: no RuntimeError (uses ThreadPoolExecutor)."""
    with patch("app.services.mcp_manager._call_mcp_tool_async", new_callable=AsyncMock) as mock:
        mock.return_value = ("test result", True)

        async def from_async_context():
            return call_mcp_tool("google_search", {"query": "x", "max_results": 1})

        result = asyncio.run(from_async_context())
    assert result == ("test result", True)
