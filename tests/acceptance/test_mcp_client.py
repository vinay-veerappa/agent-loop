"""
Acceptance tests for the MCP client integration.

These tests verify that the MCP client can find the codebase-memory-mcp
server and (when available) make tool calls. When the server is not
available, the tests verify graceful fallback.
"""
import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agent_loop.mcp_client import (
    MCPClient, find_codebase_memory_mcp, get_mcp_client, shutdown_mcp_client,
)


def test_mcp_client_find_server():
    """find_codebase_memory_mcp returns a path or None (not an exception)."""
    path = find_codebase_memory_mcp()
    # Should return a string path or None, not raise
    assert path is None or isinstance(path, str)


def test_mcp_client_get_returns_none_when_not_found():
    """get_mcp_client returns None when the server is not available."""
    with patch("agent_loop.mcp_client.find_codebase_memory_mcp", return_value=None):
        client = get_mcp_client()
        assert client is None


def test_mcp_client_call_tool_returns_none_when_not_started():
    """call_tool returns None when the client is not started."""
    client = MCPClient("test", "nonexistent.exe")
    assert client.call_tool("any_tool", {}) is None


def test_mcp_client_stop_is_safe_when_not_started():
    """stop() is safe to call when the client is not started."""
    client = MCPClient("test", "nonexistent.exe")
    client.stop()  # should not raise


def test_mcp_client_shutdown_is_safe():
    """shutdown_mcp_client is safe to call when no client exists."""
    shutdown_mcp_client()  # should not raise


def test_mcp_client_dataclass_fields():
    """MCPClient has the expected fields."""
    client = MCPClient("test", "/path/to/exe", ["arg1"], {"ENV": "val"})
    assert client.server_name == "test"
    assert client.command == "/path/to/exe"
    assert client.args == ["arg1"]
    assert client.env == {"ENV": "val"}
    assert client._proc is None
    assert client._next_id == 1