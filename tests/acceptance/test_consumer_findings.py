"""Tests for consumer findings CF-1 through CF-7.

Each test maps to a finding from CONSUMER_FINDINGS.md, documenting what
was observed in the field and verifying the fix.
"""
from __future__ import annotations

import sys
from io import StringIO

from agent_loop.memory import validate_settled, _contradicts_settled


# ---------------------------------------------------------------------------
# CF-7: settled decision that contradicts an upheld finding is dropped
# ---------------------------------------------------------------------------

def test_cf7_contradictory_settled_is_dropped():
    """The arbiter upheld 'count when ABSENT' and settled 'count ONLY when
    PRESENT' — a direct contradiction in the same ruling."""
    settled = ["The counter may increment only when the order is present"]
    upheld = ["The counter must increment when the order is absent from account.Orders"]
    safe, dropped = validate_settled(settled, upheld)
    assert len(dropped) == 1
    assert len(safe) == 0


def test_cf7_non_contradictory_settled_is_kept():
    settled = ["Always use trailing stops for prop firm accounts"]
    upheld = ["The patch lacks trailing stops"]
    safe, dropped = validate_settled(settled, upheld)
    assert len(safe) == 1
    assert len(dropped) == 0


def test_cf7_settled_without_only_is_safe():
    """The crude check keys on 'only' — without it, no contradiction is detected."""
    settled = ["Use bracket orders for all entries"]
    upheld = ["The patch does not use bracket orders"]
    safe, dropped = validate_settled(settled, upheld)
    assert len(safe) == 1
    assert len(dropped) == 0


def test_cf7_empty_upheld_keeps_all_settled():
    safe, dropped = validate_settled(["a decision"], [])
    assert len(safe) == 1
    assert len(dropped) == 0


def test_cf7_empty_settled_returns_empty():
    safe, dropped = validate_settled([], ["some finding"])
    assert len(safe) == 0
    assert len(dropped) == 0


# ---------------------------------------------------------------------------
# CF-1: ALL-CAPS tokens are not treated as code identifiers
# ---------------------------------------------------------------------------

def test_cf1_all_caps_token_is_not_code():
    """CSS, DETAIL, GOES are prose, not identifiers."""
    from agent_loop.cli import _looks_like_code  # type: ignore
    assert not _looks_like_code("CSS", "")
    assert not _looks_like_code("DETAIL", "")
    assert not _looks_like_code("GOES", "")
    assert not _looks_like_code("SCOPE", "")
    assert not _looks_like_code("LOAD", "")


def test_cf1_camelcase_is_code():
    from agent_loop.cli import _looks_like_code  # type: ignore
    assert _looks_like_code("StringBuilder", "")
    assert _looks_like_code("parseDate", "")
    assert _looks_like_code("MyClass", "")


def test_cf1_snake_case_is_code():
    from agent_loop.cli import _looks_like_code  # type: ignore
    assert _looks_like_code("my_function", "")
    assert _looks_like_code("SOME_CONSTANT", "")  # has underscore


def test_cf1_pascal_case_is_code():
    from agent_loop.cli import _looks_like_code  # type: ignore
    assert _looks_like_code("CopierStatusView", "")
    assert _looks_like_code("TradeCopierEngine", "")


# ---------------------------------------------------------------------------
# CF-4: --version prints package version and resolved path
# ---------------------------------------------------------------------------

def test_cf4_version_flag_prints_version_and_path(capsys):
    from agent_loop.cli import main
    rc = main(["--version"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "agent-loop" in captured.out
    assert "resolved:" in captured.out