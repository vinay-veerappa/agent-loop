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

Write them in the LANGUAGE AND TEST STYLE named in the request, and match the
conventions of the existing test sources you are shown. Do not import a testing
framework the project does not use.

OUTPUT FORMAT - obey exactly:
<<<TESTS>>>
```<language>
// the complete contents of the test file, in the project's language
```
<<<END TESTS>>>
<<<NOTES>>>
- why these tests cover the defect
<<<END NOTES>>>
"""


# Path-isolated test system: the test writer sees the SPEC and the DEFECT, but
# NOT the implementation code. This satisfies the independence property the
# critical review (C-section 1) identified: a test generated from the
# implementation can be tautological (it tests what the code does, not what the
# spec says). A test generated from the spec alone is independent of the
# implementation path -- even if the same model writes both, the test was
# produced without sight of the code it will be tested against.
PATH_ISOLATED_SYSTEM = """You are a senior software engineer writing acceptance tests.

Your job is to write tests that FAIL at baseline (before the fix) and PASS
after the fix is applied. You are given the DEFECT DESCRIPTION and the
SPECIFICATION of the required behaviour. You are NOT given the implementation
code -- this is deliberate: a test that is independent of the implementation
is a test that validates the spec, not the code. A test generated from the
implementation can be tautological.

Write tests that assert the SPECIFIC BEHAVIOUR described in the specification.
The tests must be real tests that fail because the behaviour is wrong, not
because of a missing import or a syntax error.

Write them in the LANGUAGE AND TEST STYLE named in the request, and match the
conventions of the existing test sources you are shown. Do not import a testing
framework the project does not use.

OUTPUT FORMAT - obey exactly:
<<<TESTS>>>
```<language>
// the complete contents of the test file, in the project's language
```
<<<END TESTS>>>
<<<NOTES>>>
- why these tests cover the defect, and why they are independent of the implementation
<<<END NOTES>>>
"""


def default_test_path(profile: profiles.Profile, ticket_id: str) -> str:
    """Where generated tests go, derived from the PROFILE.

    This used to be a `tests/acceptance/test_generated.py` default in the
    signature, with nothing consulting the profile. On the C# NT8 profile the
    consequences compounded: the path told the model the language, so it emitted
    `import pytest` and a Python module that tried `from TradeCopierEngine import
    ...` on a `.cs` file; the file landed outside `test_sources`, so the C# test
    project never compiled it; and the baseline run therefore reported the new
    tests PASSING, because nothing had run them.

    `test_sources` is the profile's own statement of where tests live, so the
    first pattern is the answer. Substituting the ticket id for the `*` keeps the
    generated file matching that glob -- which also keeps it inside the profile's
    `protected` list, so the implementer cannot edit the tests it must satisfy.
    """
    for pattern in profile.test_sources or ():
        if "*" in pattern:
            return pattern.replace("*", f"{ticket_id}Generated", 1)
        return pattern
    suffix = (profile.file_suffixes or (".py",))[0]
    return f"tests/acceptance/test_generated{suffix}"


def run_test(
    repo: Path,
    defect_description: str,
    ticket: Dict[str, Any],
    profile: profiles.Profile,
    implementer: str,
    test_file: Optional[str] = None,
    path_isolated: bool = False,
) -> Dict[str, Any]:
    """Run test mode: defect + ticket -> failing acceptance tests.

    Args:
        repo: the repo root
        defect_description: the defect to test
        ticket: the ticket JSON (from plan mode) with regions + expect_green
        profile: the language profile
        implementer: the model to use for writing tests
        test_file: where to write the generated tests
        path_isolated: if True, generate tests from the SPEC ONLY, not from
            the implementation code. This satisfies the independence property
            (C-section 1): a test generated from the implementation can be
            tautological. A test generated from the spec alone is independent
            of the implementation path.

    Returns:
        a result dict with the test file path and whether tests fail at baseline
    """
    tid = ticket.get("id", "TEST")
    if not test_file:
        test_file = default_test_path(profile, tid)
    art = repo / "logs" / "agent_loop" / tid
    art.mkdir(parents=True, exist_ok=True)

    result: Dict[str, Any] = {"ticket": tid, "test_file": test_file, "tests_pass_baseline": None}

    # Build the prompt with the defect, the ticket, and the code under test
    prompt = f"# Defect to test\n\n{defect_description}\n\n"
    prompt += f"## Specification\n{ticket.get('spec', '')}\n\n"
    prompt += f"## Ticket\n```json\n{json.dumps(ticket, indent=2)}\n```\n\n"

    # The code under test is the ticket's RESOLVED REGIONS -- not the head of the
    # file. This read `src.splitlines()[:100]`, so on a 2,700-line file whose
    # regions are at 382-534 the test writer was shown the DTO declarations and
    # never the method it had to test. It then invented a plausible name for it
    # (`CalculateCopyQuantity` for `CalculateFollowerQuantity`) -- the O39 failure
    # again, in a mode that had never been run.
    #
    # When path_isolated=True (C-section 1), the implementation code is NOT
    # shown. The test is generated from the spec alone, making it independent of
    # the implementation path. A test generated from the implementation can be
    # tautological (it tests what the code does, not what the spec says).
    if not path_isolated:
        try:
            resolved = regions.extract(repo, ticket.get("regions", []), profile)
        except regions.RegionError as exc:
            result["error"] = f"cannot read the code under test: {exc}"
            return result

        for r in resolved:
            if r.op == regions.CREATE:
                continue
            prompt += (
                f"## Code under test: {r.file} lines {r.lines_1based}\n"
                f"```{profile.language}\n{r.text}\n```\n\n"
            )
    else:
        # Path-isolated mode: the test writer gets the spec, the defect, the
        # ticket (for expect_green and region file paths), and the existing
        # test style -- but NOT the implementation code.
        result["path_isolated"] = True

    # Existing tests, so the generated file matches the project's real harness
    # rather than a framework the model assumes.
    for pattern in (profile.test_sources or ())[:1]:
        existing = sorted(repo.glob(pattern))
        if existing:
            sample = existing[0].read_text(encoding="utf-8", errors="replace")
            prompt += (
                f"## An EXISTING test source, {existing[0].name} -- match its style, "
                f"its assertion helper, and its naming\n"
                f"```{profile.language}\n{sample[:6000]}\n```\n\n"
            )

    # `expect_green` entries are matched against the FAILURE LINES the runner
    # prints, so on a harness whose failures read `[FAIL] <message>` these are
    # assertion messages, not method names. Saying "test names to use" invited
    # method names that the gate could never match, which makes the test-first
    # check vacuous -- the one check standing between the loop and a fake gate.
    expect_green = ticket.get("expect_green", [])
    if expect_green:
        prompt += (
            "## Acceptance criteria -- each MUST appear verbatim in the output of a "
            "failing assertion\n"
            "These strings are matched against the test runner's failure lines. Use "
            "each one as the assertion message (or test name, if that is what this "
            "runner prints on failure) so the gate can find it.\n"
        )
        prompt += "\n".join(f"- {t}" for t in expect_green)
        prompt += "\n\n"

    prompt += (
        f"Write the COMPLETE contents of {test_file}, in {profile.language}. "
        f"The tests must FAIL at baseline, before the fix exists.\n"
    )

    history = [
        {"role": "system", "content": PATH_ISOLATED_SYSTEM if path_isolated else TEST_SYSTEM},
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
                # `not outcome.failures` alone is NOT "everything passed": a
                # runner can report a failure COUNT in its summary while printing
                # no identifiable failure names, and then the parsed set is empty
                # for a suite that is red. That was harmless while this only
                # warned; as a refusal it would reject a correctly-red suite and
                # blame the wrong thing. Require the count to agree.
                result["tests_pass_baseline"] = outcome.failed == 0 and not outcome.failures
                if result["tests_pass_baseline"]:
                    # An ERROR, not a warning. Tests that are green before the fix
                    # exists cannot gate anything, and the commonest cause is that
                    # the runner never executed them at all -- which is exactly
                    # what happened when a `.py` file was written for a C# project
                    # whose `dotnet` runner did not compile it. Reporting this as a
                    # warning and then printing "tests written to: <path>" reads
                    # like success, so the vacuous gate ships.
                    result["error"] = (
                        f"the generated tests PASS at baseline, so they gate nothing. "
                        f"Either they do not assert the new behaviour, or the runner "
                        f"never ran them -- check that {test_file} is picked up by "
                        f"`{profile.test_cmd}`."
                    )
                    print(f"  [test-first] REFUSED: {result['error']}")
                else:
                    # A COUNT of failures is not evidence. This printed
                    # "(correct)" for a test that died in its own stub at
                    # `panel.votes` and never reached an assertion -- it could
                    # not have passed against fixed or unfixed code, and sixty
                    # turns went into satisfying it (O34, and O19 before it).
                    # So read the failures, classify them, and say which.
                    kinds = gates.failure_kinds(outcome.raw)
                    # A feature's red test fails on a name that does not
                    # exist yet, which is evidence here and a broken test
                    # anywhere else. Only the ticket knows which job this is.
                    feature = gates.is_feature_ticket(ticket)
                    reached = gates.reached_an_assertion(kinds, feature=feature)
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


# A fence's info string is whatever follows the backticks up to the newline --
# `python`, `csharp`, `c#`, `js title=x`, or nothing. It is metadata, never code.
_FENCE = re.compile(r"\A```[^\n`]*\r?\n(.*?)\r?\n?```\Z", re.DOTALL)


def _parse_tests(raw: str) -> Optional[str]:
    """Parse a <<<TESTS>>> block from the raw response.

    The fence language was hardcoded as `(?:python)?`, which did not merely fail
    to strip other languages -- it made them part of the CODE. With
    ```csharp, the optional group matched empty, `\\s*` matched nothing because
    `c` is not whitespace, and the capture began at `csharp`. The written file's
    first line was literally `csharp`, so the C# build failed on line 1 and the
    whole suite could not run.
    """
    m = re.search(r"<<<TESTS>>>(.*?)<<<END\s*TESTS>>>", raw, re.DOTALL)
    if not m:
        return None
    body = m.group(1).strip()
    fence = _FENCE.match(body)
    if fence:
        body = fence.group(1)
    return body.strip()