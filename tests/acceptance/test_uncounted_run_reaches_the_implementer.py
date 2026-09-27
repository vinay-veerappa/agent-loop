"""CF-56: when the test runner dies before its summary, the implementer must
be shown how it died.

Measured on the tvDownloadOHLC T26 ticket (C#, 2026-09-27): round 2's
patch compiled, six acceptance tests printed `[FAIL] ...: FormatException:
bad kind`, then a P/Invoke call raised `System.AccessViolationException`,
which .NET cannot catch, and the process died before `RESULTS:`. The gate
put that output in `GateResult.detail` and set `feedback` to one sentence
saying no conclusion could be drawn. The implementer is handed `feedback or
summary` -- the CF-18 shape -- so the model was told its patch had an
unknown effect while the log held both of its defects, by name, with the
crashing frame.
"""
from __future__ import annotations

import sys
from pathlib import Path

from agent_loop.gates import check_tests

CRASH = (
    "[PASS] json_reads_every_value_kind\n"
    "[FAIL] every_intent_kind_reads_from_its_wire_form: FormatException: bad kind\n"
    "Fatal error. System.AccessViolationException: Attempted to read or write protected memory.\n"
    "   at Spine.SpineLibrary.Create(System.String)\n"
)


def _cmd(tmp_path: Path, body: str, code: int) -> str:
    script = tmp_path / "runner.py"
    script.write_text(
        "import sys\nsys.stdout.write({!r})\nsys.stdout.flush()\nsys.exit({})\n".format(body, code),
        encoding="utf-8",
    )
    return '"{}" "{}"'.format(sys.executable, script)


def test_a_crash_before_the_summary_reaches_the_implementer(tmp_path: Path) -> None:
    gate, outcome = check_tests(_cmd(tmp_path, CRASH, 3), tmp_path, baseline=set())
    assert not gate.ok
    assert not outcome.counted
    # What the loop hands the model next round (loop.py: feedback or summary).
    handed = gate.feedback or gate.summary
    assert "System.AccessViolationException" in handed
    assert "SpineLibrary.Create" in handed
    assert "FormatException: bad kind" in handed


def test_only_the_tail_of_a_long_output_is_handed_on(tmp_path: Path) -> None:
    body = ("x" * 100 + "\n") * 500 + CRASH
    gate, _ = check_tests(_cmd(tmp_path, body, 3), tmp_path, baseline=set())
    handed = gate.feedback or gate.summary
    assert "System.AccessViolationException" in handed
    assert len(handed) < 6000, len(handed)


def test_a_counted_run_is_unchanged(tmp_path: Path) -> None:
    # Negative control: a run WITH a summary still takes the normal path.
    body = "[PASS] a\nRESULTS: 1 passed, 0 failed\n"
    gate, outcome = check_tests(_cmd(tmp_path, body, 0), tmp_path, baseline=set())
    assert outcome.counted
    assert "AccessViolation" not in (gate.feedback or "")
