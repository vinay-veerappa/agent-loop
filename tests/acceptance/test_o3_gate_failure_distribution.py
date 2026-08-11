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


def _gate_section(out: str):
    """The lines of the gate-failure distribution block, and nothing else.

    Assertions about the whole report pass for the wrong reason: every gate name
    also appears in other sections. Scope them.
    """
    lines = out.splitlines()
    for i, ln in enumerate(lines):
        if "Gate-failure distribution" in ln:
            body = []
            for rest in lines[i + 1:]:
                if rest.startswith("---") or (rest.strip() and not rest.startswith("  ")):
                    break
                if rest.strip():
                    body.append(rest)
            return body
    return []


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
    # Scoped to the distribution section. The generated assertion here was
    # `out.count("test") == 0 or "Gate-failure distribution" not in
    # out.split("test")[0]`, which passes whenever "Gate-failure distribution"
    # happens to appear after the first "test" anywhere in the report -- it
    # would not have failed if the keyword-scanning behaviour came back. The
    # reviewer panel caught it; the arbiter rejected the finding.
    section = _gate_section(out)
    assert section, out
    assert any("protected" in ln for ln in section), section
    assert any("2 ticket(s)" in ln for ln in section), section
    # Both entries name a *Tests.cs file in `detail`. Under the old
    # keyword-scan that produced a bogus `test` row alongside `protected`.
    assert not any(ln.strip().startswith("test ") for ln in section), section


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
    # Scoped to the section as well: "compile" and the rest appear elsewhere in
    # a full report, so asserting their absence from the WHOLE output is
    # fragile and can pass for the wrong reason.
    section = _gate_section(out)
    assert section, out
    assert any(ln.strip().startswith("static") for ln in section), section
    for other in ("compile", "lock-scope", "expect_green", "test "):
        assert not any(ln.strip().startswith(other) for ln in section), (other, section)


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


def test_gate_order_is_deterministic_regardless_of_ledger_order(capsys, tmp_path):
    """Counter.most_common() leaves ties in insertion order, so the same data in
    a different ledger order printed a different report. Reviewer finding."""
    entries = [
        {"ticket": "A", "verdict": "X", "gate": "static"},
        {"ticket": "B", "verdict": "X", "gate": "compile"},
        {"ticket": "C", "verdict": "X", "gate": "test"},
    ]
    _write_ledger(tmp_path, entries)
    run_report(tmp_path)
    first = _gate_section(capsys.readouterr().out)
    _write_ledger(tmp_path, list(reversed(entries)))
    run_report(tmp_path)
    second = _gate_section(capsys.readouterr().out)
    assert first == second, (first, second)


def test_review_mode_entries_are_not_counted_as_legacy(capsys, tmp_path):
    """Review mode runs no gate ladder, so its entries have no gate BY DESIGN.
    Counting them as 'written before the field existed' conflates a deliberate
    absence with a legacy one and inflates the excluded count forever."""
    _write_ledger(tmp_path, [
        {"mode": "review", "range": "a..b", "panel_verdict": "APPROVE"},
        {"ticket": "T1", "verdict": "TICKET_REJECTED", "gate": "protected"},
    ])
    run_report(tmp_path)
    out = capsys.readouterr().out
    assert "excluded from gate-failure distribution" not in out, out
    section = _gate_section(out)
    assert any(ln.strip().startswith("protected") for ln in section), section
