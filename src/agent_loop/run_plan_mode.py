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
    """Commit the promoted files to the plan branch via a temporary worktree.

    This advances the plan branch HEAD so the next part's worktree (created at
    the plan branch HEAD) will contain the promoted changes.

    The promoted files are already in the live working tree (run_ticket with
    apply=True put them there). We stage them, create a TEMP commit on the
    current HEAD, fast-forward the plan branch to that commit, and then reset
    the current HEAD back one commit so the user's branch is unchanged. The
    files remain in the working tree (soft reset).

    Returns the commit hash on the plan branch.
    """
    # Stage the promoted files in the live repo (they were just promoted by
    # run_ticket with apply=True).
    for f in files:
        _git(repo, "add", "--", f, check=False)

    # Remember the user's current HEAD so we can restore it.
    user_head = _git(repo, "rev-parse", "HEAD").strip()

    # Commit on the current branch (temporarily).
    msg = message or f"agent-loop: part {part_id} promoted"
    _git(repo, "commit", "-m", msg, "--allow-empty")

    # Get the commit hash.
    commit = _git(repo, "rev-parse", "HEAD").strip()

    # Fast-forward the plan branch to this commit.
    _git(repo, "branch", "-f", branch, commit)

    # Reset the user's HEAD back to where it was (soft: keep the working tree).
    # The commit is now ONLY on the plan branch. The user's working tree still
    # has the promoted files (from run_ticket's promote step), but their branch
    # ref is unchanged.
    _git(repo, "reset", "--soft", user_head)

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
    tdd: bool = False,
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
        tdd: generate failing acceptance tests before each part (A1)

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

        # Determine the base for this part's worktree. If the plan branch
        # has advanced past base_commit (because a prior part was promoted
        # and committed to it), the worktree should be at the plan branch's
        # HEAD so it sees the prior parts' code. Otherwise, at the original
        # base.
        #
        # A0-2: the old check was `result.parts[-1].applied` — which only
        # looked at the immediately preceding part. Under --continue-on-failure
        # or --from, a skipped/failed part made the next independent part
        # build at HEAD without prior parts' code. The correct rule: use the
        # branch if it has advanced past base_commit, regardless of which
        # part advanced it.
        branch_head = _git(repo, "rev-parse", branch, check=False).strip()
        if branch_head and branch_head != base_commit:
            part_base = branch
        else:
            part_base = "HEAD"

        part_result = PartResult(id=tid, title=t.get("title", ""))

        try:
            # A1: TDD test generation. When --tdd is passed, generate the
            # failing acceptance tests BEFORE running the part, and commit
            # them to the plan branch as a separate commit. This ensures:
            #   1. The test file is in the worktree (built from the branch
            #      HEAD, not from an uncommitted live file)
            #   2. The red commit and green commit are separable
            #   3. expect_green names match what the runner produces
            if tdd and t.get("expect_green"):
                from .test_mode import run_test, default_test_path

                test_file = default_test_path(profile, tid)
                print(f"  [tdd] generating tests for {tid} at {test_file}")

                test_result = run_test(
                    repo,
                    defect_description=t.get("defect", t.get("title", "")),
                    ticket=t,
                    profile=profile,
                    implementer=implementer,
                    test_file=test_file,
                    path_isolated=True,  # A1: --tdd implies path_isolated
                    base=part_base,  # A3: baseline at plan branch HEAD
                )

                if test_result.get("error"):
                    print(f"  [tdd] test generation failed: {test_result['error']}")
                    part_result.error = f"tdd: {test_result['error']}"
                    result.status = "partial" if result.parts else "failed"
                    result.parts.append(part_result)
                    break

                # Commit the generated test to the plan branch before
                # run_ticket, so the worktree (built from branch HEAD)
                # includes it. This is the "red commit."
                generated_path = test_result.get("test_file", test_file)
                if generated_path and (repo / generated_path).exists():
                    test_commit = _commit_to_branch(
                        repo, branch, [generated_path], tid,
                        message=f"agent-loop: {tid} generated tests (red)",
                    )
                    print(f"  [tdd] test committed to {branch} @ {test_commit[:8]}")
                    # Update part_base: the branch has now advanced.
                    part_base = branch

            # Run the part. We pass apply=True so the promoted files land
            # in the live working tree, then we commit them to the plan branch.
            # base_ref=part_base ensures part 2's worktree sees part 1's work.
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
                base_ref=part_base,
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

    # Clean up the scratch branch if the plan failed and --keep-branch was
    # not set. But only if no part committed to it — a branch with committed
    # work is evidence the operator may need for --resume or --from, and
    # deleting it makes those commits unreachable.
    #
    # A0-1: the old code deleted the branch on any non-complete status,
    # even after parts had committed. _commit_to_branch soft-resets the
    # user's HEAD, so the files survive in the working tree, but the commits
    # become unreachable and a fresh worktree at HEAD won't see them.
    any_committed = any(p.commit for p in result.parts)
    if result.status != "complete" and not keep_branch and not any_committed:
        print(f"  [plan] deleting scratch branch {branch} (use --keep-branch to retain)")
        _git(repo, "branch", "-D", branch, check=False)
    elif result.status != "complete" and any_committed:
        print(f"  [plan] retaining scratch branch {branch} ({sum(1 for p in result.parts if p.commit)} part(s) committed)")
        print(f"  [plan] use --resume or --from to continue from a specific part")

    # Print summary.
    print(f"\n==== PLAN SUMMARY ====")
    print(f"plan: {plan_id}  status: {result.status}  branch: {result.branch}")
    for p in result.parts:
        tag = "OK" if p.applied else "FAIL"
        print(f"  {p.id:<5} [{tag}] {p.verdict:<22} rounds={p.rounds} cost=${p.cost_usd:.4f}")
    if result.cost_usd:
        print(f"  total cost ${result.cost_usd:.4f}")

    return result