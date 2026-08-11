"""
Acceptance tests for Phase 6: Plan + Test modes.

Plan mode: defect -> ticket JSON with regions + acceptance tests.
Test mode: defect + ticket -> failing acceptance tests.
"""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from _interp import PY_EXE

from agent_loop.profiles import Profile, register
from agent_loop.providers import Completion
from agent_loop.plan_mode import run_plan, _parse_ticket
from agent_loop.test_mode import run_test, _parse_tests


PROFILE = Profile(
    name="test-plan",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    build_cmd=PY_EXE + " -m py_compile src/agent_loop/loop.py",
    test_cmd=PY_EXE + " -m pytest tests/ -v --tb=short 2>&1",
    protected=("test_*.py", "tests/*"),
    implementer_rules="test", reviewer_priorities="test",
)
register(PROFILE)


def test_phase6_parse_ticket():
    """_parse_ticket extracts a JSON ticket from a <<<TICKET>>> block."""
    raw = """Some preamble.

<<<TICKET>>>
{"id": "T1", "title": "test", "defect": "d", "spec": "s", "regions": [{"id": "R1", "file": "src/x.py", "anchor": "def x"}], "expect_green": ["test_x"]}
<<<END TICKET>>>
<<<NOTES>>>
- notes
<<<END NOTES>>>
"""
    ticket = _parse_ticket(raw)
    assert ticket is not None
    assert ticket["id"] == "T1"
    assert ticket["regions"][0]["file"] == "src/x.py"
    assert ticket["expect_green"] == ["test_x"]


def test_phase6_parse_ticket_none():
    """_parse_ticket returns None when no block found."""
    assert _parse_ticket("no ticket here") is None


def test_phase6_parse_tests():
    """_parse_tests extracts test code from a <<<TESTS>>> block."""
    raw = """<<<TESTS>>>
```python
import pytest

def test_foo():
    assert False
```
<<<END TESTS>>>
<<<NOTES>>>
- notes
<<<END NOTES>>>
"""
    code = _parse_tests(raw)
    assert code is not None
    assert "def test_foo" in code
    assert "assert False" in code


def test_phase6_parse_tests_no_fence():
    """_parse_tests handles blocks without code fences."""
    raw = """<<<TESTS>>>
def test_bar():
    assert True
<<<END TESTS>>>
"""
    code = _parse_tests(raw)
    assert "def test_bar" in code


def test_phase6_fast_plan_accepts(tmp_path):
    """fast_plan mode accepts a plan without panel review."""
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    (src / "target.py").write_text("class Target:\n    def method(self):\n        return 42\n", encoding="utf-8")
    os.system(f'cd /d "{repo}" && git init && git add -A && git commit -m init')

    def mock_impl(model, messages, **kw):
        return Completion(
            text='<<<TICKET>>>\n{"id": "T1", "title": "test", "defect": "d", "spec": "s", "regions": [{"id": "R1", "file": "src/target.py", "anchor": "class Target"}], "expect_green": ["test_t1"]}\n<<<END TICKET>>>\n<<<NOTES>>>\n- ok\n<<<END NOTES>>>',
            model=model, input_tokens=100, output_tokens=50,
        )

    with patch("agent_loop.plan_mode.chat", side_effect=mock_impl):
        result = run_plan(repo, "test defect", PROFILE, "test-impl", ["r1"], fast_plan=True)

    assert result["verdict"] == "APPROVE"
    assert result["plan"] is not None
    assert result["plan"]["id"] == "T1"