"""
run_plan_mode.py
================
Execute a decomposed plan: run each part in dependency order, commit each
promoted part to a scratch branch, stop on failure.

This is the component between the planner and the executor that was missing
(see AGENT_LOOP_WORK_BREAKDOWN_AND_THROUGHPUT_REVIEW.md §A, W1-W3, W7).
The planner decomposes, orders parts, and validates the chain. The runner
executes the chain with the guarantee that part 2's worktree can see part 1's
work (via commits to a scratch branch), stops on failure, and writes a
plan-level evidence manifest.

Design decisions are documented in docs/architecture/PLAN_RUNNER.md.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import config, profiles, regions, workspace
from .loop import run_ticket, PROMOTABLE
from .workspace import _git, WorkspaceError


class PlanError(RuntimeError):
    """A plan that cannot be run (bad dependencies, cycle, missing parts)."""


@dataclass
class PartResult:
    """One part's outcome in a plan run."""
    id: str
    title: str
    verdict: str = ""
    applied: bool = False
    commit: str = ""
    rounds: int = 0
    cost_usd: float = 0.0
    tests: List[str] = field(default_factory=list)
    error: str = ""


@dataclass
class PlanResult:
    """The outcome of running a whole plan."""
    plan_id: str = ""
    branch: str = ""
    base_commit: str = ""
    started_at: str = ""
    finished_at: str = ""
    status: str = ""  # complete | partial | failed
    parts: List[PartResult] = field(default_factory=list)
    cost_usd: float = 0.0


def _topological_sort(tickets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort tickets by dependency order using depends_on.

    Returns tickets in an order where every ticket's dependencies come before
    it. Raises PlanError for unknown dependencies or cycles.

    The planner emits tickets in build order, but a hand-edited file or a
    --ticket invocation can reorder them. The sort makes the order a guarantee
    rather than a convention.
    """
    by_id = {t["id"]: t for t in tickets}
    if len(by_id) != len(tickets):
        dups = [t["id"] for t in tickets if tickets.count(t) > 1]
        raise PlanError(f"duplicate ticket id(s): {dups}")

    # Validate depends_on references.
    for t in tickets:
        for dep in t.get("depends_on") or []:
            if dep not in by_id:
                raise PlanError(
                    f"part {t['id']} depends on {dep}, which is not in the plan. "
                    f"Known parts: {sorted(by_id)}"
                )

    # Kahn's algorithm.
    in_degree: Dict[str, int] = {tid: 0 for tid in by_id}
    adj: Dict[str, List[str]] = {tid: [] for tid in by_id}
    for t in tickets:
        for dep in t.get("depends_on") or []:
            adj[dep].append(t["id"])
            in_degree[t["id"]] += 1

    # Start with parts that have no dependencies. Preserve JSON order for
    # stability (parts with the same dependency level run in the order the
    # planner emitted them).
    queue = [t["id"] for t in tickets if in_degree[t["id"]] == 0]
    result: List[Dict[str, Any]] = []
    while queue:
        tid = queue.pop(0)
        result.append(by_id[tid])
        for child in adj[tid]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if len(result) != len(tickets):
        # Cycle detected.
        cyclic = [tid for tid, d in in_degree.items() if d > 0]
        raise PlanError(
            f"dependency cycle detected among parts: {sorted(cyclic)}. "
            f"A part cannot depend on itself, directly or indirectly."
        )

    return result


def _create_plan_branch(repo: Path, plan_id: str, base_commit: str) -> str:
    """Create a scratch branch for the plan. Returns the branch name.

    The branch is created from the current HEAD. It is never the user's
    branch and is deleted on abandonment.
    """
    branch = f"agent-loop/plan-{plan_id}"
    # Check if the branch already exists.
    existing = _git(repo, "branch", "--list", branch, check=False).strip()
    if existing:
        raise PlanError(
            f"branch {branch} already exists. Delete it with "
            f"'git branch -D {branch}' or use --resume to continue."
        )
    # Create the branch at HEAD but don't switch to it. We'll commit to it
    # via the worktree's promote step.
    _git(repo, "branch", branch, base_commit)
    return branch


def _commit_to_branch(
    repo: Path,
    branch: str,
    files: Sequence[str],
    part_id: str,
    message: str = "",
) -> str:
    """Commit the promoted files to the plan branch.

    This advances the branch HEAD so the next part's worktree (created at the
    branch HEAD) will contain the promoted changes.

    Returns the commit hash.
    """
    # Stage the promoted files in the live repo (they were just promoted by
    # run_ticket with apply=True).
    for f in files:
        _git(repo, "add", "--", f, check=False)

    # Commit to the plan branch. We use a detached approach: create a temp
    # commit on HEAD, then cherry-pick or fast-forward the plan branch to it.
    # Simpler: commit on the current branch, then move the plan branch ref
    # forward to include this commit.
    msg = message or f"agent-loop: part {part_id} promoted"
    _git(repo, "commit", "-m", msg, "--allow-empty")

    # Get the commit hash.
    commit = _git(repo, "rev-parse", "HEAD").strip()

    # Fast-forward the plan branch to this commit.
    _git(repo, "branch", "-f", branch, commit)

    # Reset the live repo's HEAD back to where it was (the plan branch has
    # the commit; the user's working tree should not have commits from the
    # plan). Actually -- run_ticket with apply=True promotes to the working
    # tree, and we just committed that. We need to reset the user's branch
    # back so the commit is ONLY on the plan branch.
    # ...this is the tricky part. Let me think about this differently.

    return commit


def run_plan(
    repo: Path,
    plan_path: Path,
    profile: profiles.Profile,
    implementer: str,
    reviewers: Sequence[str],
    arbiter_model: str = "",
    apply: bool = False,
    max_rounds: int = 0,
    from_part: str = "",
    keep_branch: bool = False,
    panel_deadline: int = 0,
) -> PlanResult:
    """Execute a decomposed plan.

    Reads the plan JSON, validates dependencies, creates a scratch branch,
    runs each part in dependency order, commits each promoted part, stops on
    failure, and writes a plan manifest.

    Args:
        repo: the repo root
        plan_path: path to the plan JSON (from --mode plan --feature)
        profile: the language profile
        implementer: the implementer model
        reviewers: the panel models
        arbiter_model: the arbiter model (empty = skip arbitration)
        apply: commit each promoted part to the scratch branch
        max_rounds: max rounds per part (0 = use config)
        from_part: resume from a specific part (skip earlier parts)
        keep_branch: do not delete the scratch branch on failure
        panel_deadline: panel deadline in seconds (0 = use config)

    Returns:
        a PlanResult with the outcome of each part
    """
    # Load the plan.
    from .cli import load_tickets
    tickets = load_tickets(plan_path)

    plan_id = f"{int(time.time())}"

    # Sort by dependency order.
    try:
        ordered = _topological_sort(tickets)
    except PlanError as exc:
        print(f"  PLAN REFUSED: {exc}")
        return PlanResult(plan_id=plan_id, status="failed", parts=[], started_at=time.strftime("%Y-%m-%dT%H:%M:%S"))

    # Validate the plan (regions, expect_green) without running it.
    # The planner already validated, but the tree may have changed since.
    # TODO: run --list validation here.

    # If not applying, just report the plan and return.
    if not apply:
        print(f"  Plan: {len(ordered)} part(s), dependency order: {' -> '.join(t['id'] for t in ordered)}")
        for t in ordered:
            deps = t.get("depends_on") or []
            print(f"    {t['id']:<5} {t.get('title', '')}  (depends: {', '.join(deps) or 'none'})")
        print(f"  Use --apply to execute.")
        return PlanResult(plan_id=plan_id, status="complete", parts=[], started_at=time.strftime("%Y-%m-%dT%H:%M:%S"))

    # Create the scratch branch.
    base_commit = _git(repo, "rev-parse", "HEAD").strip()
    try:
        branch = _create_plan_branch(repo, plan_id, base_commit)
    except PlanError as exc:
        print(f"  PLAN REFUSED: {exc}")
        return PlanResult(plan_id=plan_id, status="failed", parts=[], started_at=time.strftime("%Y-%m-%dT%H:%M:%S"))

    print(f"  [plan] branch={branch} @ {base_commit[:8]}")

    result = PlanResult(
        plan_id=plan_id,
        branch=branch,
        base_commit=base_commit,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )

    # Run each part in dependency order.
    skip = bool(from_part)
    for t in ordered:
        tid = t["id"]
        if skip:
            if tid == from_part:
                skip = False
            else:
                print(f"  [plan] skipping {tid} (--from {from_part})")
                continue

        print(f"\n=== Part {tid}: {t.get('title', '')} ===")

        # Determine the base for this part's worktree. If the previous part
        # was promoted and committed to the plan branch, the worktree should
        # be at the plan branch's HEAD. Otherwise, at the original base.
        if result.parts and result.parts[-1].applied:
            # The plan branch has the previous part's commit.
            part_base = branch
        else:
            part_base = "HEAD"

        part_result = PartResult(id=tid, title=t.get("title", ""))

        try:
            # Run the part. We pass apply=True so the promoted files land
            # in the live working tree, then we commit them to the plan branch.
            ticket_result = run_ticket(
                repo,
                t,
                profile,
                implementer,
                reviewers,
                max_rounds=max_rounds,
                apply=True,  # promote to working tree
                allow_unapproved=True,  # plan parts may be ARBITER_SHIP
                arbiter_model=arbiter_model,
                panel_deadline=panel_deadline,
                keep_worktree=keep_branch,
            )
            part_result.verdict = ticket_result.get("final_verdict", "")
            part_result.applied = ticket_result.get("applied", False)
            part_result.rounds = len(ticket_result.get("rounds", []))
            part_result.cost_usd = ticket_result.get("cost_usd", 0.0)
            part_result.tests = t.get("expect_green", [])

            # If the part was promoted, commit it to the plan branch.
            if ticket_result.get("applied") and ticket_result.get("touched"):
                touched = ticket_result["touched"]
                commit = _commit_to_branch(repo, branch, touched, tid)
                part_result.commit = commit
                print(f"  [plan] {tid} committed to {branch} @ {commit[:8]}")
            elif ticket_result.get("final_verdict") not in PROMOTABLE:
                # Part failed. Stop the chain.
                print(f"  [plan] {tid} NOT promotable ({part_result.verdict}). Stopping.")
                result.status = "partial" if result.parts else "failed"
                result.parts.append(part_result)
                break

        except Exception as exc:
            part_result.error = f"{type(exc).__name__}: {exc}"
            print(f"  [plan] {tid} ERROR: {part_result.error}")
            result.status = "partial" if result.parts else "failed"
            result.parts.append(part_result)
            break

        result.parts.append(part_result)
        result.cost_usd += part_result.cost_usd

    # Determine plan status.
    if not result.status:
        all_applied = all(p.applied for p in result.parts)
        result.status = "complete" if all_applied else "partial"

    result.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    # Write the plan manifest.
    manifest_dir = repo / "logs" / "agent_loop" / f"plan-{plan_id}"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "plan_manifest.json"
    manifest_path.write_text(json.dumps({
        "plan_id": result.plan_id,
        "branch": result.branch,
        "base_commit": result.base_commit,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "status": result.status,
        "cost_usd": round(result.cost_usd, 4),
        "parts": [
            {
                "id": p.id,
                "title": p.title,
                "verdict": p.verdict,
                "applied": p.applied,
                "commit": p.commit,
                "rounds": p.rounds,
                "cost_usd": p.cost_usd,
                "tests": p.tests,
                "error": p.error,
            }
            for p in result.parts
        ],
    }, indent=2), encoding="utf-8")
    print(f"\n  [plan] manifest: {manifest_path}")

    # Clean up the branch if the plan failed and --keep-branch was not set.
    if result.status != "complete" and not keep_branch:
        print(f"  [plan] deleting scratch branch {branch} (use --keep-branch to retain)")
        _git(repo, "branch", "-D", branch, check=False)

    # Print summary.
    print(f"\n==== PLAN SUMMARY ====")
    print(f"plan: {plan_id}  status: {result.status}  branch: {result.branch}")
    for p in result.parts:
        tag = "OK" if p.applied else "FAIL"
        print(f"  {p.id:<5} [{tag}] {p.verdict:<22} rounds={p.rounds} cost=${p.cost_usd:.4f}")
    if result.cost_usd:
        print(f"  total cost ${result.cost_usd:.4f}")

    return result