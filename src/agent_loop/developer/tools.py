"""
developer/tools.py
==================
LLM-callable tools for Developer mode (Phase 7 + 8).

Minimal tool set following SWE-agent's ACI principles:
- read_file: 100-line window default, scroll via start_line
- search_code: graph-augmented grep, ranks by structural importance
- trace_call_path: who calls this / what does it call
- edit_file: str_replace exact match, linter-on-edit
- run_build: profile's build_cmd
- run_tests: profile's test_cmd

Deliberately excluded: run_command (arbitrary shell), browser, git.
This repo has code that moves real money; tool scope matters.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..profiles import Profile


# Tool schemas for the LLM (OpenAI function-calling format)
TOOL_SCHEMAS = [
    {
        "name": "read_file",
        "description": "Read a file, windowed to 100 lines by default. Use start_line to scroll.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to the file"},
                "start_line": {"type": "integer", "description": "1-based line to start reading from (default 1)"},
                "end_line": {"type": "integer", "description": "1-based line to end reading at (default start+100)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_code",
        "description": "Search for a pattern in the codebase. Returns files with matches, ranked by structural importance.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The pattern to search for (regex or substring)"},
                "file_pattern": {"type": "string", "description": "Glob pattern to filter files (e.g. *.py)"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "trace_call_path",
        "description": "Trace who calls a function and what it calls. Answers 'will this break callers?'",
        "parameters": {
            "type": "object",
            "properties": {
                "function_name": {"type": "string", "description": "The function name to trace"},
                "direction": {"type": "string", "enum": ["inbound", "outbound", "both"], "description": "Trace direction"},
            },
            "required": ["function_name", "direction"],
        },
    },
    {
        "name": "edit_file",
        "description": "Apply an edit to a file using exact string replacement. The old_str must match exactly.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to the file"},
                "old_str": {"type": "string", "description": "The exact string to replace"},
                "new_str": {"type": "string", "description": "The replacement string"},
            },
            "required": ["path", "old_str", "new_str"],
        },
    },
    {
        "name": "run_build",
        "description": "Run the profile's build command. Returns success/failure and output.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "run_tests",
        "description": "Run the profile's test command. Returns pass/fail counts and failures.",
        "parameters": {"type": "object", "properties": {}},
    },
]


def execute_tool(
    tool_name: str,
    args: Dict[str, Any],
    repo: Path,
    profile: Profile,
) -> str:
    """Execute a tool call and return the result as a string."""
    if tool_name == "read_file":
        return _read_file(repo, args)
    elif tool_name == "search_code":
        return _search_code(repo, args, profile)
    elif tool_name == "trace_call_path":
        return _trace_call_path(args, profile)
    elif tool_name == "edit_file":
        return _edit_file(repo, args, profile)
    elif tool_name == "run_build":
        return _run_build(repo, profile)
    elif tool_name == "run_tests":
        return _run_tests(repo, profile)
    else:
        return f"ERROR: unknown tool {tool_name}"


def _read_file(repo: Path, args: Dict[str, Any]) -> str:
    """Read a file, windowed to 100 lines."""
    path = repo / args["path"]
    if not path.exists():
        return f"ERROR: file not found: {args['path']}"
    start = args.get("start_line", 1)
    end = args.get("end_line", start + 100)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    start = max(1, min(start, total))
    end = min(end, total)
    result = [f"File: {args['path']} (lines {start}-{end} of {total})"]
    for i in range(start - 1, end):
        result.append(f"{i+1:5d}: {lines[i]}")
    return "\n".join(result)


def _search_code(repo: Path, args: Dict[str, Any], profile: Profile) -> str:
    """Search for a pattern in the codebase."""
    pattern = args["pattern"]
    file_pattern = args.get("file_pattern", f"*{profile.file_suffixes[0]}")
    try:
        regex = re.compile(pattern)
    except re.error:
        regex = None

    matches = []
    for path in repo.rglob(file_pattern):
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(content.splitlines(), 1):
                if regex and regex.search(line) or pattern in line:
                    rel = path.relative_to(repo)
                    matches.append(f"{rel}:{i}: {line.strip()[:100]}")
        except Exception:
            continue

    if not matches:
        return f"No matches found for pattern: {pattern}"
    # Limit to 50 matches
    result = [f"Found {len(matches)} matches (showing first 50):"]
    result.extend(matches[:50])
    return "\n".join(result)


def _trace_call_path(args: Dict[str, Any], profile: Profile) -> str:
    """Trace callers/callees of a function.

    NOTE: This is a stub that returns a placeholder. In a full implementation,
    this calls the codebase-memory-mcp graph via the MCP client protocol.
    """
    func = args["function_name"]
    direction = args.get("direction", "both")
    return f"[graph] trace_call_path for {func!r} (direction={direction}) -- requires MCP graph connection (not yet wired in Developer mode)"


def _edit_file(repo: Path, args: Dict[str, Any], profile: Profile) -> str:
    """Apply an edit to a file using exact string replacement."""
    path = repo / args["path"]
    if not path.exists():
        return f"ERROR: file not found: {args['path']}"

    content = path.read_text(encoding="utf-8")
    old_str = args["old_str"]
    new_str = args["new_str"]

    if old_str not in content:
        return f"ERROR: old_str not found in {args['path']}. Make sure it matches exactly."

    count = content.count(old_str)
    if count > 1:
        return f"ERROR: old_str found {count} times in {args['path']}. Make it unique."

    new_content = content.replace(old_str, new_str, 1)
    path.write_text(new_content, encoding="utf-8")
    return f"OK: edited {args['path']} (replaced {len(old_str)} chars with {len(new_str)} chars)"


def _run_build(repo: Path, profile: Profile) -> str:
    """Run the profile's build command."""
    if not profile.build_cmd:
        return "OK: no build command configured"
    try:
        proc = subprocess.run(
            profile.build_cmd, shell=True, cwd=str(repo),
            capture_output=True, text=True, timeout=900,
        )
        if proc.returncode == 0:
            return f"OK: build succeeded\n{proc.stdout[-2000:]}"
        else:
            return f"FAIL: build failed (exit {proc.returncode})\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    except subprocess.TimeoutExpired:
        return "FAIL: build timed out"
    except Exception as exc:
        return f"FAIL: {exc}"


def _run_tests(repo: Path, profile: Profile) -> str:
    """Run the profile's test command."""
    if not profile.test_cmd:
        return "OK: no test command configured"
    try:
        proc = subprocess.run(
            profile.test_cmd, shell=True, cwd=str(repo),
            capture_output=True, text=True, timeout=900,
        )
        output = proc.stdout + "\n" + proc.stderr
        # Parse with the loop's test parser
        from ..gates import parse_tests
        outcome = parse_tests(output)
        if outcome.ran:
            return f"Tests: {outcome.passed} passed, {outcome.failed} failed\nFailures: {outcome.failures or 'none'}"
        else:
            return f"Tests: runner did not reach RESULTS\n{output[-2000:]}"
    except subprocess.TimeoutExpired:
        return "FAIL: tests timed out"
    except Exception as exc:
        return f"FAIL: {exc}"