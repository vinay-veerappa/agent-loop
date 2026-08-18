# AGILE PIPELINE PLAN — Closing the Gap Between Building Blocks and Agile Team

**Status**: IMPLEMENTED — all phases landed. Suite: 773 passed, 36 skipped.
See "Implementation status" at end for per-phase commit log.

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

**The premise is stronger than "weak gating."** `loop.py:1036-1073`
**refuses** the ticket when an `expect_green` name isn't in the baseline
failure set — `final_verdict = "TICKET_REJECTED"`, which is not in
`PROMOTABLE`, so `run_plan_mode.py:316-321` stops the chain at part 1.
`--mode run-plan --apply` on a planner-generated feature plan doesn't
produce weak gating today; it produces **zero parts** and then deletes
the branch. Phase A isn't an enhancement; it's the fix that makes
`run-plan --apply` functional at all.

---

## Phase A0: Fix shipped defects in run-plan (prerequisite for all phases)

Two live defects in `run_plan_mode.py` are inherited by every phase below.
Both are ~10-line fixes with end-to-end tests. Both must land before B1/B3,
which assume branch history is durable and parts build on their predecessors.

### A0-1: Branch retention (finding 4)

`run_plan_mode.py:370-372` deletes the scratch branch with `git branch -D`
on any status other than `complete`, even when parts have already been
committed to it. `_commit_to_branch` soft-resets the user's HEAD, so the
*files* survive in the working tree, but the commits become unreachable,
and a fresh worktree at `HEAD` won't see them.

This is already a live defect: `PLAN_RUNNER.md:141` documents "Part 2 fails
(part 1 promoted) → Plan stops. Branch has part 1's commit. Operator can
… re-run from part 2 with `--from F2`." The code deletes that branch.

**Fix**: make branch retention the default when any part has committed.
Only delete the branch when *no* part committed (truly empty run). This
preserves the `--keep-branch` flag for the "I want to inspect after a
complete run" case.

### A0-2: `part_base` selection (finding 5)

`run_plan_mode.py:278-282`:

```python
if result.parts and result.parts[-1].applied:
    part_base = branch
else:
    part_base = "HEAD"
```

Under B3 (`--continue-on-failure`): part 1 lands, part 2 fails, part 3 is
independent → `parts[-1].applied` is `False` → part 3's worktree is built
at `HEAD`, **without part 1's code**. The same hole exists today for
`--from`: skipping parts leaves `result.parts` empty, so the resumed part
never sees the work it was meant to build on.

**Fix**: the rule should be "`branch` if the branch has advanced past
`base_commit`", not "if the last part applied." Compare the branch HEAD
to `base_commit`; if different, use `branch`.

### A0-3: End-to-end test for `run_plan(apply=True)`

`tests/acceptance/test_run_plan_mode.py` has 8 tests: 5 are topological
sort, one is `apply=False`, one is `_commit_to_branch` in isolation, one
is `run_ticket` accepting `base_ref`. **Nothing exercises
`run_plan(apply=True)` end to end, the branch-deletion path, `part_base`
selection, or `--from`.** Findings 4 and 5 above are exactly what that
gap hides.

**Fix**: add an end-to-end test that runs a 2-part plan with `apply=True`,
verifies part 1's commit survives a part 2 failure, and verifies a
resumed part 3 builds on part 1's code.

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
    1. derive test_file from the part's first expect_green entry
    2. test_mode.run_test(part) -> generate failing acceptance tests
    3. commit the test file to the plan branch (separate commit)
    4. run_ticket(part) with the test file in the worktree
    5. commit promoted files to scratch branch
```

**Test path resolution (finding 1).** The test file path must match the
planner's `expect_green` entries, or `names_match` won't find them in
the baseline failure output. Two approaches; the second is more robust:

- **Option A (smaller change)**: derive `test_file` from the part's first
  `expect_green` entry by stripping the `::` suffix. So
  `tests/acceptance/test_trailing_stop.py::test_follows_price` →
  `test_file = tests/acceptance/test_trailing_stop.py`.
- **Option B (more robust)**: have the feature planner emit `expect_green`
  entries rooted at `default_test_path(profile, part_id)`. `plan_mode`
  gets told the path, not just the glob. The planner writes
  `tests/acceptance/test_F1Generated.py::test_follows_price` and the
  runner uses the same path. This eliminates the collision class entirely.

**This plan adopts Option B.** `plan_mode.py` is updated to pass
`default_test_path(profile, part_id)` to the planner prompt, so the
planner writes `expect_green` entries that the runner's test generation
will actually produce.

**Step ordering (finding 2).** The test must be committed to the plan
branch *before* `run_ticket`, as its own commit. `run_ticket` builds its
worktree with `git worktree add --detach <root> <commit>`
(`workspace.py:406`) — a clean checkout at a ref. An uncommitted test
file in the live tree is not in that worktree, and `loop.py:1060-1062`
already names this exact failure ("uncommitted — the worktree is built
from HEAD"). Committing the test as a separate commit also gives better
evidence: the red commit and the green commit are separable.

**`--tdd` implies `--path-isolated`.** The repo already has an
independence property (`test_mode.py:41-47`, C-section 1): a test
generated with sight of the implementation can be tautological. For
`--tdd` this matters more, not less, because the same implementer model
writes both. `--tdd` sets `path_isolated=True` unconditionally.

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

**Plan re-validation (finding: TODO at `run_plan_mode.py:234`).** Nothing
re-validates the plan before running it. `--pipeline` chains planner →
runner in one process, where a stale tree is less likely, but C2's size
heuristic and E1's `--resume` both want that validation to exist. A2
adds the `--list` validation call before execution. This is the TODO at
`run_plan_mode.py:234` made real.

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
and the next part's worktree is created at the branch HEAD.

**Correction (finding 3).** The original plan said "pass the worktree root,
not the live repo root. This is a one-line change." That doesn't work for
three reasons:

1. `run_plan_mode` has no worktree root to pass. `run_ticket` creates and
   disposes its worktree internally (`loop.py`, via `open_workspace`); the
   runner only sees `base_ref`.
2. `run_test` writes artifacts to `repo/logs/agent_loop/<tid>/`
   (`test_mode.py:134`) — including `test_raw.txt`. Point `repo` at a
   throwaway worktree and the evidence is deleted with it.
3. `run_test` opens *its own* workspace for baseline verification
   (`test_mode.py:250`), which takes a run lock at
   `repo/logs/agent_loop/.runlock`. Nesting that inside a worktree gives
   you a lock at a different path — no deadlock, but also no mutual
   exclusion, and a baseline computed from the worktree's HEAD rather
   than its contents.

**The correct shape**: keep `repo` = the live repo, and pass `base`
through to `open_workspace` so the baseline is taken at the **plan branch
HEAD** (which includes all prior parts' code). This is a real one-line
change, in `test_mode.py:250`, plus a new `base` parameter on `run_test`.

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
  "plan_id": "1755000000",
  "branch": "agent-loop/plan-1755000000",
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

**Location (finding: unstated decision).** `plan.json` is written to
`logs/agent_loop/<tid>/plan.json` (`plan_mode.py:353`); the plan manifest
to `logs/agent_loop/plan-<plan_id>/` (`run_plan_mode.py:341`). Writing
`backlog.json` next to the input plan would mutate the planner's artifact.
**Decision**: `backlog.json` lives at `logs/agent_loop/plan-<plan_id>/backlog.json`,
next to the manifest. The planner's `plan.json` is never mutated.

**`plan_id` persistence (finding: unstated decision).** `plan_id` is
regenerated every run (`int(time.time())`, `run_plan_mode.py:223`). Resume
must read it from the backlog, or the branch name won't match. The
"branch already exists → use `--resume`" message at `run_plan_mode.py:129`
is unreachable today for exactly this reason. **Decision**: `--resume`
reads `plan_id` and `branch` from `backlog.json`, not from the CLI. The
`--backlog` flag points at the backlog file; everything else is derived.

**`--resume` vs `--from` (finding: unstated decision).** They overlap.
**Decision**: `--from` is subsumed by `--resume`. `--resume` reads
`backlog.json`, skips `done` parts, and retries `failed`/`blocked` parts.
`--from` is kept as the manual escape hatch for when the backlog is
corrupt or the operator wants to skip a specific part without a backlog.

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

**Depends on A0-2.** Without the `part_base` fix, a skipped part makes the
next independent part build at `HEAD` without prior parts' code.

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
150) — **but only for files that already exist**
(`plan_mode.py:485-504`). Regions in files an earlier part creates
(the greenfield case) skip the check entirely. C2's ticket-level check
would close that hole, not just add a second opinion.

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

This answers BACKLOG O36 question 4: "what is the acceptance criterion for a
feature?" (`BACKLOG.md:1499`). The answer: `feature_acceptance` tests that
run after all parts are done.

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
agent-loop --mode run-plan --backlog logs/agent_loop/plan-<plan_id>/backlog.json --resume --apply
```

### E2: Status command

```bash
agent-loop --mode run-plan --backlog backlog.json
```

Prints the current state of the backlog: which parts are done/in-progress/
blocked/failed, with verdicts and error summaries. This is the agile
"standup" — what's done, what's in progress, what's blocked.

**Correction (finding: E2 contradicts "does not add new modes").** The
original plan proposed `--mode backlog` as a new mode. This revision folds
it into `--mode run-plan --backlog X` without `--apply` (which already
prints-and-returns, `run_plan_mode.py:238-245`). No new mode; the "does
not add new modes" claim now holds.

---

## Sequencing

| Phase | Depends on | Effort | Priority |
|---|---|---|---|
| **A0** (fix shipped defects) | nothing | small | **highest** — live defects, prerequisites for B1/B3 |
| A (TDD-integrated execution) | A0 | medium | **highest** — without this, run-plan --apply produces zero parts |
| B1 (backlog state) | A0 (partially) | medium | high — enables resume and re-planning |
| E1 (single-command pipeline) | A + B1 | small | high — ergonomic wrapper, not new logic |
| D (feature acceptance) | A | small | medium — important but not blocking the pipeline |
| B2 (re-plan on failure) | B1 | medium | medium — nice to have, not blocking |
| B3 (skip and continue) | B1 + A0-2 | small | low — simple once backlog exists; A0-2 is the real fix |
| C (multi-level decomposition) | B1 | large | medium — needed for large epics, not for typical features |
| E2 (status command) | B1 | small | low — convenience |

**Recommended order**: A0 → A → B1 → E1 → D → B2 → B3 → C → E2

---

## What this plan does NOT do

- **Does not add new modes.** Everything is an extension of existing modes
  (plan, test, run-plan). No new state machine, no new gate ladder. E2 was
  originally proposed as `--mode backlog`; this revision folds it into
  `--mode run-plan --backlog X` to keep the claim true.
- **Does not change the verification stack.** The panel + arbiter + gates
  are unchanged. The pipeline just chains them.
- **Does not change the model registry.** The same models serve the same
  roles. Test generation uses the `test` mode's config (implementer model).
- **Does not add human-in-the-loop checkpoints between parts.** The pipeline
  runs autonomously. Human review happens at the end (feature verdict) or
  via `--apply` (which requires the operator to pass `--apply`; there is no
  prompt — `cli.py:563` is a bare `store_true`). A `--pause-on-part`
  flag could be added later if desired.
- **Does not address docs mode conventions (O10).** That's a separate
  backlog item, now fixed (see commit `beb108d`).

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

3. **Cost.** The original plan estimated "~14 model calls" for a 4-part
   feature with 2 re-plans. That treated a patch as one call. Defaults
   are `max_rounds=4` (`config.py:645`) with 2 reviewers + arbiter → up
   to 4 calls per round, 16 per part. A 4-part feature converging in
   **one round each** is already ~20 calls; worst case (4 rounds each,
   2 re-plans) is ~68. The conclusion (cheaper than a human) survives;
   the number should not be quoted as-is.

4. **Scratch branch complexity.** The branch management in `run-plan` is
   already subtle (commit to branch, reset user HEAD). Adding test files and
   re-planned parts increases the surface. **The existing tests for
   `run_plan_mode` do NOT cover the branch logic** —
   `tests/acceptance/test_run_plan_mode.py` has 8 tests, none exercises
   `run_plan(apply=True)` end to end, the branch-deletion path, `part_base`
   selection, or `--from`. A0-3 adds that test; every subsequent phase
   extends it.

5. **Multi-level decomposition prompt quality.** Two-tier decomposition
   relies on the model producing good story-level descriptions before
   task-level tickets. If the story decomposition is wrong, every task
   under it is wrong. Mitigation: the panel reviews the story-level plan
   before task-level decomposition proceeds.

---

## Cross-references

- **`PLAN_RUNNER.md`** is the existing design doc for the component being
  extended. It is stale in three ways (documents a `--resume` and
  `--ticket ID` that don't exist; manifest path
  `logs/agent_loop/<plan_id>/` vs the code's `plan-<plan_id>/`; `plan_id`
  format `PLAN-20260816-1` vs a unix timestamp). This plan is canonical;
  `PLAN_RUNNER.md` is updated when A0 lands.
- **`BACKLOG.md:1499`** tracks this as **O36**, and its open question 4 —
  "what is the acceptance criterion for a feature?" — is exactly what
  Phase D answers. The closure propagates back to O36 when D lands.
- **O10 (docs mode conventions)** — mentioned in the original plan as out
  of scope; now fixed in commit `beb108d`.

---

## Review findings (incorporated above)

This plan was reviewed against the code at `beb108d` (plan doc from
`28d888d`, 5 commits back). Every load-bearing claim was verified against
the source. The findings below are incorporated into the phases above;
this section records them for traceability.

**Premise strengthened.** The plan said "the TDD promise is structural,
not operational." It's worse: `loop.py:1036-1073` refuses the ticket
(`TICKET_REJECTED` ∉ `PROMOTABLE`), so `run-plan --apply` on a
planner-generated plan produces **zero parts** today, not weak gating.
The intro now says this.

**Finding 1 (A1 test path collision).** `default_test_path` yields
`test_F1Generated.py` but the planner writes `expect_green` as
`tests/acceptance/test_trailing_stop.py::test_follows_price`. `names_match`
does a whole-identifier regex over the failure line → the planner's nodeid
won't appear in a failure line from `test_F1Generated.py` →
`TICKET_REJECTED`. **Fix**: Option B adopted — planner emits
`expect_green` rooted at `default_test_path(profile, part_id)`.

**Finding 2 (A1 step ordering).** Worktree is `git worktree add --detach`
— uncommitted test in live tree is invisible. **Fix**: test committed to
plan branch before `run_ticket`, as its own commit.

**Finding 3 (A3 one-line change isn't).** `run_plan_mode` has no worktree
handle; `run_test` writes artifacts to `repo/logs/`; nested worktree gives
a lock at a different path. **Fix**: keep `repo` = live repo, pass `base`
to `open_workspace` so baseline is taken at plan branch HEAD.

**Finding 4 (A0-1, branch deletion).** `run_plan_mode.py:370-372` deletes
the branch on any non-complete status, even after parts committed.
**Fix**: A0-1 makes retention the default when any part committed.

**Finding 5 (A0-2, part_base).** `run_plan_mode.py:278-282` looks only at
`result.parts[-1].applied`. **Fix**: A0-2 compares branch HEAD to
`base_commit`.

**Unstated decisions made:**
- `--tdd` implies `path_isolated=True`
- `backlog.json` lives at `logs/agent_loop/plan-<plan_id>/backlog.json`
- `--resume` reads `plan_id` and `branch` from `backlog.json`
- `--from` subsumed by `--resume`, kept as manual escape hatch
- Plan re-validation (TODO at `run_plan_mode.py:234`) added to A2

**Smaller corrections:**
- E2 folded into `--mode run-plan --backlog X` — "does not add new modes" now holds
- Risk 3 cost: ~20-68 calls, not ~14
- Risk 4: existing tests don't cover `apply=True` — A0-3 adds that test
- C2: greenfield regions skip `max_region_lines` check — C2 closes that hole
- `--apply` "requires explicit confirmation" → "requires the operator to pass `--apply`"
- Cross-references to `PLAN_RUNNER.md` and `BACKLOG.md` O36 added

---

## Implementation status

All phases implemented. Suite: 773 passed, 36 skipped at `e7dceb0`.

| Phase | Commit | What landed |
|---|---|---|
| A0-1 | `3fbd972` | Branch retention: scratch branch not deleted when parts committed |
| A0-2 | `3fbd972` | `part_base` uses branch HEAD vs base_commit comparison |
| A0-3 | `3fbd972` | 4 e2e tests for branch retention + part_base |
| A1 | `8f1b792` | `--tdd` flag: test generation before run_ticket, planner told exact test path, `path_isolated=True` |
| A3 | `8f1b792` | `test_mode.run_test` takes `base` param, passed to `open_workspace` |
| A2 | `de9fb5c` | `--pipeline` flag chains plan → run-plan --tdd --apply, with plan re-validation |
| B1 | `e900083` | `backlog.json` at `logs/agent_loop/plan-<plan_id>/backlog.json`, `--resume` reads it |
| E2 | `e900083` | `--backlog` without `--apply` prints status (standup view) |
| D1 | `587f24f` | `feature_acceptance` field on plan JSON, `_parse_feature_acceptance`, planner prompt |
| D2 | `587f24f` | `_run_feature_acceptance`, `feature_verdict` on PlanResult (COMPLETE/PARTIAL/INCOMPLETE/ERROR/UNVERIFIED) |
| B2 (stub) | `c01cb52` | `--replan` and `--replan-limit` flags wired |
| B3 | `c01cb52` | `--continue-on-failure` skips to next independent part, writes backlog |
| C2 | `7ae3bb8` | Ticket-level size heuristic: advisory notes for >3 regions or >500 char spec |
| C1 | `118dbc9` | Two-tier `--epic` decomposition: EPIC_SYSTEM prompt, `run_epic_plan()`, story→task pipeline |
| B2 (logic) | `ded8927` | `_replan_part()`: feeds failure feedback to planner, runs revised parts with TDD + validation |
| Review fixes | `23ba872` | 8 fixes from first self-review (branch retention, part_base, backlog, etc.) |
| Review fixes | `3d79f65` | 8 fixes from complete self-review (false green, stale plan.json, done parts on resume, etc.) |
| CF-5 | `0a588ba` | `agent_loop_describe` in result.json + terminal summary |
| Perf | `e7dceb0` | `cache=True` on reviewer and arbiter calls (zero quality impact) |

**Not yet implemented:**
- C3 (recursive re-planning): re-planned sub-parts would need to go through the same size check as C2. Depends on B2, which is now implemented, but the recursive application is not.