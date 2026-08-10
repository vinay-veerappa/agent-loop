"""
populate_graph_context.py
=========================
Queries the codebase-memory-mcp graph and writes the context cache file
that build_context_slice() reads.

Usage:
    python scripts/populate_graph_context.py --project C-Users-vinay-agent-loop

This is a standalone script (not part of the loop) that runs before a
loop session to pre-populate the graph context. In a future phase, the
loop itself will call this via the MCP client protocol at startup.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# This script uses the codebase-memory-mcp MCP tools. When run inside
# an MCP-aware agent (like opencode or claude), the tools are available
# as function calls. When run standalone, it reads the graph data from
# the MCP server via stdin/stdout JSON-RPC.

# For now, this script is a placeholder that writes a minimal context
# cache for testing. In production, replace the body of this function
# with actual MCP graph queries (trace_call_path, search_graph, etc.).


def populate_context(project: str, repo: Path) -> None:
    """Populate the graph context cache for the given project."""
    # TODO: call codebase-memory-mcp tools via MCP client protocol
    # For now, write a minimal cache with the key functions from the loop
    context = {
        "run_ticket": {
            "callees": ["chat", "parse_blocks", "build_implement_prompt", "review_panel",
                        "check_static", "check_compile", "check_tests", "check_lock_scope",
                        "append_ledger", "regions.extract", "regions.apply", "workspace.open_workspace"],
            "callers": ["cli.main"],
            "tests": ["test_phase1_state_machine.py::test_p1_1_stale_artifacts_purged",
                      "test_phase1_state_machine.py::test_p1_2_arbiter_deadlock"],
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
            "tests": ["test_package.py::test_strip_code_python",
                      "test_package.py::test_strip_code_csharp"],
            "types": ["GateResult", "Profile"],
        },
        "main": {
            "callees": ["run_ticket", "profiles.get", "DEFAULT_REGISTRY.get"],
            "callers": [],
            "tests": ["test_package.py::test_import"],
            "types": [],
        },
    }

    cache_path = repo / "logs" / "agent_loop" / "graph_context.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(context, indent=2), encoding="utf-8")
    print(f"Wrote graph context cache: {cache_path} ({len(context)} regions)")
    for rid, data in context.items():
        print(f"  {rid}: {len(data['callees'])} callees, {len(data['callers'])} callers, "
              f"{len(data['tests'])} tests, {len(data['types'])} types")


def main() -> int:
    ap = argparse.ArgumentParser(description="Populate graph context cache")
    ap.add_argument("--project", required=True, help="codebase-memory-mcp project name")
    ap.add_argument("--repo", default=".", help="repo root path")
    args = ap.parse_args()
    populate_context(args.project, Path(args.repo))
    return 0


if __name__ == "__main__":
    sys.exit(main())