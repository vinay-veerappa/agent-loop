"""
mcp_client.py
=============
Lightweight MCP (Model Context Protocol) client for stdio-based servers.

The codebase-memory-mcp server runs as a local exe that speaks JSON-RPC
over stdin/stdout. This module provides a minimal client that can:
- Spawn the MCP server process
- Send tool calls and receive responses
- Be used by context.py, developer/tools.py, and populate_graph_context.py

The MCP protocol is simple: each message is a JSON-RPC 2.0 object sent
as a line on stdin, with responses read from stdout. The server expects:
1. An initialize handshake
2. A tools/list call to discover available tools
3. tools/call invocations with tool name and arguments

This is a minimal implementation -- no streaming, no notifications,
no resource subscriptions. Just call a tool and get the result.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent_loop import __version__


# --------------------------------------------------------------------------
# MCP client
# --------------------------------------------------------------------------
@dataclass
class MCPClient:
    """A minimal MCP client for stdio-based servers.

    Usage:
        client = MCPClient("codebase-memory-mcp", "/path/to/server.exe")
        client.start()
        result = client.call_tool("trace_call_path", {
            "function_name": "run_ticket",
            "direction": "outbound",
            "project": "C-Users-vinay-agent-loop",
        })
        client.stop()
    """
    server_name: str
    command: str
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    _proc: Optional[subprocess.Popen] = None
    _next_id: int = 1
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _tools: List[Dict[str, Any]] = field(default_factory=list)

    def start(self) -> None:
        """Spawn the MCP server process and perform the initialize handshake."""
        full_env = dict(os.environ)
        full_env.update(self.env)
        self._proc = subprocess.Popen(
            [self.command] + self.args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
            text=True,
            bufsize=1,
            encoding="utf-8",
        )
        # Initialize handshake
        resp = self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "agent-loop", "version": __version__},
        })
        if resp is None:
            raise RuntimeError(f"MCP server {self.server_name} did not respond to initialize")

        # Send initialized notification (no response expected)
        self._notify("notifications/initialized", {})

        # Discover available tools
        tools_resp = self._send("tools/list", {})
        if tools_resp and "result" in tools_resp:
            self._tools = tools_resp["result"].get("tools", [])

    def stop(self) -> None:
        """Terminate the MCP server process."""
        if self._proc:
            try:
                self._proc.stdin.close()
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
            self._proc = None

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Optional[str]:
        """Call a tool on the MCP server and return the text result.

        Returns None if the call fails or the server is not running.
        """
        if not self._proc:
            return None
        resp = self._send("tools/call", {"name": name, "arguments": arguments})
        if resp is None:
            return None
        if "error" in resp:
            error = resp["error"]
            return f"ERROR: {error.get('message', 'unknown error')}"
        result = resp.get("result", {})
        # MCP tools return content as a list of content blocks
        content = result.get("content", [])
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        return "\n".join(texts) if texts else None

    def list_tools(self) -> List[Dict[str, Any]]:
        """Return the list of available tools."""
        return self._tools

    def _send(self, method: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Send a JSON-RPC request and read the response."""
        with self._lock:
            if not self._proc or not self._proc.stdin or not self._proc.stdout:
                return None
            msg_id = self._next_id
            self._next_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": method,
                "params": params,
            }
            self._proc.stdin.write(json.dumps(request) + "\n")
            self._proc.stdin.flush()

            # Read response lines until we get our response
            deadline = time.time() + 120  # 2-minute timeout
            while time.time() < deadline:
                line = self._proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Skip notifications (no id)
                if "id" not in msg:
                    continue
                if msg["id"] == msg_id:
                    return msg
            return None

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if not self._proc or not self._proc.stdin:
            return
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        self._proc.stdin.write(json.dumps(notification) + "\n")
        self._proc.stdin.flush()


# --------------------------------------------------------------------------
# Convenience: find the codebase-memory-mcp server
# --------------------------------------------------------------------------
def find_codebase_memory_mcp() -> Optional[str]:
    """Find the codebase-memory-mcp executable.

    Checks:
    1. CBM_MCP_PATH env var
    2. The standard install location on Windows
    3. PATH
    """
    # 1. Env var
    path = os.getenv("CBM_MCP_PATH")
    if path and Path(path).exists():
        return path

    # 2. Standard Windows install location
    windows_path = Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "codebase-memory-mcp" / "codebase-memory-mcp.exe"
    if windows_path.exists():
        return str(windows_path)

    # 3. Check .mcp.json in the repo root
    mcp_json = Path(".mcp.json")
    if mcp_json.exists():
        try:
            config = json.loads(mcp_json.read_text(encoding="utf-8"))
            cbm = config.get("mcpServers", {}).get("codebase-memory-mcp", {})
            cmd = cbm.get("command", "")
            if cmd and Path(cmd).exists():
                return cmd
        except (json.JSONDecodeError, OSError):
            pass

    return None


# --------------------------------------------------------------------------
# Cached client (so we don't spawn a new process for every call)
# --------------------------------------------------------------------------
_cached_client: Optional[MCPClient] = None


def get_mcp_client() -> Optional[MCPClient]:
    """Get or create a cached MCP client for codebase-memory-mcp.

    Returns None if the server is not found or fails to start.
    """
    global _cached_client
    if _cached_client and _cached_client._proc and _cached_client._proc.poll() is None:
        return _cached_client

    cmd = find_codebase_memory_mcp()
    if not cmd:
        return None

    try:
        client = MCPClient("codebase-memory-mcp", cmd)
        client.start()
        _cached_client = client
        return client
    except Exception:
        return None


def shutdown_mcp_client() -> None:
    """Shut down the cached MCP client if it exists."""
    global _cached_client
    if _cached_client:
        _cached_client.stop()
        _cached_client = None