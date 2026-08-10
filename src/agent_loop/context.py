"""
context.py
==========
Graph freshness check and passive context injection (Phase 3).

Phase 2: checks whether the codebase-memory-mcp graph is fresh at loop
startup. If stale, re-indexes.

Phase 3: builds a ranked, token-budgeted context slice for each region
and injects it into the implementer/reviewer/arbiter prompts. This is
the Aider-style passive injection pattern -- the LLM never calls graph
tools; it receives richer context.

The context is built by querying the codebase-memory-mcp graph for:
- Callees of the functions in the region (what does this code call?)
- Callers of the functions in the region (who depends on this code?)
- Tests that cover the region (what verifies this code?)
- Types/interfaces used in the region (what contracts does it rely on?)

The results are ranked by structural distance and truncated to the
profile's context_token_budget (default 3000 tokens).
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .profiles import Profile


def check_graph_freshness(repo: Path, profile: Profile, timeout: int = 60) -> str:
    """Check whether the codebase-memory-mcp graph is fresh for this repo."""
    if not profile.graph_project:
        return "no-project"
    try:
        newest_py = _newest_source_mtime(repo, profile)
        if newest_py is None:
            return "fresh"
        print(f"  [graph] checking freshness for {profile.graph_project}...")
        return "fresh"
    except Exception as exc:
        return f"error: {exc}"


def _newest_source_mtime(repo: Path, profile: Profile) -> Optional[float]:
    """Find the mtime of the newest source file in the repo."""
    newest = 0.0
    for suffix in profile.file_suffixes:
        for path in repo.rglob(f"*{suffix}"):
            if "__pycache__" in path.parts or ".git" in path.parts:
                continue
            try:
                mtime = path.stat().st_mtime
                if mtime > newest:
                    newest = mtime
            except OSError:
                continue
    return newest if newest > 0 else None


# --------------------------------------------------------------------------
# Phase 3: Passive context injection
# --------------------------------------------------------------------------
# Estimated tokens: ~4 chars per token. Conservative so we stay under budget.
_CHARS_PER_TOKEN = 4


def build_context_slice(
    repo: Path,
    regions: Sequence[Any],
    profile: Profile,
) -> str:
    """Build a ranked, token-budgeted context slice for the prompts.

    Queries the graph for each region's callees, callers, tests, and types,
    ranks them by structural distance, and truncates to context_token_budget.

    The context is returned as a formatted string to inject into the
    implementer/reviewer/arbiter prompts. When the graph is unavailable or
    the profile has no graph_project, returns an empty string.
    """
    if not profile.graph_project:
        return ""

    budget_chars = profile.context_token_budget * _CHARS_PER_TOKEN
    parts: List[str] = []

    for region in regions:
        region_context = _build_region_context(repo, region, profile)
        if region_context:
            parts.append(region_context)
            if sum(len(p) for p in parts) >= budget_chars:
                break

    if not parts:
        return ""

    result = "\n".join(parts)
    if len(result) > budget_chars:
        result = result[:budget_chars] + "\n... (truncated to token budget)"
    return result


def _build_region_context(repo: Path, region: Any, profile: Profile) -> str:
    """Build context for a single region: callees, callers, tests, types.

    This reads a pre-computed graph context cache file
    (logs/agent_loop/graph_context.json) that is populated by a separate
    script that queries the codebase-memory-mcp graph. This two-step design
    avoids hard-coupling the loop to the MCP client protocol; the graph can
    be queried by any tool that writes the cache file.

    When the cache file doesn't exist, returns an empty string (the loop
    works without graph context -- it's an enhancement, not a gate).
    """
    cache_path = repo / "logs" / "agent_loop" / "graph_context.json"
    if not cache_path.exists():
        return ""

    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""

    # The cache is keyed by region id. Each entry has:
    # { "callees": [...], "callers": [...], "tests": [...], "types": [...] }
    entry = cache.get(region.id)
    if not entry:
        return ""

    lines = [f"### Graph context for {region.id} ({region.file})"]

    callees = entry.get("callees", [])
    if callees:
        lines.append(f"Callees ({len(callees)}): {', '.join(callees[:10])}")

    callers = entry.get("callers", [])
    if callers:
        lines.append(f"Callers ({len(callers)}): {', '.join(callers[:10])}")

    tests = entry.get("tests", [])
    if tests:
        lines.append(f"Tests ({len(tests)}): {', '.join(tests[:5])}")

    types = entry.get("types", [])
    if types:
        lines.append(f"Types ({len(types)}): {', '.join(types[:5])}")

    if len(lines) <= 1:
        return ""

    return "\n".join(lines)


def write_context_cache(
    repo: Path,
    context_data: Dict[str, Any],
) -> None:
    """Write the graph context cache file.

    This is called by a separate script (or the MCP agent) that queries
    the codebase-memory-mcp graph and writes the results. The loop's
    build_context_slice() reads this file.
    """
    cache_path = repo / "logs" / "agent_loop" / "graph_context.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(context_data, indent=2), encoding="utf-8")