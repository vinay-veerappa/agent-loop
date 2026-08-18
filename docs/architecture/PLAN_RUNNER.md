# Plan Runner Architecture

> **Status:** done. Extended by the AGILE Pipeline (see
> [AGILE_PIPELINE_PLAN.md](./AGILE_PIPELINE_PLAN.md) for the full feature set).
> **Parent doc:** [ARCHITECTURE.md](./ARCHITECTURE.md) §12 (Modes)
> **Review origin:** [AGENT_LOOP_WORK_BREAKDOWN_AND_THROUGHPUT_REVIEW.md](./AGENT_LOOP_WORK_BREAKDOWN_AND_THROUGHPUT_REVIEW.md) §A, W1–W3, W7
> **Fixes:** [AGENT_LOOP_FIFTH_REVIEW.md](./AGENT_LOOP_FIFTH_REVIEW.md) R5-1, R5-2

> **Canonical doc:** AGILE_PIPELINE_PLAN.md is now canonical for the pipeline
> features (--tdd, --pipeline, --resume, --backlog, --replan,
> --continue-on-failure, --epic, feature_acceptance). This doc covers the
> original plan runner core (branch management, per-part execution, manifest).
> The stale items below have been corrected in the AGILE_PIPELINE_PLAN.md.

## Problem

`--mode plan --feature` decomposes a feature into ordered parts with dependency
edges (`depends_on`), validates the chain, and permits part 2 to anchor into a
file part 1 creates. Then `plan.json` is handed to a runner with no concept of a
plan:

- `promote()` writes uncommitted; `open_workspace` checks out `HEAD`. Part 2's
  worktree cannot see part 1's work. Measured: `T2 sees new.txt: False`.
- `depends_on` is emitted by the planner and read by nothing (two grep hits,
  both inside the prompt string).
- A failed part does not stop the chain — parts 2, 3, 4 each pay a full baseline
  suite against a tree where part 1 never landed.
- There is no rollback. Parts 1 and 2 promoted, part 3 fails: the live tree
  holds two thirds of a feature, uncommitted, with no manifest.
- The plan has no identity after `plan.json` is written.

## Solution

A new mode, `--mode run-plan --plan plan.json`, that owns the gap between the
planner and the executor.

### Design decisions

#### D1. Commit each promoted part to a scratch branch

A git worktree can only be created at a **commit**. Promoting into the
uncommitted working tree is what makes W1 unfixable — part 2's worktree at
`HEAD` never contains part 1's changes.

The plan runner commits each promoted part to a scratch branch named
`agent-loop/plan-<plan_id>`. This branch:

- Is **never** the user's branch. It is created from `HEAD` and deleted on
  abandonment.
- Gives rollback for free: delete the branch and the entire plan is gone.
- Gives per-part bisection for free: each part is a commit on the branch.
- Does not conflict with the user's uncommitted work — the worktree is a
  separate checkout.

The user's branch receives nothing until the entire plan is green and the
operator explicitly promotes it (e.g. `git merge agent-loop/plan-<plan_id>`).

#### D2. Topological sort on `depends_on`

The runner validates `depends_on` before the first part runs:

- Every `depends_on` id must exist in the plan (refuse unknown ids).
- No cycles (refuse circular dependencies).
- Parts are executed in topological order, not JSON array order.

If `depends_on` is empty or absent for all parts, the plan runs in JSON array
order (the planner's build order), which is the current behavior.

#### D3. Stop on failure

A part whose final verdict is not promotable (`APPROVE`, `APPROVE_PARTIAL`,
`ARBITER_SHIP`) ends the plan. The runner does not continue to parts 3 and 4
against a tree where part 1 never landed. The scratch branch retains whatever
was promoted before the failure, and the manifest records which parts completed.

#### D4. Plan-level evidence ledger

The runner writes a manifest at `logs/agent_loop/plan-<plan_id>/plan_manifest.json`:

> **Note:** `plan_id` is a unix timestamp (`int(time.time())`), not the
> `PLAN-YYYYMMDD-N` format shown in the example below. The manifest path
> is `plan-<plan_id>/`, not `<plan_id>/`.

```json
{
  "plan_id": "PLAN-20260816-1",
  "branch": "agent-loop/plan-PLAN-20260816-1",
  "base_commit": "abc123...",
  "started_at": "2026-08-16T...",
  "finished_at": "2026-08-16T...",
  "status": "complete|partial|failed",
  "parts": [
    {
      "id": "F1",
      "verdict": "APPROVE",
      "commit": "def456...",
      "tests": ["test_widget_exists"],
      "rounds": 2,
      "cost_usd": 0.0034
    },
    ...
  ]
}
```

This is the evidence ledger the prior reviews asked for (C-§2, W7): one record
per plan, listing each part's verdict, the commit it landed at, and its
acceptance tests.

### What must survive (from §D of the work-breakdown review)

1. **The test-first gate proves the specification is satisfiable.** The plan
   runner does not weaken it. For `op=create` parts, the test-first gate
   distinguishes "suite doesn't build" from "tests are green" (T-BLG1, O64).
2. **The baseline is frozen, not recomputed.** Each part's worktree captures a
   baseline at its base commit (which includes earlier parts' commits). The
   baseline is immutable for that part's run.
3. **`promote` is all-or-nothing, and deliberately not `--3way`.** The plan
   runner composes parts via commits, not via promote's patch application. Each
   part's worktree is created at the previous part's commit, so composition is
   git's own checkout — no patch application needed.
4. **`--feature` reviews the plan whole.** The runner does not re-review the
   plan; it executes it. The plan was reviewed whole by the panel+arbiter in
   `--mode plan --feature`.

### Workflow

```
Step 1: Plan
  agent-loop --mode plan --feature "add a widget subsystem" ...
  → writes plan.json with ordered parts + depends_on

Step 2: List (optional)
  agent-loop --mode run-plan --plan plan.json --list
  → validates regions, expect_green, dependencies

Step 3: Run
  agent-loop --mode run-plan --plan plan.json --apply
  → creates scratch branch, runs each part in dependency order,
    commits each promoted part, stops on failure

Step 4: Promote (manual)
  git merge agent-loop/plan-<plan_id>
  → the user's branch receives the complete feature
```

### Failure modes

| Scenario | What happens |
|---|---|
| Part 1 fails | Plan stops. Branch has no commits. Branch is deleted. |
| Part 2 fails (part 1 promoted) | Plan stops. Branch has part 1's commit. Operator can inspect, fix, and re-run from part 2 with `--from F2`. |
| `depends_on` names unknown id | Refused before any part runs. No branch created. |
| Cycle in `depends_on` | Refused before any part runs. No branch created. |
| Branch already exists | Refused. Operator deletes it or uses `--resume --backlog <path>`. |

> **`--resume`** now exists (implemented in the AGILE Pipeline plan, phase B1).
> It reads `backlog.json` from `logs/agent_loop/plan-<plan_id>/backlog.json`,
> reuses the `plan_id` and `branch`, skips `done` parts, and retries
> `failed`/`blocked` parts. See AGILE_PIPELINE_PLAN.md §B1 for details.
| Suite doesn't build at baseline | Part is refused (T-BLG1/O64). For `op=create`, message explains the scaffold-first workaround. |

### CLI

```
agent-loop --mode run-plan --plan plan.json [options]

Options:
  --plan PATH         Path to the plan JSON (from --mode plan --feature)
  --apply             Commit each promoted part to the scratch branch
  --from PART_ID      Resume from a specific part (skip earlier parts)
  --list              Validate the plan without running it
  --keep-branch       Do not delete the scratch branch on failure
  --max-rounds N      Max rounds per part (default: from config)
  --ticket ID         Run only one part (for debugging)
```

### Interaction with existing components

| Component | Change |
|---|---|
| `cli.py` | New `run-plan` mode entry; `--plan` argument |
| `run_plan_mode.py` (new) | Topological sort, branch management, per-part execution, manifest |
| `loop.py` | `run_ticket` accepts `base_ref` parameter (R5-2, defaults to `HEAD`). The runner passes the scratch branch's HEAD so part N's worktree sees part N-1's promoted code. |
| `workspace.py` | `open_workspace` already accepts a `base` parameter (defaults to `HEAD`). `run_ticket` passes `base_ref` through. |
| `plan_mode.py` | No change. The planner already validates the plan whole. |