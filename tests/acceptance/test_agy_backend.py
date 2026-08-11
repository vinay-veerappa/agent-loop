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
from agent_loop.providers import ProviderError, split_model, _AGY_PROMPT_LIMIT


def _proc(stdout="OK", stderr="", rc=0):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def test_agy_prefix_routes_to_the_cli_backend():
    assert split_model("agy:gemini-3.1-pro-high") == ("agy", "gemini-3.1-pro-high")


def test_a_prompt_over_the_windows_limit_is_refused_not_truncated():
    """agy takes the prompt as a command-line ARGUMENT and CreateProcess caps
    that at 32767 chars. Truncating would silently drop the end of the diff and
    the arbiter would rule on a patch it was never shown."""
    huge = "x" * (_AGY_PROMPT_LIMIT + 1)
    with patch("os.path.exists", return_value=True):
        with pytest.raises(ProviderError) as exc:
            providers.chat("agy:gemini-3.1-pro-high", [{"role": "user", "content": huge}])
    msg = str(exc.value)
    assert "Refusing rather than truncating" in msg
    assert str(_AGY_PROMPT_LIMIT) in msg


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


def test_system_and_user_messages_both_reach_the_prompt():
    seen = {}

    def fake_run(cmd, **kw):
        seen["prompt"] = cmd[-1]
        return _proc("ok")

    with patch("os.path.exists", return_value=True):
        with patch.object(subprocess, "run", side_effect=fake_run):
            providers.chat("agy:m", [{"role": "system", "content": "RULES-HERE"},
                                     {"role": "user", "content": "CASE-HERE"}], timeout=60)
    assert "RULES-HERE" in seen["prompt"] and "CASE-HERE" in seen["prompt"]


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
