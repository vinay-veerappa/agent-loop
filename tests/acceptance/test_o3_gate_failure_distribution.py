"""
BACKLOG O3: `--mode report`'s gate-failure distribution is fabricated from prose
and double-counts.

The report must count structurally-recorded failing gate names, not keyword-scan
the `detail` field. Old ledger entries without the new field must be excluded
from the distribution visibly, not silently reported as zero.
"""
import json
from pathlib import Path

import pytest

from agent_loop.report import run_report


def _write_ledger(tmp_path: Path, entries):
    ledger = tmp_path / "logs" / "agent_loop" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def test_gate_failure_distribution_counts_structural_gate_field(capsys, tmp_path):
    """A protected-path rejection whose detail mentions a *Tests.cs file must
    not be counted as both 'protected' and 'test'. Only the structural gate
    field should be counted, and only once."""
    _write_ledger(
        tmp_path,
        [
            {
                "ticket": "T1",
                "verdict": "TICKET_REJECTED",
                "detail": "touches src/FooTests.cs",
                "gate": "protected",
            },
            {
                "ticket": "T2",
                "verdict": "TICKET_REJECTED",
                "detail": "touches src/BarTests.cs",
                "gate": "protected",
            },
        ],
    )
    code = run_report(tmp_path)
    assert code == 0
    out = capsys.readouterr().out
    assert "Gate-failure distribution" in out
    assert "protected" in out
    assert out.count("test") == 0 or "Gate-failure distribution" not in out.split("test")[0]


def test_gate_failure_distribution_excludes_legacy_entries(capsys, tmp_path):
    """Ledger entries written before the gate field existed must not be
    silently counted as zero; they must be excluded from the distribution and
    that exclusion must be visible."""
    _write_ledger(
        tmp_path,
        [
            {"ticket": "T1", "verdict": "TICKET_REJECTED", "detail": "touches X"},
            {"ticket": "T2", "verdict": "TICKET_REJECTED", "detail": "touches Y"},
        ],
    )
    code = run_report(tmp_path)
    assert code == 0
    out = capsys.readouterr().out
    # No gate distribution should be printed when every entry is unmeasurable.
    assert "Gate-failure distribution" not in out
    # The exclusion must be visible.
    assert "unmeasurable" in out.lower() or "legacy" in out.lower() or "excluded" in out.lower()


def test_gate_failure_distribution_counts_each_gate_once(capsys, tmp_path):
    """One ledger entry contributes at most one count, even if its detail
    contains multiple gate keywords."""
    _write_ledger(
        tmp_path,
        [
            {
                "ticket": "T1",
                "verdict": "TICKET_REJECTED",
                "detail": "static compile test lock-scope protected expect_green all here",
                "gate": "static",
            },
        ],
    )
    code = run_report(tmp_path)
    assert code == 0
    out = capsys.readouterr().out
    assert "Gate-failure distribution" in out
    # Only 'static' should appear; the others must not be counted from detail.
    assert "static" in out
    assert "compile" not in out
    assert "lock-scope" not in out
    assert "expect_green" not in out


# ---------------------------------------------------------------------------
# The WRITER half. The generated tests above cover only the report reader: they
# hand-build ledger entries, so deleting the write site in loop.py entirely left
# every one of them green. Verified by mutation.
# ---------------------------------------------------------------------------
from agent_loop.loop import failed_gate_names, append_ledger, run_ticket
from agent_loop import profiles as _profiles


def _round(stage, ok):
    return {"round": 1, "stage": stage, "ok": ok, "summary": "", "detail": ""}


def test_failed_gate_names_ignores_rounds_that_passed():
    assert failed_gate_names([_round("test", True), _round("compile", False)]) == ["compile"]


def test_failed_gate_names_dedupes_and_sorts():
    rounds = [_round("test", False), _round("compile", False), _round("test", False)]
    assert failed_gate_names(rounds) == ["compile", "test"]


def test_failed_gate_names_is_empty_when_nothing_failed():
    assert failed_gate_names([_round("test", True)]) == []
    assert failed_gate_names([]) == []


def test_failed_gate_names_survives_a_malformed_round():
    """A ledger is append-only and long-lived. Crashing here would lose a
    completed ticket's outcome over one malformed round record."""
    assert failed_gate_names([{"ok": False}, _round("test", False)]) == ["test"]


def test_protected_rejection_records_its_gate(tmp_path):
    """run_ticket's protected-path refusal must write gate=protected. It returns
    before any worktree or model call, so this exercises the real write site."""
    prof = _profiles.Profile(
        name="o3-writer-probe",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        protected=("tests/*",),
        implementer_rules="t", reviewer_priorities="t",
    )
    _profiles.register(prof)
    ticket = {"id": "T-PROT", "regions": [{"file": "tests/test_thing.py"}]}
    result = run_ticket(tmp_path, ticket, prof, "impl", ["r1"])
    assert result["final_verdict"] == "TICKET_REJECTED"
    entries = [
        json.loads(ln)
        for ln in (tmp_path / "logs" / "agent_loop" / "ledger.jsonl").read_text(
            encoding="utf-8").splitlines() if ln.strip()
    ]
    assert entries[-1]["gate"] == "protected", entries[-1]
    assert entries[-1]["detail"], "the detail field must be preserved for other readers"


def _result(rounds, verdict="MAX_ROUNDS"):
    return {"final_verdict": verdict, "applied": False, "rounds": rounds, "cost_usd": 0.0}


def test_terminal_record_carries_a_single_gate_as_a_string():
    from agent_loop.loop import terminal_ledger_record
    rec = terminal_ledger_record("T1", _result([_round("test", False)]))
    assert rec["gate"] == "test"


def test_terminal_record_carries_several_gates_as_a_list():
    from agent_loop.loop import terminal_ledger_record
    rec = terminal_ledger_record(
        "T1", _result([_round("test", False), _round("compile", False)]))
    assert rec["gate"] == ["compile", "test"]


def test_terminal_record_omits_gate_when_nothing_failed():
    """Omitted, not empty. The report tells 'no gate failure' apart from
    'written before this field existed', and a falsy value collapses them into
    the legacy bucket -- reporting a clean ticket as unmeasurable."""
    from agent_loop.loop import terminal_ledger_record
    rec = terminal_ledger_record("T1", _result([_round("test", True)], "APPROVE"))
    assert "gate" not in rec
    assert rec["verdict"] == "APPROVE"
