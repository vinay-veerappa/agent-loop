"""
docs_mode.py
============
Docs mode: generates documentation from intent + context.

Four sub-modes, selected by --docs-type:

1. changelog  (default): diff → changelog entry
   Input: git diff
   Output: markdown changelog for the diff

2. handover: session state → handover document
   Input: ledger.jsonl + current git status + diff
   Output: "what I did, what's left, what to watch for"

3. design: feature idea → design document
   Input: intent string (--defect "Add a trailing stop to the copier")
   Output: design doc with architecture, trade-offs, ADR references

4. prd: defect/feature → product requirements document
   Input: intent string (--defect "Fix the copier not copying exits")
   Output: PRD with requirements, acceptance criteria, out-of-scope

All sub-modes use the graph context (callers, callees, types) when
available, and the profile's language/build/test context.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .profiles import Profile
from .providers import Completion, ProviderError, chat


# ---------------------------------------------------------------------------
# System prompts per sub-mode
# ---------------------------------------------------------------------------

_CHANGELOG_SYSTEM = """You are a technical writer creating a changelog entry
from a code diff. Write concise, user-facing changelog notes.

OUTPUT FORMAT - obey exactly:
<<<DOCS>>>
## Changes

- bullet list of changes, grouped by category (Added, Fixed, Changed, Removed)
<<<END DOCS>>>
<<<NOTES>>>
- What sections were updated
<<<END NOTES>>>
"""

_HANDOVER_SYSTEM = """You are a senior engineer writing a session handover.
The next engineer to touch this codebase needs to know: what was done, what
remains, and where the traps are.

Read the session ledger (what tickets ran, what verdicts, what cost) and the
current git state. Write a handover that a cold-starting engineer can read in
2 minutes and know exactly what to do next.

OUTPUT FORMAT - obey exactly:
<<<DOCS>>>
# Session Handover

## What was done
- bullet list of completed work, with ticket IDs and verdicts

## What remains
- bullet list of unfinished work, with the next concrete step

## Known issues / traps
- bullet list of things that will bite the next engineer if they don't know

## Next steps
- numbered list of concrete next actions, in priority order
<<<END DOCS>>>
<<<NOTES>>>
- What session this handover covers
<<<END NOTES>>>
"""

_DESIGN_SYSTEM = """You are a senior software engineer writing a design
document for a proposed feature. You have the codebase's graph context
(callers, callees, types) and the architectural decision records.

Write a design document that covers: problem statement, proposed approach,
alternatives considered, trade-offs, impact on existing code, and open
questions.

OUTPUT FORMAT - obey exactly:
<<<DOCS>>>
# Design: <feature name>

## Problem
<what problem this solves>

## Proposed approach
<how it works, with key design decisions>

## Alternatives considered
<what else was on the table and why it was rejected>

## Impact on existing code
<what files/functions change, what callers are affected>

## Open questions
<what needs to be decided before implementation>
<<<END DOCS>>>
<<<NOTES>>>
- Key design decisions and their rationale
<<<END NOTES>>>
"""

_PRD_SYSTEM = """You are a product engineer writing a product requirements
document for a defect fix or feature. You have the codebase's graph context
and the profile's build/test configuration.

Write a PRD that covers: background, requirements, acceptance criteria,
out-of-scope, and risks.

OUTPUT FORMAT - obey exactly:
<<<DOCS>>>
# PRD: <feature/defect name>

## Background
<why this matters, what's broken or missing>

## Requirements
- numbered list of what must be true when this is done

## Acceptance criteria
- numbered list of testable conditions (each must be verifiable)

## Out of scope
- bullet list of what this deliberately does NOT address

## Risks
- bullet list of what could go wrong
<<<END DOCS>>>
<<<NOTES>>>
- Key requirements and their priority
<<<END NOTES>>>
"""

_SYSTEMS = {
    "changelog": _CHANGELOG_SYSTEM,
    "handover": _HANDOVER_SYSTEM,
    "design": _DESIGN_SYSTEM,
    "prd": _PRD_SYSTEM,
}


# ---------------------------------------------------------------------------
# Graph context builder
# ---------------------------------------------------------------------------

def _build_graph_context(repo: Path, profile: Profile, intent: str = "") -> str:
    """Build graph context for the prompt: callers, callees, types.

    When the graph is available (via codebase-memory-mcp), this queries
    the functions mentioned in the intent or the diff. When the graph is
    not available, returns an empty string (the mode still works, just
    without graph context).
    """
    if not profile.graph_project:
        return ""

    # Try to use the MCP client for live graph context
    try:
        from .mcp_client import get_mcp_client
        client = get_mcp_client()
        if not client:
            return ""

        # Extract function names from the intent
        import re as _re
        names = _re.findall(r"\b([a-z_][a-z0-9_]*)\b", intent.lower())
        # Filter to likely function names (not stopwords)
        stop = {"the", "a", "an", "to", "in", "for", "of", "and", "or", "is",
                "it", "that", "this", "with", "from", "by", "on", "at", "be",
                "fix", "add", "remove", "update", "change", "make", "get", "set"}
        names = [n for n in names if n not in stop and len(n) > 2][:5]

        if not names:
            return ""

        parts = ["## Graph context (from code knowledge graph)"]
        for name in names[:3]:
            result = client.call_tool("trace_call_path", {
                "function_name": name,
                "direction": "both",
                "project": profile.graph_project,
                "depth": 1,
            })
            if result and not result.startswith("ERROR"):
                # Truncate each trace result
                parts.append(f"### {name}\n{result[:500]}")

        return "\n".join(parts) if len(parts) > 1 else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Sub-mode runners
# ---------------------------------------------------------------------------

def _run_changelog(
    repo: Path,
    diff_ref: str,
    profile: Profile,
    implementer: str,
    output_path: str,
) -> Dict[str, Any]:
    """diff → changelog entry."""
    diff_proc = subprocess.run(
        ["git", "diff", diff_ref], cwd=str(repo),
        capture_output=True, text=True, timeout=30,
    )
    diff = diff_proc.stdout
    if not diff.strip():
        return {"error": f"no diff found against {diff_ref}"}

    diff_truncated = diff[:60000]
    if len(diff) > 60000:
        diff_truncated += f"\n... (truncated, {len(diff)} total chars)"

    prompt = f"# Code diff\n\n```diff\n{diff_truncated}\n```\n\n"
    prompt += f"Language: {profile.language}\n"
    prompt += f"Write a changelog entry for these changes.\n"

    return _generate_and_write(
        repo, _CHANGELOG_SYSTEM, prompt, implementer, output_path, "CHANGELOG"
    )


def _run_handover(
    repo: Path,
    profile: Profile,
    implementer: str,
    output_path: str,
) -> Dict[str, Any]:
    """Session state → handover document."""
    # Load ledger
    ledger_path = repo / "logs" / "agent_loop" / "ledger.jsonl"
    ledger_text = ""
    if ledger_path.exists():
        entries = []
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        ledger_text = json.dumps(entries[-20:], indent=2)  # last 20 tickets

    # Get git status
    status_proc = subprocess.run(
        ["git", "status", "--short"], cwd=str(repo),
        capture_output=True, text=True, timeout=10,
    )
    git_status = status_proc.stdout or "(clean)"

    # Get recent diff (if any)
    diff_proc = subprocess.run(
        ["git", "diff", "--stat"], cwd=str(repo),
        capture_output=True, text=True, timeout=10,
    )
    diff_stat = diff_proc.stdout or "(no uncommitted changes)"

    prompt = f"# Session state\n\n"
    prompt += f"## Recent tickets (last 20 from ledger)\n```json\n{ledger_text}\n```\n\n"
    prompt += f"## Git status\n```\n{git_status}\n```\n\n"
    prompt += f"## Uncommitted changes\n```\n{diff_stat}\n```\n\n"
    prompt += f"Write a handover document for the next engineer.\n"

    return _generate_and_write(
        repo, _HANDOVER_SYSTEM, prompt, implementer, output_path, "HANDOVER"
    )


def _run_design(
    repo: Path,
    intent: str,
    profile: Profile,
    implementer: str,
    output_path: str,
) -> Dict[str, Any]:
    """Feature idea → design document."""
    graph_ctx = _build_graph_context(repo, profile, intent)

    prompt = f"# Feature to design\n\n{intent}\n\n"
    prompt += f"## Context\n"
    prompt += f"Language: {profile.language}\n"
    prompt += f"Build: {profile.build_cmd or '(none)'}\n"
    prompt += f"Test: {profile.test_cmd or '(none)'}\n"
    if graph_ctx:
        prompt += f"\n{graph_ctx}\n"
    prompt += f"\nWrite a design document for this feature.\n"

    return _generate_and_write(
        repo, _DESIGN_SYSTEM, prompt, implementer, output_path, "DESIGN"
    )


def _run_prd(
    repo: Path,
    intent: str,
    profile: Profile,
    implementer: str,
    output_path: str,
) -> Dict[str, Any]:
    """Defect/feature → product requirements document."""
    graph_ctx = _build_graph_context(repo, profile, intent)

    prompt = f"# Defect/feature to document\n\n{intent}\n\n"
    prompt += f"## Context\n"
    prompt += f"Language: {profile.language}\n"
    prompt += f"Build: {profile.build_cmd or '(none)'}\n"
    prompt += f"Test: {profile.test_cmd or '(none)'}\n"
    if graph_ctx:
        prompt += f"\n{graph_ctx}\n"
    prompt += f"\nWrite a PRD for this defect/feature.\n"

    return _generate_and_write(
        repo, _PRD_SYSTEM, prompt, implementer, output_path, "PRD"
    )


# ---------------------------------------------------------------------------
# Shared generation + write
# ---------------------------------------------------------------------------

def _generate_and_write(
    repo: Path,
    system_prompt: str,
    user_prompt: str,
    implementer: str,
    output_path: str,
    mode_name: str,
) -> Dict[str, Any]:
    """Send prompt to model, parse <<<DOCS>>>, write to file."""
    tid = mode_name
    art = repo / "logs" / "agent_loop" / tid
    art.mkdir(parents=True, exist_ok=True)

    result: Dict[str, Any] = {"ticket": tid, "output_path": output_path, "docs": None}

    history = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        out = chat(implementer, history, max_tokens=8000)
    except ProviderError as exc:
        result["error"] = str(exc)
        return result

    raw = out.text
    (art / "docs_raw.txt").write_text(raw, encoding="utf-8")
    print(f"  {mode_name.lower()} generation: {out.usage_line()}")

    # Parse docs
    m = re.search(r"<<<DOCS>>>\s*(.*?)<<<END\s*DOCS>>>", raw, re.DOTALL)
    if m:
        result["docs"] = m.group(1).strip()

    if result["docs"]:
        docs_path = repo / output_path
        docs_path.parent.mkdir(parents=True, exist_ok=True)
        docs_path.write_text(result["docs"], encoding="utf-8")
        print(f"  wrote {len(result['docs'])} chars to {output_path}")

    (art / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_docs(
    repo: Path,
    profile: Profile,
    implementer: str,
    docs_type: str = "changelog",
    diff_ref: str = "HEAD~1",
    intent: str = "",
    output_path: str = "docs/UPDATES.md",
) -> Dict[str, Any]:
    """Run docs mode.

    Args:
        repo: the repo root
        profile: the language profile
        implementer: the model to use
        docs_type: one of "changelog", "handover", "design", "prd"
        diff_ref: git ref for changelog mode (e.g. "HEAD~1")
        intent: feature/defect description for design/prd modes
        output_path: where to write the documentation
    """
    if docs_type == "changelog":
        return _run_changelog(repo, diff_ref, profile, implementer, output_path)
    elif docs_type == "handover":
        return _run_handover(repo, profile, implementer, output_path)
    elif docs_type == "design":
        if not intent:
            return {"error": "design mode needs --defect (the feature description)"}
        return _run_design(repo, intent, profile, implementer, output_path)
    elif docs_type == "prd":
        if not intent:
            return {"error": "prd mode needs --defect (the defect/feature description)"}
        return _run_prd(repo, intent, profile, implementer, output_path)
    else:
        return {"error": f"unknown docs type: {docs_type!r} (use changelog, handover, design, or prd)"}