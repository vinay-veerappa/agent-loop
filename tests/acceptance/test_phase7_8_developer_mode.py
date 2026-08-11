"""
Acceptance tests for Phase 7+8: Developer mode tools and driver.

Phase 7: the 6 LLM-callable tools (read_file, search_code, trace_call_path,
edit_file, run_build, run_tests) must work correctly.

Phase 8: the Developer mode driver must localize a defect, edit files,
and produce a diff -- autonomously, without pre-declared regions.
"""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop.profiles import Profile, register
from agent_loop.providers import Completion
from agent_loop.developer.tools import execute_tool, TOOL_SCHEMAS
from agent_loop.developer.driver import run_developer, _parse_tool_calls, _check_file_scope


PROFILE = Profile(
    name="test-developer",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    build_cmd="python -m py_compile src/agent_loop/loop.py",
    test_cmd="python -m pytest tests/ -v --tb=short 2>&1",
    file_scope_whitelist=("src/",),
    protected=("test_*.py", "tests/*"),
    implementer_rules="test", reviewer_priorities="test",
)
register(PROFILE)


def _make_repo(tmpdir):
    repo = tmpdir / "repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    (src / "target.py").write_text("class TargetClass:\n    def method(self):\n        return 42\n", encoding="utf-8")
    os.system(f'cd /d "{repo}" && git init && git add -A && git commit -m init')
    return repo


# === Phase 7: Tool tests ===

def test_phase7_tool_schemas_exist():
    """All 7 tools are defined in TOOL_SCHEMAS."""
    names = [t["name"] for t in TOOL_SCHEMAS]
    assert "read_file" in names
    assert "search_code" in names
    assert "trace_call_path" in names
    assert "edit_file" in names
    assert "run_build" in names
    assert "run_tests" in names
    assert "write_test" in names  # the red phase
    assert len(TOOL_SCHEMAS) == 7


def test_phase7_read_file(tmp_path):
    """read_file returns windowed file content."""
    repo = _make_repo(tmp_path)
    result = execute_tool("read_file", {"path": "src/target.py"}, repo, PROFILE)
    assert "TargetClass" in result
    assert "return 42" in result
    assert "lines 1-" in result


def test_phase7_read_file_not_found(tmp_path):
    """read_file returns error for missing file."""
    repo = _make_repo(tmp_path)
    result = execute_tool("read_file", {"path": "nonexistent.py"}, repo, PROFILE)
    assert "ERROR" in result


def test_phase7_search_code(tmp_path):
    """search_code finds patterns in the codebase."""
    repo = _make_repo(tmp_path)
    result = execute_tool("search_code", {"pattern": "TargetClass"}, repo, PROFILE)
    assert "target.py" in result
    assert "TargetClass" in result


def test_phase7_search_code_no_matches(tmp_path):
    """search_code returns 'no matches' when pattern not found."""
    repo = _make_repo(tmp_path)
    result = execute_tool("search_code", {"pattern": "nonexistent_function"}, repo, PROFILE)
    assert "No matches" in result


def test_phase7_edit_file(tmp_path):
    """edit_file applies an exact string replacement."""
    repo = _make_repo(tmp_path)
    result = execute_tool("edit_file", {
        "path": "src/target.py",
        "old_str": "return 42",
        "new_str": "return 43",
    }, repo, PROFILE)
    assert "OK" in result
    content = (repo / "src" / "target.py").read_text()
    assert "return 43" in content
    assert "return 42" not in content


def test_phase7_edit_file_not_found(tmp_path):
    """edit_file returns error when old_str not found."""
    repo = _make_repo(tmp_path)
    result = execute_tool("edit_file", {
        "path": "src/target.py",
        "old_str": "nonexistent string",
        "new_str": "replacement",
    }, repo, PROFILE)
    assert "ERROR" in result
    assert "not found" in result


def test_phase7_edit_file_ambiguous(tmp_path):
    """edit_file returns error when old_str is not unique."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "dup.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    result = execute_tool("edit_file", {
        "path": "src/dup.py",
        "old_str": "x = 1",
        "new_str": "x = 2",
    }, repo, PROFILE)
    assert "ERROR" in result
    assert "2 times" in result or "not unique" in result.lower() or "ambiguous" in result.lower()


def test_phase7_run_build(tmp_path):
    """run_build executes the profile's build command."""
    repo = _make_repo(tmp_path)
    result = execute_tool("run_build", {}, repo, PROFILE)
    # Build should succeed (py_compile of loop.py)
    assert "OK" in result or "FAIL" in result


def test_phase7_run_tests(tmp_path):
    """run_tests executes the profile's test command."""
    repo = _make_repo(tmp_path)
    result = execute_tool("run_tests", {}, repo, PROFILE)
    # Tests should run (may pass or fail depending on the repo state)
    assert "Tests:" in result or "FAIL" in result or "OK" in result


def test_phase7_unknown_tool(tmp_path):
    """execute_tool returns error for unknown tool."""
    repo = _make_repo(tmp_path)
    result = execute_tool("nonexistent_tool", {}, repo, PROFILE)
    assert "ERROR" in result


# === Phase 8: Driver tests ===

def test_phase8_parse_tool_calls():
    """_parse_tool_calls extracts tool calls from <<<TOOL>>> blocks."""
    raw = '<<<TOOL name="read_file">>>\n{"path": "src/x.py"}\n<<<END TOOL>>>'
    calls = _parse_tool_calls(raw, ["read_file"])
    assert len(calls) == 1
    assert calls[0]["name"] == "read_file"
    assert calls[0]["args"]["path"] == "src/x.py"


def test_phase8_parse_tool_calls_multiple():
    """_parse_tool_calls handles multiple tool calls."""
    raw = '<<<TOOL name="read_file">>>\n{"path": "a.py"}\n<<<END TOOL>>>\n<<<TOOL name="search_code">>>\n{"pattern": "foo"}\n<<<END TOOL>>>'
    calls = _parse_tool_calls(raw, ["read_file", "search_code"])
    assert len(calls) == 2
    assert calls[0]["name"] == "read_file"
    assert calls[1]["name"] == "search_code"


def test_phase8_parse_tool_calls_filters_unavailable():
    """_parse_tool_calls only returns tools in the available list."""
    raw = '<<<TOOL name="edit_file">>>\n{"path": "a.py", "old_str": "x", "new_str": "y"}\n<<<END TOOL>>>'
    # edit_file not in explore phase
    calls = _parse_tool_calls(raw, ["read_file", "search_code"])
    assert len(calls) == 0


def test_phase8_check_file_scope_allowed():
    """_check_file_scope allows files in the whitelist."""
    assert _check_file_scope("src/agent_loop/loop.py", PROFILE) is True


def test_phase8_check_file_scope_blocked():
    """_check_file_scope blocks files outside the whitelist."""
    assert _check_file_scope("tests/test_x.py", PROFILE) is False
    assert _check_file_scope("docs/readme.md", PROFILE) is False


def test_phase8_check_file_scope_no_whitelist():
    """_check_file_scope allows all when whitelist is empty."""
    p = Profile(name="no-scope", language="python", file_suffixes=(".py",),
                line_comment="#", block_comment=(), block_kind="indent",
                file_scope_whitelist=(),
                implementer_rules="t", reviewer_priorities="t")
    assert _check_file_scope("anywhere/x.py", p) is True


def test_phase8_driver_completes(tmp_path):
    """Developer mode driver runs to completion with a mock implementer."""
    repo = _make_repo(tmp_path)

    # Use a profile with a build command that works on the test repo
    dev_profile = Profile(
        name="test-dev-complete",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        build_cmd="python -m py_compile src/target.py",
        test_cmd="",
        file_scope_whitelist=("src/",),
        protected=("test_*.py", "tests/*"),
        implementer_rules="test", reviewer_priorities="test",
    )
    register(dev_profile)

    call_count = [0]
    def mock_impl(model, messages, **kw):
        call_count[0] += 1
        if call_count[0] == 1:
            return Completion(
                text='<<<TOOL name="read_file">>>\n{"path": "src/target.py"}\n<<<END TOOL>>>',
                model=model, input_tokens=100, output_tokens=50,
            )
        elif call_count[0] == 2:
            return Completion(
                text='<<<TOOL name="edit_file">>>\n{"path": "src/target.py", "old_str": "return 42", "new_str": "return 43"}\n<<<END TOOL>>>',
                model=model, input_tokens=100, output_tokens=50,
            )
        else:
            return Completion(
                text='<<<DONE>>>\nChanged return value from 42 to 43\n<<<END DONE>>>',
                model=model, input_tokens=100, output_tokens=50,
            )

    with patch("agent_loop.developer.driver.chat", side_effect=mock_impl):
        with patch("agent_loop.loop.review_panel") as mock_panel:
            from agent_loop.loop import PanelResult, Vote
            mock_panel.return_value = PanelResult(
                votes=[Vote("r1", "APPROVE")], verdict="APPROVE", valid=True,
            )
            result = run_developer(
                repo, "The return value is wrong", dev_profile,
                "test-impl", ["r1"], arbiter_model="",
                max_turns=10, apply=False,
            )

    assert result["verdict"] in ("DONE", "APPROVE")
    assert "Changed return value" in result.get("summary", "")
    # Developer mode works in a disposable worktree. Without --apply the LIVE
    # tree must be untouched -- it used to edit the live tree directly, which
    # destroyed uncommitted work and swept unrelated edits into the patch.
    content = (repo / "src" / "target.py").read_text()
    assert "return 42" in content, "live tree must not change without --apply"
    assert result["patch"], "a patch must be exported"
    assert "return 43" in Path(result["patch"]).read_text(encoding="utf-8")


def test_phase8_driver_applies_when_asked(tmp_path):
    """With apply=True the approved edit is promoted into the live tree."""
    repo = _make_repo(tmp_path)

    dev_profile = Profile(
        name="test-dev-apply",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        build_cmd="python -m py_compile src/target.py",
        test_cmd="",
        file_scope_whitelist=("src/",),
        protected=("test_*.py", "tests/*"),
        implementer_rules="test", reviewer_priorities="test",
    )
    register(dev_profile)

    call_count = [0]
    def mock_impl(model, messages, **kw):
        call_count[0] += 1
        if call_count[0] == 1:
            return Completion(
                text='<<<TOOL name="edit_file">>>\n{"path": "src/target.py", "old_str": "return 42", "new_str": "return 43"}\n<<<END TOOL>>>',
                model=model, input_tokens=100, output_tokens=50,
            )
        return Completion(text='<<<DONE>>>\nfixed\n<<<END DONE>>>', model=model)

    with patch("agent_loop.developer.driver.chat", side_effect=mock_impl):
        with patch("agent_loop.loop.review_panel") as mock_panel:
            from agent_loop.loop import PanelResult, Vote
            mock_panel.return_value = PanelResult(
                votes=[Vote("r1", "APPROVE")], verdict="APPROVE", valid=True,
            )
            result = run_developer(
                repo, "The return value is wrong", dev_profile,
                "test-impl", ["r1"], arbiter_model="",
                max_turns=10, apply=True,
            )

    assert result.get("applied") is True, f"apply=True must promote, got {result}"
    assert "return 43" in (repo / "src" / "target.py").read_text()


def test_phase8_driver_refuses_protected_edit(tmp_path):
    """Developer mode may not edit the tests that grade it."""
    repo = _make_repo(tmp_path)
    (repo / "src" / "test_target.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")

    # The protected pattern and the file-scope whitelist OVERLAP here, which is
    # the real nt8 shape: its *Tests.cs live inside the whitelisted addon
    # directory, so the scope check alone cannot stop the loop editing them.
    guard_profile = Profile(
        name="test-dev-protected",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        build_cmd="", test_cmd="",
        file_scope_whitelist=("src/",),
        protected=("test_*.py",),
        implementer_rules="t", reviewer_priorities="t",
    )
    register(guard_profile)

    def mock_impl(model, messages, **kw):
        if "edit_file result" in messages[-1].get("content", ""):
            return Completion(text='<<<ESCALATE>>>\nblocked\n<<<END ESCALATE>>>', model=model)
        return Completion(
            text='<<<TOOL name="edit_file">>>\n{"path": "src/test_target.py", "old_str": "assert True", "new_str": "assert False"}\n<<<END TOOL>>>',
            model=model,
        )

    with patch("agent_loop.developer.driver.chat", side_effect=mock_impl):
        result = run_developer(
            repo, "make the test pass", guard_profile,
            "test-impl", [], arbiter_model="", max_turns=4, apply=False,
        )

    assert "assert True" in (repo / "src" / "test_target.py").read_text(), \
        "a protected test file must not be edited"
    assert result["verdict"] != "DONE" or not result.get("patch")


def test_phase8_driver_escalates(tmp_path):
    """Developer mode driver handles ESCALATE correctly."""
    repo = _make_repo(tmp_path)

    # No test_cmd: PROFILE's points at a tests/ directory this fixture repo does
    # not have, and developer mode now refuses to start when it cannot establish
    # a baseline (see test_phase8_driver_refuses_without_baseline).
    esc_profile = Profile(
        name="test-dev-escalate",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        build_cmd="", test_cmd="",
        file_scope_whitelist=("src/",),
        protected=("test_*.py", "tests/*"),
        implementer_rules="t", reviewer_priorities="t",
    )
    register(esc_profile)

    def mock_impl(model, messages, **kw):
        return Completion(
            text='<<<ESCALATE>>>\nCannot find the defect\n<<<END ESCALATE>>>',
            model=model, input_tokens=100, output_tokens=50,
        )

    with patch("agent_loop.developer.driver.chat", side_effect=mock_impl):
        result = run_developer(
            repo, "Unfixable defect", esc_profile,
            "test-impl", ["r1"], arbiter_model="",
            max_turns=5, apply=False,
        )

    assert result["verdict"] == "ESCALATED"


def test_phase8_driver_refuses_without_baseline(tmp_path):
    """A test_cmd that cannot report a summary must stop the run, not proceed
    against an empty baseline that makes every old failure look like a new one."""
    repo = _make_repo(tmp_path)

    with patch("agent_loop.developer.driver.chat") as mock_chat:
        result = run_developer(
            repo, "defect", PROFILE, "test-impl", ["r1"],
            arbiter_model="", max_turns=5, apply=False,
        )

    assert result["verdict"] == "TEST_BASELINE_UNAVAILABLE"
    assert mock_chat.call_count == 0, "must refuse before spending a model call"


def test_phase8_driver_scope_rejection(tmp_path):
    """Developer mode rejects edits outside the file scope whitelist."""
    repo = _make_repo(tmp_path)
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "readme.md").write_text("# Readme\n", encoding="utf-8")

    scope_profile = Profile(
        name="test-scope-reject",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        file_scope_whitelist=("src/",),
        implementer_rules="t", reviewer_priorities="t",
    )
    register(scope_profile)

    call_count = [0]
    def mock_impl(model, messages, **kw):
        call_count[0] += 1
        if call_count[0] == 1:
            return Completion(
                text='<<<TOOL name="edit_file">>>\n{"path": "docs/readme.md", "old_str": "# Readme", "new_str": "# Modified"}\n<<<END TOOL>>>',
                model=model, input_tokens=100, output_tokens=50,
            )
        else:
            return Completion(text='<<<DONE>>>\nDone\n<<<END DONE>>>', model=model)

    with patch("agent_loop.developer.driver.chat", side_effect=mock_impl):
        result = run_developer(
            repo, "test defect", scope_profile,
            "test-impl", ["r1"], arbiter_model="",
            max_turns=5, apply=False,
        )

    readme = (repo / "docs" / "readme.md").read_text()
    assert "# Readme" in readme, "file outside scope should not be modified"
    assert "# Modified" not in readme