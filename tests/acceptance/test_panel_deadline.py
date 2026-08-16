"""
Acceptance test for T-REL6: panel deadline is a hard wall-clock bound.

The ThreadPoolExecutor `with` block's __exit__ calls pool.shutdown(wait=True),
which blocks until ALL threads finish -- including a hung reviewer. The panel
deadline bounded the wait for results, but not the cleanup. This test verifies
that review_panel returns within the deadline even when a reviewer hangs.
"""
import time
from unittest.mock import patch, MagicMock

from agent_loop.loop import review_panel, UNREACHABLE


def test_panel_deadline_is_hard_bound():
    """review_panel returns within the deadline even when a reviewer hangs.

    Without the fix, the `with` block's __exit__ would block on shutdown(wait=True)
    until the hung thread's HTTP timeout fires, stalling past the deadline.
    """
    def slow_chat(model, messages, **kwargs):
        # Simulate a hung reviewer that never returns within the deadline.
        time.sleep(10)
        return MagicMock(text="<<<VERDICT>>>\nAPPROVE\n<<<END VERDICT>>>",
                        secs=10.0, input_tokens=100, output_tokens=10,
                        usage_line=lambda: "100+10",
                        cost_usd=0.0)

    with patch("agent_loop.loop.chat", side_effect=slow_chat):
        # deadline_secs=1 means the panel should return in ~1 second, not 10.
        t0 = time.time()
        result = review_panel(
            reviewers=["slow-model"],
            prompt="test prompt",
            system="test system",
            art=MagicMock(),
            rnd=1,
            deadline_secs=1,
        )
        elapsed = time.time() - t0
        # The panel must return within roughly 2x the deadline (allowing for
        # thread scheduling overhead). Without the fix, this takes 10+ seconds.
        assert elapsed < 4, (
            f"review_panel took {elapsed:.1f}s with a 1s deadline -- "
            f"the `with` block is blocking on shutdown(wait=True)"
        )
        # The slow reviewer should be UNREACHABLE.
        assert any(v.status == UNREACHABLE for v in result.votes), (
            f"expected at least one UNREACHABLE vote, got {[v.status for v in result.votes]}"
        )
        assert not result.valid, "panel with an unreachable reviewer is not valid"