"""
review_mode.py
==============
Adversarial review of code that is ALREADY WRITTEN. No implementer, no regions,
no worktree, no apply path.

Why this exists
---------------
`patch` mode's guarantee is that the grader is written by a different party than
the one being graded: gate 0 makes `*Tests.cs` unreachable to the implementer.
Hand-written work has no such guarantee. When one author writes the change AND
its tests, the tests encode exactly the cases that author already thought of,
and the suite goes green for the same reason the bug got written.

That is not hypothetical. `P0-9`'s bracket replication shipped in `51892d54`
with `Math.Abs` where a signed offset was required, so a leader trailing its
stop into profit mirrored onto the LOSING side of the follower's entry. It
survived a green 515-test suite, a clean net48 compile, and a 20/20
falsifiability check -- because every one of those artifacts was authored by
the party that made the mistake. It was found by a human asking a question.

Review mode buys back the adversary. It is deliberately advisory: it reports
and never edits, in the same way `ARBITER_SHIP` never ships.

Usage
-----
    python -m scripts.agent_loop --mode review --review-base HEAD~1
    python -m scripts.agent_loop --mode review --review-base main --review-head HEAD \\
        --review-paths scripts/ninjatrader/addons \\
        --review-intent "P0-9: mirror the leader's protective stop to followers"
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import arbiter, profiles
from .loop import append_ledger, review_panel

# A diff larger than this is a sign the range is wrong (a merge, a vendored
# drop). Reviewing it would burn the whole context on noise and return
# confident nonsense, so refuse and make the operator narrow it.
MAX_DIFF_CHARS = 90_000


class ReviewError(RuntimeError):
    pass


def _git(repo: Path, *args: str, timeout: int = 120) -> str:
    p = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=timeout
    )
    if p.returncode != 0:
        raise ReviewError(f"git {' '.join(args)} failed: {p.stderr.strip()[:300]}")
    return p.stdout


def collect_diff(repo: Path, base: str, head: str, paths: Sequence[str]) -> str:
    args = ["diff", f"{base}..{head}", "--no-color", "-U8"]
    if paths:
        args += ["--", *paths]
    return _git(repo, *args)


def changed_files(repo: Path, base: str, head: str, paths: Sequence[str]) -> List[str]:
    args = ["diff", "--name-only", f"{base}..{head}"]
    if paths:
        args += ["--", *paths]
    return [ln.strip() for ln in _git(repo, *args).splitlines() if ln.strip()]


def commit_subjects(repo: Path, base: str, head: str) -> str:
    return _git(repo, "log", "--oneline", f"{base}..{head}").strip()


def build_prompt(
    title: str,
    intent: str,
    diff: str,
    files: Sequence[str],
    subjects: str,
    profile: profiles.Profile,
    gate_summary: str,
    orchestrator_note: str = "",
) -> str:
    parts = [
        f"# REVIEW: {title}",
        "",
        "## What this change claims to do",
        intent.strip() or "(no intent supplied - infer it from the diff and say so)",
        "",
    ]
    if subjects:
        parts += ["## Commits in range", "```", subjects, "```", ""]
    parts += ["## Files changed", "\n".join(f"- {f}" for f in files), ""]
    if gate_summary:
        parts += [
            "## Mechanical gates (facts - you may not contradict these)",
            gate_summary,
            "",
        ]

    settled = list(profile.settled)
    if orchestrator_note:
        settled.append(orchestrator_note.strip())
    if settled:
        parts += [
            "## SETTLED DECISIONS - AUTHORITATIVE, DO NOT RE-LITIGATE",
            "These were decided by earlier arbitration and SUPERSEDE anything the diff or its "
            "commit messages imply. Do NOT raise a finding that contradicts one.",
            "",
        ] + [f"- {s}" for s in settled] + [""]

    # The single most important instruction in this mode.
    parts += [
        "## READ THIS BEFORE REVIEWING - how this differs from a patch review",
        "",
        "This code is ALREADY WRITTEN AND COMMITTED, and **its tests were written by the same "
        "author as the code**. In this project's `patch` mode the test file is unreachable to "
        "the implementer precisely so the grader is independent; here that guarantee does NOT "
        "hold. Assume the tests encode the cases the author already thought of, and that a green "
        "suite therefore proves less than usual.",
        "",
        "So your highest-value output is NOT restating what the tests already cover. It is:",
        "",
        "1. **A case the tests do not exercise.** Name the input or ordering, and the wrong "
        "   result it produces. Sign errors, inverted comparisons, and boundary conditions that "
        "   the author's own examples all happen to sit on one side of are the target.",
        "2. **A stated invariant the diff breaks** - lock scope, fail-closed on exits, "
        "   reference-vs-id keying, anything in the settled list above.",
        "3. **Behaviour that is correct in the test harness but wrong against the real API.** "
        "   Test stubs are frequently more forgiving than production; a stub that hands out one "
        "   stable id per object will hide a defect that a real broker's re-issued id exposes.",
        "",
        "A finding that says 'this is untested' without naming the failing case is noise. "
        "A finding that names inputs and the wrong output is what this run is for.",
        "",
        "## The change under review (unified diff)",
        "```diff",
        diff,
        "```",
    ]
    return "\n".join(parts)


def run_review(
    repo: Path,
    *,
    base: str,
    head: str = "HEAD",
    paths: Sequence[str] = (),
    profile: profiles.Profile,
    reviewers: Sequence[str],
    arbiter_model: str = "",
    intent: str = "",
    title: str = "",
    gate_summary: str = "",
    orchestrator_note: str = "",
    panel_deadline: int = 1800,
) -> Dict[str, Any]:
    t0 = time.time()
    diff = collect_diff(repo, base, head, paths)
    if not diff.strip():
        raise ReviewError(
            f"{base}..{head} is empty" + (f" for paths {list(paths)}" if paths else "")
        )
    if len(diff) > MAX_DIFF_CHARS:
        raise ReviewError(
            f"diff is {len(diff):,} chars (limit {MAX_DIFF_CHARS:,}). Narrow it with "
            "--review-paths or a tighter --review-base; a review that does not fit in "
            "context returns confident nonsense."
        )

    files = changed_files(repo, base, head, paths)
    subjects = commit_subjects(repo, base, head)
    label = title or f"{base}..{head}"
    slug = "review-" + "".join(c if c.isalnum() or c in "-_" else "_" for c in f"{base}_{head}")
    art = repo / "logs" / "agent_loop" / slug
    art.mkdir(parents=True, exist_ok=True)
    (art / "diff.patch").write_text(diff, encoding="utf-8")

    prompt = build_prompt(
        label, intent, diff, files, subjects, profile, gate_summary, orchestrator_note
    )
    (art / "review_prompt.txt").write_text(prompt, encoding="utf-8")

    print(f"  reviewing {len(files)} file(s), {len(diff):,} chars of diff")
    panel = review_panel(
        reviewers, prompt, profile.reviewer_system, art, rnd=1, deadline_secs=panel_deadline
    )
    for v in panel.votes:
        print(f"    {v.model:<28} {v.status:<12} {v.usage or ''}")

    if not panel.valid:
        dead = ", ".join(v.model for v in panel.unreachable)
        print(f"  PANEL INVALID - unreachable: {dead}")
        print("  An unreachable reviewer is NOT a clean review. Re-run before trusting this.")

    # Reuse the patch loop's adjudication verbatim. A synthetic ticket keeps the
    # arbiter's contract unchanged rather than forking a second prompt that would drift.
    # Findings come from Vote.finding_list, which parse_review already produced with
    # severities attached -- re-parsing the text here would flatten every finding to
    # one severity and silently break the arbiter's BLOCKER/MINOR weighting.
    flat: List[Any] = [f for v in panel.votes if v.counted for f in v.finding_list]

    adj: Optional[arbiter.Adjudication] = None
    if arbiter_model and flat:
        synthetic = {
            "id": slug,
            "title": label,
            "defect": intent.strip() or "(review of already-committed work; intent not supplied)",
            "spec": "Judge the diff as shipped. There is no ticket spec to conform to.",
        }
        adj = arbiter.adjudicate(
            arbiter_model, synthetic, flat, gate_summary, diff, profile.settled
        )
        (art / "arbiter.txt").write_text(adj.raw or adj.error or "", encoding="utf-8")
        print(f"  arbiter: {adj.summary()}")
        if adj.rationale:
            print(f"  rationale: {adj.rationale[:300]}")

    record = {
        "mode": "review",
        "range": f"{base}..{head}",
        "files": files,
        "panel_verdict": panel.verdict,
        "panel_valid": panel.valid,
        "findings_total": len(flat),
        "findings_blocking": sum(1 for f in flat if f.blocking),
        "arbiter": adj.summary() if adj else "(not run)",
        "arbiter_upheld": len(adj.by(arbiter.UPHELD)) if adj else 0,
        "artifacts": str(art),
        "secs": round(time.time() - t0, 1),
    }
    append_ledger(repo, record)
    (art / "result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")

    print(f"\n  findings -> {art / 'review_prompt.txt'}")
    print(f"  artifacts -> {art}")
    print("  REVIEW MODE IS ADVISORY. It changes nothing; read the findings and decide.")
    return record
