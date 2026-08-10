"""
Acceptance tests for Phase 4: compaction.

Phase 4a: verbose old outputs above a threshold are pruned to truncation
markers that preserve per-finding structure.

Phase 4b: when the total history exceeds the round_input_token_budget,
all prior rounds are mechanically summarized into a compact block.
"""
import pytest
from agent_loop.profiles import Profile, register
from agent_loop.compaction import (
    compact_history, _compact_findings, _mechanical_summary,
    estimate_tokens, history_token_count,
)


PROFILE = Profile(
    name="test-compaction",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    round_input_token_budget=10000,  # 10K tokens = ~40K chars
    implementer_rules="test", reviewer_priorities="test",
)
register(PROFILE)

SMALL_PROFILE = Profile(
    name="test-compaction-small",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    round_input_token_budget=100,  # 100 tokens = ~400 chars
    implementer_rules="test", reviewer_priorities="test",
)
register(SMALL_PROFILE)


def test_phase4_no_compaction_on_round_1():
    """Round 1: no compaction needed."""
    history = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "implement this"},
    ]
    result = compact_history(history, 1, PROFILE)
    assert result == history, "round 1 should not compact"


def test_phase4_prunes_verbose_assistant():
    """Phase 4a: verbose assistant messages from prior rounds are pruned."""
    long_output = "x" * 10000  # 10K chars, above the 5K threshold
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "impl"},
        {"role": "assistant", "content": long_output},
        {"role": "user", "content": "feedback"},
        {"role": "assistant", "content": "short"},
        {"role": "user", "content": "latest impl"},
    ]
    result = compact_history(history, 3, PROFILE)
    # The long assistant message should be pruned
    pruned_assistant = [m for m in result if m["role"] == "assistant" and "COMPACTED" in m["content"]]
    assert len(pruned_assistant) == 1, "verbose assistant message should be pruned"
    assert len(pruned_assistant[0]["content"]) < len(long_output), "pruned should be shorter"


def test_phase4_preserves_latest_round():
    """Phase 4a: the latest round's full exchange is preserved."""
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "impl round 1"},
        {"role": "assistant", "content": "x" * 10000},
        {"role": "user", "content": "latest impl round 2"},
    ]
    result = compact_history(history, 2, PROFILE)
    last_user = [m for m in result if m["content"] == "latest impl round 2"]
    assert len(last_user) == 1, "latest user message must be preserved exactly"


def test_phase4_compact_findings_preserves_structure():
    """Phase 4a: findings are compacted to per-finding summaries."""
    findings = (
        "A review panel returned REVISE.\n\n"
        "FINDINGS:\n"
        "- [BLOCKER] R1: the lock is held during a broker call\n"
        "- [MAJOR] R2: race condition in OnExecution\n"
        "- [MINOR] R3: missing null check\n\n"
        "Fix exactly these and re-emit ALL blocks in full."
    )
    result = _compact_findings(findings)
    assert "[BLOCKER]" in result, "compacted findings must preserve severity"
    assert "lock is held" in result, "compacted findings must preserve finding summary"
    assert "Fix exactly these" in result, "compacted findings must preserve instruction"
    assert len(result) < len(findings), "compacted should be shorter"


def test_phase4b_mechanical_summary_when_over_budget():
    """Phase 4b: when history exceeds budget, prior rounds are summarized."""
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "impl1"},
        {"role": "assistant", "content": "x" * 5000},
        {"role": "user", "content": "- [BLOCKER] finding1\n- [MAJOR] finding2"},
        {"role": "assistant", "content": "y" * 5000},
        {"role": "user", "content": "- [BLOCKER] finding3"},
        {"role": "assistant", "content": "z" * 5000},
        {"role": "user", "content": "latest impl"},
    ]
    result = compact_history(history, 4, SMALL_PROFILE)
    # The result should be much smaller than the input
    assert history_token_count(result) < history_token_count(history)
    # Should contain a summary marker
    summary_msgs = [m for m in result if "PRIOR ROUNDS SUMMARY" in m.get("content", "")]
    assert len(summary_msgs) == 1, "should contain a prior rounds summary"
    # The last user message should be preserved
    assert "latest impl" in result[-1]["content"]


def test_phase4_estimate_tokens():
    """estimate_tokens returns a reasonable estimate."""
    assert estimate_tokens("hello world") == 2  # 11 chars / 4 = 2.75 -> 2
    assert estimate_tokens("") == 0


def test_phase4_history_token_count():
    """history_token_count sums tokens across all messages."""
    history = [
        {"role": "system", "content": "ab"},   # 0 tokens
        {"role": "user", "content": "abcd"},   # 1 token
        {"role": "assistant", "content": "abcdefgh"},  # 2 tokens
    ]
    assert history_token_count(history) == 3