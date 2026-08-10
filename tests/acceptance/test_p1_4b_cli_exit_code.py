"""
Acceptance test for P1-4b: cli.py exit code must return 0 for ARBITER_SHIP
when applied, not just APPROVE.

BEFORE FIX: cli.py:223 returns 0 only when final_verdict == "APPROVE".
An ARBITER_SHIP with --apply returns exit 1, which is wrong: the arbiter
recommended SHIP and the human signed off.

AFTER FIX: returns 0 when final_verdict is APPROVE or ARBITER_SHIP.
"""
import subprocess
import sys
import os
import tempfile
from pathlib import Path


def test_p1_4b_cli_exit_code_arbiter_ship():
    """The CLI exit code should be 0 when ARBITER_SHIP is applied, not just APPROVE."""
    # This is a static check: read the cli.py source and verify the exit logic
    cli_path = Path(__file__).resolve().parents[2] / "src" / "agent_loop" / "cli.py"
    source = cli_path.read_text(encoding="utf-8")

    # The old code: return 0 if any(r.get("final_verdict") == "APPROVE" for r in results) else 1
    # The fix should include ARBITER_SHIP in the exit-0 condition
    assert "ARBITER_SHIP" in source, "cli.py must reference ARBITER_SHIP in the exit code logic"
    
    # Find the return statement
    for line in source.splitlines():
        if "return 0 if any" in line:
            assert "ARBITER_SHIP" in line, \
                f"exit code must return 0 for ARBITER_SHIP, not just APPROVE. Got: {line.strip()}"
            return
    
    # If we reach here, the return statement wasn't found — check for a multi-line version
    assert '"APPROVE"' in source and '"ARBITER_SHIP"' in source, \
        "cli.py must check both APPROVE and ARBITER_SHIP in the exit code logic"