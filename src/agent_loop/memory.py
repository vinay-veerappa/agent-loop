"""
memory.py
=========
Persistent memory for settled decisions (Phase 5) + learning feedback (Phase 9).

SETTLED DECISIONS:
The arbiter nominates settled decisions in its <<<SETTLED>>> output. This
module auto-extracts them, deduplicates by ticket+hash, and persists to a
JSONL store. At the start of each ticket, the most recent N decisions are
loaded and injected into the review prompt alongside hand-curated ones.

Context bloat control: only the most recent MAX_SETTLED_INJECTED decisions
are injected (default 20). Older decisions stay on disk for auditability
but don't bloat the prompt. This caps settled-decision injection at ~1K
tokens regardless of how many tickets have run.

LEARNING FEEDBACK:
After each round, the loop records what happened — which findings were
upheld, which rejected, how many rounds it took, which models were used.
This feedback store is queried at the start of each ticket to surface
"last time this kind of finding came up, the arbiter rejected it" —
teaching reviewers not to repeat known false positives.

Concurrency: uses atomic file writes (os.replace) to avoid corruption.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


# Cap on settled decisions injected into the prompt (context bloat control).
# Older decisions stay on disk for auditability but don't bloat the prompt.
MAX_SETTLED_INJECTED = 20

# Per-file lock for save_settled. On Windows, concurrent line-buffered appends
# to the same file from multiple threads/processes are NOT atomic (NTFS does
# not guarantee atomic appends), so a lock is needed. Named by the resolved
# store path so different repos don't contend.
_settled_locks: Dict[str, threading.Lock] = {}
_settled_locks_guard = threading.Lock()


def _get_settled_lock(path: Path) -> threading.Lock:
    """Get or create a per-file lock for settled-decisions writes."""
    key = str(path.resolve())
    with _settled_locks_guard:
        if key not in _settled_locks:
            _settled_locks[key] = threading.Lock()
        return _settled_locks[key]


# Cap on learning feedback entries injected into the prompt.
MAX_FEEDBACK_INJECTED = 10


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
        r"<<<SETTLED>{2,}\r?\n(.*?)<<<END\s*SETTLED>{2,}",
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

    Line-buffered append, matching save_feedback's pattern. The previous
    implementation read the entire file, appended new entries, and wrote the
    whole thing back via os.replace -- O(N) per save and not safe under
    concurrent tickets (two processes can both read, both append, and the
    second os.replace wins, losing the first's append). os.replace is atomic
    per-rename, not per-append.

    Deduplication is on READ (load_settled already deduplicates by key), so
    a duplicate append is harmless: it is filtered out at injection time.

    Returns the number of decisions saved (excluding exact-key duplicates
    already present in the file).
    """
    if not decisions:
        return 0

    path = _settled_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Per-file lock: on Windows, concurrent line-buffered appends to the same
    # file are NOT atomic, so without a lock 20 concurrent writers can lose 2
    # entries. The lock is per-store-path so different repos don't contend.
    lock = _get_settled_lock(path)

    # Load existing keys to skip exact duplicates. This is a read, not a
    # read-modify-write: the append below is line-buffered and independent of
    # this read. A concurrent writer's entries may not be visible here, but
    # they will be filtered by load_settled's dedup on read.
    existing_keys: set = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                existing_keys.add(entry.get("key", ""))
            except json.JSONDecodeError:
                continue

    # Append new entries one line at a time under the per-file lock.
    saved = 0
    with lock:
        with path.open("a", encoding="utf-8") as f:
            for decision in decisions:
                key = f"{ticket_id}:{_hash_text(decision)}"
                if key in existing_keys:
                    continue
                entry = {
                    "ticket": ticket_id,
                    "key": key,
                    "decision": decision,
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                existing_keys.add(key)
                saved += 1

    return saved


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

    Context bloat control: only the most recent MAX_SETTLED_INJECTED
    auto-extracted decisions are included. Older decisions stay on disk
    for auditability but are not injected into the prompt.
    """
    auto = load_settled(repo)
    if not auto:
        return list(profile_settled)

    # Cap auto-extracted decisions to prevent context bloat.
    # load_settled already returns most-recent-first.
    capped_auto = auto[:MAX_SETTLED_INJECTED]

    # Combine: profile-settled first (they are reviewed), then auto-extracted
    combined = list(profile_settled)
    for decision in capped_auto:
        if decision not in combined:
            combined.append(decision)
    return combined


# ---------------------------------------------------------------------------
# Learning feedback store (Phase 9)
# ---------------------------------------------------------------------------
def _feedback_path(repo: Path) -> Path:
    """Path to the learning feedback store."""
    return repo / "logs" / "agent_loop" / "learning_feedback.jsonl"


def save_feedback(
    repo: Path,
    ticket_id: str,
    round_num: int,
    reviewer_model: str,
    finding_text: str,
    finding_severity: str,
    arbiter_ruling: str,
) -> int:
    """Record a learning feedback entry: what the reviewer found and how
    the arbiter ruled. This teaches future reviewers which findings are
    real and which are false positives.

    Args:
        repo: the repo root
        ticket_id: the ticket being worked on
        round_num: which round (1-based)
        reviewer_model: which model raised the finding
        finding_text: the finding text
        finding_severity: BLOCKER / MAJOR / MINOR
        arbiter_ruling: UPHELD / REJECTED / OUT_OF_SCOPE

    Returns:
        1 if saved, 0 if skipped (duplicate)
    """
    path = _feedback_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Deduplicate: skip if we've already recorded this exact finding+ruling
    key = f"{ticket_id}:{round_num}:{_hash_text(finding_text)}:{arbiter_ruling}"

    # Load existing keys
    existing_keys = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                existing_keys.add(entry.get("key", ""))
            except json.JSONDecodeError:
                continue

    if key in existing_keys:
        return 0

    entry = {
        "ticket": ticket_id,
        "round": round_num,
        "reviewer": reviewer_model,
        "finding": finding_text,
        "severity": finding_severity,
        "ruling": arbiter_ruling,
        "key": key,
    }

    # Append a single line. Copying the whole store to a temp file per finding
    # made recording a round quadratic in the store's size for no benefit: a
    # line-buffered append of one line is what the JSONL format is for.
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return 1


def load_rejected_findings(repo: Path) -> List[Dict[str, str]]:
    """Load findings the arbiter has REJECTED in prior tickets.

    These are known false positives — reviewers should not re-raise them.
    Returns the most recent MAX_FEEDBACK_INJECTED entries, most recent first.
    """
    path = _feedback_path(repo)
    if not path.exists():
        return []

    rejected = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            if entry.get("ruling") == "REJECTED":
                rejected.append(entry)
        except json.JSONDecodeError:
            continue

    # Most recent first
    rejected.reverse()
    return rejected[:MAX_FEEDBACK_INJECTED]


def load_upheld_findings(repo: Path) -> List[Dict[str, str]]:
    """Load findings the arbiter has UPHELD in prior tickets.

    These are known real defects — reviewers should continue to flag them.
    Returns the most recent MAX_FEEDBACK_INJECTED entries, most recent first.
    """
    path = _feedback_path(repo)
    if not path.exists():
        return []

    upheld = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            if entry.get("ruling") == "UPHELD":
                upheld.append(entry)
        except json.JSONDecodeError:
            continue

    # Most recent first
    upheld.reverse()
    return upheld[:MAX_FEEDBACK_INJECTED]


def build_learning_context(repo: Path) -> str:
    """Build a compact learning context string for the reviewer prompt.

    This injects:
    - Recently REJECTED findings ("don't re-raise these known false positives")
    - Recently UPHELD findings ("these are real, keep flagging them")

    Returns an empty string when no feedback exists (first run).
    """
    rejected = load_rejected_findings(repo)
    upheld = load_upheld_findings(repo)

    if not rejected and not upheld:
        return ""

    def distinct(entries: List[Dict[str, str]], limit: int = 5) -> List[str]:
        """First `limit` distinct finding texts. The store keys by round, so
        the same finding raised in three rounds is three entries; injecting it
        three times would spend the whole cap on one lesson."""
        seen, out = set(), []
        for entry in entries:
            text = (entry.get("finding", "") or "").strip()[:120]
            if not text or text.lower() in seen:
                continue
            seen.add(text.lower())
            out.append(text)
            if len(out) >= limit:
                break
        return out

    parts = ["## LEARNING FEEDBACK (from prior tickets)"]

    rejected_texts = distinct(rejected)
    if rejected_texts:
        parts.append("\n### Known false positives (arbiter REJECTED these - do NOT re-raise):")
        parts += [f"- REJECTED: {t}" for t in rejected_texts]

    upheld_texts = distinct(upheld)
    if upheld_texts:
        parts.append("\n### Known real defects (arbiter UPHELD these - keep flagging if you see them):")
        parts += [f"- UPHELD: {t}" for t in upheld_texts]

    if len(parts) == 1:
        return ""
    return "\n".join(parts)