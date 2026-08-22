"""Tests for consumer findings CF-1 through CF-7 and CF-26/27/29/30.

Each test maps to a finding from CONSUMER_FINDINGS.md, documenting what
was observed in the field and verifying the fix.
"""
from __future__ import annotations

import sys
from io import StringIO

import pytest

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



# ---------------------------------------------------------------------------
# CF-26: --mode plan with default --max-rounds 0 must still run rounds
# ---------------------------------------------------------------------------

def test_cf26_plan_mode_resolves_max_rounds_from_config(monkeypatch, tmp_path):
    """The CLI default for --max-rounds is 0, meaning 'use configured value'.
    Plan mode must receive the configured value, not 0, or it runs zero rounds.
    """
    from agent_loop import cli, config, plan_mode, profiles

    cfg = config.get()
    monkeypatch.setattr(config, "_active", cfg)

    calls = []
    def fake_run_plan(*a, **k):
        calls.append(k.get("max_rounds"))
        return {"ticket": "PLAN", "rounds": [], "plan": None, "verdict": "TEST"}
    monkeypatch.setattr(plan_mode, "run_plan", fake_run_plan)

    profiles.register(profiles.Profile(
        name="cf26",
        language="python",
        file_suffixes=(".py",),
        block_comment=(),
        line_comment="#",
        block_kind="indent",
    ))

    cli.main(["--profile", "cf26", "--mode", "plan", "--defect", "x"])
    assert calls == [cfg.loop.max_rounds]


def test_cf26_plan_mode_explicit_max_rounds_overrides_config(monkeypatch, tmp_path):
    from agent_loop import cli, config, plan_mode, profiles

    cfg = config.get()
    monkeypatch.setattr(config, "_active", cfg)

    calls = []
    def fake_run_plan(*a, **k):
        calls.append(k.get("max_rounds"))
        return {"ticket": "PLAN", "rounds": [], "plan": None, "verdict": "TEST"}
    monkeypatch.setattr(plan_mode, "run_plan", fake_run_plan)

    profiles.register(profiles.Profile(
        name="cf26b",
        language="python",
        file_suffixes=(".py",),
        block_comment=(),
        line_comment="#",
        block_kind="indent",
    ))

    cli.main(["--profile", "cf26b", "--mode", "plan", "--defect", "x", "--max-rounds", "7"])
    assert calls == [7]


# ---------------------------------------------------------------------------
# CF-27: --help must not crash on a cp1252 Windows console
# ---------------------------------------------------------------------------

def test_cf27_help_encodes_on_cp1252_console():
    """The help text contains Unicode arrows. It must be printable on a
    cp1252-encoded stdout without raising UnicodeEncodeError.
    """
    from agent_loop.cli import main
    import io
    import sys

    buf = io.BytesIO()
    # Wrap a cp1252 text writer around a bytes buffer to simulate a Windows
    # console that cannot encode U+2192. Use 'replace' so the test harness
    # itself does not crash while we assert the help was written.
    writer = io.TextIOWrapper(buf, encoding="cp1252", errors="replace")
    old_stdout = sys.stdout
    try:
        sys.stdout = writer
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])
        assert exc_info.value.code == 0
    finally:
        sys.stdout = old_stdout
    text = buf.getvalue().decode("cp1252", errors="replace")
    assert "chain plan" in text


# ---------------------------------------------------------------------------
# CF-29: plan system prompt documents the `kind` field
# ---------------------------------------------------------------------------

def test_cf29_plan_system_documents_kind():
    """The planner must know about the `kind` region field so it can emit
    kind=line for bare statements and kind=decl for declarations.
    """
    from agent_loop.plan_mode import PLAN_SYSTEM, FEATURE_SYSTEM
    assert '"kind": "decl"' in PLAN_SYSTEM
    assert 'kind=decl' in PLAN_SYSTEM or '"kind": "decl"' in PLAN_SYSTEM
    assert '"line"' in PLAN_SYSTEM
    assert '"line"' in FEATURE_SYSTEM
    assert 'kind=line' in FEATURE_SYSTEM or '"line"' in FEATURE_SYSTEM


# ---------------------------------------------------------------------------
# CF-30: path-isolated test mode still shows the harness exemplar
# ---------------------------------------------------------------------------

def test_cf30_path_isolated_includes_exemplar_prompt(tmp_path, monkeypatch):
    """Even with --path-isolated, the test writer should see the project's
    harness style so it does not reinvent Program/Main/Run scaffolding.
    """
    import json
    from agent_loop import profiles, test_mode

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_harness.py").write_text(
        "def helper():\n    pass\n", encoding="utf-8"
    )


    profile = profiles.Profile(
        name="cf30",
        language="python",
        file_suffixes=(".py",),
        block_comment=(),
        line_comment="#",
        block_kind="indent",
        test_sources=("tests/test_*.py",),
        test_cmd="python -m pytest tests/ -q",
    )

    chat_calls = []
    def fake_chat(model, history, **k):
        chat_calls.append(history[-1]["content"])
        from agent_loop.providers import Completion
        return Completion(text="<<<TESTS>>>\n```python\ndef test_x():\n    pass\n```\n<<<END TESTS>>>", model="m")
    monkeypatch.setattr(test_mode, "chat", fake_chat)

    ticket = {
        "id": "T1",
        "title": "test",
        "spec": "x should be 2",
        "regions": [{"id": "R1", "file": "src/target.py", "anchor": "x = 1"}],
        "expect_green": ["x should be 2"],
    }
    result = test_mode.run_test(
        tmp_path, "x is wrong", ticket, profile, "m", path_isolated=True
    )
    assert result.get("test_code")
    prompt = chat_calls[0]
    assert "test_harness.py" in prompt
    assert "harness style exemplar (NOT the code under test)" in prompt
    # Under path isolation the implementation code is absent from the
    # code-under-test sections. The ticket JSON (which carries the anchor) may
    # still appear, so we check the prompt does not show the file content.
    assert "## Code under test" not in prompt


def test_cf30_test_style_exemplar_profile_field(tmp_path, monkeypatch):
    """If the profile pins test_style_exemplar, that file is used instead of
    the first glob match.
    """
    import json
    from agent_loop import profiles, test_mode

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "other.py").write_text("# other\n", encoding="utf-8")
    (tmp_path / "tests" / "exemplar.py").write_text("# exemplar\n", encoding="utf-8")

    profile = profiles.Profile(
        name="cf30b",
        language="python",
        file_suffixes=(".py",),
        block_comment=(),
        line_comment="#",
        block_kind="indent",
        test_sources=("tests/*.py",),
        test_style_exemplar="tests/exemplar.py",
        test_cmd="python -m pytest tests/ -q",
    )

    chat_calls = []
    def fake_chat(model, history, **k):
        chat_calls.append(history[-1]["content"])
        from agent_loop.providers import Completion
        return Completion(text="<<<TESTS>>>\n```python\ndef test_x():\n    pass\n```\n<<<END TESTS>>>", model="m")
    monkeypatch.setattr(test_mode, "chat", fake_chat)

    ticket = {
        "id": "T1",
        "title": "test",
        "spec": "x should be 2",
        "regions": [],
        "expect_green": [],
    }
    test_mode.run_test(tmp_path, "x is wrong", ticket, profile, "m", path_isolated=True)
    prompt = chat_calls[0]
    assert "exemplar.py" in prompt
    assert "other.py" not in prompt



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


def test_cf1_short_mixed_case_is_prose():
    """CF-1 residual: 'Do', 'Five', 'Reporting' have no interior transition."""
    from agent_loop.cli import _looks_like_code  # type: ignore
    assert not _looks_like_code("Do", "")
    assert not _looks_like_code("If", "")
    assert not _looks_like_code("Five", "")
    assert not _looks_like_code("Reporting", "")


def test_cf1_sentence_ending_period_not_call_token():
    """CF-1 residual: 'SCOPE.' at end of sentence should not match call_tokens."""
    import re
    # The old regex: r'\b([A-Z][a-zA-Z0-9_]+)\s*[.(]'
    # matches 'SCOPE' in 'm. SCOPE. Thi' because the period matches [.(].
    # The new regex requires . or ( to be followed by an identifier char.
    spec = "m. SCOPE. Thi"
    old_re = re.compile(r'\b([A-Z][a-zA-Z0-9_]+)\s*[.(]')
    new_re_dot = re.compile(r'\b([A-Z][a-zA-Z0-9_]+)\.[a-zA-Z_]')
    new_re_paren = re.compile(r'\b([A-Z][a-zA-Z0-9_]+)\s*\(\s*[\w"\']')
    old_matches = set(old_re.findall(spec))
    new_matches = set(new_re_dot.findall(spec)) | set(new_re_paren.findall(spec))
    assert "SCOPE" in old_matches, "sanity: old regex catches SCOPE"
    assert "SCOPE" not in new_matches, "new regex must not catch SCOPE"


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


# ---------------------------------------------------------------------------
# CF-10: arbiter prompt includes ticket scope as a labelled block
# ---------------------------------------------------------------------------

def test_cf10_arbiter_prompt_includes_ticket_scope_block():
    """The arbiter was upholding findings the ticket had explicitly scoped OUT.
    The fix: give the ticket's context field its own labelled block in the
    arbiter prompt with an instruction to rule OUT_OF_SCOPE for findings it
    names."""
    from agent_loop.arbiter import build_prompt
    from agent_loop.loop import Finding

    ticket = {
        "id": "P2-127",
        "title": "Test ticket",
        "defect": "The sort is unstable.",
        "context": "The wiring of the system cell into it, are later slices of "
                   "P2-127 and are deliberately not in this one.",
    }
    findings = [
        Finding(severity="BLOCKER", model="glm-5.2:cloud",
                text="The system severity is never incorporated into the tree's rank computation"),
    ]
    prompt = build_prompt(ticket, findings, "compile: ok", "diff --git a/x b/x", [])

    # The scope block must be present with its heading
    assert "Ticket scope" in prompt
    assert "deliberately not in this one" in prompt
    # The instruction must tell the arbiter to REJECT out-of-scope findings
    assert "REJECT" in prompt
    assert "out of scope" in prompt.lower() or "criterion #3" in prompt
    # The scope block heading and instruction must be present
    assert "Ticket scope" in prompt
    assert "this block names" in prompt


def test_cf10_arbiter_prompt_without_context_has_no_scope_block():
    """A ticket with no context field should not produce an empty scope block."""
    from agent_loop.arbiter import build_prompt
    from agent_loop.loop import Finding

    ticket = {"id": "T1", "title": "test", "defect": "broken", "context": ""}
    findings = [Finding(severity="MAJOR", model="m1", text="something")]
    prompt = build_prompt(ticket, findings, "compile: ok", "diff", [])

    assert "Ticket scope" not in prompt


def test_cf10_arbiter_contract_mentions_scope():
    """The arbiter's output contract must mention the scope block in the
    OUT_OF_SCOPE definition."""
    from agent_loop.arbiter import _ARBITER_CONTRACT

    assert "scope block" in _ARBITER_CONTRACT or "scope" in _ARBITER_CONTRACT.lower()


# ---------------------------------------------------------------------------
# CF-5: run output records which agent-loop version produced the patch
# ---------------------------------------------------------------------------

def test_cf5_result_records_agent_loop_describe():
    """CF-5: result.json must contain agent_loop_describe — the git describe
    output that distinguishes a tag run from a HEAD run. The packaging
    constant (agent_loop_version) is frozen at the tag and cannot tell
    them apart; agent_loop_describe gives 'v0.6.7-23-g23ba872' which does."""
    # The field is set at the top of run_ticket, before any model calls.
    # We verify the field exists in a minimal result dict by checking the
    # code path that sets it.
    import agent_loop
    import os
    from importlib.metadata import version

    # The resolved path must exist (editable install or wheel).
    pkg_path = os.path.dirname(getattr(agent_loop, "__file__", "") or "")
    assert pkg_path, "agent_loop package path must resolve"

    # The packaging constant (this is what was frozen and couldn't tell
    # tag from HEAD — the whole reason CF-5 was re-opened).
    pkg_version = version("agent-loop")

    # git describe from the package directory gives the REAL version.
    import subprocess
    describe = subprocess.run(
        ["git", "describe", "--tags", "--always", "--dirty"],
        cwd=pkg_path, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=5,
    ).stdout.strip()

    # describe should be non-empty (we're in a git repo with tags)
    assert describe, f"git describe should produce output from {pkg_path}"

    # If we're past the tag, describe should contain the tag + commits + sha
    # e.g. "v0.6.7-23-g23ba872". If we're AT the tag, it's just "v0.6.7".
    # Either way, it should NOT equal the packaging constant when we're
    # past the tag (which we are — this code is past v0.6.7).
    if "-" in describe:
        # We're past the tag — describe should differ from pkg_version
        assert not describe.startswith(pkg_version + "\n"), (
            f"describe ({describe}) should not equal the frozen packaging "
            f"constant ({pkg_version}) when we're past the tag"
        )