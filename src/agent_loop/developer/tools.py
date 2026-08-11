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

from .._io import read_text_verbatim, write_text_verbatim
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
        "name": "write_test",
        "description": (
            "Write a test that FAILS against the current code, proving the defect "
            "exists. Available only in the red phase, before any source edit. The "
            "test is run immediately: if it passes, it does not test the defect and "
            "is rejected. Once it fails you may edit source, and the test becomes "
            "read-only for the rest of the run."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path of the test file to create or overwrite"},
                "content": {"type": "string", "description": "The complete contents of the test file"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_build",
        "description": "Run the profile's build command. Takes no arguments. Returns success/failure and output.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "run_tests",
        "description": "Run the profile's test command. Takes no arguments. Returns pass/fail counts and failures.",
        "parameters": {"type": "object", "properties": {}},
    },
]


def render_tool_docs(available: List[str]) -> str:
    """The tool-call protocol and argument schemas, for the system prompt.

    Generated from TOOL_SCHEMAS rather than restated in prose. The prompt used
    to name the tools but never state the call format or the argument names,
    while the parser accepted exactly one undocumented syntax -- so the model
    had to guess the protocol, and a wrong guess parsed as "no tool calls".
    """
    lines = [
        "TOOL CALL FORMAT - emit one block per call, exactly like this:",
        "",
        '<<<TOOL name="read_file">>>',
        '{"path": "src/example.py", "start_line": 1}',
        "<<<END TOOL>>>",
        "",
        "The body must be a single JSON object. You may emit several blocks in one turn.",
        "",
        "TOOLS AVAILABLE TO YOU NOW:",
    ]
    for schema in TOOL_SCHEMAS:
        if schema["name"] not in available:
            continue
        props = schema["parameters"].get("properties", {})
        required = set(schema["parameters"].get("required", []))
        args = ", ".join(
            f"{k}{'' if k in required else '?'}: {v.get('type', 'string')}"
            for k, v in props.items()
        )
        lines.append(f"- {schema['name']}({args}) - {schema['description']}")
    return "\n".join(lines)


def _resolve_in_repo(repo: Path, rel: str) -> Optional[Path]:
    """Resolve `rel` inside `repo`, or None if it escapes.

    `str.startswith` is not containment: it accepts /repo-backup for /repo.
    """
    root = repo.resolve()
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def execute_tool(
    tool_name: str,
    args: Dict[str, Any],
    repo: Path,
    profile: Profile,
    edited: Optional[List[str]] = None,
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
    elif tool_name == "write_test":
        return _write_test(repo, args, profile)
    elif tool_name == "run_build":
        return _reject_args(tool_name, args) or _run_build(repo, profile, edited)
    elif tool_name == "run_tests":
        return _reject_args(tool_name, args) or _run_tests(repo, profile)
    else:
        return f"ERROR: unknown tool {tool_name}"


def _reject_args(tool_name: str, args: Dict[str, Any]) -> str:
    """run_build and run_tests take no arguments and run the PROFILE's command.

    They used to accept and silently discard whatever they were given. A model
    passed `command` with an ad-hoc verification script, got back the output of
    the ordinary build, and reported in its summary that it had verified the
    new behaviour -- it had not, and nothing told it so. Refusing out loud is
    the same principle the driver already applies to a tool call it cannot
    offer: answer it, never drop it.
    """
    if not args:
        return ""
    return (
        f"ERROR: {tool_name} takes no arguments and runs the profile's configured "
        f"command; it cannot run an arbitrary one. You passed {sorted(args)}, which "
        f"was NOT executed. To check behaviour the suite does not cover, write it as "
        f"a test."
    )


def _read_file(repo: Path, args: Dict[str, Any]) -> str:
    """Read a file, windowed to 100 lines."""
    path = _resolve_in_repo(repo, args["path"])
    if path is None:
        return f"ERROR: path {args['path']} escapes the repo root"
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
    """Trace callers/callees of a function via the codebase-memory-mcp graph."""
    func = args["function_name"]
    direction = args.get("direction", "both")

    if not profile.graph_project:
        return f"[graph] no graph_project configured for this profile"

    try:
        from ..mcp_client import get_mcp_client
        client = get_mcp_client()
        if not client:
            return f"[graph] codebase-memory-mcp not available; cannot trace {func!r}"

        result = client.call_tool("trace_call_path", {
            "function_name": func,
            "direction": direction,
            "project": profile.graph_project,
            "depth": 2,
        })

        if result is None:
            return f"[graph] no result from trace_call_path for {func!r}"
        if result.startswith("ERROR"):
            return f"[graph] {result}"
        return result
    except Exception as exc:
        return f"[graph] trace_call_path failed: {exc}"


def _write_test(repo: Path, args: Dict[str, Any], profile: Profile) -> str:
    """Create the failing test that licenses an edit.

    The inverse of `_edit_file`'s protected check: this may write ONLY where the
    profile says tests live, so the red phase cannot be used to reach source.
    Whether the test is actually red is not decided here -- the driver runs it
    and refuses to leave the red phase until it fails.
    """
    from fnmatch import fnmatch

    rel = args["path"].replace("\\", "/")
    path = _resolve_in_repo(repo, args["path"])
    if path is None:
        return f"ERROR: path {args['path']} escapes the repo root"

    patterns = profile.test_sources or ()
    if not patterns:
        return (
            "ERROR: this profile declares no test_sources, so there is nowhere a "
            "generated test may legally be written."
        )
    if not any(fnmatch(rel, p) or fnmatch(Path(rel).name, p) for p in patterns):
        return (
            f"ERROR: {args['path']} is not a test location for this profile. "
            f"Tests must match one of: {', '.join(patterns)}."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    path.write_text(args["content"], encoding="utf-8")
    return (
        f"OK: {'overwrote' if existed else 'wrote'} {args['path']} "
        f"({len(args['content'].splitlines())} lines). Running the suite to confirm "
        f"it fails."
    )


def _edit_file(repo: Path, args: Dict[str, Any], profile: Profile) -> str:
    """Apply an edit to a file using exact string replacement."""
    path = _resolve_in_repo(repo, args["path"])
    if path is None:
        return f"ERROR: path {args['path']} escapes the repo root"

    # Gate 0 applies here too. Patch mode refuses a ticket whose regions touch
    # the code that grades it; Developer mode picks its own targets, so without
    # this check it could edit the very tests it must pass -- and the nt8
    # profile's file_scope_whitelist contains the directory its *Tests.cs live
    # in, so the scope check alone does not stop it.
    from ..gates import check_protected_paths
    from ..profiles import DEFAULT_PROTECTED

    verdict = check_protected_paths([args["path"]], profile.protected or DEFAULT_PROTECTED)
    if not verdict.ok:
        return (
            f"ERROR: {args['path']} is protected - it is part of the code that grades "
            f"this change and may not be edited ({verdict.detail}). Fix the defect in "
            f"the implementation instead."
        )

    if not path.exists():
        return f"ERROR: file not found: {args['path']}"

    # Match against LF-normalised text but write back the file's own line
    # terminators, so editing one line of a CRLF file does not rewrite every
    # line of it.
    raw = read_text_verbatim(path)
    newline = "\r\n" if "\r\n" in raw else "\n"
    content = raw.replace("\r\n", "\n")
    old_str = args["old_str"].replace("\r\n", "\n")
    new_str = args["new_str"].replace("\r\n", "\n")

    if old_str not in content:
        return f"ERROR: old_str not found in {args['path']}. Make sure it matches exactly."

    count = content.count(old_str)
    if count > 1:
        return f"ERROR: old_str found {count} times in {args['path']}. Make it unique."

    new_content = content.replace(old_str, new_str, 1)
    if newline != "\n":
        new_content = new_content.replace("\n", newline)
    write_text_verbatim(path, new_content)
    return f"OK: edited {args['path']} (replaced {len(old_str)} chars with {len(new_str)} chars)"


def _run_build(repo: Path, profile: Profile, files: Optional[List[str]] = None) -> str:
    """Run the profile's build command."""
    if not profile.build_cmd:
        return "OK: no build command configured"
    cmd = profile.build_cmd
    if "{files}" in cmd:
        if not files:
            return "OK: no files edited yet, nothing to build"
        cmd = cmd.replace("{files}", " ".join(f'"{f}"' for f in files))
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(repo),
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