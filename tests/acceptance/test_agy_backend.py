"""
The `agy:` backend — Antigravity's CLI as a provider.

Three paths reach Gemini and they are NOT interchangeable. Measured 2026-08-10:

  gemini:   direct HTTP, AI Studio key, API ids only, no overhead
  SDK       google.antigravity LocalAgentConfig -- ALSO requires GEMINI_API_KEY
            (same free-tier quota), rejects agy's `-high` ids with HTTP 404, and
            wraps every call in ~13600 prompt tokens of agent scaffold
  agy:      the CLI -- Antigravity SUBSCRIPTION auth, so no AI Studio quota, and
            the only path exposing gemini-3.1-pro-high, claude-opus-4-6-thinking,
            claude-sonnet-4-6 and gpt-oss-120b-medium

The CLI is used here because it is the only one that reaches those models. The
SDK is the better tool for an agentic workload with vision and tools; it is the
wrong one for a single stateless ruling, where the scaffold is pure cost.
"""
import os
import subprocess
from unittest.mock import patch

import pytest

from agent_loop import providers
from agent_loop.providers import ProviderError, split_model


def _proc(stdout="OK", stderr="", rc=0):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def test_agy_prefix_routes_to_the_cli_backend():
    assert split_model("agy:gemini-3.1-pro-high") == ("agy", "gemini-3.1-pro-high")


def test_a_large_prompt_is_written_to_a_file_not_passed_on_the_command_line():
    """Prompts over the Windows command-line limit used to be refused. Now they
    are written to a temp file and referenced via @prompt.md, which has no
    length limit. A 50K-char prompt must reach agy via the file, not as a -p
    argument."""
    huge = "x" * 50000
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["cwd"] = kw.get("cwd")
        return _proc("OK")

    with patch("os.path.exists", return_value=True):
        with patch.object(subprocess, "run", side_effect=fake_run):
            out = providers.chat("agy:gemini-3.7-flash-high",
                                 [{"role": "user", "content": huge}], timeout=60)

    # The -p argument should reference the file, not contain the huge prompt
    p_arg = [a for a in seen["cmd"] if a.startswith("@") or a == "-p"]
    # Find the actual prompt argument (the one after -p)
    cmd_list = seen["cmd"]
    p_idx = cmd_list.index("-p")
    prompt_arg = cmd_list[p_idx + 1]
    assert "prompt.md" in prompt_arg or prompt_arg.startswith("@")
    assert "x" * 100 not in prompt_arg  # the huge content is NOT on the command line
    assert "--add-dir" in seen["cmd"]   # the temp dir is added to agy's workspace
    assert out.text == "OK"


def test_it_runs_sandboxed_and_never_in_the_callers_directory():
    """agy is an AGENT with file and terminal tools. Run in the repo it could
    edit the code under review."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["cwd"] = kw.get("cwd")
        return _proc("PONG")

    with patch("os.path.exists", return_value=True):
        with patch.object(subprocess, "run", side_effect=fake_run):
            out = providers.chat("agy:gemini-3.6-flash-low",
                                 [{"role": "user", "content": "hi"}], timeout=60)

    assert "--sandbox" in seen["cmd"]
    assert seen["cwd"] and seen["cwd"] != os.getcwd()
    assert "agy-" in os.path.basename(seen["cwd"])
    assert "--model=gemini-3.6-flash-low" in seen["cmd"]
    assert out.text == "PONG"
    assert out.model == "agy:gemini-3.6-flash-low"


def test_system_and_user_messages_both_reach_the_prompt_file():
    """Both system and user content are written to the temp prompt file."""

    def fake_run(cmd, **kw):
        cwd = kw.get("cwd", ".")
        prompt_file = os.path.join(cwd, "prompt.md")
        if os.path.exists(prompt_file):
            seen["file_content"] = open(prompt_file, encoding="utf-8").read()
        return _proc("ok")

    seen = {}
    with patch("os.path.exists", return_value=True):
        with patch.object(subprocess, "run", side_effect=fake_run):
            providers.chat("agy:m", [{"role": "system", "content": "RULES-HERE"},
                                     {"role": "user", "content": "CASE-HERE"}], timeout=60)
    assert "RULES-HERE" in seen.get("file_content", "") and "CASE-HERE" in seen.get("file_content", "")


def test_empty_output_is_an_error_carrying_stderr():
    """Silence must not read as an answer."""
    with patch("os.path.exists", return_value=True):
        with patch.object(subprocess, "run", side_effect=lambda *a, **k: _proc("", "boom", 1)):
            with pytest.raises(ProviderError) as exc:
                providers.chat("agy:m", [{"role": "user", "content": "hi"}], timeout=60)
    assert "returned nothing" in str(exc.value) and "boom" in str(exc.value)


def test_a_missing_binary_says_where_it_looked():
    with patch("os.path.exists", return_value=False):
        with pytest.raises(ProviderError) as exc:
            providers.chat("agy:m", [{"role": "user", "content": "hi"}], timeout=60)
    assert "AGY_BIN" in str(exc.value) and "agy models" in str(exc.value)


def test_cleanup_failure_does_not_discard_a_good_answer():
    """agy holds a file open in its cwd, so TemporaryDirectory's cleanup raised
    WinError 32 AFTER a successful call and the exception threw the completion
    away. Cleanup is best-effort now."""
    import shutil

    def boom(*a, **k):
        raise PermissionError("WinError 32")

    with patch("os.path.exists", return_value=True):
        with patch.object(subprocess, "run", side_effect=lambda *a, **k: _proc("ANSWER")):
            with patch.object(shutil, "rmtree", side_effect=boom):
                # ignore_errors=True is passed, but pin the behaviour regardless:
                # a cleanup problem must never surface as a provider failure.
                try:
                    out = providers.chat("agy:m", [{"role": "user", "content": "hi"}], timeout=60)
                except PermissionError:
                    pytest.fail("cleanup failure escaped and destroyed the answer")
    assert out.text == "ANSWER"
