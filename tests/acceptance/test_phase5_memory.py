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