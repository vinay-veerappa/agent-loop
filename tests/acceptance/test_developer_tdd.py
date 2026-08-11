"""
Acceptance tests: developer mode is test-first by default.

Why this exists. O3's first developer-mode patch compiled, passed all 232
tests, and did not fix the defect -- it read `r.get("gate")` from a round record
whose field is named `stage`, so the branch it added could never fire. Every
gate was green because nothing in the suite tested the thing being fixed, and a
gate ladder cannot refuse a fix for a defect it cannot observe. A reviewer
caught it; the gates could not.

So the ladder now starts with a rung that CAN fail: the model must write a test
that is red against the unfixed code before it may edit source, and that test is
required green at the end.
"""
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop import config
from agent_loop.profiles import Profile, register
from agent_loop.providers import Completion
from agent_loop.developer.driver import run_developer, RED_TOOLS
from agent_loop.developer.tools import execute_tool


def _profile(name):
    p = Profile(
        name=name,
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        build_cmd="python -m py_compile src/target.py",
        test_cmd="python -m pytest tests/ -q",
        file_scope_whitelist=("src/",),
        protected=("tests/*",),
        test_sources=("tests/*.py",),
        implementer_rules="test", reviewer_priorities="test",
    )
    register(p)
    return p


def _make_repo(tmpdir):
    """A repo with a real defect: double() returns x+2, not x*2."""
    repo = tmpdir / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "src" / "target.py").write_text(
        "def double(x):\n    return x + 2\n", encoding="utf-8")
    (repo / "tests" / "test_existing.py").write_text(
        "def test_placeholder():\n    assert True\n", encoding="utf-8")
    os.system(f'cd /d "{repo}" && git init && git add -A && git commit -m init')
    return repo


RED_TEST = (
    "import sys; sys.path.insert(0, 'src')\n"
    "def test_double_doubles():\n"
    "    from target import double\n"
    "    assert double(3) == 6\n"
)

# Passes against the unfixed code -- asserts the bug rather than the requirement.
GREEN_TEST = (
    "import sys; sys.path.insert(0, 'src')\n"
    "def test_double_adds_two():\n"
    "    from target import double\n"
    "    assert double(3) == 5\n"
)

FIX = {"path": "src/target.py", "old_str": "return x + 2", "new_str": "return x * 2"}


def _panel_approve():
    from agent_loop.loop import PanelResult, Vote
    return PanelResult(votes=[Vote("r1", "APPROVE")], verdict="APPROVE", valid=True)


def _run(repo, prof, replies, max_turns=12):
    calls = [0]

    def impl(model, messages, **kw):
        i = calls[0]
        calls[0] += 1
        return replies[i] if i < len(replies) else Completion(
            text="<<<DONE>>>\ndone\n<<<END DONE>>>", model=model)

    with patch("agent_loop.developer.driver.chat", side_effect=impl):
        with patch("agent_loop.loop.review_panel", return_value=_panel_approve()):
            return run_developer(
                repo, "double() adds 2 instead of doubling", prof,
                "test-impl", ["r1"], arbiter_model="",
                max_turns=max_turns, apply=False,
            )


def _tool(name, args):
    return Completion(text="", model="m", tool_calls=[{"name": name, "args": args}])


# ---------------------------------------------------------------------------
def test_red_phase_offers_write_test_not_edit_file():
    assert "write_test" in RED_TOOLS
    assert "edit_file" not in RED_TOOLS, "the red phase must not be able to edit source"


def test_happy_path_red_then_fix(tmp_path):
    """The intended flow: red test, then the fix, then green."""
    repo = _make_repo(tmp_path)
    prof = _profile("tdd-happy")
    result = _run(repo, prof, [
        _tool("write_test", {"path": "tests/test_red.py", "content": RED_TEST}),
        _tool("edit_file", FIX),
        Completion(text="<<<DONE>>>\nfixed double()\n<<<END DONE>>>", model="m"),
    ])
    assert result["verdict"] in ("DONE", "APPROVE"), (result["verdict"], result.get("error"))
    assert result["acceptance"], "the red test must be recorded as acceptance criteria"
    assert any("double_doubles" in t for t in result["acceptance"])
    patch_text = Path(result["patch"]).read_text(encoding="utf-8")
    assert "return x * 2" in patch_text
    assert "test_red.py" in patch_text, "the test ships with the fix"


def test_a_test_that_passes_at_baseline_is_rejected(tmp_path):
    """A test asserting the buggy behaviour proves nothing. It must not unlock
    the edit phase -- this is the check that makes the rung able to fail."""
    repo = _make_repo(tmp_path)
    prof = _profile("tdd-green-test")
    result = _run(repo, prof, [
        _tool("write_test", {"path": "tests/test_green.py", "content": GREEN_TEST}),
        _tool("edit_file", FIX),
        Completion(text="<<<DONE>>>\nfixed\n<<<END DONE>>>", model="m"),
    ], max_turns=4)
    assert not result.get("acceptance")
    # The load-bearing assertion is that the run stayed in the red phase, so
    # the edit was REFUSED. Asserting only that `acceptance` is empty is too
    # weak to be evidence: accepting the useless test would also leave it
    # empty, and a mutant that did exactly that survived until this was added.
    rejected = [r for t in result["turns"] for r in t["rejected"]]
    assert "edit_file" in rejected, result["turns"]
    assert result["verdict"] == "MAX_TURNS_EXHAUSTED", result["verdict"]
    assert result["patch"] is None, "nothing may be exported without a red test"


def test_cannot_finish_in_the_red_phase(tmp_path):
    """DONE before any failing test must be refused, or TDD is advisory."""
    repo = _make_repo(tmp_path)
    prof = _profile("tdd-early-done")
    result = _run(repo, prof, [
        Completion(text="<<<DONE>>>\nnothing to do here\n<<<END DONE>>>", model="m"),
    ], max_turns=3)
    assert result["verdict"] == "MAX_TURNS_EXHAUSTED", result["verdict"]
    assert not result.get("acceptance")


def test_edit_file_is_refused_before_a_red_test(tmp_path):
    """The red phase does not offer edit_file, so the call is rejected and the
    source is untouched."""
    repo = _make_repo(tmp_path)
    prof = _profile("tdd-edit-first")
    result = _run(repo, prof, [
        _tool("edit_file", FIX),
        _tool("edit_file", FIX),
    ], max_turns=3)
    assert result["verdict"] == "MAX_TURNS_EXHAUSTED"
    rejected = [r for t in result["turns"] for r in t["rejected"]]
    assert "edit_file" in rejected, result["turns"]
    assert "return x + 2" in (repo / "src" / "target.py").read_text()


def test_the_red_test_is_read_only_once_red(tmp_path):
    """The model must not be able to weaken the test it has to pass."""
    repo = _make_repo(tmp_path)
    prof = _profile("tdd-lock")
    result = _run(repo, prof, [
        _tool("write_test", {"path": "tests/test_red.py", "content": RED_TEST}),
        _tool("edit_file", {"path": "tests/test_red.py",
                            "old_str": "== 6", "new_str": "== 5"}),
        _tool("edit_file", FIX),
        Completion(text="<<<DONE>>>\nfixed\n<<<END DONE>>>", model="m"),
    ])
    assert result["verdict"] in ("DONE", "APPROVE"), (result["verdict"], result.get("error"))
    # The work happens in a disposable worktree, so the surviving evidence is
    # the patch: it must carry the test with its ORIGINAL assertion, and the
    # source fix. If the model had been able to weaken the test, `== 6` would
    # have become `== 5` and the fix would have been unnecessary.
    patch_text = Path(result["patch"]).read_text(encoding="utf-8")
    assert "== 6" in patch_text
    assert "== 5" not in patch_text
    assert "return x * 2" in patch_text


def test_write_test_cannot_reach_source(tmp_path):
    """write_test bypasses the protected check by design, so its own location
    check is the only thing stopping it being used to edit source."""
    repo = _make_repo(tmp_path)
    prof = _profile("tdd-escape")
    out = execute_tool(
        "write_test", {"path": "src/target.py", "content": "# pwned\n"},
        repo, prof, [],
    )
    assert out.startswith("ERROR")
    assert "return x + 2" in (repo / "src" / "target.py").read_text()


def test_run_tests_rejects_an_argument_instead_of_ignoring_it(tmp_path):
    """run_build/run_tests used to accept and silently discard `command`. A
    model passed a verification script, got the ordinary build's output back,
    and reported in its summary that it had verified the new behaviour."""
    repo = _make_repo(tmp_path)
    prof = _profile("tdd-args")
    out = execute_tool("run_tests", {"command": "echo pwned"}, repo, prof, [])
    assert out.startswith("ERROR")
    assert "was NOT executed" in out
    out = execute_tool("run_build", {"command": "echo pwned"}, repo, prof, [])
    assert out.startswith("ERROR")


def test_tdd_can_be_turned_off(tmp_path):
    """Default-on, but not mandatory: a profile with no test_cmd, or an explicit
    config override, keeps the old two-phase behaviour."""
    import dataclasses
    repo = _make_repo(tmp_path)
    prof = _profile("tdd-off")
    base = config.DEFAULTS
    modes = dict(base.modes)
    modes["developer"] = dataclasses.replace(modes["developer"], require_failing_test=False)
    try:
        config.set_active(dataclasses.replace(base, modes=modes))
        result = _run(repo, prof, [
            _tool("edit_file", FIX),
            Completion(text="<<<DONE>>>\nfixed\n<<<END DONE>>>", model="m"),
        ])
    finally:
        config.reset()
    assert result["verdict"] in ("DONE", "APPROVE"), (result["verdict"], result.get("error"))
    assert result["tdd"] is False


def test_locked_test_is_readonly_even_when_protected_does_not_cover_it(tmp_path):
    """`protected` is the general defence, but a profile can declare test
    locations that its protected globs do not match -- and then the only thing
    stopping the model editing the test it must pass is the per-run lock. With
    protected pointed elsewhere, disabling the lock lets the model rewrite its
    own acceptance criteria."""
    repo = _make_repo(tmp_path)
    prof = Profile(
        name="tdd-lock-only",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        build_cmd="python -m py_compile src/target.py",
        test_cmd="python -m pytest tests/ -q",
        # Deliberately covers neither tests/ nor test_*.py.
        file_scope_whitelist=("src/", "tests/"),
        protected=("docs/*",),
        test_sources=("tests/*.py",),
        implementer_rules="test", reviewer_priorities="test",
    )
    register(prof)
    result = _run(repo, prof, [
        _tool("write_test", {"path": "tests/test_red.py", "content": RED_TEST}),
        _tool("edit_file", {"path": "tests/test_red.py",
                            "old_str": "== 6", "new_str": "== 5"}),
        _tool("edit_file", FIX),
        Completion(text="<<<DONE>>>\nfixed\n<<<END DONE>>>", model="m"),
    ])
    assert result["verdict"] in ("DONE", "APPROVE"), (result["verdict"], result.get("error"))
    patch_text = Path(result["patch"]).read_text(encoding="utf-8")
    assert "== 6" in patch_text, "the acceptance test was weakened"
    assert "== 5" not in patch_text
