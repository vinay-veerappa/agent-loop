"""
Acceptance tests for the CLI exit code.

P1-4b: an ARBITER_SHIP that a human signed off must exit 0, not 1.

And the defect found in review: the exit code was `any(...)`, so a run of
several tickets exited 0 when ONE of them produced a candidate and the rest
failed. Every caller -- a human, a shell script, CI -- reads that as success.

These used to be static greps of cli.py's source, which cannot tell whether the
logic works, only whether a string appears in the file.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop.cli import main
from agent_loop.profiles import Profile, register


PROFILE = Profile(
    name="test-cli-exit",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    implementer_rules="t", reviewer_priorities="t",
)
register(PROFILE)


def _tickets_file(tmp_path: Path, ids) -> Path:
    path = tmp_path / "tickets.json"
    path.write_text(json.dumps({"tickets": [
        {
            "id": tid, "title": f"ticket {tid}", "defect": "d", "spec": "s",
            "regions": [{"id": "R1", "file": "src/x.py", "anchor": "def x"}],
        }
        for tid in ids
    ]}), encoding="utf-8")
    return path


def _run(tmp_path, ids, verdicts, extra_argv=()):
    """Run main() with run_ticket stubbed to return the given verdicts."""
    path = _tickets_file(tmp_path, ids)
    by_id = dict(zip(ids, verdicts))

    def fake_run_ticket(repo, ticket, *a, **kw):
        return {
            "ticket": ticket["id"],
            "final_verdict": by_id[ticket["id"]],
            "applied": False,
            "rounds": [],
            "cost_usd": 0.0,
        }

    with patch("agent_loop.cli.run_ticket", side_effect=fake_run_ticket):
        return main([
            "--profile", "test-cli-exit", "--tickets", str(path), *extra_argv,
        ])


def test_arbiter_ship_exits_zero(tmp_path):
    assert _run(tmp_path, ["T1"], ["ARBITER_SHIP"]) == 0


def test_approve_exits_zero(tmp_path):
    assert _run(tmp_path, ["T1"], ["APPROVE"]) == 0


def test_quorum_partial_exits_zero(tmp_path):
    """A quorum-only approval still produced a promotable candidate."""
    assert _run(tmp_path, ["T1"], ["APPROVE_PARTIAL"]) == 0


def test_failure_exits_nonzero(tmp_path):
    assert _run(tmp_path, ["T1"], ["NOT_CONVERGING"]) == 1


def test_one_success_among_failures_exits_nonzero(tmp_path):
    """`any` made this exit 0 -- three failed tickets reported as success."""
    code = _run(
        tmp_path,
        ["T1", "T2", "T3", "T4"],
        ["APPROVE", "NOT_CONVERGING", "ESCALATED", "TICKET_REJECTED"],
    )
    assert code == 1


def test_all_success_exits_zero(tmp_path):
    assert _run(tmp_path, ["T1", "T2"], ["APPROVE", "ARBITER_SHIP"]) == 0


def test_unknown_ticket_id_is_an_error(tmp_path):
    """Selecting a ticket that does not exist ran nothing and exited 1, which is
    indistinguishable from "the ticket ran and failed"."""
    path = _tickets_file(tmp_path, ["T1"])
    with patch("agent_loop.cli.run_ticket") as spy:
        code = main([
            "--profile", "test-cli-exit", "--tickets", str(path), "--ticket", "T99",
        ])
    assert code == 2
    assert spy.call_count == 0
