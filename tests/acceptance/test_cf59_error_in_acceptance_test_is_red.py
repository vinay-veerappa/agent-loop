"""CF-59: a red acceptance test that ERRORs in its fixture reads as a broken
suite.

Measured on tvDownloadOHLC T43 (python-tvdownloadohlc, 2026-09-28). The
acceptance test's fixture called the stub loader during setup
(`yield rb.load_risk_bindings()`), which raises `NotImplementedError`. pytest
reports that as ERROR rather than FAILED. `capture_baseline` counted the two
ERRORs as "suite-level error(s)" and rejected the ticket with "the test
command is broken independently of any patch" -- wrong: the suite was fine,
those two tests were simply red, and whether a test-first stub shows up as
FAILED or ERROR depends only on whether the first call to unimplemented code
sits in the test body or a fixture. Workaround used in the consumer repo:
load inside the test body instead of a fixture (`46de196f`).

Wanted (per the CF entry): an ERROR whose node id is one of the ticket's
`expect_green` tests, or lives in a file that also holds one, counts as red,
not suite-level. Collection errors and ERRORs in unrelated files stay
suite-level.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agent_loop import gates, workspace


def _git_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "target.py").write_text("def f():\n    return 42\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo, check=True,
    )
    return repo


def _cmd(tmp_path: Path, body: str) -> str:
    """A test_cmd that just prints a fixed pytest-shaped transcript."""
    script = tmp_path / "runner.py"
    script.write_text(
        "import sys\nsys.stdout.write({!r})\nsys.stdout.flush()\n".format(body),
        encoding="utf-8",
    )
    return '"{}" "{}"'.format(sys.executable, script)


# The CF-59 shape: one ordinary FAILED test, one ERROR whose node id belongs
# to the ticket's own acceptance test.
_ACCEPTANCE_ERROR = (
    "FAILED tests/test_risk_bindings.py::test_something_else - AssertionError\n"
    "ERROR tests/test_risk_bindings.py::test_reads_risk_bindings - NotImplementedError: not implemented\n"
    "=================== short test summary info ===================\n"
    "FAILED tests/test_risk_bindings.py::test_something_else - AssertionError\n"
    "ERROR tests/test_risk_bindings.py::test_reads_risk_bindings - NotImplementedError: not implemented\n"
    "=============== 1 failed, 1 error in 0.12s ================\n"
)


def test_error_in_the_ticket_own_acceptance_test_is_red_not_suite_level(tmp_path):
    repo = _git_repo(tmp_path)
    with workspace.open_workspace(repo, "CF59A") as ws:
        # Must NOT raise: the ERROR belongs to expect_green, so it is red,
        # not a broken suite.
        workspace.capture_baseline(
            ws,
            _cmd(tmp_path, _ACCEPTANCE_ERROR),
            gates.parse_tests,
            expect_green=["tests/test_risk_bindings.py::test_reads_risk_bindings"],
        )
    assert "tests/test_risk_bindings.py::test_reads_risk_bindings" in ws.baseline
    assert "tests/test_risk_bindings.py::test_something_else" in ws.baseline


def test_error_in_a_file_that_holds_an_acceptance_test_is_also_red(tmp_path):
    """The 'lives in a file that contains an acceptance test' half of the
    spec: a different test in the SAME file as an expect_green entry."""
    repo = _git_repo(tmp_path)
    with workspace.open_workspace(repo, "CF59B") as ws:
        workspace.capture_baseline(
            ws,
            _cmd(tmp_path, _ACCEPTANCE_ERROR),
            gates.parse_tests,
            # Names a DIFFERENT test in the same file as the errored one.
            expect_green=["tests/test_risk_bindings.py::test_something_else"],
        )
    assert "tests/test_risk_bindings.py::test_reads_risk_bindings" in ws.baseline


def test_a_bare_expect_green_name_finds_its_file(tmp_path):
    """T43's actual shape: expect_green holds BARE names, and the ERROR is a
    different test in the file that holds one of them."""
    repo = _git_repo(tmp_path)
    with workspace.open_workspace(repo, "CF59E") as ws:
        workspace.capture_baseline(
            ws,
            _cmd(tmp_path, _ACCEPTANCE_ERROR),
            gates.parse_tests,
            expect_green=["test_something_else"],
        )
    assert "tests/test_risk_bindings.py::test_reads_risk_bindings" in ws.baseline


def test_error_in_an_unrelated_file_still_ends_the_run(tmp_path):
    """Negative control: the ERROR's file is not named by expect_green at
    all, so it must stay suite-level and still reject the baseline."""
    repo = _git_repo(tmp_path)
    with workspace.open_workspace(repo, "CF59C") as ws:
        with pytest.raises(workspace.WorkspaceError, match="suite-level error"):
            workspace.capture_baseline(
                ws,
                _cmd(tmp_path, _ACCEPTANCE_ERROR),
                gates.parse_tests,
                expect_green=["tests/test_completely_unrelated.py::test_x"],
            )


def test_collection_error_stays_suite_level_even_with_expect_green(tmp_path):
    """Negative control: a genuine collection error (no test node id at all)
    must still end the run, no matter what expect_green names."""
    repo = _git_repo(tmp_path)
    body = (
        "ERROR tests/test_risk_bindings.py - FileNotFoundError\n"
        "==== 1 warning, 1 errors in 0.10s ====\n"
    )
    with workspace.open_workspace(repo, "CF59D") as ws:
        with pytest.raises(workspace.WorkspaceError, match="suite-level error"):
            workspace.capture_baseline(
                ws,
                _cmd(tmp_path, body),
                gates.parse_tests,
                expect_green=["tests/test_risk_bindings.py::test_reads_risk_bindings"],
            )
