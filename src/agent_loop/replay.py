"""
replay.py
=========
Replay corpus: re-runs the panel and arbiter against recorded implementer
outputs, allowing prompt changes to be regression-tested.

The loop already writes `r{N}_review_*.txt`, `r{N}_arbiter.txt`,
`r{N}_impl_raw.txt` to disk. A replay command loads these, re-runs the
current panel (with current prompts) against the recorded implementer
output, re-runs the arbiter, and compares the new verdict to the
recorded verdict.

Usage:
    agent-loop --mode replay --replay-dir logs/agent_loop/T2
    agent-loop --mode replay --replay-dir logs/agent_loop/T3

The replay corpus turns prompt changes from vibes into measurements.
Freeze a dozen real tickets with known outcomes. Change the arbiter
prompt. Run the replay. See which tickets flip verdict.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import arbiter as arbiter_mod
from . import profiles
from .loop import review_panel, PanelResult, Finding, parse_blocks, RoundRecord
from .providers import Completion, ProviderError, chat


def run_replay(
    repo: Path,
    ticket_dir: Path,
    profile: profiles.Profile,
    reviewers: List[str],
    arbiter_model: str,
    max_rounds: int = 4,
) -> Dict[str, Any]:
    """Replay a recorded ticket against the current panel + arbiter.

    Reads the recorded implementer outputs from `r{N}_impl_raw.txt` files
    in `ticket_dir`, re-runs the panel and arbiter with the current
    prompts, and compares the new verdict to the recorded verdict.

    Returns a dict with:
        - recorded_verdict: the original verdict from result.json
        - replayed_verdict: the new verdict from this replay
        - flipped: whether the verdict changed
        - rounds: list of per-round replay results
    """
    # Load the recorded result
    result_path = ticket_dir / "result.json"
    if not result_path.exists():
        return {"error": f"no result.json in {ticket_dir}"}

    recorded = json.loads(result_path.read_text(encoding="utf-8"))
    recorded_verdict = recorded.get("final_verdict", "?")
    ticket_id = recorded.get("ticket", ticket_dir.name)

    # Find all recorded implementer outputs
    impl_files = sorted(ticket_dir.glob("r*_impl_raw.txt"))
    if not impl_files:
        return {"error": f"no r*_impl_raw.txt files in {ticket_dir}"}

    # Load the ticket spec (if available)
    ticket_spec_path = ticket_dir / "00_implement_prompt.md"
    ticket_spec = ""
    if ticket_spec_path.exists():
        ticket_spec = ticket_spec_path.read_text(encoding="utf-8")

    # Replay each round
    replay_rounds: List[Dict[str, Any]] = []
    final_verdict = "MAX_ROUNDS_EXHAUSTED"
    convergence: List[Tuple[int, set]] = []

    for rnd_idx, impl_file in enumerate(impl_files[:max_rounds], 1):
        raw_impl = impl_file.read_text(encoding="utf-8")
        blocks, notes = parse_blocks(raw_impl)

        # Build the review prompt (same as the loop does)
        # We don't have the original regions, but we can build a minimal
        # review prompt from the ticket spec + the implementer output
        review_prompt = (
            f"# Replay review for ticket {ticket_id} (round {rnd_idx})\n\n"
            f"## Ticket spec\n{ticket_spec[:2000]}\n\n"
            f"## Implementer output\n```\n{raw_impl[:8000]}\n```\n\n"
            f"Review this output: is it correct? Does it close the defect?\n"
        )

        # Run the panel
        art = ticket_dir
        panel = review_panel(
            reviewers,
            review_prompt,
            profile.reviewer_system,
            art,
            rnd_idx,
            deadline_secs=1800,
        )

        all_findings = [f for v in panel.votes if v.counted for f in v.finding_list]
        blocking = [f for f in all_findings if f.blocking]
        convergence.append((len(blocking), {f.signature for f in blocking}))

        panel_verdict = panel.verdict or "INVALID"
        replay_rounds.append({
            "round": rnd_idx,
            "panel_verdict": panel_verdict,
            "findings": len(all_findings),
            "blocking": len(blocking),
        })

        # If panel approves, we're done
        if panel.unanimous_approve:
            final_verdict = "APPROVE"
            break

        # If panel is unreachable, stop
        if not panel.valid:
            final_verdict = "PANEL_UNREACHABLE"
            break

        # Run the arbiter
        if arbiter_model and all_findings:
            adj = arbiter_mod.adjudicate(
                arbiter_model,
                {"id": ticket_id, "title": "replay", "defect": "replay", "spec": "replay"},
                all_findings,
                "replay",
                "",  # no diff available in replay
                settled=profile.settled,
                round_history=_history_note(convergence),
            )
            if adj.ok:
                if adj.recommendation == arbiter_mod.SHIP:
                    final_verdict = "ARBITER_SHIP"
                    break
                if adj.recommendation == arbiter_mod.ESCALATE:
                    final_verdict = "ESCALATED"
                    break

            # Check for thrashing
            stall = arbiter_mod.thrashing(convergence)
            if stall:
                final_verdict = "NOT_CONVERGING"
                break

    flipped = (final_verdict != recorded_verdict)

    return {
        "ticket": ticket_id,
        "recorded_verdict": recorded_verdict,
        "replayed_verdict": final_verdict,
        "flipped": flipped,
        "rounds": replay_rounds,
    }


def _history_note(convergence: List[Tuple[int, set]]) -> str:
    """Build a compact history note for the arbiter."""
    if not convergence:
        return "(first round)"
    parts = []
    for i, (count, sigs) in enumerate(convergence):
        parts.append(f"round {i+1}: {count} blocking findings")
    return "; ".join(parts)


def run_replay_corpus(
    repo: Path,
    corpus_dirs: List[Path],
    profile: profiles.Profile,
    reviewers: List[str],
    arbiter_model: str,
    max_rounds: int = 4,
) -> Dict[str, Any]:
    """Replay an entire corpus of recorded tickets.

    Returns a summary with per-ticket results and aggregate statistics.
    """
    results: List[Dict[str, Any]] = []
    for ticket_dir in corpus_dirs:
        result = run_replay(
            repo,
            ticket_dir,
            profile,
            reviewers,
            arbiter_model,
            max_rounds,
        )
        results.append(result)
        status = "FLIPPED" if result.get("flipped") else "same"
        print(f"  {result.get('ticket', '?'):<10} "
              f"recorded={result.get('recorded_verdict', '?'):<25} "
              f"replayed={result.get('replayed_verdict', '?'):<25} "
              f"[{status}]")

    # Aggregate
    total = len(results)
    flipped = sum(1 for r in results if r.get("flipped"))
    errors = sum(1 for r in results if "error" in r)

    print(f"\n--- Replay corpus summary ---")
    print(f"  Total tickets:  {total}")
    print(f"  Flipped:        {flipped}")
    print(f"  Errors:         {errors}")
    print(f"  Same verdict:   {total - flipped - errors}")

    return {
        "results": results,
        "total": total,
        "flipped": flipped,
        "errors": errors,
    }