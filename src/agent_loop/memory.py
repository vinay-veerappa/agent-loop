"""
memory.py
==========
Persistent memory for settled decisions (Phase 5).

The arbiter already nominates settled decisions in its <<<SETTLED>>>
output. Nothing persists them. A human reads the arbiter response and
copies decisions into profiles.py by hand.

This module auto-extracts the SETTLED section from every arbiter response
and writes it to a settled-decisions store (a JSONL file at
logs/agent_loop/settled_decisions.jsonl, keyed by ticket + hash of
decision text). At the start of each ticket, load all settled decisions
and inject them into profile.settled alongside the hand-curated ones.

Concurrency: uses atomic file writes to avoid corruption when multiple
agent loops run concurrently.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


def _settled_path(repo: Path) -> Path:
    """Path to the settled-decisions store."""
    return repo / "logs" / "agent_loop" / "settled_decisions.jsonl"


def _hash_text(text: str) -> str:
    """Create a short hash of decision text for deduplication."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def extract_settled(arbiter_raw: str) -> List[str]:
    """Extract settled decisions from the arbiter's raw response.

    The arbiter emits a <<<SETTLED>>> section with one decision per line:
      <<<SETTLED>>>
      - decision text here
      - another decision
      <<<END SETTLED>>>

    Returns a list of decision strings (without the leading "- ").
    """
    import re
    m = re.search(
        r"<<<SETTLED>>>\r?\n(.*?)<<<END\s*SETTLED>>>",
        arbiter_raw,
        re.DOTALL,
    )
    if not m:
        return []
    lines = m.group(1).strip().splitlines()
    decisions = []
    for line in lines:
        stripped = line.strip().lstrip("- ").strip()
        if stripped and stripped.upper() not in ("NONE", "- NONE", ""):
            decisions.append(stripped)
    return decisions


def save_settled(
    repo: Path,
    ticket_id: str,
    decisions: List[str],
) -> int:
    """Append settled decisions to the JSONL store.

    Uses os.replace() for atomic file writes: reads all existing entries,
    appends new ones, writes to a temp file, then atomically replaces the
    target. This avoids corruption from concurrent appends.

    Returns the number of decisions saved (excluding duplicates).
    """
    if not decisions:
        return 0

    path = _settled_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing entries to check for duplicates
    existing_lines: List[str] = []
    existing_keys = set()
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8").splitlines()
        for line in existing_lines:
            try:
                entry = json.loads(line)
                existing_keys.add(entry.get("key", ""))
            except json.JSONDecodeError:
                continue

    # Build new entries
    new_lines = []
    for decision in decisions:
        key = f"{ticket_id}:{_hash_text(decision)}"
        if key in existing_keys:
            continue
        entry = {
            "ticket": ticket_id,
            "key": key,
            "decision": decision,
        }
        new_lines.append(json.dumps(entry, ensure_ascii=False))
        existing_keys.add(key)

    if not new_lines:
        return 0

    # Atomic write: write all lines (existing + new) to temp file, then replace
    all_lines = existing_lines + new_lines
    temp_fd, temp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix="settled_"
    )
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            f.write("\n".join(all_lines) + "\n")
        os.replace(temp_path, str(path))
    finally:
        Path(temp_path).unlink(missing_ok=True)

    return len(new_lines)


def load_settled(repo: Path) -> List[str]:
    """Load all settled decisions from the JSONL store.

    Returns a list of decision strings, most recent first.
    """
    path = _settled_path(repo)
    if not path.exists():
        return []

    decisions = []
    seen_keys = set()
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        try:
            entry = json.loads(line)
            key = entry.get("key", "")
            if key in seen_keys:
                continue
            seen_keys.add(key)
            decisions.append(entry.get("decision", ""))
        except json.JSONDecodeError:
            continue

    # Most recent first (entries are appended chronologically)
    decisions.reverse()
    return decisions


def inject_settled(profile_settled: Sequence[str], repo: Path) -> List[str]:
    """Combine hand-curated settled decisions with auto-extracted ones.

    Hand-curated decisions (from the profile) take precedence; auto-extracted
    ones are advisory and appended after.
    """
    auto = load_settled(repo)
    if not auto:
        return list(profile_settled)

    # Combine: profile-settled first (they are reviewed), then auto-extracted
    combined = list(profile_settled)
    for decision in auto:
        if decision not in combined:
            combined.append(decision)
    return combined