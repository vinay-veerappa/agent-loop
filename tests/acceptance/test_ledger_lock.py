"""
Acceptance test for R5-6: append_ledger thread safety.

append_ledger had no lock, so concurrent writes from plan parts or parallel
ticket runs on Windows could interleave and corrupt the JSONL. The fix adds
a per-file threading.Lock (same pattern as save_settled, N2).
"""
import json
import threading
from pathlib import Path

from agent_loop.loop import append_ledger


def test_append_ledger_writes_complete_lines(tmp_path):
    """Each append writes one complete JSON line, not fragments."""
    repo = tmp_path
    records = [
        {"ticket": f"T{i}", "verdict": "APPROVE", "applied": True}
        for i in range(20)
    ]

    # Write 20 records from 4 threads.
    threads = []
    for i in range(4):
        batch = records[i * 5:(i + 1) * 5]

        def write_batch(batch=batch):
            for r in batch:
                append_ledger(repo, r)

        t = threading.Thread(target=write_batch)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # Every line must be valid JSON.
    ledger_path = repo / "logs" / "agent_loop" / "ledger.jsonl"
    lines = ledger_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 20
    for line in lines:
        record = json.loads(line)
        assert "ticket" in record
        assert "ts" in record
        assert record["verdict"] == "APPROVE"


def test_append_ledger_concurrent_no_corruption(tmp_path):
    """Concurrent appends do not produce corrupted or interleaved lines."""
    repo = tmp_path

    # Write 50 records from 10 threads simultaneously.
    def write_5(offset):
        for i in range(5):
            append_ledger(repo, {"ticket": f"T{offset + i}", "verdict": "APPROVE"})

    threads = []
    for i in range(10):
        t = threading.Thread(target=write_5, args=(i * 5,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    ledger_path = repo / "logs" / "agent_loop" / "ledger.jsonl"
    lines = ledger_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 50
    # Every line must parse as JSON.
    for line in lines:
        json.loads(line)