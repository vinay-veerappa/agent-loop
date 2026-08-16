"""
Acceptance test for Wave 4.1: evidence ledger per ticket (C-2).

The ledger used to record only "the run finished" (ticket, verdict, applied,
rounds, cost). Now it also records what was PROVEN: the acceptance criteria,
the gate that verified the patch, and the token usage. This turns the ledger
from a completion record into an evidence record.
"""
from agent_loop.loop import terminal_ledger_record, PROMOTABLE


def test_evidence_ledger_promotable_has_evidence():
    """A promotable ticket's ledger record carries evidence.

    The evidence includes the gate ladder (static, compile, test, lock-scope)
    from the round that cleared every gate — the MECHANICAL evidence that the
    patch compiles and passes tests, not the panel's opinion.
    """
    result = {
        "final_verdict": "APPROVE",
        "applied": True,
        "rounds": [
            {"round": 1, "stage": "compile", "ok": False, "summary": "build failed"},
            {"round": 2, "stage": "review", "ok": True, "summary": "APPROVE [glm=APPROVE(0), ds=APPROVE(0)]",
             "impl_input_tokens": 5000, "impl_output_tokens": 2000,
             "reviewer_input_tokens": 8000, "reviewer_output_tokens": 1000,
             "gate_ladder": [
                 {"gate": "static", "ok": True, "summary": "2 region(s) checked"},
                 {"gate": "compile", "ok": True, "summary": "build ok"},
                 {"gate": "test", "ok": True, "summary": "676 passed, 34 skipped"},
                 {"gate": "lock-scope", "ok": True, "summary": "no risk calls in locks"},
             ]},
        ],
        "cost_usd": 0.0034,
        "exported_round": 2,
    }
    record = terminal_ledger_record("T1", result)
    assert record["ticket"] == "T1"
    assert record["verdict"] == "APPROVE"
    assert record["applied"] is True
    assert "evidence" in record
    ev = record["evidence"]
    assert ev["verdict"] == "APPROVE"
    assert ev["exported_round"] == 2
    # The evidence records the gate ladder, not the review stage.
    assert "gate_ladder" in ev
    assert len(ev["gate_ladder"]) == 4
    assert ev["gate_ladder"][0]["gate"] == "static"
    assert ev["gate_ladder"][2]["gate"] == "test"
    assert ev["gate_ladder"][2]["summary"] == "676 passed, 34 skipped"
    assert "tokens" in ev
    assert ev["tokens"]["impl_input_tokens"] == 5000


def test_evidence_ledger_failed_has_block_evidence():
    """A failed ticket's ledger record carries what blocked it."""
    result = {
        "final_verdict": "MAX_ROUNDS_EXHAUSTED",
        "applied": False,
        "rounds": [
            {"round": 1, "stage": "compile", "ok": False, "summary": "build failed"},
            {"round": 2, "stage": "test", "ok": False, "summary": "1 regression"},
        ],
        "cost_usd": 0.001,
    }
    record = terminal_ledger_record("T2", result)
    assert record["verdict"] == "MAX_ROUNDS_EXHAUSTED"
    assert record["applied"] is False
    assert "evidence" in record
    ev = record["evidence"]
    assert ev["blocked_by"] == "test"
    assert "regression" in ev["block_summary"]


def test_evidence_ledger_no_evidence_for_non_promotable_no_gate():
    """A ticket with no gate failure and no promotable verdict has no evidence.

    "implement" is a stage, not a gate (N8). A provider timeout in the
    implement stage is an outage, not a gate failure, so there is no gate
    evidence to record.
    """
    result = {
        "final_verdict": "IMPLEMENTER_UNREACHABLE",
        "applied": False,
        "rounds": [
            {"round": 1, "stage": "implement", "ok": False, "summary": "provider timeout"},
        ],
        "cost_usd": 0.0,
    }
    record = terminal_ledger_record("T3", result)
    assert record["verdict"] == "IMPLEMENTER_UNREACHABLE"
    assert "gate" not in record
    # No gate evidence (implement is not a gate).
    assert "evidence" not in record or record.get("evidence", {}) == {}