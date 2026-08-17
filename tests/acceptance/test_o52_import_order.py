"""
O52 — `import agent_loop.models` could not be the first import of the package.

    python -c "import agent_loop.models"
    ImportError: cannot import name 'model_family' from partially initialized
                 module 'agent_loop.models' (most likely due to a circular import)

`models.py` does `from . import config` at module scope. `config.py` ends with
`check_panel_policy(_DEFAULT_ROLES)` at module scope -- deliberately, so a
drifting default fails at import rather than in a test that passes the value
explicitly -- and that function did `from .models import model_family`. Reached
via `models`, the module is executing its own line 18 and `model_family` does not
exist yet.

**Import ORDER decided it, which is why 584 tests never saw it.** Import `config`
first, or anything that reaches it first (`cli`, `__main__`, every other test
module), and both modules initialise fine. By the time any test ran,
`agent_loop.config` was already in `sys.modules`.

It bit for real when `tests/acceptance/test_registry_budgets.py` was run ALONE:
that file's first import is `agent_loop.models`, and collection failed outright.

`model_family` is a pure string function over a model name. It now lives in
`config.py`, which needs it, and is re-exported from `models` for the callers
that already import it from there -- so `config` no longer depends on `models`
at all and the cycle is gone rather than deferred.

EVERY test here runs a FRESH interpreter. In-process the import is already
cached, so an in-process assertion proves nothing about the thing under test --
which is precisely how this survived so long.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import agent_loop

# The subprocess must import THIS checkout, not whatever is installed for the
# interpreter running the tests. Without it these pass on 3.14 (where the package
# is installed) and fail on 3.12 (where it is not) -- measuring the environment
# instead of the import order they exist to test.
_SRC = str(Path(agent_loop.__file__).resolve().parent.parent)


def _run(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env,
        encoding="utf-8", errors="replace",
    )

# Each of these is a plausible FIRST import of the package from a consumer
# script or a test module.
FIRST_IMPORTS = [
    "import agent_loop.models",
    "from agent_loop.models import ModelRegistry",
    "from agent_loop.models import model_family",
    "import agent_loop.config",
    "import agent_loop.regions",
    "import agent_loop.loop",
    "import agent_loop.arbiter",
    "import agent_loop",
]


@pytest.mark.parametrize("stmt", FIRST_IMPORTS)
def test_any_module_may_be_the_first_import(stmt):
    proc = _run(stmt)
    assert proc.returncode == 0, (
        f"`{stmt}` fails as the first import of the package:\n{proc.stderr}"
    )


def test_model_family_is_still_reachable_from_models():
    """cli.py and two existing tests import it from `models`. Moving it must not
    become a rename."""
    proc = _run(
        "from agent_loop.models import model_family;"
        "print(model_family('agy:claude-sonnet-4-6'))"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "claude"


def test_the_panel_policy_still_runs_at_import():
    """The check must not have been deferred to fix the cycle: a default that
    drifts to one reviewer, or to two of one family, has to fail at import and
    not at whatever time someone first calls a function."""
    proc = _run(
        "import agent_loop.config as c;"
        "import dataclasses;"
        "r = dataclasses.replace(c.DEFAULTS.roles['reviewer'], extra_members=());"
        "c.check_panel_policy({**c.DEFAULTS.roles, 'reviewer': r})"
    )
    assert proc.returncode != 0, "a one-member panel was accepted"
    assert "panel" in proc.stderr.lower()
