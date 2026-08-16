"""
Acceptance tests for T-INV3 and T-INV4.

T-INV3 (N8): failed_gate_names excludes non-gate stages ("implement", "review").
A provider timeout in the implement stage is not a gate failure -- it is an
outage. The ledger previously labeled it as a gate failure.

T-INV4 (N4): the thrashing detector counts UPHELD findings only, not all
blocking findings filed by reviewers. A reviewer filing repeated MAJORs the
arbiter consistently REJECTS should not trigger NOT_CONVERGING.
"""
from agent_loop.loop import failed_gate_names
from agent_loop.arbiter import thrashing


# T-INV3: failed_gate_names excludes non-gate stages

def test_inv3_excludes_implement_stage():
    """An implement-stage failure is not a gate failure."""
    rounds = [
        {"stage": "implement", "ok": False, "summary": "provider timeout"},
        {"stage": "compile", "ok": False, "summary": "build failed"},
    ]
    gates = failed_gate_names(rounds)
    assert "compile" in gates
    assert "implement" not in gates, "implement is a stage, not a gate"


def test_inv3_excludes_review_stage():
    """A review-stage failure is not a gate failure."""
    rounds = [
        {"stage": "review", "ok": False, "summary": "panel unreachable"},
    ]
    gates = failed_gate_names(rounds)
    assert gates == [], "review is a stage, not a gate"


def test_inv3_includes_real_gates():
    """Real mechanical gates are included."""
    rounds = [
        {"stage": "static", "ok": False, "summary": "bad indent"},
        {"stage": "test", "ok": False, "summary": "regression"},
    ]
    gates = failed_gate_names(rounds)
    assert "static" in gates
    assert "test" in gates


def test_inv3_empty_rounds():
    """Empty rounds produce no gate names."""
    assert failed_gate_names([]) == []


# T-INV4: thrashing detector counts UPHELD only

def test_inv4_no_thrashing_when_upheld_count_is_falling():
    """Thrashing should not fire when upheld findings are decreasing."""
    # Round 1: 3 upheld, round 2: 1 upheld, round 3: 0 upheld -- converging.
    history = [(3, {"a", "b", "c"}), (1, {"a"}), (0, set())]
    assert thrashing(history) is None


def test_inv4_thrashing_fires_when_upheld_not_converging():
    """Thrashing fires when upheld findings don't overlap and don't decrease."""
    # 3 rounds, no overlap, count not falling.
    history = [(3, {"a", "b", "c"}), (3, {"d", "e", "f"}), (3, {"g", "h", "i"})]
    result = thrashing(history)
    assert result is not None
    assert "no convergence" in result


def test_inv4_no_thrashing_with_overlapping_findings():
    """Thrashing should not fire when findings overlap between rounds."""
    history = [(3, {"a", "b", "c"}), (3, {"a", "b", "d"}), (3, {"a", "b", "e"})]
    assert thrashing(history) is None


def test_inv4_no_thrashing_with_fewer_than_min_rounds():
    """Thrashing requires at least min_rounds of history."""
    history = [(3, {"a", "b", "c"}), (3, {"d", "e", "f"})]
    assert thrashing(history) is None