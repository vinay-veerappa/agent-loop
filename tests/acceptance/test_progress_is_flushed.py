"""Progress output must reach a pipe while the run is still going.

Live: a plan run was piped to a log and backgrounded. Python block-buffers a
non-tty stdout, so four rounds' progress lines -- 26 minutes of work -- were
still sitting in the buffer, and the only evidence the run was alive was the
mtime of its artifact files. Anything long enough to background is exactly what
gets piped, so this is the normal case, not the exotic one.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"


def _run(code: str) -> str:
    """Run `code` in a child whose stdout is a PIPE, not a tty."""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, cwd=str(REPO),
        env={**__import__("os").environ, "PYTHONPATH": str(SRC)},
    )
    return proc.stdout


def test_piped_stdout_is_block_buffered_without_the_fix():
    """Pins the PREMISE. If this ever fails, the fix is no longer needed."""
    out = _run("import sys; print(sys.stdout.line_buffering)")
    assert out.strip() == "False"


def test_main_switches_stdout_to_line_buffering():
    out = _run(
        "import sys\n"
        "from agent_loop import cli\n"
        "cli.main(['--mode', 'report'])\n"
        "print('LB', sys.stdout.line_buffering)\n"
    )
    assert "LB True" in out, f"main() left stdout block-buffered: {out[-300:]!r}"
