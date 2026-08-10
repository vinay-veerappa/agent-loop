"""
populate_graph_context.py
=========================
Queries the codebase-memory-mcp graph and writes the context cache file
that build_context_slice() reads as a fallback.

Usage:
    python scripts/populate_graph_context.py --project C-Users-vinay-agent-loop

When the MCP server is available, this script queries the graph for each
function in the repo and writes a context cache. When the MCP server is
not available, it writes a minimal hardcoded cache.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def populate_context_live(project: str, repo: Path) -> None:
    """Populate the graph context cache via live MCP queries."""
    sys.path.insert(0, str(repo / "src"))
    from agent_loop.mcp_client import get_mcp_client, shutdown_mcp_client

    client = get_mcp_client()
    if not client:
        print("MCP server not available; writing minimal cache")
        populate_context_fallback(project, repo)
        return

    print(f"MCP server started; tools: {[t['name'] for t in client.list_tools()]}")

    # Get the architecture overview to find key functions
    arch_result = client.call_tool("get_architecture", {
        "project": project,
        "aspects": ["packages"],
    })

    # Query key functions from the loop
    key_functions = [
        "run_ticket", "adjudicate", "check_static", "check_compile",
        "check_tests", "check_lock_scope", "parse_blocks", "review_panel",
        "build_implement_prompt", "build_review_prompt", "main",
        "compact_history", "build_context_slice", "extract_settled",
        "save_settled", "run_plan", "run_test", "run_developer",
        "execute_tool", "find_region", "strip_code",
    ]

    context = {}
    for func_name in key_functions:
        entry = {"callees": [], "callers": [], "tests": [], "types": []}

        # Get callees (outbound)
        result = client.call_tool("trace_call_path", {
            "function_name": func_name,
            "direction": "outbound",
            "project": project,
            "depth": 1,
        })
        if result and not result.startswith("ERROR"):
            import json as _json
            try:
                data = _json.loads(result)
                for callee in data.get("callees", []):
                    entry["callees"].append(callee.get("name", ""))
            except (_json.JSONDecodeError, TypeError):
                # Parse text format
                for line in result.splitlines():
                    if '"name"' in line:
                        import re
                        m = re.search(r'"name":\s*"([^"]+)"', line)
                        if m:
                            entry["callees"].append(m.group(1))

        # Get callers (inbound)
        result = client.call_tool("trace_call_path", {
            "function_name": func_name,
            "direction": "inbound",
            "project": project,
            "depth": 1,
        })
        if result and not result.startswith("ERROR"):
            try:
                data = json.loads(result)
                for caller in data.get("callees", []):
                    entry["callers"].append(caller.get("name", ""))
            except (json.JSONDecodeError, TypeError):
                for line in result.splitlines():
                    if '"name"' in line:
                        import re
                        m = re.search(r'"name":\s*"([^"]+)"', line)
                        if m:
                            entry["callers"].append(m.group(1))

        # Search for tests
        result = client.call_tool("search_code", {
            "pattern": func_name,
            "file_pattern": "*test*",
            "path_filter": "tests/",
            "project": project,
        })
        if result and not result.startswith("ERROR"):
            for line in result.splitlines()[:5]:
                if func_name in line:
                    entry["tests"].append(line.strip()[:80])

        if entry["callees"] or entry["callers"] or entry["tests"]:
            context[func_name] = entry
            print(f"  {func_name}: {len(entry['callees'])} callees, "
                  f"{len(entry['callers'])} callers, {len(entry['tests'])} tests")

    shutdown_mcp_client()

    if context:
        cache_path = repo / "logs" / "agent_loop" / "graph_context.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(context, indent=2), encoding="utf-8")
        print(f"Wrote graph context cache: {cache_path} ({len(context)} functions)")
    else:
        print("No context data collected; writing fallback cache")
        populate_context_fallback(project, repo)


def populate_context_fallback(project: str, repo: Path) -> None:
    """Write a minimal hardcoded cache (fallback when MCP is not available)."""
    context = {
        "run_ticket": {
            "callees": ["chat", "parse_blocks", "build_implement_prompt", "review_panel",
                        "check_static", "check_compile", "check_tests", "check_lock_scope",
                        "append_ledger", "regions.extract", "regions.apply"],
            "callers": ["cli.main"],
            "tests": ["test_phase1_state_machine.py"],
            "types": ["PanelResult", "Vote", "GateResult", "RoundRecord"],
        },
        "adjudicate": {
            "callees": ["chat", "_section", "build_prompt"],
            "callers": ["run_ticket"],
            "tests": [],
            "types": ["Adjudication", "Ruling"],
        },
        "check_static": {
            "callees": ["strip_code"],
            "callers": ["run_ticket"],
            "tests": ["test_package.py::test_strip_code_python"],
            "types": ["GateResult", "Profile"],
        },
        "main": {
            "callees": ["run_ticket", "profiles.get"],
            "callers": [],
            "tests": ["test_package.py::test_import"],
            "types": [],
        },
    }

    cache_path = repo / "logs" / "agent_loop" / "graph_context.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(context, indent=2), encoding="utf-8")
    print(f"Wrote fallback cache: {cache_path} ({len(context)} functions)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Populate graph context cache")
    ap.add_argument("--project", required=True, help="codebase-memory-mcp project name")
    ap.add_argument("--repo", default=".", help="repo root path")
    args = ap.parse_args()
    populate_context_live(args.project, Path(args.repo))
    return 0


if __name__ == "__main__":
    sys.exit(main())