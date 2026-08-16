"""
Acceptance test for Wave 4.2: TDD independence -- path-isolation check (C-1).

The critical review identified that the test-first gate enforces "human-authored"
as a proxy for independence, but the real requirement is "the test was generated
from the spec, not from the implementation." A test generated from the
implementation can be tautological -- it tests what the code does, not what the
spec says.

Path-isolated mode generates tests from the spec alone, without showing the
implementation code. This satisfies the independence property even when the same
model writes both the test and the implementation: the test was produced without
sight of the code it will be tested against.
"""
from unittest.mock import patch, MagicMock
from pathlib import Path

from agent_loop.test_mode import run_test, TEST_SYSTEM, PATH_ISOLATED_SYSTEM
from agent_loop.profiles import Profile, register


PROFILE = Profile(
    name="test-path-isolated",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    test_cmd="python -m pytest tests/ -q",
    test_sources=("tests/test_*.py",),
    implementer_rules="test", reviewer_priorities="test",
)
register(PROFILE)


TICKET = {
    "id": "T-ISOL",
    "title": "test independence",
    "defect": "parse_date returns the wrong day for leap years.",
    "spec": "Fix the leap year check to handle Feb 29 correctly.",
    "regions": [
        {"id": "PARSE_DATE", "file": "src/dates.py", "anchor": "def parse_date"}
    ],
    "expect_green": ["test_parse_date_leap_year"],
}


def test_path_isolated_uses_isolated_system_prompt(tmp_path):
    """When path_isolated=True, the PATH_ISOLATED_SYSTEM prompt is used."""
    repo = tmp_path
    (repo / "src").mkdir()
    (repo / "src" / "dates.py").write_text("def parse_date():\n    pass\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_sample.py").write_text("def test_sample():\n    assert True\n", encoding="utf-8")

    captured_system = {}

    def mock_chat(model, history, **kwargs):
        captured_system["system"] = history[0]["content"]
        return MagicMock(
            text="<<<TESTS>>>\n```python\ndef test_x():\n    assert False\n```\n<<<END TESTS>>>\n<<<NOTES>>>\n- covers it\n<<<END NOTES>>>",
            secs=1.0, input_tokens=100, output_tokens=50,
            usage_line=lambda: "100+50", cost_usd=0.0,
        )

    with patch("agent_loop.test_mode.chat", side_effect=mock_chat):
        run_test(repo, "leap year bug", TICKET, PROFILE, "model-a",
                 path_isolated=True)

    assert captured_system["system"] == PATH_ISOLATED_SYSTEM


def test_default_uses_standard_system_prompt(tmp_path):
    """When path_isolated=False (default), the TEST_SYSTEM prompt is used."""
    repo = tmp_path
    (repo / "src").mkdir()
    (repo / "src" / "dates.py").write_text("def parse_date():\n    pass\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_sample.py").write_text("def test_sample():\n    assert True\n", encoding="utf-8")

    captured_system = {}

    def mock_chat(model, history, **kwargs):
        captured_system["system"] = history[0]["content"]
        return MagicMock(
            text="<<<TESTS>>>\n```python\ndef test_x():\n    assert False\n```\n<<<END TESTS>>>\n<<<NOTES>>>\n- covers it\n<<<END NOTES>>>",
            secs=1.0, input_tokens=100, output_tokens=50,
            usage_line=lambda: "100+50", cost_usd=0.0,
        )

    with patch("agent_loop.test_mode.chat", side_effect=mock_chat):
        run_test(repo, "leap year bug", TICKET, PROFILE, "model-a",
                 path_isolated=False)

    assert captured_system["system"] == TEST_SYSTEM


def test_path_isolated_does_not_include_implementation_code(tmp_path):
    """When path_isolated=True, the prompt does NOT include the implementation code."""
    repo = tmp_path
    (repo / "src").mkdir()
    impl = "def parse_date(date_str):\n    # BROKEN: does not handle Feb 29\n    return date_str\n"
    (repo / "src" / "dates.py").write_text(impl, encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_sample.py").write_text("def test_sample():\n    assert True\n", encoding="utf-8")

    captured_prompt = {}

    def mock_chat(model, history, **kwargs):
        captured_prompt["prompt"] = history[1]["content"]
        return MagicMock(
            text="<<<TESTS>>>\n```python\ndef test_x():\n    assert False\n```\n<<<END TESTS>>>\n<<<NOTES>>>\n- covers it\n<<<END NOTES>>>",
            secs=1.0, input_tokens=100, output_tokens=50,
            usage_line=lambda: "100+50", cost_usd=0.0,
        )

    with patch("agent_loop.test_mode.chat", side_effect=mock_chat):
        run_test(repo, "leap year bug", TICKET, PROFILE, "model-a",
                 path_isolated=True)

    prompt = captured_prompt["prompt"]
    # The spec IS included.
    assert "Fix the leap year check" in prompt
    # The implementation code is NOT included.
    assert "BROKEN" not in prompt
    assert "def parse_date(date_str)" not in prompt


def test_default_includes_implementation_code(tmp_path):
    """When path_isolated=False (default), the prompt DOES include the implementation code."""
    repo = tmp_path
    (repo / "src").mkdir()
    impl = "def parse_date(date_str):\n    # BROKEN: does not handle Feb 29\n    return date_str\n"
    (repo / "src" / "dates.py").write_text(impl, encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_sample.py").write_text("def test_sample():\n    assert True\n", encoding="utf-8")

    captured_prompt = {}

    def mock_chat(model, history, **kwargs):
        captured_prompt["prompt"] = history[1]["content"]
        return MagicMock(
            text="<<<TESTS>>>\n```python\ndef test_x():\n    assert False\n```\n<<<END TESTS>>>\n<<<NOTES>>>\n- covers it\n<<<END NOTES>>>",
            secs=1.0, input_tokens=100, output_tokens=50,
            usage_line=lambda: "100+50", cost_usd=0.0,
        )

    with patch("agent_loop.test_mode.chat", side_effect=mock_chat):
        run_test(repo, "leap year bug", TICKET, PROFILE, "model-a",
                 path_isolated=False)

    prompt = captured_prompt["prompt"]
    # The implementation code IS included.
    assert "BROKEN" in prompt
    assert "def parse_date(date_str)" in prompt