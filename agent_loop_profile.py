"""Minimal Python profile for reviewing the agent-loop repo itself."""
import sys
from pathlib import Path

from agent_loop.profiles import Profile, register

AGENT_LOOP_PYTHON = Profile(
    name="agent-loop-python",
    language="python",
    file_suffixes=(".py",),
    line_comment="#",
    block_comment=(),
    block_kind="indent",
    preprocessor_directives=(),
    build_cmd=f'"{sys.executable}" -m py_compile {{files}}',
    test_cmd=(
        f'"{sys.executable}" -m pytest tests/ -q --tb=short -p no:cacheprovider'
    ),
    focused_test_cmd=(
        f'"{sys.executable}" -m pytest tests/acceptance/ -q --tb=short -p no:cacheprovider'
    ),
    lock_name="",
    risk_calls=(),
    file_scope_whitelist=("src/agent_loop/", "tests/"),
    protected=(
        "test_*.py",
        "*_test.py",
        "conftest.py",
    ),
    test_sources=("tests/acceptance/test_*.py",),
)

register(AGENT_LOOP_PYTHON)