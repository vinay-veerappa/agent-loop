"""
report.py
=========
The `report` command: reads ledger.jsonl and learning_feedback.jsonl
and prints a summary of the loop's behavior.

Usage:
    agent-loop --mode report
    agent-loop --mode report --report-last 20

Prints:
1. Cost per ticket (input, output, cache-read, total USD)
2. Rounds per ticket (distribution)
3. Gate-failure distribution (which rung catches the most)
4. Per-reviewer marginal value (upheld findings the *other* reviewer missed)
5. Arbiter calibration (correlation between upheld count and rounds to converge)
6. Per-ticket verdict distribution
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _ledger_path(repo: Path) -> Path:
    return repo / "logs" / "agent_loop" / "ledger.jsonl"


def _feedback_path(repo: Path) -> Path:
    return repo / "logs" / "agent_loop" / "learning_feedback.jsonl"


def load_ledger(repo: Path) -> List[Dict[str, Any]]:
    path = _ledger_path(repo)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def load_feedback(repo: Path) -> List[Dict[str, Any]]:
    path = _feedback_path(repo)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def run_report(repo: Path, last_n: int = 0) -> int:
    ledger = load_ledger(repo)
    feedback = load_feedback(repo)

    if not ledger and not feedback:
        print("No data found. Run some tickets first.")
        return 1

    if last_n > 0:
        ledger = ledger[-last_n:]

    print("=" * 60)
    print("AGENT LOOP REPORT")
    print("=" * 60)

    _print_verdict_distribution(ledger)
    _print_cost_summary(ledger)
    _print_rounds_distribution(ledger)
    _print_gate_failures(ledger)
    _print_reviewer_marginal_value(feedback)
    _print_arbiter_calibration(ledger, feedback)

    print("=" * 60)
    return 0


def _print_verdict_distribution(ledger: List[Dict[str, Any]]) -> None:
    verdicts = Counter(e.get("verdict", "?") for e in ledger)
    total = len(ledger)
    print(f"\n--- Verdict distribution ({total} tickets) ---")
    for verdict, count in verdicts.most_common():
        pct = 100 * count / total if total else 0
        print(f"  {verdict:<25} {count:>3} ({pct:.0f}%)")


def _print_cost_summary(ledger: List[Dict[str, Any]]) -> None:
    costs = [e.get("cost_usd", 0.0) for e in ledger if "cost_usd" in e]
    if not costs:
        return
    print(f"\n--- Cost summary ---")
    print(f"  Total:   ${sum(costs):.4f}")
    print(f"  Average: ${statistics.mean(costs):.4f}")
    print(f"  Median:  ${statistics.median(costs):.4f}")
    print(f"  Max:     ${max(costs):.4f}")
    # Cost by verdict
    by_verdict: Dict[str, List[float]] = defaultdict(list)
    for e in ledger:
        v = e.get("verdict", "?")
        c = e.get("cost_usd", 0.0)
        by_verdict[v].append(c)
    print(f"  By verdict:")
    for v in sorted(by_verdict):
        cs = by_verdict[v]
        print(f"    {v:<25} avg=${statistics.mean(cs):.4f} n={len(cs)}")


def _print_rounds_distribution(ledger: List[Dict[str, Any]]) -> None:
    rounds = [e.get("rounds", 0) for e in ledger if "rounds" in e]
    if not rounds:
        return
    print(f"\n--- Rounds distribution ---")
    print(f"  Average: {statistics.mean(rounds):.1f}")
    print(f"  Median:  {statistics.median(rounds):.0f}")
    print(f"  Max:     {max(rounds)}")
    rd = Counter(rounds)
    for r in sorted(rd):
        print(f"  {r} rounds: {rd[r]} ticket(s)")


def _print_gate_failures(ledger: List[Dict[str, Any]]) -> None:
    # Gate failures are recorded structurally in the "gate" field of ledger
    # entries. Legacy entries without this field are unmeasurable and are excluded
    # from the distribution; that exclusion is reported explicitly.
    gate_counts: Counter[str] = Counter()
    unmeasurable = 0
    for e in ledger:
        gate = e.get("gate")
        if gate is None:
            unmeasurable += 1
            continue
        if isinstance(gate, list):
            # A ticket may have been blocked by multiple distinct gates across
            # rounds. Count each distinct gate once per ticket.
            for g in gate:
                gate_counts[g] += 1
        else:
            gate_counts[str(gate)] += 1
    if gate_counts:
        print(f"\n--- Gate-failure distribution ---")
        for kw, count in gate_counts.most_common():
            print(f"  {kw:<15} {count} ticket(s)")
    if unmeasurable:
        print(f"\n--- Unmeasurable legacy entries (no gate field) ---")
        print(f"  excluded from gate-failure distribution: {unmeasurable} ticket(s)")


def _same_finding(a: str, b: str) -> bool:
    """Return True if two finding texts describe the same finding.

    Normalises each text by lowercasing, dropping non-letters/non-spaces,
    collapsing whitespace, and comparing the overlap of the resulting
    word sets.  The findings are considered the same when they share at
    least two words and the shared words are a strict majority of the
    smaller word set.
    """

    def _words(text: str) -> set[str]:
        cleaned = "".join(
            ch if ch.isalpha() or ch == " " else "" for ch in text.lower()
        )
        return set(cleaned.split())

    a_words = _words(a)
    b_words = _words(b)
    if not a_words or not b_words:
        return False

    shared = a_words & b_words
    if len(shared) < 2:
        return False

    return len(shared) / min(len(a_words), len(b_words)) > 0.5


def _print_reviewer_marginal_value(feedback: List[Dict[str, Any]]) -> None:
    if not feedback:
        return

    # Group by ticket+round, then for each group, find which reviewers
    # had UPHELD findings that the other reviewer didn't raise
    by_ticket_round: Dict[Tuple[str, int], List[Dict[str, Any]]] = defaultdict(list)
    for entry in feedback:
        key = (entry.get("ticket", "?"), entry.get("round", 0))
        by_ticket_round[key].append(entry)

    # For each ticket+round, compute per-reviewer marginal value:
    # upheld findings by reviewer A that reviewer B didn't also raise
    reviewer_upheld: Dict[str, int] = defaultdict(int)
    reviewer_total: Dict[str, int] = defaultdict(int)

    for (ticket, rnd), entries in by_ticket_round.items():
        # Get all UPHELD findings per reviewer
        upheld_by_reviewer: Dict[str, List[str]] = defaultdict(list)
        all_findings_by_reviewer: Dict[str, List[str]] = defaultdict(list)
        for e in entries:
            reviewer = e.get("reviewer", "?")
            finding = e.get("finding", "")
            ruling = e.get("ruling", "")
            all_findings_by_reviewer[reviewer].append(finding)
            if ruling == "UPHELD":
                upheld_by_reviewer[reviewer].append(finding)

        # For each reviewer, count upheld findings the other didn't raise
        reviewers = list(all_findings_by_reviewer.keys())
        for r in reviewers:
            reviewer_total[r] += len(all_findings_by_reviewer[r])
            other_reviewers = [x for x in reviewers if x != r]
            for finding in upheld_by_reviewer[r]:
                # Did any other reviewer raise the same finding?
                raised_by_other = any(
                    _same_finding(finding, other_finding)
                    for other in other_reviewers
                    for other_finding in all_findings_by_reviewer[other]
                )
                if not raised_by_other:
                    reviewer_upheld[r] += 1

    if reviewer_total:
        print(f"\n--- Per-reviewer marginal value ---")
        print(f"  (upheld findings the *other* reviewer missed)")
        for r in sorted(reviewer_total):
            upheld = reviewer_upheld.get(r, 0)
            total = reviewer_total[r]
            pct = 100 * upheld / total if total else 0
            print(f"  {r:<25} {upheld}/{total} ({pct:.0f}% unique-upheld)")


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Return the Pearson correlation coefficient using population statistics.

    Returns 0.0 when there are fewer than two points, mismatched lengths, or
    zero variance on either side.
    """
    n = len(xs)
    if n < 2 or len(ys) != n:
        return 0.0

    mean_x = statistics.mean(xs)
    mean_y = statistics.mean(ys)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / n
    std_x = statistics.pstdev(xs)
    std_y = statistics.pstdev(ys)
    denom = std_x * std_y

    return cov / denom if denom else 0.0


def _print_arbiter_calibration(
    ledger: List[Dict[str, Any]],
    feedback: List[Dict[str, Any]],
) -> None:
    if not feedback or not ledger:
        return

    # Count upheld findings per ticket
    upheld_per_ticket: Dict[str, int] = defaultdict(int)
    for e in feedback:
        if e.get("ruling") == "UPHELD":
            upheld_per_ticket[e.get("ticket", "?")] += 1

    # Get rounds per ticket from ledger
    rounds_per_ticket: Dict[str, int] = {}
    for e in ledger:
        ticket = e.get("ticket", "?")
        rounds = e.get("rounds", 0)
        if rounds:
            rounds_per_ticket[ticket] = rounds

    # Compute correlation between upheld count and rounds
    pairs: List[Tuple[int, int]] = []
    for ticket, upheld in upheld_per_ticket.items():
        rounds = rounds_per_ticket.get(ticket)
        if rounds and rounds > 0:
            pairs.append((upheld, rounds))

    if len(pairs) < 3:
        print(f"\n--- Arbiter calibration ---")
        print(f"  (need >= 3 tickets with upheld findings; have {len(pairs)})")
        return

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    n = len(pairs)
    corr = _pearson(xs, ys)

    print(f"\n--- Arbiter calibration ---")
    print(f"  Correlation (upheld_count vs rounds_to_converge): {corr:.2f}")
    if corr < -0.3:
        print(f"  Interpretation: arbiter is filtering real findings (more upheld = fewer rounds)")
    elif corr > 0.3:
        print(f"  Interpretation: arbiter is upholding noise (more upheld = MORE rounds)")
    else:
        print(f"  Interpretation: no strong signal (arbiter rulings don't predict convergence)")
    print(f"  Data points: {n} tickets")