"""
Acceptance test for T-REL1: MCP stderr drain prevents pipe deadlock.

Without the background stderr drain, an MCP server that emits verbose
diagnostics fills its 64KB OS stderr pipe buffer, blocks on its next stderr
write, and the client blocks on stdout.readline() waiting for a response that
never arrives. The 120-second deadline in _send is never evaluated because
readline() holds the thread.

This test spawns a fake server that writes ~100KB to stderr BEFORE responding
on stdout, and verifies the client receives the response. Without the drain,
this test would hang until the pytest timeout.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from agent_loop.mcp_client import MCPClient


def _fake_server_script() -> str:
    """A script that writes ~100KB to stderr, then responds on stdout.

    This simulates a chatty MCP server. Without the stderr drain, the OS pipe
    buffer (64KB) fills, the server blocks on stderr.write, and the client
    never receives the stdout response.
    """
    return textwrap.dedent("""\
        import json, sys, time

        # Write ~100KB to stderr -- more than the 64KB OS pipe buffer.
        # Without a drain, the server blocks here and never writes to stdout.
        for i in range(2000):
            sys.stderr.write(f"diagnostic line {i}: " + "x" * 50 + "\\n")
        sys.stderr.flush()

        # Respond to initialize on stdout.
        line = sys.stdin.readline()
        msg = json.loads(line)
        resp = {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
        sys.stdout.write(json.dumps(resp) + "\\n")
        sys.stdout.flush()

        # Consume the notifications/initialized notification (no id, no response).
        sys.stdin.readline()

        # Respond to tools/list on stdout.
        line = sys.stdin.readline()
        msg = json.loads(line)
        resp = {"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": []}}
        sys.stdout.write(json.dumps(resp) + "\\n")
        sys.stdout.flush()

        # Keep alive for the duration of the test.
        time.sleep(10)
    """)


def test_mcp_stderr_drain_prevents_deadlock(tmp_path):
    """A chatty MCP server does not deadlock the client.

    The fake server writes ~100KB to stderr before responding. Without the
    stderr drain thread, the client would hang on stdout.readline() because the
    server blocked on its stderr write. With the drain, the client receives
    the initialize response and completes the handshake.
    """
    script = tmp_path / "fake_server.py"
    script.write_text(_fake_server_script(), encoding="utf-8")

    client = MCPClient("test-chatty", sys.executable, [str(script)])
    try:
        t0 = time.time()
        client.start()
        elapsed = time.time() - t0
        # If the drain is not working, this hangs for the full 120s deadline.
        # A working drain completes in well under 10 seconds.
        assert elapsed < 30, (
            f"start() took {elapsed:.1f}s -- the stderr drain is not working "
            f"and the client deadlocked on the pipe"
        )
        assert client._tools == [], "tools/list should return empty list"
        # The stderr buffer should have captured diagnostic lines.
        assert len(client._stderr_lines) > 0, "stderr drain captured nothing"
    finally:
        client.stop()


def test_mcp_stderr_tail_returns_last_lines(tmp_path):
    """stderr_tail() returns the last N lines of captured stderr."""
    script = tmp_path / "fake_server.py"
    script.write_text(_fake_server_script(), encoding="utf-8")

    client = MCPClient("test-tail", sys.executable, [str(script)])
    try:
        client.start()
        # Wait for the drain to capture lines.
        time.sleep(0.5)
        tail = client.stderr_tail(5)
        assert "diagnostic line" in tail, f"stderr tail should contain diagnostic lines: {tail!r}"
        # stderr_tail(0) returns empty string.
        assert client.stderr_tail(0) == ""
    finally:
        client.stop()


def test_mcp_stderr_drain_is_daemon_thread(tmp_path):
    """The stderr drain thread is a daemon so it does not block process exit."""
    script = tmp_path / "fake_server.py"
    script.write_text(_fake_server_script(), encoding="utf-8")

    client = MCPClient("test-daemon", sys.executable, [str(script)])
    try:
        client.start()
        assert client._stderr_thread is not None, "stderr thread should be running"
        assert client._stderr_thread.daemon, (
            "stderr drain thread must be a daemon so it does not block "
            "process exit when the pipe closes"
        )
    finally:
        client.stop()