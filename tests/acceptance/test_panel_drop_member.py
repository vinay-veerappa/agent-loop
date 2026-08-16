"""
Acceptance test for T-BLG4 (O67/R2): drop a malfunctioning reviewer instead
of stopping the run.
"""
import time
from unittest.mock import patch, MagicMock

from agent_loop.loop import review_panel, UNREACHABLE, APPROVE, REVISE
from agent_loop.providers import ProviderError


def test_panel_drops_malfunctioning_reviewer_and_proceeds():
    """When one reviewer is unreachable but a quorum answered, the panel
    proceeds with the surviving reviewers' verdict."""
    def mock_chat(model, messages, **kwargs):
        if model == "broken-model":
            raise ProviderError("connection reset")
        return MagicMock(
            text="<<<VERDICT>>>\nAPPROVE\n<<<END VERDICT>>>\n"
                 "<<<FINDINGS>>>\n- NONE\n<<<END FINDINGS>>>\n"
                 "<<<REQUIRED>>>\n- NONE\n<<<END REQUIRED>>>",
            secs=1.0, input_tokens=100, output_tokens=10,
            usage_line=lambda: "100+10", cost_usd=0.0,
        )

    with patch("agent_loop.loop.chat", side_effect=mock_chat):
        result = review_panel(
            reviewers=["good-model", "broken-model"],
            prompt="test",
            system="test",
            art=MagicMock(),
            rnd=1,
            deadline_secs=10,
        )
    assert any(v.status == UNREACHABLE for v in result.votes)
    assert any(v.status == APPROVE for v in result.votes)
    assert not result.valid
    assert result.verdict == APPROVE


def test_panel_outage_when_no_quorum():
    """When no quorum is reached, the panel is an outage, not a verdict."""
    def mock_chat(model, messages, **kwargs):
        raise ProviderError("all providers down")

    with patch("agent_loop.loop.chat", side_effect=mock_chat):
        result = review_panel(
            reviewers=["model-a", "model-b", "model-c"],
            prompt="test",
            system="test",
            art=MagicMock(),
            rnd=1,
            deadline_secs=2,
        )
    assert not result.valid
    assert result.verdict == ""