# AGILE PIPELINE PLAN — Closing the Gap Between Building Blocks and Agile Team

**Status**: PROPOSED — awaiting review.

**Problem**: The agent-loop has all the building blocks of an agile team
(decomposition, TDD, dependency ordering, per-chunk verification, sequential
execution) but has no glue connecting them into a single autonomous pipeline.
The gaps identified in the feature review:

1. No end-to-end pipeline command (4+ manual CLI invocations required)
2. Test mode is not wired into run-plan (expect_green names tests that don't exist)
3. No recursive/multi-level decomposition (epic -> stories -> tasks)
4. No persistent backlog state across runs
5. No re-planning on part failure
6. No feature-level acceptance criteria

---

## Phase A: TDD-integrated execution (addresses gaps 1 + 2)

**The single most important gap.** `run-plan` runs each part through patch mode
(implement -> gates -> panel -> arbiter) but never generates the failing
acceptance tests that `expect_green` references. So the TDD promise is
structural, not operational: the plan names tests, the gate checks for them,
but nobody creates them.

### A1: `run-plan --tdd` flag

When `--tdd` is passed, `run_plan()` inserts a test-generation step before
patch mode for each part:

```
for each part in dependency order:
    1. test_mode.run_test(part) -> generate failing acceptance tests
    2. verify tests fail at baseline (test_mode already does this)
    3. run_ticket(part) with the generated test file in the worktree
    4. commit promoted files + test file to scratch branch
```

The test file path comes from `default_test_path(profile, part_id)`. The
generated tests are committed alongside the fix so later parts' baselines
include them.

**Why not always-on**: test generation costs a model call per part. For plans
where tests already exist (hand-written or from a prior `--mode test` run),
`--tdd` is wasteful. For plans where tests don't exist, it's essential. The
flag makes the choice explicit.

### A2: `run-plan --pipeline` flag (the end-to-end command)

A convenience flag that chains the full flow in one invocation:

```
agent-loop --mode run-plan --pipeline --feature "Add trailing stop to copier" --apply
```

This runs:
1. brainstorm (optional, if `--brainstorm` also passed)
2. plan --feature (decompose into parts)
3. run-plan --tdd --apply (execute each part with test generation)

Output: a plan manifest + a feature-level verdict.

**Why a flag, not a new mode**: `run-plan` already orchestrates execution.
Adding `--pipeline` makes it the single entry point for the full flow without
creating a new mode that duplicates its logic. The sub-steps (brainstorm,
plan) are called as functions, not as separate CLI invocations.

### A3: Test mode batch support

Currently `test_mode.run_test()` takes a single ticket. For `--tdd` to work
inside `run-plan`, it needs to handle the per-part workflow:

- Part 1 creates `new_file.py` -> test file references `new_file.py`
- Part 2 inserts into `new_file.py` -> test file references the same file

The test for part 2 must run against a worktree that includes part 1's code.
This already works because `run-plan` commits each part to the scratch branch
and the next part's worktree is created at the branch HEAD. The test
generation just needs to happen inside that worktree, not against the base
tree.

**Change**: `test_mode.run_test()` already takes a `repo: Path` — pass the
worktree root, not the live repo root. This is a one-line change in
`run_plan_mode.py`'s per-part loop.

---

## Phase B: Backlog state management (addresses gaps 4 + 5)

### B1: `backlog.json` — persistent plan state

Today `plan.json` is write-once: the planner writes it, `run-plan` reads it,
and neither updates it with part status. A failed run leaves no trace of
which parts succeeded.

**`backlog.json`** is the plan JSON + a `status` field per part:

```json
{
  "feature": "Add trailing stop to copier",
  "created_at": "...",
  "updated_at": "...",
  "parts": [
    {
      "id": "F1",
      "title": "...",
      "status": "done",        // done | in_progress | blocked | pending | failed
      "verdict": "APPROVE",
      "commit": "abc123",
      "attempts": 1,
      "last_error": "",
      ...existing ticket fields...
    },
    {
      "id": "F2",
      "title": "...",
      "status": "failed",
      "verdict": "MAX_ROUNDS_EXHAUSTED",
      "attempts": 2,
      "last_error": "arbiter could not converge on lock-scope finding",
      ...
    }
  ]
}
```

`run-plan` writes `backlog.json` after each part. On re-run with
`--resume`, it reads `backlog.json` instead of `plan.json`, skips `done`
parts, and retries `failed`/`blocked` parts.

### B2: Re-plan on failure

When a part fails, `run-plan` currently stops the chain. With `--replan`,
it instead:

1. Records the failure in `backlog.json`
2. Feeds the failure feedback (verdict + arbiter findings + test errors) to
   `plan_mode.run_plan()` as a re-planning prompt for JUST that part
3. The re-plan produces either:
   - A revised single-part ticket (same id, refined regions/spec)
   - A split into smaller sub-parts (new ids, `depends_on` the failed part's
     dependencies)
4. Runs the revised part(s) through the same TDD + patch + verify cycle
5. Continues the chain

`--replan-limit N` caps how many times a single part can be re-planned
(default 2). After N replans, the part is marked `blocked` and the chain
continues with independent parts (those that don't depend on the blocked
part).

### B3: Skip and continue

With `--continue-on-failure`, `run-plan` marks the failed part as `failed`
in `backlog.json` and continues to the next independent part (one that
doesn't `depend_on` the failed part). Dependent parts are marked `blocked`.

This is the agile behaviour: a blocked story doesn't stop the sprint, it
moves to the backlog and the team pulls the next available story.

---

## Phase C: Multi-level decomposition (addresses gap 3)

### C1: Two-tier decomposition

Today `--feature` does one level: feature -> parts. For large epics, the
parts can be too large to converge in 4 rounds.

**`--epic` flag on plan mode**:

```
agent-loop --mode plan --epic "Add risk-adjusted position sizing to the copier"
```

This runs plan mode twice:

1. **Story-level decomposition**: the epic breaks into user-valued stories,
   each with a one-paragraph description and acceptance criteria. No regions,
   no code-level anchors — just "what does this story deliver?"
2. **Task-level decomposition per story**: each story breaks into technical
   tasks (tickets with regions, anchors, `expect_green`). This is the
   existing `--feature` flow, called once per story.

Output: a `backlog.json` with two levels of nesting — stories contain tasks.

### C2: Size heuristic for auto-splitting

The plan mode prompt asks for "the SMALLEST parts." But the model's
definition of "small" varies. A mechanical check already exists:
`_validate_feature_plan()` rejects regions > `max_region_lines` (default
150). 

**Add a ticket-level size check**: if a part's `spec` exceeds a token
threshold (e.g., 500 tokens) or it has > 3 regions, the planner is asked to
split it. This is advisory (the planner can argue the part is cohesive), not
a hard refusal.

### C3: Recursive re-planning

When `--replan` (from B2) splits a failed part, the sub-parts go through the
same size check. If a sub-part is still too large, it's split again. The
`--replan-limit` caps the recursion depth.

---

## Phase D: Feature-level acceptance (addresses gap 6)

### D1: `feature_acceptance` field on plan JSON

After all parts are done, the feature should work end-to-end. Today "complete"
means "all parts promoted" — it doesn't verify the feature.

**Add an optional `feature_acceptance` field** to the plan JSON:

```json
{
  "feature": "Add trailing stop to copier",
  "feature_acceptance": [
    "test_trailing_stop_follows_price",
    "test_trailing_stop_triggers_exit"
  ],
  "parts": [...]
}
```

These are integration-level tests that run AFTER all parts are done. They're
generated by `test_mode` with the full feature spec (not a single part's
spec) and run against the scratch branch HEAD (which has all parts' code).

### D2: Feature verdict

`run-plan` produces a feature-level verdict in addition to per-part verdicts:

- `FEATURE_COMPLETE`: all parts done + feature acceptance tests pass
- `FEATURE_PARTIAL`: all parts done but feature acceptance tests fail
- `FEATURE_INCOMPLETE`: some parts failed/blocked
- `FEATURE_FAILED`: a critical part (one that everything depends on) failed

This is the verdict that matters to the operator. Per-part verdicts are
diagnostic detail.

---

## Phase E: CLI ergonomics (addresses gap 1 fully)

### E1: Single-command pipeline

```bash
# Full autonomous flow: epic -> stories -> tasks -> tests -> patches -> green
agent-loop --mode run-plan --pipeline --epic "..." --apply

# Feature-level (one tier of decomposition)
agent-loop --mode run-plan --pipeline --feature "..." --apply

# Just execute an existing plan with TDD
agent-loop --mode run-plan --plan plan.json --tdd --apply

# Resume a partial run
agent-loop --mode run-plan --backlog backlog.json --resume --apply
```

### E2: Status command

```bash
agent-loop --mode backlog --backlog backlog.json
```

Prints the current state of the backlog: which parts are done/in-progress/
blocked/failed, with verdicts and error summaries. This is the agile
"standup" — what's done, what's in progress, what's blocked.

---

## Sequencing

| Phase | Depends on | Effort | Priority |
|---|---|---|---|
| A (TDD-integrated execution) | nothing | medium | **highest** — without this, the TDD promise is hollow |
| B (backlog state) | A (partially) | medium | high — enables resume and re-planning |
| E1 (single-command pipeline) | A + B | small | high — ergonomic wrapper, not new logic |
| D (feature acceptance) | A | small | medium — important but not blocking the pipeline |
| C (multi-level decomposition) | B | large | medium — needed for large epics, not for typical features |
| B2 (re-plan on failure) | B1 | medium | medium — nice to have, not blocking |
| B3 (skip and continue) | B1 | small | low — simple once backlog exists |
| E2 (status command) | B1 | small | low — convenience |

**Recommended order**: A -> B1 -> E1 -> D -> B2 -> B3 -> C -> E2

---

## What this plan does NOT do

- **Does not add new modes.** Everything is an extension of existing modes
  (plan, test, run-plan). No new state machine, no new gate ladder.
- **Does not change the verification stack.** The panel + arbiter + gates
  are unchanged. The pipeline just chains them.
- **Does not change the model registry.** The same models serve the same
  roles. Test generation uses the `test` mode's config (implementer model).
- **Does not add human-in-the-loop checkpoints between parts.** The pipeline
  runs autonomously. Human review happens at the end (feature verdict) or
  via `--apply` (which requires explicit confirmation). A `--pause-on-part`
  flag could be added later if desired.
- **Does not address docs mode conventions (O10).** That's a separate
  backlog item.

---

## Risks

1. **Test generation quality.** `test_mode` generates tests from the spec.
   If the spec is vague, the tests are vague, and a vague test that passes
   gates nothing. Mitigation: the panel reviews the plan (including
   `expect_green` entries) before execution. A bad test name is caught at
   plan review, not at execution.

2. **Re-planning loops.** A part that keeps failing could be re-planned
   indefinitely. `--replan-limit` caps this. After the limit, the part is
   `blocked` and the operator decides.

3. **Cost.** `--tdd` adds one model call per part (test generation).
   `--replan` adds plan + test + patch calls per re-plan attempt. A 4-part
   feature with 2 re-plans could cost 4 + 4 + 2*(plan+test+patch) = ~14
   model calls. This is still cheaper than a human doing the same work.

4. **Scratch branch complexity.** The branch management in `run-plan` is
   already subtle (commit to branch, reset user HEAD). Adding test files and
   re-planned parts increases the surface. Mitigation: the existing tests
   for `run_plan_mode` cover the branch logic; extend them.

5. **Multi-level decomposition prompt quality.** Two-tier decomposition
   relies on the model producing good story-level descriptions before
   task-level tickets. If the story decomposition is wrong, every task
   under it is wrong. Mitigation: the panel reviews the story-level plan
   before task-level decomposition proceeds.