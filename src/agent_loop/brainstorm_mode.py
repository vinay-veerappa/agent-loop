"""
brainstorm_mode.py
==================
Brainstorm mode: input is a defect description, output is candidate
approaches + trade-offs. No code changes. Exploratory — the LLM proposes
multiple approaches, the user picks one for plan mode.

Deferred mode, now implemented.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .profiles import Profile
from .providers import Completion, ProviderError, chat


BRAINSTORM_SYSTEM = """You are a senior software engineer brainstorming approaches
to fix a defect. Propose 2-4 distinct approaches, each with:
- A one-paragraph description of the approach
- Pros (what makes this approach good)
- Cons (what makes this approach risky or limited)
- An effort estimate (small/medium/large)

OUTPUT FORMAT - obey exactly:
<<<APPROACHES>>>
## Approach 1: <name>
<description>

**Pros**: ...
**Cons**: ...
**Effort**: small/medium/large

## Approach 2: <name>
...

## Approach 3: <name>
...
<<<END APPROACHES>>>
<<<RECOMMENDATION>>>
Which approach you recommend and why (1-2 sentences).
<<<END RECOMMENDATION>>>
"""


def run_brainstorm(
    repo: Path,
    defect_description: str,
    profile: Profile,
    implementer: str,
) -> Dict[str, Any]:
    """Run brainstorm mode: defect -> candidate approaches + trade-offs.

    Args:
        repo: the repo root
        defect_description: the defect to brainstorm about
        profile: the language profile
        implementer: the model to use for brainstorming

    Returns:
        a result dict with the approaches text and recommendation
    """
    tid = "BRAINSTORM"
    art = repo / "logs" / "agent_loop" / tid
    art.mkdir(parents=True, exist_ok=True)

    result: Dict[str, Any] = {"ticket": tid, "approaches": None, "recommendation": ""}

    prompt = f"# Defect to brainstorm\n\n{defect_description}\n\n"
    prompt += f"## Context\n"
    prompt += f"Language: {profile.language}\n"
    prompt += f"File suffixes: {', '.join(profile.file_suffixes)}\n"
    prompt += f"Build: {profile.build_cmd or '(none)'}\n"
    prompt += f"Test: {profile.test_cmd or '(none)'}\n\n"
    prompt += "Propose 2-4 distinct approaches to fix this defect.\n"

    history = [
        {"role": "system", "content": BRAINSTORM_SYSTEM},
        {"role": "user", "content": prompt},
    ]

    try:
        out = chat(implementer, history, max_tokens=8000)
    except ProviderError as exc:
        result["error"] = str(exc)
        return result

    raw = out.text
    (art / "brainstorm_raw.txt").write_text(raw, encoding="utf-8")
    print(f"  brainstorm: {out.usage_line()}")

    # Parse approaches
    m = re.search(r"<<<APPROACHES>>>\s*(.*?)<<<END\s*APPROACHES>>>", raw, re.DOTALL)
    if m:
        result["approaches"] = m.group(1).strip()

    m = re.search(r"<<<RECOMMENDATION>>>\s*(.*?)<<<END\s*RECOMMENDATION>>>", raw, re.DOTALL)
    if m:
        result["recommendation"] = m.group(1).strip()

    (art / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    if result["approaches"]:
        (art / "approaches.md").write_text(result["approaches"], encoding="utf-8")
        print(f"  BRAINSTORM -> {art / 'approaches.md'}")
    return result