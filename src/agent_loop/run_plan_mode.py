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
    feature_verdict: str = ""  # D1: FEATURE_COMPLETE | FEATURE_PARTIAL | FEATURE_INCOMPLETE | FEATURE_FAILED


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


# ---------------------------------------------------------------------------
# B1: backlog.json — persistent plan state
# ---------------------------------------------------------------------------

def _backlog_path(repo: Path, plan_id: str) -> Path:
    """Where backlog.json lives: next to the plan manifest."""
    return repo / "logs" / "agent_loop" / f"plan-{plan_id}" / "backlog.json"


def _write_backlog(
    repo: Path,
    plan_id: str,
    branch: str,
    base_commit: str,
    tickets: List[Dict[str, Any]],
    parts: List[PartResult],
    status: str,
) -> Path:
    """Write backlog.json after each part completes.

    The backlog is the plan JSON + a status field per part. It's written
    next to the plan manifest at logs/agent_loop/plan-<plan_id>/backlog.json.
    The planner's plan.json is never mutated.

    B8: on resume, attempts are incremented from the existing backlog so
    retry counts are preserved across runs.
    """
    bl_path = _backlog_path(repo, plan_id)

    # B8: read existing backlog to preserve attempt counts on resume AND
    # to preserve done parts from prior sessions that aren't in the current
    # session's parts list (they were skipped via done_ids).
    existing_parts: Dict[str, Dict[str, Any]] = {}
    if bl_path.exists():
        try:
            old = json.loads(bl_path.read_text(encoding="utf-8"))
            for p in old.get("parts", []):
                existing_parts[p.get("id", "")] = p
        except (json.JSONDecodeError, OSError):
            pass

    # Build a status map from parts.
    part_status: Dict[str, Dict[str, Any]] = {}
    for p in parts:
        pid = p.id
        prev_attempts = existing_parts.get(pid, {}).get("attempts", 0)
        # Increment attempts only if this part already had a status
        # (i.e., it's being retried on resume, not run for the first time).
        attempts = prev_attempts + 1 if prev_attempts > 0 else 1
        part_status[pid] = {
            "status": "done" if p.applied else "failed",
            "verdict": p.verdict,
            "commit": p.commit,
            "attempts": attempts,
            "last_error": p.error,
        }

    # R5: merge existing done parts from prior sessions that aren't in the
    # current session. Without this, done parts from a prior run are lost
    # (written as "pending") because they were skipped via done_ids and
    # aren't in result.parts.
    for pid, pstatus in existing_parts.items():
        if pid not in part_status and pstatus.get("status") == "done":
            part_status[pid] = {
                "status": "done",
                "verdict": pstatus.get("verdict", ""),
                "commit": pstatus.get("commit", ""),
                "attempts": pstatus.get("attempts", 1),
                "last_error": pstatus.get("last_error", ""),
            }

    bl_path.parent.mkdir(parents=True, exist_ok=True)

    # Merge tickets with status.
    backlog_parts = []
    for t in tickets:
        tid = t["id"]
        st = part_status.get(tid, {"status": "pending"})
        backlog_parts.append({**t, **st})

    backlog = {
        "plan_id": plan_id,
        "branch": branch,
        "base_commit": base_commit,
        "status": status,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "parts": backlog_parts,
    }

    bl_path.write_text(json.dumps(backlog, indent=2), encoding="utf-8")
    return bl_path


def _read_backlog(backlog_path: Path) -> Dict[str, Any]:
    """Read backlog.json. Returns the backlog dict, or raises if not found."""
    if not backlog_path.is_file():
        raise PlanError(f"backlog file not found: {backlog_path}")
    return json.loads(backlog_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# B2: Re-plan a failed part
# ---------------------------------------------------------------------------

_REPLAN_SYSTEM = """You are a senior software engineer re-planning a FAILED part
of a decomposed feature plan. The part was attempted and failed. Your job is to
produce a REVISED version of the part that addresses the failure, or split it
into smaller sub-parts that can each succeed.

You are given:
- The original part's spec, regions, and expect_green
- The failure verdict (why it failed)
- The gate summary (what went wrong in the build/test/review cycle)
- The last round's arbiter findings (if any)

Produce a revised plan. You may:
1. Refine the regions (different anchors, different files)
2. Refine the spec (more specific, smaller scope)
3. Split into 2+ sub-parts (new IDs, depends_on the original part's dependencies)
4. Change the approach entirely

OUTPUT FORMAT - obey exactly, one block per revised part:
<<<TICKET>>>
{
  "id": "F2a",
  "title": "revised part title",
  "defect": "what is missing today",
  "spec": "what this revised part must do",
  "depends_on": [],
  "regions": [
    {"id": "R1", "file": "path/to/file.py", "op": "create"}
  ],
  "expect_green": ["tests/path/test_x.py::test_the_new_behaviour"]
}
<<<END TICKET>>>
<<<NOTES>>>
- why this revision, and what changed from the original
<<<END NOTES>>>
"""


def _replan_part(
    repo: Path,
    failed_ticket: Dict[str, Any],
    failure_result: Dict[str, Any],
    profile: profiles.Profile,
    implementer: str,
    reviewers: Sequence[str],
    arbiter_model: str,
    max_rounds: int,
    fast_plan: bool,
) -> List[Dict[str, Any]]:
    """B2: re-plan a failed part by feeding failure feedback to the planner.

    Returns a list of revised part(s), or [] if re-planning failed.
    """
    from .plan_mode import _parse_tickets
    from .providers import chat as _chat, ProviderError

    tid = failed_ticket.get("id", "?")
    art = repo / "logs" / "agent_loop" / f"REPLAN-{tid}"
    art.mkdir(parents=True, exist_ok=True)

    # Build the re-plan prompt from the failure feedback.
    prompt = f"# Failed part to re-plan\n\n"
    prompt += f"## Original part\n```json\n{json.dumps(failed_ticket, indent=2)}\n```\n\n"

    verdict = failure_result.get("final_verdict", "unknown")
    prompt += f"## Failure verdict\n{verdict}\n\n"

    # Extract gate summary and arbiter findings from the last round.
    rounds = failure_result.get("rounds", [])
    if rounds:
        last_round = rounds[-1]
        gate_summary = last_round.get("gate_summary", "(none)")
        prompt += f"## Last round gate summary\n{gate_summary}\n\n"

        arbiter = last_round.get("arbiter")
        if arbiter:
            prompt += f"## Last round arbiter\n{arbiter}\n\n"

    prompt += f"## Context\nLanguage: {profile.language}\n"
    prompt += f"File suffixes: {', '.join(profile.file_suffixes)}\n"
    if profile.test_sources:
        prompt += f"Tests live in: {', '.join(profile.test_sources)}\n"

    prompt += "\nProduce a revised plan for this part. Output one <<<TICKET>>> block per revised part.\n"

    from . import config
    cfg = config.get().mode("plan")

    history = [
        {"role": "system", "content": _REPLAN_SYSTEM},
        {"role": "user", "content": prompt},
    ]

    try:
        out = _chat(implementer, history, max_tokens=cfg.max_tokens, think=cfg.think)
    except ProviderError as exc:
        print(f"  [replan] model error: {exc}")
        return []

    raw = out.text
    (art / "replan_raw.txt").write_text(raw, encoding="utf-8")
    print(f"  [replan] {out.usage_line()}")

    revised = _parse_tickets(raw)
    if not revised:
        print(f"  [replan] no parseable <<<TICKET>>> blocks in re-plan output")
        return []

    (art / "revised_parts.json").write_text(json.dumps(revised, indent=2), encoding="utf-8")
    return revised


# ---------------------------------------------------------------------------
# D1: Feature-level acceptance verification
# ---------------------------------------------------------------------------

def _run_feature_acceptance(
    repo: Path,
    profile: profiles.Profile,
    acceptance_tests: List[str],
    branch: str,
) -> str:
    """Run feature-level acceptance tests against the scratch branch HEAD.

    Returns a feature verdict:
    - FEATURE_COMPLETE: all parts done + acceptance tests pass
    - FEATURE_PARTIAL: all parts done but acceptance tests fail
    - FEATURE_ERROR: infrastructure error (workspace creation, runner crash)
    - FEATURE_INCOMPLETE: some parts failed (caller checks this before calling)
    """
    from . import gates, workspace

    if not profile.test_cmd:
        print("  [feature] no test_cmd configured — cannot verify acceptance")
        return "FEATURE_UNVERIFIED"

    if not acceptance_tests:
        return "FEATURE_COMPLETE"

    print(f"  [feature] running {len(acceptance_tests)} acceptance test(s) against {branch}")

    # B6: distinguish infrastructure errors from test failures. A workspace
    # creation failure (stale worktree, disk full, lock contention) is NOT
    # "tests failed" — it's "tests didn't run." Lumping them together makes
    # the operator think the feature is broken when the real problem is a
    # leftover worktree.
    try:
        with workspace.open_workspace(repo, "feature-acceptance", base=branch) as ws:
            outcome = gates.run_tests(profile.test_cmd, ws.root)
    except Exception as exc:
        print(f"  [feature] acceptance workspace/runner error: {exc}")
        return "FEATURE_ERROR"

    if not outcome.ran:
        print(f"  [feature] test runner produced no parseable result")
        return "FEATURE_ERROR"

    # B1: a test name that matches neither a failure NOR a pass is a test
    # that never ran — treat it as failing, not passing.
    # R1/R2 fix: the old code used `name in outcome.raw` as a fallback check,
    # which is a substring check — a test name appearing in a failure
    # traceback would be falsely marked as passing. Now we ONLY use
    # _extract_passed_names, and if the pytest output format doesn't print
    # "PASSED" (e.g. default -q mode), we treat the test as "did not run"
    # rather than falsely marking it as passing.
    passed_names = _extract_passed_names(outcome.raw)
    passing = []
    failing = []
    for name in acceptance_tests:
        matched_fail = any(gates.names_match(name, f) for f in outcome.failures)
        if matched_fail:
            failing.append(name)
        elif any(gates.names_match(name, p) for p in passed_names):
            passing.append(name)
        else:
            # Not in failures AND not in explicit passes → didn't run.
            failing.append(f"{name} (not found in output — did not run)")

    print(f"  [feature] {len(passing)} passed, {len(failing)} failed")
    if failing:
        print(f"  [feature] failing: {failing}")
        return "FEATURE_PARTIAL"
    return "FEATURE_COMPLETE"


def _extract_passed_names(raw: str) -> set:
    """Extract passed test names from pytest output.

    Pytest prints passed tests as 'test_name PASSED' or in the short test
    summary as 'test_file.py::test_name PASSED'. We extract these so
    _run_feature_acceptance can verify a test actually ran and passed,
    not just that it didn't fail.

    Handles parametrized tests: 'test_foo[param1] PASSED' → extracts
    'test_foo' and 'test_foo[param1]'.
    """
    import re
    passed = set()
    # Match 'path::test_name[params] PASSED' or 'path::test_name PASSED'
    for m in re.finditer(r"(\S+)::(\S+?(?:\[[^\]]*\])?)\s+PASSED", raw):
        full = m.group(2)
        passed.add(full)
        # Also add the base name without params
        if "[" in full:
            passed.add(full.split("[")[0])
    # Match bare 'test_name PASSED' (verbose without path)
    for m in re.finditer(r"^(\S+?(?:\[[^\]]*\])?)\s+PASSED\s*$", raw, re.MULTILINE):
        full = m.group(1)
        passed.add(full)
        if "[" in full:
            passed.add(full.split("[")[0])
    return passed


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
    resume: bool = False,
    backlog_path: str = "",
    replan: bool = False,
    replan_limit: int = 2,
    continue_on_failure: bool = False,
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
        resume: B1 — read backlog.json, skip done parts, retry failed/blocked
        backlog_path: B1 — path to backlog.json (for --resume)
        replan: B2 — re-plan a failed part instead of stopping. Feeds the
            failure feedback (verdict, gate summary, arbiter findings) to the
            planner, which produces a revised part or a split into sub-parts.
            The revised part(s) are re-run through the same TDD + patch cycle.
        replan_limit: B2 — max re-plans per part (default 2). After N replans,
            the part is marked failed and the chain stops (or continues if
            --continue-on-failure is also set).
        continue_on_failure: B3 — continue to next independent part on failure.
            Parts that depend on the failed part are skipped (marked BLOCKED).

    Returns:
        a PlanResult with the outcome of each part
    """
    # Load the plan.
    from .cli import load_tickets
    tickets = load_tickets(plan_path)

    # D1: load feature_acceptance from the plan JSON (optional).
    feature_acceptance: List[str] = []
    try:
        plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
        feature_acceptance = plan_data.get("feature_acceptance", [])
    except (json.JSONDecodeError, OSError):
        pass

    # B1: resume reads plan_id, branch, and per-part status from backlog.json.
    # Without this, plan_id is regenerated (int(time.time())) and the branch
    # name won't match the existing one — the "branch already exists" error
    # is unreachable for exactly this reason.
    done_ids: set = set()
    if resume and backlog_path:
        bl_path = Path(backlog_path)
        try:
            backlog = _read_backlog(bl_path)
        except PlanError as exc:
            print(f"  RESUME REFUSED: {exc}")
            return PlanResult(plan_id="", status="failed", parts=[], started_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        plan_id = backlog.get("plan_id", "")
        branch = backlog.get("branch", "")
        base_commit = backlog.get("base_commit", "")
        # B5: validate that the current repo HEAD matches the backlog's
        # base_commit. If the operator ran git reset --hard to a different
        # commit, base_commit is stale and any diff against it is wrong.
        current_head = _git(repo, "rev-parse", "HEAD").strip()
        if current_head != base_commit:
            print(f"  [resume] WARNING: current HEAD ({current_head[:8]}) != "
                  f"backlog base_commit ({base_commit[:8]})")
            print(f"  [resume] updating base_commit to current HEAD")
            base_commit = current_head
        # Skip done parts; retry failed/blocked parts.
        done_ids = {p["id"] for p in backlog.get("parts", []) if p.get("status") == "done"}
        if done_ids:
            print(f"  [resume] skipping done parts: {sorted(done_ids)}")
        # Verify the branch still exists.
        existing = _git(repo, "branch", "--list", branch, check=False).strip()
        if not existing:
            print(f"  RESUME REFUSED: branch {branch} no longer exists")
            return PlanResult(plan_id=plan_id, status="failed", parts=[], started_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    else:
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

    # Create the scratch branch (or reuse existing on resume).
    if not resume:
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
    failed_ids: set = set()  # B2: track failed parts for continue-on-failure
    replan_attempts: Dict[str, int] = {}  # B2: track re-plan attempts per part
    for t in ordered:
        tid = t["id"]
        # B1: skip done parts on resume.
        if tid in done_ids:
            print(f"  [plan] skipping {tid} (done in prior run)")
            continue
        # B2: skip parts that depend on a failed part under --continue-on-failure.
        if failed_ids and continue_on_failure:
            deps = set(t.get("depends_on") or [])
            blocked_by = deps & failed_ids
            if blocked_by:
                print(f"  [plan] skipping {tid} (blocked by failed: {sorted(blocked_by)})")
                part_result = PartResult(id=tid, title=t.get("title", ""),
                                         verdict="BLOCKED", error=f"blocked by failed: {sorted(blocked_by)}")
                result.parts.append(part_result)
                continue
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
                    # B4: write backlog on break paths too.
                    _write_backlog(repo, plan_id, branch, base_commit, tickets,
                                   result.parts, result.status)
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
                # Part failed.
                print(f"  [plan] {tid} NOT promotable ({part_result.verdict}).")
                failed_ids.add(tid)  # B2: track for continue-on-failure

                # B2: re-plan the failed part. Feed the failure feedback
                # to plan_mode and get a revised part (or a split into
                # sub-parts). Then re-run the revised part(s).
                if replan and replan_attempts.get(tid, 0) < replan_limit:
                    replan_attempts[tid] = replan_attempts.get(tid, 0) + 1
                    attempt = replan_attempts[tid]
                    print(f"  [plan] --replan: re-planning {tid} (attempt {attempt}/{replan_limit})")

                    revised_parts = _replan_part(
                        repo, t, ticket_result, profile,
                        implementer, reviewers, arbiter_model,
                        max_rounds, fast_plan=False,
                    )

                    if revised_parts:
                        # R3: validate the revised parts before running them.
                        # The original plan was validated by _validate_feature_plan;
                        # re-planned parts must pass the same check.
                        from .plan_mode import _validate_feature_plan
                        all_known_ids = {t2["id"] for t2 in ordered}
                        for rp in revised_parts:
                            for dep in rp.get("depends_on") or []:
                                if dep not in all_known_ids and dep not in failed_ids:
                                    print(f"  [replan] WARNING: {rp['id']} depends on {dep}, "
                                          f"which is not in the plan")
                        validation = _validate_feature_plan(repo, revised_parts, profile)
                        if validation:
                            print(f"  [replan] validation failed: {validation}")
                        else:
                            print(f"  [plan] {tid} re-planned into {len(revised_parts)} part(s)")
                            for rp in revised_parts:
                                rp_id = rp["id"]
                                print(f"\n=== Re-planned Part {rp_id}: {rp.get('title', '')} ===")

                                # Use the same part_base (branch if advanced).
                                branch_head = _git(repo, "rev-parse", branch, check=False).strip()
                                rp_base = branch if (branch_head and branch_head != base_commit) else "HEAD"

                                # R7: pass tdd flag to re-planned parts.
                                if tdd and rp.get("expect_green"):
                                    from .test_mode import run_test, default_test_path
                                    rp_test_file = default_test_path(profile, rp_id)
                                    print(f"  [tdd] generating tests for {rp_id} at {rp_test_file}")
                                    rp_test = run_test(
                                        repo,
                                        defect_description=rp.get("defect", rp.get("title", "")),
                                        ticket=rp,
                                        profile=profile,
                                        implementer=implementer,
                                        test_file=rp_test_file,
                                        path_isolated=True,
                                        base=rp_base,
                                    )
                                    if rp_test.get("error"):
                                        print(f"  [tdd] test generation failed: {rp_test['error']}")
                                    else:
                                        rp_gen = rp_test.get("test_file", rp_test_file)
                                        if rp_gen and (repo / rp_gen).exists():
                                            _commit_to_branch(
                                                repo, branch, [rp_gen], rp_id,
                                                message=f"agent-loop: {rp_id} generated tests (red)",
                                            )
                                            rp_base = branch

                                rp_result = PartResult(id=rp_id, title=rp.get("title", ""))
                                try:
                                    rp_ticket = run_ticket(
                                        repo, rp, profile, implementer, reviewers,
                                        max_rounds=max_rounds,
                                        apply=True, allow_unapproved=True,
                                        arbiter_model=arbiter_model,
                                        panel_deadline=panel_deadline,
                                        keep_worktree=keep_branch,
                                        base_ref=rp_base,
                                    )
                                    rp_result.verdict = rp_ticket.get("final_verdict", "")
                                    rp_result.applied = rp_ticket.get("applied", False)
                                    rp_result.rounds = len(rp_ticket.get("rounds", []))
                                    rp_result.cost_usd = rp_ticket.get("cost_usd", 0.0)
                                    rp_result.tests = rp.get("expect_green", [])

                                    if rp_ticket.get("applied") and rp_ticket.get("touched"):
                                        commit = _commit_to_branch(repo, branch, rp_ticket["touched"], rp_id)
                                        rp_result.commit = commit
                                        print(f"  [plan] {rp_id} committed to {branch} @ {commit[:8]}")
                                    elif rp_ticket.get("final_verdict") not in PROMOTABLE:
                                        print(f"  [plan] {rp_id} NOT promotable ({rp_result.verdict})")
                                        failed_ids.add(rp_id)
                                except Exception as exc:
                                    rp_result.error = f"{type(exc).__name__}: {exc}"
                                    print(f"  [plan] {rp_id} ERROR: {rp_result.error}")
                                    failed_ids.add(rp_id)

                                result.parts.append(rp_result)
                                result.cost_usd += rp_result.cost_usd
                                _write_backlog(repo, plan_id, branch, base_commit, tickets,
                                               result.parts, result.status or "in_progress")

                            # After re-planned parts, continue the main loop.
                            continue
                    else:
                        print(f"  [plan] {tid} re-plan produced no parts")

                # B3: continue-on-failure skips to the next independent part.
                if continue_on_failure:
                    print(f"  [plan] --continue-on-failure: marking {tid} as failed, continuing")
                    # B3 fix: set "partial" unconditionally, not "failed" when
                    # result.parts is empty. The old code set "failed" on the
                    # first failure, which was never overwritten even if later
                    # parts succeeded.
                    result.status = "partial"
                    result.parts.append(part_result)
                    result.cost_usd += part_result.cost_usd
                    _write_backlog(repo, plan_id, branch, base_commit, tickets,
                                   result.parts, result.status)
                    continue
                result.status = "partial" if result.parts else "failed"
                result.parts.append(part_result)
                # B4: write backlog on break paths too.
                _write_backlog(repo, plan_id, branch, base_commit, tickets,
                               result.parts, result.status)
                break

        except Exception as exc:
            part_result.error = f"{type(exc).__name__}: {exc}"
            print(f"  [plan] {tid} ERROR: {part_result.error}")
            failed_ids.add(tid)  # B2: track for continue-on-failure
            # B3: continue-on-failure on exceptions too.
            if continue_on_failure:
                print(f"  [plan] --continue-on-failure: marking {tid} as failed, continuing")
                result.status = "partial"  # B3 fix: always "partial", not "failed"
                result.parts.append(part_result)
                result.cost_usd += part_result.cost_usd
                _write_backlog(repo, plan_id, branch, base_commit, tickets,
                               result.parts, result.status)
                continue
            result.status = "partial" if result.parts else "failed"
            result.parts.append(part_result)
            # B4: write backlog on break paths too.
            _write_backlog(repo, plan_id, branch, base_commit, tickets,
                           result.parts, result.status)
            break

        result.parts.append(part_result)
        result.cost_usd += part_result.cost_usd

        # B1: write backlog.json after each part so a crash or failure
        # leaves a trace of which parts succeeded.
        _write_backlog(repo, plan_id, branch, base_commit, tickets,
                       result.parts, result.status or "in_progress")

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

    # D1: feature-level acceptance verification.
    # After all parts are done, if feature_acceptance tests are present,
    # run them against the scratch branch HEAD (which has all parts' code).
    feature_verdict = ""
    if feature_acceptance and result.status == "complete":
        feature_verdict = _run_feature_acceptance(
            repo, profile, feature_acceptance, branch
        )
    elif feature_acceptance:
        feature_verdict = "FEATURE_INCOMPLETE"
        print(f"  [feature] skipping acceptance tests (plan status: {result.status})")
    else:
        feature_verdict = "FEATURE_COMPLETE" if result.status == "complete" else "FEATURE_INCOMPLETE"

    result.feature_verdict = feature_verdict
    print(f"  [feature] verdict: {feature_verdict}")

    # B1: write final backlog with the final status.
    bl_path = _write_backlog(repo, plan_id, branch, base_commit, tickets,
                             result.parts, result.status)
    print(f"  [plan] backlog: {bl_path}")

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