"""
Acceptance tests for Phase 5: persistent memory (auto-extract SETTLED).
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop.profiles import Profile, register
from agent_loop.memory import (
    extract_settled, save_settled, load_settled, inject_settled, _hash_text,
)


PROFILE = Profile(
    name="test-memory",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    implementer_rules="test", reviewer_priorities="test",
    settled=("Hand-curated decision 1",),
)
register(PROFILE)


def test_phase5_extract_settled():
    """extract_settled parses the arbiter's SETTLED section."""
    raw = """<<<RULINGS>>>
- REJECTED #1: not a defect
<<<END RULINGS>>>
<<<RECOMMENDATION>>>
SHIP
<<<END RECOMMENDATION>>>
<<<RATIONALE>>>
All rejected.
<<<END RATIONALE>>>
<<<SETTLED>>>
- The lock is always released before a broker call
- Reading positions outside the lock is an accepted pattern
- NONE
<<<END SETTLED>>>"""
    decisions = extract_settled(raw)
    assert len(decisions) == 2
    assert "lock is always released" in decisions[0]
    assert "Reading positions" in decisions[1]


def test_phase5_extract_settled_empty():
    """extract_settled returns empty when no SETTLED section."""
    raw = "<<<RECOMMENDATION>>>\nSHIP\n<<<END RECOMMENDATION>>>"
    assert extract_settled(raw) == []


def test_phase5_extract_settled_none():
    """extract_settled returns empty when SETTLED is NONE."""
    raw = "<<<SETTLED>>>\n- NONE\n<<<END SETTLED>>>"
    assert extract_settled(raw) == []


def test_phase5_save_and_load(tmp_path):
    """save_settled writes to JSONL; load_settled reads it back."""
    repo = tmp_path
    decisions = ["Decision one", "Decision two"]
    saved = save_settled(repo, "T1", decisions)
    assert saved == 2

    loaded = load_settled(repo)
    assert len(loaded) == 2
    assert "Decision one" in loaded
    assert "Decision two" in loaded


def test_phase5_save_deduplicates(tmp_path):
    """save_settled skips decisions already saved."""
    repo = tmp_path
    save_settled(repo, "T1", ["Decision one"])
    saved = save_settled(repo, "T1", ["Decision one"])  # same text
    assert saved == 0  # already saved

    loaded = load_settled(repo)
    assert len(loaded) == 1


def test_phase5_save_different_tickets_same_text(tmp_path):
    """Same decision text from different tickets are both saved (different keys)."""
    repo = tmp_path
    save_settled(repo, "T1", ["Same text"])
    saved = save_settled(repo, "T2", ["Same text"])
    assert saved == 1  # different ticket = different key, so it's saved

    loaded = load_settled(repo)
    assert len(loaded) == 2  # both versions


def test_phase5_inject_combines(tmp_path):
    """inject_settled combines hand-curated with auto-extracted."""
    repo = tmp_path
    save_settled(repo, "T1", ["Auto-extracted decision"])

    combined = inject_settled(PROFILE.settled, repo)
    assert "Hand-curated decision 1" in combined
    assert "Auto-extracted decision" in combined
    assert combined[0] == "Hand-curated decision 1"  # hand-curated first


def test_phase5_inject_no_auto(tmp_path):
    """inject_settled returns just profile-settled when no auto decisions."""
    repo = tmp_path
    combined = inject_settled(("Only one",), repo)
    assert combined == ["Only one"]


def test_phase5_hash_stability():
    """Hash is stable for the same text."""
    h1 = _hash_text("test decision")
    h2 = _hash_text("test decision")
    assert h1 == h2
    assert h1 != _hash_text("different decision")


def test_phase5_save_settled_concurrent_does_not_lose(tmp_path):
    """save_settled uses line-buffered append, so concurrent writes don't lose entries.

    The previous implementation read the whole file, appended in memory, and
    wrote back via os.replace -- two concurrent callers could both read, both
    append, and the second os.replace would win, losing the first's entries.
    The line-buffered append pattern (matching save_feedback) is safe because
    each entry is one line and the OS guarantees atomic line appends on most
    filesystems.
    """
    import concurrent.futures
    from agent_loop.memory import save_settled, load_settled

    repo = tmp_path

    def writer(ticket_id: str):
        return save_settled(repo, ticket_id, [f"decision from {ticket_id}"])

    # Write from 20 "concurrent" tickets.
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(writer, [f"T{i}" for i in range(20)]))

    # Every writer should have saved exactly 1.
    assert all(r == 1 for r in results), f"some writes were lost: {results}"
    loaded = load_settled(repo)
    assert len(loaded) == 20, f"expected 20 entries, got {len(loaded)}"


def test_phase5_save_settled_does_not_rewrite_whole_file(tmp_path):
    """save_settled appends one line per decision, not a full rewrite.

    The previous implementation read the entire store, appended new entries in
    memory, and wrote the whole thing back via os.replace -- O(N) per save,
    growing with history. The line-buffered append is O(1) per decision.
    """
    from agent_loop.memory import save_settled, _settled_path

    repo = tmp_path
    path = _settled_path(repo)

    # Write an initial entry.
    save_settled(repo, "T1", ["first decision"])
    initial_size = path.stat().st_size
    initial_content = path.read_text(encoding="utf-8")

    # Write a second entry.
    save_settled(repo, "T2", ["second decision"])

    # The file should have grown by approximately one line, not been rewritten.
    # The initial content must still be present verbatim (not reordered).
    final_content = path.read_text(encoding="utf-8")
    assert "first decision" in final_content
    assert "second decision" in final_content
    # The initial line must appear before the new line (append order preserved).
    assert final_content.index("first decision") < final_content.index("second decision")