"""
test_mode.py
============
Test mode: input is a defect description + a ticket JSON (from plan mode).
Output is failing acceptance tests written to a test file.

The LLM uses the graph to find test patterns and the code under test.
The tests must fail at baseline (the loop's test-first check enforces this).

Phase 6 of the execution plan.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import config, gates, profiles, regions, workspace
from .providers import Completion, ProviderError, chat


TEST_SYSTEM = """You are a senior software engineer writing acceptance tests.
Your job is to write tests that FAIL at baseline (before the fix) and PASS
after the fix is applied. The tests must be real tests that assert the
specific behaviour described in the defect.

OUTPUT FORMAT - obey exactly:
<<<TESTS>>>
```python
# the test code
import pytest

def test_name():
    assert False, "not yet implemented"
```
<<<END TESTS>>>
<<<NOTES>>>
- why these tests cover the defect
<<<END NOTES>>>
"""


def run_test(
    repo: Path,
    defect_description: str,
    ticket: Dict[str, Any],
    profile: profiles.Profile,
    implementer: str,
    test_file: str = "tests/acceptance/test_generated.py",
) -> Dict[str, Any]:
    """Run test mode: defect + ticket -> failing acceptance tests.

    Args:
        repo: the repo root
        defect_description: the defect to test
        ticket: the ticket JSON (from plan mode) with regions + expect_green
        profile: the language profile
        implementer: the model to use for writing tests
        test_file: where to write the generated tests

    Returns:
        a result dict with the test file path and whether tests fail at baseline
    """
    tid = ticket.get("id", "TEST")
    art = repo / "logs" / "agent_loop" / tid
    art.mkdir(parents=True, exist_ok=True)

    result: Dict[str, Any] = {"ticket": tid, "test_file": test_file, "tests_pass_baseline": None}

    # Build the prompt with the defect, the ticket, and the code under test
    prompt = f"# Defect to test\n\n{defect_description}\n\n"
    prompt += f"## Ticket\n```json\n{json.dumps(ticket, indent=2)}\n```\n\n"

    # Read the code under test (the regions from the ticket)
    for spec in ticket.get("regions", []):
        path = repo / spec["file"]
        if path.exists():
            src = path.read_text(encoding="utf-8")
            # Show the first 100 lines of the file
            lines = src.splitlines()[:100]
            prompt += f"## Code under test: {spec['file']}\n```{profile.language}\n"
            prompt += "\n".join(lines)
            prompt += "\n```\n\n"

    # Show the expect_green names so the test writer knows what to name tests
    expect_green = ticket.get("expect_green", [])
    if expect_green:
        prompt += f"## Test names to use\n"
        prompt += "\n".join(f"- {t}" for t in expect_green)
        prompt += "\n\n"

    prompt += f"Write the tests to {test_file}. The tests must FAIL at baseline.\n"

    history = [
        {"role": "system", "content": TEST_SYSTEM},
        {"role": "user", "content": prompt},
    ]

    try:
        _c = config.get().mode("test")
        out = chat(implementer, history, max_tokens=_c.max_tokens, think=_c.think)
    except ProviderError as exc:
        result["error"] = str(exc)
        return result

    raw = out.text
    (art / "test_raw.txt").write_text(raw, encoding="utf-8")
    print(f"  test generation: {out.usage_line()}")

    # Parse the test code
    test_code = _parse_tests(raw)
    if not test_code:
        result["error"] = "no <<<TESTS>>> block found in response"
        return result

    # Write the test file
    test_path = repo / test_file
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(test_code, encoding="utf-8")
    print(f"  wrote {len(test_code)} chars of tests to {test_file}")

    result["test_code"] = test_code

    # Verify the new tests fail at baseline (the test-first check).
    #
    # This used to `git stash` the LIVE repo, run the suite, and `git stash pop`.
    # Three things were wrong with that, and the third was destructive: a stash
    # hides the user's unrelated work in progress; `_git` returns a string, so
    # the two-value unpack raised ValueError BEFORE the pop ran and the stash
    # was never restored; and the caller reported success anyway because
    # test_code was set. A throwaway worktree gets the same clean baseline
    # without touching the live tree at all -- which is what workspace.py is for.
    if profile.test_cmd:
        try:
            with workspace.open_workspace(repo, f"{tid}-testgen") as ws:
                dest = ws.root / test_file
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(test_code, encoding="utf-8")
                outcome = gates.run_tests(profile.test_cmd, ws.root)
            if not outcome.ran:
                result["error"] = (
                    "cannot verify the generated tests: the runner produced no "
                    "parseable result summary at baseline"
                )
                print(f"  [test-first] WARNING: {result['error']}")
            else:
                failing = [f for f in outcome.failures if any(
                    gates.names_match(t, f) for t in (ticket.get("expect_green") or [])
                )] or sorted(outcome.failures)
                result["tests_pass_baseline"] = not outcome.failures
                if not outcome.failures:
                    print("  [test-first] WARNING: tests pass at baseline (they should fail)")
                else:
                    # A COUNT of failures is not evidence. This printed
                    # "(correct)" for a test that died in its own stub at
                    # `panel.votes` and never reached an assertion -- it could
                    # not have passed against fixed or unfixed code, and sixty
                    # turns went into satisfying it (O34, and O19 before it).
                    # So read the failures, classify them, and say which.
                    kinds = gates.failure_kinds(outcome.raw)
                    reached = gates.reached_an_assertion(kinds)
                    result["failure_kinds"] = sorted(kinds)
                    result["reached_assertion"] = reached
                    why = ", ".join(sorted(kinds)) if kinds else "(no exception identified)"
                    print(f"  [test-first] {len(failing)} test(s) failing at baseline: {why}")
                    if reached is False:
                        result["error"] = (
                            f"the generated test(s) failed with {why} and never reached an "
                            "assertion, so the failure demonstrates nothing about the defect. "
                            "The test file is on disk; fix its scaffolding and re-verify."
                        )
                        print(f"  [test-first] REFUSED: {result['error']}")
                    elif reached is None:
                        print(
                            "  [test-first] could not identify why they failed, so whether "
                            "they reached an assertion is UNKNOWN. Read the output before "
                            "trusting this red."
                        )
        except Exception as exc:
            result["error"] = f"test verification failed: {type(exc).__name__}: {exc}"
            print(f"  [test-first] {result['error']}")

    return result


def _parse_tests(raw: str) -> Optional[str]:
    """Parse a <<<TESTS>>> block from the raw response."""
    m = re.search(r"<<<TESTS>>>\s*```(?:python)?\s*(.*?)```\s*<<<END\s*TESTS>>>", raw, re.DOTALL)
    if not m:
        # Try without code fence
        m = re.search(r"<<<TESTS>>>\s*(.*?)<<<END\s*TESTS>>>", raw, re.DOTALL)
        if not m:
            return None
    return m.group(1).strip()