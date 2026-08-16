"""
Acceptance test for T-REL5: arbiter diff truncation inserts a visible marker.

The arbiter's diff was silently truncated at 60K chars with no marker. A
multi-region ticket against a large method can exceed this, and the arbiter
would see a partial diff with no indication it was incomplete -- it could rule
on findings about code it could not see. See AGENT_LOOP_THIRD_REVIEW.md N5.
"""
from agent_loop.arbiter import _truncate_diff


def test_truncate_diff_no_truncation_when_under_limit():
    """A diff under the limit is returned unchanged (modulo strip)."""
    diff = "diff --git a/file b/file\n+added line\n-removed line"
    result = _truncate_diff(diff, max_chars=1000)
    assert result == diff
    assert "TRUNCATED" not in result


def test_truncate_diff_inserts_marker_when_over_limit():
    """A diff over the limit gets a visible truncation marker."""
    diff = "x" * 70000
    result = _truncate_diff(diff, max_chars=60000)
    assert "TRUNCATED" in result
    assert "10000 chars omitted" in result
    assert len(result) < 70000  # shorter than the original
    assert result.startswith("x" * 100)  # starts with the beginning


def test_truncate_diff_empty_diff_returns_placeholder():
    """An empty diff returns the placeholder string."""
    assert _truncate_diff("") == "(no diff available)"
    assert _truncate_diff("   \n  ") == "(no diff available)"


def test_truncate_diff_at_exact_limit():
    """A diff exactly at the limit is not truncated."""
    diff = "x" * 60000
    result = _truncate_diff(diff, max_chars=60000)
    assert result == diff
    assert "TRUNCATED" not in result