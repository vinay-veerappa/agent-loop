"""CF-32 -- an unresolved-symbol guess produced a patch that passed EVERY gate
green; only hand-verification against the real source caught it.

When --allow-unresolved-symbols is used, the model's guessed symbols are not
surfaced in the run output or the final report. The --list warning is pre-run
and easy to forget by the time a green patch appears. A guessed symbol on a
data-source decision made the patch functionally wrong (read account.Positions
instead of state.Positions) and no gate could tell, because the acceptance tests
set the snapshot field directly and never exercised the population path.

Fixes:

1. **`_scan_unresolved_symbols`** -- extracted from `_list` into a reusable
   function that returns the guessed symbols as data.

2. **`_has_unresolved_candidates`** -- quick check whether the spec contains
   code-like tokens, so the scan (and the deepcopy) only runs when there's
   something to find. Most tickets have no such tokens.

3. **Guessed symbols surfaced in `run_ticket`** -- stored in
   `result["guessed_symbols"]`, printed in the final report with an explicit
   "ships UNVERIFIED" warning when --allow-unresolved-symbols is set.

4. **Help text** -- warns that the opt-out is not always safe.

The fake coverage warning (basename string match against test source text)
was removed -- it was not coverage analysis and would produce false positives
and false negatives. The guessed-symbol surfacing is the real fix; the
operator reviews the patch knowing exactly which facts the model invented.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop import cli
from agent_loop.profiles import Profile, register


PROFILE = Profile(
    name="test-cf32",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    build_cmd="true", test_cmd="true",
    implementer_rules="test", reviewer_priorities="test",
)
register(PROFILE)


# ---------------------------------------------------------------------------
# Fix 1: _scan_unresolved_symbols is a reusable function that returns the list
# ---------------------------------------------------------------------------
def test_scan_returns_refused_symbols(tmp_path, capsys):
    """CF-32: the scan must return the guessed symbols, not just print them."""
    src = tmp_path / "mod.py"
    src.write_text("class Foo:\n    pass\n\nclass Bar:\n    pass\n", encoding="utf-8")

    ticket = {
        "id": "T1",
        "title": "test",
        "defect": "d",
        "spec": "Use the `Bar` class from mod.py",
        "regions": [{"id": "R1", "file": "mod.py", "anchor": "class Foo"}],
        "expect_green": [],
    }

    import os
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        guessed = cli._scan_unresolved_symbols(ticket, PROFILE, allow_unresolved=True, verbose=False)
    finally:
        os.chdir(old)

    assert any(s["symbol"] == "Bar" for s in guessed), (
        f"expected Bar in guessed symbols, got {guessed}"
    )


def test_scan_returns_empty_for_clean_ticket(tmp_path):
    """CF-32: a ticket whose spec only names symbols inside the region returns
    an empty list."""
    src = tmp_path / "mod.py"
    src.write_text("class Foo:\n    def bar(self):\n        pass\n", encoding="utf-8")

    ticket = {
        "id": "T1",
        "title": "test",
        "defect": "d",
        "spec": "Change the `bar` method",
        "regions": [{"id": "R1", "file": "mod.py", "anchor": "class Foo"}],
        "expect_green": [],
    }

    import os
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        guessed = cli._scan_unresolved_symbols(ticket, PROFILE, allow_unresolved=True, verbose=False)
    finally:
        os.chdir(old)

    assert guessed == [], f"expected no guessed symbols, got {guessed}"


# ---------------------------------------------------------------------------
# Fix 2: _has_unresolved_candidates is the conditional gate for the scan
# ---------------------------------------------------------------------------
def test_has_candidates_true_when_spec_names_symbols():
    """CF-32: a spec that names a code-like token should trigger the scan."""
    ticket = {"spec": "Use the `Bar` class", "context": "", "regions": []}
    assert cli._has_unresolved_candidates(ticket) is True


def test_has_candidates_false_when_spec_is_plain_english():
    """CF-32: a spec with no code-like tokens should NOT trigger the scan
    (no deepcopy, no file reads, no latency)."""
    ticket = {"spec": "Fix the bug in the run method", "context": "", "regions": []}
    assert cli._has_unresolved_candidates(ticket) is False


def test_has_candidates_false_for_empty_spec():
    """CF-32: an empty spec should not trigger the scan."""
    ticket = {"spec": "", "context": "", "regions": []}
    assert cli._has_unresolved_candidates(ticket) is False


# ---------------------------------------------------------------------------
# Fix 3: help text warns about unverified ships
# ---------------------------------------------------------------------------
def test_help_text_warns_about_unverified_ships():
    """CF-32: the --allow-unresolved-symbols help text must warn that a guessed
    symbol the tests do not discriminate ships unverified."""
    import inspect
    src = inspect.getsource(cli)
    assert "ships unverified" in src, (
        "the --allow-unresolved-symbols help must warn about unverified ships"
    )