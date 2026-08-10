"""
docs_mode.py
============
Docs mode: input is a diff + graph context, output is documentation
updates. Generates or updates docs from the diff and the code knowledge
graph.

Deferred mode, now implemented.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .profiles import Profile
from .providers import Completion, ProviderError, chat


DOCS_SYSTEM = """You are a technical writer updating documentation based on
a code diff. Generate or update documentation that accurately reflects
the changes in the diff.

OUTPUT FORMAT - obey exactly:
<<<DOCS>>>
The documentation content (markdown format).
<<<END DOCS>>>
<<<NOTES>>>
- What was updated and why
<<<END NOTES>>>
"""


def run_docs(
    repo: Path,
    diff_ref: str,
    profile: Profile,
    implementer: str,
    output_path: str = "docs/UPDATES.md",
) -> Dict[str, Any]:
    """Run docs mode: diff -> documentation updates.

    Args:
        repo: the repo root
        diff_ref: git ref to diff against (e.g. "HEAD~1")
        profile: the language profile
        implementer: the model to use
        output_path: where to write the documentation

    Returns:
        a result dict with the documentation content
    """
    tid = "DOCS"
    art = repo / "logs" / "agent_loop" / tid
    art.mkdir(parents=True, exist_ok=True)

    result: Dict[str, Any] = {"ticket": tid, "output_path": output_path, "docs": None}

    # Get the diff
    diff_proc = subprocess.run(
        ["git", "diff", diff_ref], cwd=str(repo),
        capture_output=True, text=True, timeout=30,
    )
    diff = diff_proc.stdout
    if not diff.strip():
        result["error"] = f"no diff found against {diff_ref}"
        return result

    # Truncate diff to 60K chars for the prompt
    diff_truncated = diff[:60000]
    if len(diff) > 60000:
        diff_truncated += f"\n... (truncated, {len(diff)} total chars)"

    prompt = f"# Code diff to document\n\n```diff\n{diff_truncated}\n```\n\n"
    prompt += f"## Context\n"
    prompt += f"Language: {profile.language}\n"
    prompt += f"File suffixes: {', '.join(profile.file_suffixes)}\n\n"
    prompt += f"Generate documentation updates that reflect these changes. "
    prompt += f"Write to {output_path}.\n"

    history = [
        {"role": "system", "content": DOCS_SYSTEM},
        {"role": "user", "content": prompt},
    ]

    try:
        out = chat(implementer, history, max_tokens=8000)
    except ProviderError as exc:
        result["error"] = str(exc)
        return result

    raw = out.text
    (art / "docs_raw.txt").write_text(raw, encoding="utf-8")
    print(f"  docs generation: {out.usage_line()}")

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