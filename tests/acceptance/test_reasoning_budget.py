"""
Acceptance test for Wave 4.3: reasoning budget pre-dispatch validation (E-P1a).

On a reasoning model, chain-of-thought is spent from the SAME budget as the
answer. A budget sized for the expected output becomes a budget shared with an
unbounded reasoning prefix, and the model can return EMPTY CONTENT having
spent the whole budget reasoning. This was measured on the implementer:
125,070 chars of reasoning, empty content, the run died.

The fix: a pre-dispatch warning when think=True and max_tokens is below the
measured minimum (32K). This does not refuse the call -- the budget may be
intentionally small for a short task -- but it makes the hazard visible before
the call is spent, not after.
"""
import io
import sys
from unittest.mock import patch, MagicMock

from agent_loop.providers import chat
from agent_loop.providers import Completion


def _mock_completion():
    return Completion(
        text="answer", model="test", secs=1.0,
        input_tokens=100, output_tokens=50,
        thinking_chars=0,
    )


def _patched_chat():
    """Patch _BACKENDS so chat() doesn't hit a real server."""
    return patch("agent_loop.providers._BACKENDS", {
        "ollama": MagicMock(return_value=_mock_completion()),
        "anthropic": MagicMock(return_value=_mock_completion()),
        "openai": MagicMock(return_value=_mock_completion()),
    })


def test_reasoning_budget_warns_when_think_true_and_budget_low():
    """A warning is printed when think=True and max_tokens < 32000."""
    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        with _patched_chat():
            chat("test-model", [{"role": "user", "content": "hi"}],
                 max_tokens=16000, think=True)
    finally:
        sys.stdout = old_stdout

    output = captured.getvalue()
    assert "WARNING" in output
    assert "think=True" in output
    assert "max_tokens=16000" in output


def test_reasoning_budget_no_warning_when_think_false():
    """No warning when think=False (or None)."""
    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        with _patched_chat():
            chat("test-model", [{"role": "user", "content": "hi"}],
                 max_tokens=16000, think=False)
    finally:
        sys.stdout = old_stdout

    output = captured.getvalue()
    assert "WARNING" not in output


def test_reasoning_budget_no_warning_when_budget_high():
    """No warning when think=True and max_tokens >= 32000."""
    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        with _patched_chat():
            chat("test-model", [{"role": "user", "content": "hi"}],
                 max_tokens=48000, think=True)
    finally:
        sys.stdout = old_stdout

    output = captured.getvalue()
    assert "WARNING" not in output