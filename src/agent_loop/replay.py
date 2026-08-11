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
from ._io import read_text_verbatim
from .loop import review_panel, parse_blocks


def _review_prompt_path(ticket_dir: Path, impl_file: Path) -> Path:
    """The recorded review prompt that pairs with an `r{N}_impl_raw.txt` file.

    Derived from the implementer filename rather than the loop index so the
    pairing survives a corpus whose rounds are not 1..N contiguous (a resumed or
    partially pruned run).
    """
    stem = impl_file.name.replace("_impl_raw.txt", "")
    return ticket_dir / f"{stem}_review_prompt.md"


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

    # A replay is only a measurement if the prompt is held constant. A corpus
    # recorded before the loop started saving the rendered prompt cannot support
    # one, so refuse rather than approximate: this function used to rebuild a
    # prompt from the truncated implement prompt plus the truncated implementer
    # output, which meant a "flip" reported the difference between two prompts
    # and not the effect of the change under test. Refusing costs nothing;
    # a plausible-looking non-measurement is what does the damage.
    missing = [
        f.name for f in impl_files[:max_rounds]
        if not _review_prompt_path(ticket_dir, f).exists()
    ]
    if missing:
        return {
            "ticket": ticket_id,
            "error": (
                "no recorded review prompt for "
                + ", ".join(missing)
                + f" in {ticket_dir}. This corpus predates prompt recording, so a "
                "replay cannot hold the prompt constant and any verdict flip "
                "would be meaningless. Re-run the ticket to record "
                "r{N}_review_prompt.md, then replay it."
            ),
        }

    # Replay artifacts go in a subdirectory. `art = ticket_dir` handed the corpus
    # to review_panel, which writes r{N}_review_{model}.txt -- so a replay
    # overwrote the very recording it was measuring against.
    art_root = ticket_dir / "replay"
    art_root.mkdir(parents=True, exist_ok=True)

    # Replay each round
    replay_rounds: List[Dict[str, Any]] = []
    final_verdict = "MAX_ROUNDS_EXHAUSTED"
    convergence: List[Tuple[int, set]] = []

    for rnd_idx, impl_file in enumerate(impl_files[:max_rounds], 1):
        raw_impl = impl_file.read_text(encoding="utf-8")
        blocks, notes = parse_blocks(raw_impl)

        # The recorded prompt, verbatim. Read as bytes-preserving text so a CRLF
        # corpus is re-sent exactly as it was recorded: re-encoding the prompt
        # would itself be a prompt change.
        review_prompt = read_text_verbatim(_review_prompt_path(ticket_dir, impl_file))

        panel = review_panel(
            reviewers,
            review_prompt,
            profile.reviewer_system,
            art_root,
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

        # Run the arbiter. If the corpus recorded the arbiter's rendered prompt,
        # re-send it verbatim for the same reason the review prompt is re-sent:
        # the ticket, the diff and the round history are not recoverable here, so
        # a rebuilt prompt ("replay"/"replay"/"" as ticket, gate summary and
        # diff) is not the prompt that produced the recorded verdict.
        if arbiter_model and all_findings:
            arb_prompt_path = ticket_dir / f"r{rnd_idx}_arbiter_prompt.md"
            arb_override = (
                read_text_verbatim(arb_prompt_path) if arb_prompt_path.exists() else ""
            )
            adj = arbiter_mod.adjudicate(
                arbiter_model,
                {"id": ticket_id, "title": "replay", "defect": "replay", "spec": "replay"},
                all_findings,
                "replay",
                "",  # no diff available in replay
                settled=profile.settled,
                round_history=_history_note(convergence),
                prompt_override=arb_override,
            )
            (art_root / f"r{rnd_idx}_arbiter.txt").write_text(
                adj.raw or adj.error, encoding="utf-8"
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
        # An errored ticket must not print as "same": it was not compared at all,
        # and "same" reads as a passing measurement. This is the same conflation
        # O2 is about, in the display layer.
        if "error" in result:
            print(f"  {result.get('ticket', '?'):<10} [ERROR] {result['error']}")
            continue
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