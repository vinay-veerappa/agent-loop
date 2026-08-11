"""
O34: "failing at baseline (correct)" was a count, not evidence.

Test mode generated an acceptance test, ran it, saw one failure and printed

    [test-first] 1 test(s) failing at baseline (correct)

The test was red because its own stub returned a `dict` where `review_panel`
returns a `PanelResult`, so it died at `panel.votes` BEFORE reaching a single one
of its assertions. It could never have passed, against fixed or unfixed code.

The gate counted failures. It could not tell "red because the defect is there"
from "red because the test is broken", and printed `(correct)` either way.

The discriminator used here: a test written to demonstrate a defect asserts
something about behaviour, so it should end in an AssertionError (or pytest's
`Failed`, which is what `pytest.raises` produces when nothing is raised). A test
that dies with AttributeError/TypeError/ImportError never reached its assertion.

Deliberately NOT a hard rule in one case: when no exception kind can be
identified at all -- a non-pytest runner such as the NT8 profile's, which reports
`[FAIL] Suite.Test` and nothing else -- the check reports that it cannot tell,
rather than failing every run on a runner it does not understand. Absence of
evidence is reported as absence of evidence.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from _interp import PY_EXE

from agent_loop import gates, test_mode
from agent_loop.profiles import Profile, register
from agent_loop.providers import Completion


# --------------------------------------------------------------------------
# gates.failure_kinds
# --------------------------------------------------------------------------
ASSERTION_OUTPUT = """\
=================================== FAILURES ===================================
______________________________ test_double_doubles _____________________________
    def test_double_doubles():
>       assert double(3) == 6
E       AssertionError: assert 5 == 6
=========================== short test summary info ============================
FAILED tests/test_x.py::test_double_doubles - AssertionError: assert 5 == 6
1 failed in 0.05s
"""

# The real output from the O34 run, trimmed.
BROKEN_TEST_OUTPUT = """\
=================================== FAILURES ===================================
________________ test_run_review_logs_correct_artifact_paths ___________________
tests/acceptance/t.py:87: in test_run_review_logs_correct_artifact_paths
    review_mode.run_review(**kwargs)
src/agent_loop/review_mode.py:195: in run_review
    for v in panel.votes:
E   AttributeError: 'dict' object has no attribute 'votes'
=========================== short test summary info ============================
FAILED tests/acceptance/t.py::test_run_review_logs_correct_artifact_paths
1 failed in 0.08s
"""

NT8_OUTPUT = "[FAIL] RiskGuardTests.StopIsPlaced\nRESULTS: Passed = 30, Failed = 1"


def test_an_assertion_failure_is_recognised():
    assert gates.failure_kinds(ASSERTION_OUTPUT) == {"AssertionError"}


def test_a_broken_test_is_recognised_by_its_exception():
    assert gates.failure_kinds(BROKEN_TEST_OUTPUT) == {"AttributeError"}


def test_pytest_raises_counts_as_an_assertion():
    """`pytest.raises` that does not raise produces `Failed: DID NOT RAISE`.
    That IS the test reaching its assertion, and must not be called broken."""
    out = "E   Failed: DID NOT RAISE <class 'ValueError'>\n"
    assert gates.reached_an_assertion(gates.failure_kinds(out))


def test_the_summary_line_is_read_too():
    """With --tb=no there is no `E   ` block, only the short summary."""
    out = "FAILED tests/t.py::test_a - TypeError: f() takes 1 positional argument\n"
    assert gates.failure_kinds(out) == {"TypeError"}


def test_a_runner_we_cannot_parse_yields_nothing():
    """The NT8 profile's runner. Not 'no assertion' -- 'cannot tell'."""
    assert gates.failure_kinds(NT8_OUTPUT) == set()


def test_reached_an_assertion_is_false_only_when_we_know():
    assert gates.reached_an_assertion({"AssertionError"}) is True
    assert gates.reached_an_assertion({"AttributeError"}) is False
    assert gates.reached_an_assertion({"AttributeError", "AssertionError"}) is True
    # Nothing identified: unknown, and unknown is not a refusal.
    assert gates.reached_an_assertion(set()) is None


# --------------------------------------------------------------------------
# test mode uses it
# --------------------------------------------------------------------------
def _profile(name, test_cmd):
    p = Profile(
        name=name,
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        test_cmd=test_cmd,
        test_sources=("tests/*.py",),
        implementer_rules="t", reviewer_priorities="t",
    )
    register(p)
    return p


def _repo(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "src" / "target.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo, check=True,
    )
    return repo


def _run(repo, prof, capsys):
    raw = (
        "<<<TESTS>>>\n```python\ndef test_generated():\n    assert True\n```\n"
        "<<<END TESTS>>>"
    )
    with patch.object(test_mode, "chat", return_value=Completion(text=raw, model="m")):
        result = test_mode.run_test(
            repo, "a defect", {"id": "T1", "expect_green": ["test_generated"]},
            prof, "impl", test_file="tests/test_generated.py",
        )
    return result, capsys.readouterr().out


def test_a_test_red_from_a_broken_stub_is_not_accepted(tmp_path, capsys):
    """The O34 case. A failure that never reached an assertion is not evidence,
    so it is an error -- the tests exist, but they are not yet proof of
    anything, which is the contract cli._test already documents."""
    cmd = PY_EXE + " -c \"" + (
        "print('E   AttributeError: it broke in the fixture');"
        "print('FAILED tests/test_generated.py::test_generated');"
        "print('==== 1 failed in 0.1s ====')"
    ) + "\""
    prof = _profile("test-o34-broken", cmd)
    result, out = _run(_repo(tmp_path), prof, capsys)

    assert result.get("error"), "a spurious red must not be reported as success"
    assert "AttributeError" in out, "say WHY it failed"
    assert "(correct)" not in out, "the misleading word must be gone"


def test_a_genuine_assertion_failure_is_accepted(tmp_path, capsys):
    cmd = PY_EXE + " -c \"" + (
        "print('E   AssertionError: assert 1 == 2');"
        "print('FAILED tests/test_generated.py::test_generated');"
        "print('==== 1 failed in 0.1s ====')"
    ) + "\""
    prof = _profile("test-o34-genuine", cmd)
    result, out = _run(_repo(tmp_path), prof, capsys)

    assert not result.get("error"), out
    assert result["tests_pass_baseline"] is False
    assert "AssertionError" in out


def test_an_unparseable_runner_is_reported_as_unknown_not_refused(tmp_path, capsys):
    """The NT8 shape. We cannot tell, and we say so instead of refusing."""
    cmd = PY_EXE + " -c \"" + (
        "print('[FAIL] tests/test_generated.py::test_generated');"
        "print('RESULTS: Passed = 3, Failed = 1')"
    ) + "\""
    prof = _profile("test-o34-unknown", cmd)
    result, out = _run(_repo(tmp_path), prof, capsys)

    assert not result.get("error"), out
    assert "could not" in out.lower() or "unknown" in out.lower(), out


def test_tests_that_pass_at_baseline_are_still_refused(tmp_path, capsys):
    """The pre-existing check must survive: a green test proves nothing either."""
    cmd = PY_EXE + " -c \"print('==== 3 passed in 0.1s ====')\""
    prof = _profile("test-o34-green", cmd)
    result, out = _run(_repo(tmp_path), prof, capsys)

    # Was `"should fail" in out` against a WARNING. This is now a refusal that
    # sets result["error"], so the run exits non-zero instead of printing a
    # caution above "tests written to: <path>" and returning 0 (O48).
    assert "REFUSED" in out, out
    assert "gate nothing" in out, out
    assert result.get("error"), "a green-at-baseline suite must be an error"


# --------------------------------------------------------------------------
# Against REAL pytest output, not a fixture of what it is assumed to look like
# --------------------------------------------------------------------------
#
# The synthetic fixtures above encode an assumption about pytest's format, and
# an instrument built on a wrong assumption produces a confident wrong reading
# -- that is what the first compactor benchmark did (O24). So run the real
# runner over the two cases and classify its actual output.

BROKEN = (
    "def test_broken():\n"
    "    d = {}\n"
    "    d.votes\n"          # AttributeError: never reaches an assertion
)
GENUINE_BARE = (
    "def test_genuine():\n"
    "    assert 1 + 1 == 3\n"  # bare assert: NO exception name in the output
)
GENUINE_MSG = (
    "def test_genuine_msg():\n"
    "    assert 1 + 1 == 3, 'arithmetic is broken'\n"
)
RAISES = (
    "import pytest\n"
    "def test_raises():\n"
    "    with pytest.raises(ValueError):\n"
    "        pass\n"          # Failed: DID NOT RAISE
)


def _real_pytest(tmp_path, body, tb):
    import subprocess

    (tmp_path / "test_case.py").write_text(body, encoding="utf-8")
    proc = subprocess.run(
        [__import__("sys").executable, "-m", "pytest", "test_case.py", "-q", f"--tb={tb}",
         "-p", "no:cacheprovider"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    return proc.stdout + proc.stderr


@pytest.mark.parametrize("tb", ["short", "long", "line", "no"])
def test_real_pytest_a_broken_test_never_reached_an_assertion(tmp_path, tb):
    raw = _real_pytest(tmp_path, BROKEN, tb)
    kinds = gates.failure_kinds(raw)
    assert kinds, f"nothing identified in real --tb={tb} output:\n{raw}"
    assert gates.reached_an_assertion(kinds) is False, (kinds, raw)


@pytest.mark.parametrize("body", [GENUINE_BARE, GENUINE_MSG, RAISES])
@pytest.mark.parametrize("tb", ["short", "long", "line", "no"])
def test_real_pytest_a_genuine_failure_reached_its_assertion(tmp_path, body, tb):
    raw = _real_pytest(tmp_path, body, tb)
    kinds = gates.failure_kinds(raw)
    assert gates.reached_an_assertion(kinds) is True, (kinds, raw)


# --------------------------------------------------------------------------
# The same blind spot in developer mode's red phase
# --------------------------------------------------------------------------
#
# Developer mode already shows the failure output and tells the model in prose to
# "satisfy yourself that these fail BECAUSE OF THE DEFECT" (O19). That relies on
# the model noticing, and on the O34 run the model did not. Now that the
# classification is mechanical, compute it and say so.
#
# A WARNING here, not a refusal as in test mode, and the asymmetry is deliberate:
# test mode is one-shot and reports to a human, so a refusal costs nothing and
# the file is still on disk. Developer mode is iterative and the model cannot
# override a gate -- refusing a legitimate crash-defect test (one that fails with
# the very exception the defect raises) would strand the run in the red phase
# burning every remaining turn. Loud and escapable beats correct-and-stuck.

DEV_BROKEN_TEST = (
    "def test_broken_scaffold():\n"
    "    d = {}\n"
    "    d.votes\n"
)


def test_developer_red_phase_flags_a_test_that_never_asserted(tmp_path, capsys):
    import os
    from agent_loop.developer.driver import run_developer

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "src" / "target.py").write_text("def double(x):\n    return x + 2\n", encoding="utf-8")
    (repo / "tests" / "test_existing.py").write_text(
        "def test_placeholder():\n    assert True\n", encoding="utf-8")
    os.system(f'cd /d "{repo}" && git init && git add -A && git commit -m init')

    prof = Profile(
        name="test-o34-dev-red",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        build_cmd=PY_EXE + " -m py_compile src/target.py",
        test_cmd=PY_EXE + " -m pytest tests/ -q",
        file_scope_whitelist=("src/",),
        protected=("tests/*",),
        test_sources=("tests/*.py",),
        implementer_rules="t", reviewer_priorities="t",
    )
    register(prof)

    replies = [Completion(text="", model="m", tool_calls=[
        {"name": "write_test",
         "args": {"path": "tests/test_red.py", "content": DEV_BROKEN_TEST}},
    ])]
    calls = [0]

    def impl(model, messages, **kw):
        i = calls[0]
        calls[0] += 1
        return replies[i] if i < len(replies) else Completion(
            text="<<<ESCALATE>>>\nthe test is wrong\n<<<END ESCALATE>>>", model=model)

    with patch("agent_loop.developer.driver.chat", side_effect=impl):
        run_developer(
            repo, "double() adds 2", prof, "impl", ["r1"], arbiter_model="",
            max_turns=3, apply=False,
        )

    out = capsys.readouterr().out
    assert "AttributeError" in out, out[-3000:]
    assert "never reached an assertion" in out.lower(), out[-3000:]
