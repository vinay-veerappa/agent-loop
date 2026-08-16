# Agent Loop Review — Work Breakdown, Throughput, Reliability

> **Read at** `v0.6.7` / `374dc36`, clean tree, **2026-08-16**.
> **Suite re-run for this review**: `636 passed, 34 skipped in 61.04s` (Python 3.14, Windows).
> **Two claims below were MEASURED by running the code, not inferred from reading it.**
> They are marked ⚡ and the exact commands are given, because both contradict
> what the source comments say the system does.

## Why a fourth review, and what this one is not

Three reviews already sit in this folder, and this one deliberately does not
repeat them:

| Document | Axis | Overlap with this one |
|---|---|---|
| [`AGENT_LOOP_CRITICAL_REVIEW.md`](./AGENT_LOOP_CRITICAL_REVIEW.md) | design & prompt architecture | §2 there says the loop is *"not grounded in truly atomic Agile task items"*. It argues the principle. **This review shows the mechanism that makes it structurally impossible**, and measures it. |
| [`AGENT_LOOP_INDEPENDENT_ENGINEERING_REVIEW.md`](./AGENT_LOOP_INDEPENDENT_ENGINEERING_REVIEW.md) | code-level audit, cost, latency | Its §2A ("no size constraint per ticket") is one item; the throughput numbers here extend its latency table with the multiplier that decomposition introduces. |
| [`AGENT_LOOP_THIRD_REVIEW.md`](./AGENT_LOOP_THIRD_REVIEW.md) (untracked) | independent code audit, N1–N9 | **No overlap intended.** N1–N9 are unenforced invariants and parser/concurrency defects. Nothing here restates them; where a fix interacts, it is cross-referenced. |

The three of them, taken together, have one blind spot. All three review **one
ticket's journey through the loop**. Nobody has reviewed **what happens when
there are four tickets and part 2 needs part 1 to exist** — which is exactly the
capability being asked for. That is §A below, and it is the largest finding in
this document.

Section map:

- **§A — Work breakdown.** Why the loop cannot execute a decomposed plan today. 7 findings, `W1`–`W7`.
- **§B — Reliability.** What has actually been failing, from the backlog and from one new measured defect. 6 findings, `R1`–`R6`.
- **§C — Throughput.** Where the wall clock goes and which parts are recoverable. 5 findings, `S1`–`S5`.
- **§D — What is genuinely good**, and must survive any change made here.
- **§E — Ranked plan**, each item with the measurement that would prove it closed.

New IDs are `W`/`R`/`S` prefixed so they cannot collide with the backlog's `O1`–`O67`,
the engineering review's `E`-numbers, or the third review's `N1`–`N9`.

---

# §A — Work breakdown: the loop plans a chain it cannot run

## The shape of the problem in one paragraph

`--mode plan --feature` does a genuinely good job. It refuses a part with no
`expect_green`, refuses two parts creating the same file, refuses a region
targeting a file no earlier part creates, walks the parts **in order**, and
carries forward the set of files earlier parts will create
([`plan_mode.py:404-496`](../../src/agent_loop/plan_mode.py)). It emits
`depends_on`. It reviews the plan **whole** rather than part-by-part, with a
comment explaining that reviewing part 1 of 4 misses the only thing worth
reviewing in a decomposition.

Then the plan is written to `plan.json` and **handed to a runner that has no
concept of a plan at all**. Everything the planner established — order,
dependency, the promise that a deferred anchor "is resolved by the loop at the
moment it runs that part" — is discarded at the file boundary.

## ⚡ W1. A later part cannot see an earlier part's work. MEASURED.

**Severity: this is the one that blocks the feature the user is asking for.**

`plan_mode._validate_feature_plan` permits part 2 to anchor into a file part 1
creates, and says so explicitly:

```python
# plan_mode.py:484-495
# An anchor inside a file a previous part will create cannot be
# checked yet -- there is nothing to look in. Deferred to the loop,
# which resolves regions per ticket at the moment it runs it.
```

The loop resolves regions against the **worktree**
([`loop.py:688`](../../src/agent_loop/loop.py) — `regions.extract(ws.root, ...)`),
and the worktree is created from the **HEAD commit**
([`workspace.py:365`](../../src/agent_loop/workspace.py) — `_git(repo, "rev-parse", base)`,
`base="HEAD"`). `promote()` applies the approved patch to the live **working
tree**, uncommitted ([`workspace.py:272-283`](../../src/agent_loop/workspace.py)).

So part 1's output lands somewhere part 2's worktree cannot see.

Measured, on a scratch repo, using this package's own `workspace` module:

```
$ PYTHONPATH=src python -c "
from pathlib import Path; from agent_loop import workspace
repo = Path(r'C:\Users\vinay\alx').resolve()
with workspace.open_workspace(repo,'T1') as ws:
    (ws.root/'new.txt').write_text('made by T1')
    ws.stage_new_files(['new.txt'])
    print('T1 promote ->', ws.promote(['new.txt']))
print('live new.txt exists:', (repo/'new.txt').exists())
with workspace.open_workspace(repo,'T2') as ws2:
    print('T2 base:', ws2.base_commit[:8])
    print('T2 sees new.txt:', (ws2.root/'new.txt').exists())"

T1 promote -> ['new.txt']
live new.txt exists: True
T2 base: 8fef6022
T2 sees new.txt: False        <-- the finding
```

The consequence for a real feature plan: part 2 with `op=insert` into a file part 1
created raises
`RegionError: R1: file does not exist: <path>` ([`regions.py:638-639`](../../src/agent_loop/regions.py)),
which `cli.py:614` catches as a bare `ERROR F2: RegionError: ...` and then
**continues to part 3**.

Note the disagreement inside the codebase itself. `promote`'s docstring
([`workspace.py:204-206`](../../src/agent_loop/workspace.py)) says the patch
approach "lets a later ticket compose its edits with an earlier promoted
ticket's uncommitted changes". That is true of `promote`'s own mechanics and
false of the pipeline, because the later ticket's worktree never contains those
uncommitted changes to compose with. **Two components are each individually
correct and the join is the defect** — the same shape as `P2-109` in the
`nt8-mcp-bridge` repo, and it is invisible to any review that reads one file.

**The workaround that exists today** is undocumented: `git commit` between every
part. Nothing in the README, `plan_mode.py`, or the printed output says so, and
the failure mode is a `RegionError` that reads as a bad anchor.

## W2. `depends_on` is written by the planner and read by nothing

`FEATURE_SYSTEM` instructs the model to emit `depends_on`
([`plan_mode.py:68,77`](../../src/agent_loop/plan_mode.py)). A grep over the
whole package:

```
$ grep -rn "depends_on" src/agent_loop/
src/agent_loop/plan_mode.py:68:   `depends_on`. Two parts must not create the same file.
src/agent_loop/plan_mode.py:77:   "depends_on": [],
```

Two hits, both inside the prompt string. `load_tickets`
([`cli.py:31-75`](../../src/agent_loop/cli.py)) validates only that each ticket
is a dict with an `id`. No reference check (a `depends_on` naming a part that
does not exist passes), no cycle check, no topological sort, no gating.
`_validate_feature_plan` — which validates six other things — does not look at
it either.

This is **dead safety machinery**: a field that reads as a dependency
guarantee in every artifact it appears in, backed by nothing. It passes the
panel review (reviewers see a well-formed dependency graph), it passes
`--list`, and it is a comment.

## W3. The runner has no plan semantics: file order, no stop, no rollback

[`cli.py:592-616`](../../src/agent_loop/cli.py):

```python
for t in tickets:
    if t["id"] not in wanted: continue
    ...
    except Exception as exc:
        print(f"  ERROR {t['id']}: ...")
        results.append({... "applied": False})
```

Three properties fall out, and all three are wrong for a decomposed plan:

1. **Order is JSON array order**, not dependency order. Correct today only
   because the planner happens to emit build order and nothing reorders it.
   A hand-edited ticket file, or a `--ticket F3 --ticket F1` invocation,
   silently runs backwards.
2. **A failed part does not stop the chain.** Parts 2, 3 and 4 each run a full
   baseline suite and up to four model rounds against a tree where part 1 never
   landed. On a `dotnet test` consumer that is tens of minutes of certain
   failure. The exit code is correct (`cli.py:628` counts every non-promotable),
   but the cost is paid first.
3. **There is no rollback.** Parts 1 and 2 promoted, part 3 fails: the live tree
   holds two thirds of a feature, uncommitted, and nothing records which parts
   are in it. The `--apply` flag is per-invocation; there is no plan-level
   transaction and no manifest.

## W4. `--mode test` is single-ticket, so the TDD half does not decompose

[`cli.py:210-215`](../../src/agent_loop/cli.py) takes `tickets[0]`, or the first
`--ticket` id. Generating the red tests for a 4-part plan is four invocations,
each re-typing `--defect` (which is a *feature-level* description being used as
a *part-level* one — the model is told the whole feature and asked for one
part's tests, with no statement of which part).

So the workflow the loop advertises —
`plan → test → patch` — has a one-to-many step in the middle that the CLI
models as one-to-one. In practice this is where a ticket author gives up and
hand-writes the tests, which is exactly what the `nt8-riskguard` operator did
(backlog O64) and what this repo's own `--feature` runs did.

## W5. O64 is the decomposition case, not an edge case

Backlog O64 records that the test-first gate refuses additive work: no test can
be red against a function that does not exist, because the suite does not
compile, and a suite that does not compile produces no failure lines.

It is filed as a rough edge. It is not — **it is the first part of every feature
plan**. Part 1 of a decomposition is, by construction, `op=create` on a file
that does not exist yet. The gate at [`loop.py:669-682`](../../src/agent_loop/loop.py)
refuses it with `TICKET_REJECTED`, and the message it prints
(*"Either the name is wrong, or the test passes without the fix"*) names
neither of the two causes that actually apply.

The consumer's discovered workaround — commit a compiling stub, then let the
loop fill the bodies — is a good process and is written down nowhere.
Combined with `W1`, the honest description of feature mode today is:
**it plans a chain, and a human must hand-scaffold and hand-commit between
every link.**

## W6. Nothing bounds the size of a work package

Already raised (engineering review §2A). Adding the mechanical form, because
"keep tickets small" is advice and this needs a gate: `_validate_feature_plan`
checks six structural properties and none of them is a size. A single part may
declare five regions spanning several hundred lines; the reviewer panel then
diverges, compaction blows the budget, and the round is lost.

The check is cheap and belongs beside the existing six: after regions resolve,
sum the line spans and refuse a part above a configured ceiling with the
instruction to split it. The information is already in hand — `regions.extract`
returns `lines_1based` and `plan_mode` already prints them.

## W7. There is no evidence ledger per work package

Raised in both prior reviews; restating only the part that matters for
decomposition. `append_ledger` ([`loop.py:548`](../../src/agent_loop/loop.py))
records a terminal verdict per ticket. For a plan, the question a human actually
asks is *"which parts of this feature are done, proven by what, and what is left"* —
and answering it today means reading four `logs/agent_loop/<ID>/result.json`
files and knowing which ids belonged to the same plan. The plan has no identity
after `plan.json` is written.

## What §A adds up to

The loop has a **planner** that decomposes, and an **executor** that runs
independent tickets against an immutable base. There is no component between
them. Everything the user is asking for lives in that gap:

```
plan --feature  ──►  plan.json  ──►  (nothing)  ──►  run_ticket × N
   orders parts        depends_on      ▲              fresh worktree @ HEAD
   validates chain     order           │              no ordering
   reviews whole       deferred anchors│              no stop-on-fail
                                       │              no rollback
                              THIS IS THE GAP
```

The good news is that this is a **missing component, not a wrong design**. Every
primitive it needs already exists and is tested: worktrees, `promote`'s
all-or-nothing patch application, `_validate_feature_plan`'s ordered walk,
`stage_new_files`, the run lock, the ledger. See `§E-1` for the shape.

---

# §B — Reliability: what has actually been failing

## ⚡ R1. The encoding gate inspects 26 of 29 source files, and the 3 it misses hold the defect it was written for. MEASURED.

**Severity: high, and it is one commit old.**

`v0.6.7` — the current tag, and the pin in the consumer — is the fix for
`text=True` decoding as cp1252 on Windows, where one non-ASCII byte kills the
reader thread and the caller gets `stdout=None` with no traceback. It shipped
with a permanent AST gate,
[`tests/acceptance/test_subprocess_capture_encoding.py`](../../tests/acceptance/test_subprocess_capture_encoding.py),
which is a genuinely well-built check: it parses rather than greps, refuses a
file it cannot read instead of skipping it, carries a positive control
(`assert seen >= 8`), and drives the real hazard on Windows.

Its subject is wrong:

```python
# test_subprocess_capture_encoding.py:65 and :102
for py in sorted(SRC.glob("*.py")):
```

`glob`, not `rglob`. Measured:

```
$ python -c "from pathlib import Path; SRC=Path('src/agent_loop')
print('top-level:', len(list(SRC.glob('*.py'))))
print('recursive:', len(list(SRC.rglob('*.py'))))
print('missed:', [str(p) for p in SRC.rglob('*.py') if p.parent != SRC])"

top-level: 26
recursive: 29
missed: ['src\agent_loop\developer\driver.py',
         'src\agent_loop\developer\tools.py',
         'src\agent_loop\developer\__init__.py']
```

And the missed subpackage contains two unpinned captures:

```python
# developer/tools.py:378-381  (_build)
proc = subprocess.run(cmd, shell=True, cwd=str(repo),
                      capture_output=True, text=True, timeout=900)
# developer/tools.py:396-399  (_run_tests)
proc = subprocess.run(profile.test_cmd, shell=True, cwd=str(repo),
                      capture_output=True, text=True, timeout=900)
```

**The consequence is worse than the original defect, because it inverts a
verdict rather than losing one.** In `_build`, when the reader thread dies
`proc.stdout` is `None` while `proc.returncode` is still `0`, so control reaches
`f"OK: build succeeded\n{proc.stdout[-2000:]}"` → `TypeError: 'NoneType' object
is not subscriptable` → caught by the handler three lines below → the tool
returns **`FAIL: 'NoneType' object is not subscriptable`**. Developer mode is
told its patch does not build, when it built. `_run_tests` does
`proc.stdout + "\n" + proc.stderr`, which raises the same way.

Three things to take from this rather than one:

1. **Fix the glob.** One character.
2. **The 61-second suite emits a `charmap` `UnicodeDecodeError` warning and it is
   NOT this defect** — it is line 136 of the gate deliberately driving the
   unpinned form as a control. Worth knowing, so nobody chases it; also worth
   noting that a real occurrence would now be camouflaged by an expected one.
3. **This is the third gate in three repos caught proving less than it printed**
   (`check_anchors.py` and `check_expected_survivors.py` in `nt8-riskguard`, then
   `check_bridge_parses.py` in `nt8-mcp-bridge`, all within days). The class is
   *the region a check inspects is never stated, so it silently shrinks*. The
   durable fix is not per-gate: **every gate in this repo should print the count
   of what it inspected**, and the positive control should be derived
   (`seen >= number_of_source_files`), not a literal `8` that a shrinking subject
   still satisfies.

## R2. `PANEL_UNREACHABLE` ends a ticket on one malfunctioning member (O67, confirmed in code)

Confirmed at the mechanism level, which O67 does not record.

A reviewer returning more findings than the cap becomes `UNPARSEABLE`
([`loop.py:180-186`](../../src/agent_loop/loop.py)) — deliberately, and the
reasoning is sound: there is no principled way to pick 60 of 1,219 repetitions.
`Vote.counted` is false for `UNPARSEABLE` (`loop.py:130-132`), so
`valid = all(v.counted ...)` is false (`loop.py:465`), so the round is invalid.

There **is** a quorum rescue at [`loop.py:955-967`](../../src/agent_loop/loop.py)
— but read its condition: `if len(counted) >= quorum and all(v.status == APPROVE
for v in counted)`. It rescues only the case where every surviving reviewer
approves. The moment the surviving reviewer has a finding — which is the normal
case, and was the case in both O67 incidents — the run ends with
`PANEL_UNREACHABLE`.

So the machinery for "proceed on the reviewers that answered" exists and is
scoped to the one situation where proceeding matters least. Widening it to
`REVISE`/`REJECT` is a small change with one real design question: a single
surviving reviewer's `REJECT` must still be downgraded, which `v0.6.6` already
does for uncorroborated rejects.

## R3. `TICKET_REJECTED` has three causes and one message (O64/O65)

Measured from the consumer's own account: six loop runs to land one ticket, one
of which failed on the model's reasoning. The other five were the ticket or the
harness, and four were diagnosable only after spending a model call.

`TICKET_REJECTED` today means any of:
- an `expect_green` string that names nothing (a typo — the gate would be **vacuous**),
- an `expect_green` string that is genuinely green (the test does not test the defect),
- the suite does not build, so there are no failure lines at all (O64).

They are three different problems with three different fixes, and the printed
detail names only the middle one. `--list` validates regions and says nothing
about `expect_green` at all (O65) — and the consumer has since written the
~50-line set-difference check in their own repo, which is the signal that it
belongs here.

## R4. A region can resolve to the wrong code and print `OK` (O66)

The half of O66 worth elevating: **regions are not only the editing window, they
are the model's entire view of the file.** A type named in `spec` whose
declaration falls outside every region is invisible to the implementer, and an
invisible type gets a plausible guess — four rounds of invented member names,
measured.

The proposed lint (warn when a capitalised identifier in `spec`/`context` is not
declared inside any region of the same file) is the right mechanical shape. It
interacts with `W6`: the fix for "regions too small to see what they name" and
the fix for "parts too large to review" pull in opposite directions, so both
need the same measurement — resolved region line spans — and should land
together.

## R5. Nine of the eleven items in the third review's table are still open

Not re-litigated here; the point is the aggregate. The third review verified in
current code that E1, E2, E3, E5, the untracked-file revert leak, and the TDD
independence proxy are all open, alongside N1–N9. Combined with O64–O67, the
loop currently carries **roughly 20 known-open findings**, several of which
(MCP stderr deadlock, compaction admission control, panel hard deadline) are
outage-class rather than annoyance-class.

The reliability problem is not that these exist. It is that there is no ranking
that a person can act on: `BACKLOG.md` is 2,860 lines and chronological, and its
`## STATUS` block still says *"Last updated: 2026-08-11, session 4"* and
*"tvDownloadOHLC still pins v0.3.0"* — both false at `v0.6.7`. **The status
block of a backlog is the one line everybody reads and the one nobody updates.**

## R6. There is no smoke path that proves the loop can run at all in a consumer repo

The defect that `v0.6.7` fixed had a specific signature: `nt8-mcp-bridge` **had
never once been runnable by this loop, and nothing said so**. The error blamed
the consumer's output format.

There is still no command that answers *"can this loop drive this repo?"*
without spending a model call. `selftest` (13/13) exercises the package. What is
missing is a `--preflight` that, in the consumer's repo and for free, runs the
build and test commands, confirms the output parses, confirms `expect_green`
strings match real failures, confirms regions resolve to code that mentions the
symbols in `spec`, and prints the counts. Four of the consumer's five lost runs
would have been caught by it.

---

# §C — Throughput: where the wall clock goes

Baseline for the arithmetic below, from config: `max_rounds=4`
([`config.py:622`](../../src/agent_loop/config.py)), `panel_deadline_secs=1800`
(`config.py:625`), provider `timeout_secs=900` (`config.py:647`).

## S1. The full test suite runs once at baseline and once per round — per part

[`loop.py:659`](../../src/agent_loop/loop.py) captures the baseline;
[`loop.py:851`](../../src/agent_loop/loop.py) runs `check_tests` inside each
round, which calls `run_tests(cmd, ...)` on the profile's whole test command
([`gates.py:496-505`](../../src/agent_loop/gates.py)).

One ticket at 4 rounds: **5 full-suite runs**. A 4-part feature plan: **up to 20**.
On this repo that is 61s × 20 ≈ 20 minutes of pure test time; on the
`nt8-riskguard` consumer (1,436 tests under `dotnet test`) it dominates
everything else the loop does.

## S2. The ladder already knows how to scope by file — and tests do not use it

`check_compile` and `check_lint` substitute `{files}` with the files the patch
actually touched ([`gates.py:248-262`](../../src/agent_loop/gates.py), called at
[`loop.py:843,847`](../../src/agent_loop/loop.py)), with a docstring explaining
exactly why an unscoped build is a gate that cannot fail.

`check_tests` has no such mechanism. Yet the ticket already declares the tests
that matter: `expect_green`. The cheap, safe ordering is **two-phase**:

1. Run only `expect_green` (seconds) — if they are still red, the round is over
   and the feedback is more focused than a full-suite dump.
2. Run the full suite **only when the acceptance tests pass**, to catch regressions.

This preserves the regression guarantee exactly (the full suite still gates
every promotable candidate) and removes it from the 60–75% of rounds that fail
on the acceptance tests. Requires one new profile field — how to run a named
subset — which pytest and `dotnet test` both support.

## S3. The baseline is recomputed per ticket from an identical commit

Every ticket opens a worktree at `HEAD` and runs the full suite to freeze the
expected-failure set. Four parts of one plan, run back to back with no commits
in between, compute **the same baseline four times** — provably the same, since
`base_commit` is identical and the worktree is a clean checkout.

Cache it keyed on `(base_commit, test_cmd)`, written under
`logs/agent_loop/.baseline/`. Two rules keep it honest: it must be invalidated
by the commit hash (not a timestamp), and a cache hit must print the hash it hit
— a silent cache on the input to the *test-first* gate is exactly the kind of
thing that later turns out to have been proving nothing.

Saves 3 of 4 baseline runs on a 4-part plan; on the C# consumer that is minutes
per part.

## S4. Serialization is repo-global, and the isolation for parallelism already exists

`open_workspace` takes an exclusive `run_lock` on `logs/agent_loop/.runlock`
for the whole ticket ([`workspace.py:364`](../../src/agent_loop/workspace.py)).
`ROADMAP.md` records the decision not to parallelize, on two grounds: promote
conflicts, and an inconsistent shared learning store.

That reasoning is sound for **arbitrary** tickets and does not hold for **parts
of one plan**, because the planner has already established what a general runner
cannot know: `depends_on`. Parts with no dependency relation and no shared file
(both checkable — `_validate_feature_plan` already tracks the file set) can run
concurrently in separate worktrees, and the promote step stays serial. That is
the standard shape: parallel work, serialized integration.

Worth doing **only after** `W1`–`W3`, and worth measuring before assuming a win —
the sibling repo's CI matrix predicted 10m and measured 15m36s, because
concurrent jobs each run 10–20% slower and the fan-out has a slot limit.

## S5. Latency the panel does not have to pay

Two items already identified elsewhere, quantified here for the ranking:

- **Graph enrichment is serial** (E1): 9 RPCs per region, 15–45s per ticket.
  Credit where due — `loop.py:698-701` already moved it from per-prompt to
  per-ticket, which was the larger win. The remainder is parallelizable across
  regions since the calls are independent reads.
- **The panel deadline is not a hard wall** (third review N7): the
  `ThreadPoolExecutor` `with` block blocks on exit until hung threads finish, so
  `panel_deadline_secs=1800` bounds the *wait for results*, not the *run*. On a
  hung member the ticket stalls past its own deadline.

---

# §D — What is good, and must survive

Any change proposed above has to preserve these, because they are the reason
this loop is worth improving rather than replacing:

1. **The test-first gate proves the specification is satisfiable.** The
   consumer's own account of the run that reached 17 of 18 green and stalled —
   because the spec was *unsatisfiable*, demanding a `QUARANTINED` verdict from
   an enumerator that excludes quarantined items by default — is the strongest
   argument in any of these documents for the design. No amount of model quality
   closes that ticket. Do not weaken the gate while fixing O64; **distinguish its
   causes** instead.

2. **The baseline is frozen, not recomputed.** `capture_baseline` refuses a dirty
   worktree and treats the failure set as immutable, so a patch cannot widen the
   baseline to pass. Any caching in `S3` must not touch this property.

3. **`promote` is all-or-nothing, and deliberately not `--3way`.** The comment
   explaining why (a 3-way merge writes conflict markers into a live file and
   *then* returns non-zero) is the kind of reasoning that should be preserved
   verbatim through any refactor of the plan runner.

4. **The failure comments are the repo's real asset.** Nearly every non-obvious
   line carries the measured incident that produced it — the 979 discarded
   BLOCKERs, the plan that shipped one part of four, the promote hint that
   deleted its own input. That is a genuinely unusual quality bar. Anything added
   by this review should be written the same way.

5. **`--feature` reviews the plan whole.** Reviewing part 1 of 4 cannot see
   whether the parts compose. Keep this when adding a plan runner; the runner
   must not become a reason to review parts individually.

---

# §E — Ranked plan

Ranked by **what unblocks the requested capability**, then by **consequence**,
then by **whether the evidence is obtainable without a model call** — a check
that runs for free is worth taking ahead of one that needs a live run, because
it can be proven closed in the same session.

### E-1. `W1`–`W3`, `W7`: a plan runner. *The requested capability.*

One new mode, `--mode run-plan --plan plan.json`, owning what the gap contains:

| Requirement | Mechanism | Why it is not optional |
|---|---|---|
| Dependency order | topological sort on `depends_on`; refuse an unknown id or a cycle **before** the first run | `W2` — the field must bind or be deleted |
| Chain stops on failure | a part whose verdict is not promotable ends the plan | `W3` — otherwise N−1 parts burn full suites against a tree that never got part 1 |
| Later parts see earlier ones | commit each promoted part to a **plan branch**, so the next worktree's `HEAD` contains it | `W1` — measured; without this the chain cannot execute at all |
| Rollback is one command | the plan branch is discardable; nothing lands on the user's branch until the whole plan is green | `W3` — no half-features in an uncommitted working tree |
| One evidence record per plan | a `plan_id` in the ledger, one manifest listing each part's verdict, tests and commit | `W7` |

The commit-per-part choice deserves argument, because it is the load-bearing
one. Promoting into the uncommitted working tree is what makes `W1` unfixable —
a git worktree can only be created at a **commit**. Committing to a scratch
branch, which is never the user's branch and is deleted on abandonment, is the
smallest change that makes the planner's own promise true. It also gives
rollback and per-part bisection for free.

**Proven closed by**: a 3-part plan where part 2 inserts into a file part 1
creates and part 3 into a file part 2 creates, run end to end with no human
step, plus a test asserting the runner **refuses** a `depends_on` naming an
unknown id and one asserting the chain **stops** after a failed part.

### E-2. `R1`: the encoding gate's subject. *One character; inverts a verdict today.*

`glob` → `rglob` at `test_subprocess_capture_encoding.py:65` and `:102`; pin
`encoding="utf-8", errors="replace"` on `developer/tools.py:380` and `:399`;
derive the positive control from the file count instead of the literal `8`.
Then sweep the same question across every other check in the repo: **what region
does it inspect, and does it print that count?**

**Proven closed by**: the gate failing on the current tree before the two
captures are pinned (watch it go red), and `seen` printing 29-file coverage after.

### E-3. `R6` + `R3` + O65: `--preflight`. *Free, and it is where the runs are lost.*

One command, no model call, run in the consumer's repo:
build and test commands execute and their output **parses**; `expect_green`
set-differenced against the actual failure set **in both directions** (a string
matching no failure = a vacuous gate; a failure no string claims = a forgotten
criterion); regions resolve **and** every capitalised identifier in `spec` is
declared inside some region (`R4`); counts printed for each.

Fold the three `TICKET_REJECTED` causes into distinct messages at the same time —
they are the same diagnosis surfaced at a different moment.

**Proven closed by**: replaying the consumer's five lost runs against it and
recording how many it catches. Anything less than four means the check is aimed
wrong.

### E-4. `S2` + `S3`: two-phase tests, cached baseline. *The throughput items with no design risk.*

Acceptance tests first, full suite only on their pass; baseline cached on
`(base_commit, test_cmd)` with the hit printed. Both preserve the regression
guarantee and the frozen baseline exactly.

**Proven closed by**: measured wall clock for one ticket and one 4-part plan,
before and after, on both this repo and the C# consumer — **stated as a range,
not a single number.**

### E-5. `R2`: partial-panel progress. *Turns an outage into a degraded run.*

Widen the quorum rescue past `all APPROVE`; drop a member returning 8× the cap
with a loud line naming it and why; make `PANEL_UNREACHABLE` distinguish
malfunction from network from auth in its text. Keep the uncorroborated-`REJECT`
downgrade.

**Proven closed by**: a test where one member returns 500 findings and the round
still reaches a verdict, naming the dropped member.

### E-6. `W5` + `W4`: the additive-work path. *Needs `E-1` to be worth much.*

Let `op=create` parts declare a scaffold whose acceptance tests are red for the
right reason, and make `--mode test` accept a whole plan and generate per-part
tests with the part's own spec. Document the stub-first process in the README
regardless — it is currently discovered by having a ticket rejected.

### E-7. `W6` + `R4`: the region-size lint. *Both directions, one measurement.*

Refuse a part above a configured resolved-line ceiling; warn when a symbol named
in `spec` is declared in no region. Same input, opposite failure directions, so
land them together or the second will be tuned against the first.

### E-8. Housekeeping that keeps costing sessions

Fix `BACKLOG.md`'s `## STATUS` block (it is five sessions and four tags stale,
and it is the first thing a reader trusts). Track `AGENT_LOOP_THIRD_REVIEW.md`,
which is currently untracked and would be lost by a clean. Give this document and
that one entries in the backlog so the `W`/`R`/`S`/`N` ids are reachable from the
place people look.

---

## What this review did not verify

Stated so nothing here is read as broader than it is:

- **Only two claims were driven** (`W1`, `R1`). Everything else is read from
  source with line citations, or taken from `BACKLOG.md`'s recorded measurements.
- **No live model call was made.** Every latency figure in `§C` is arithmetic
  over the configured limits and the one measured suite time (61.04s), not an
  observed end-to-end run.
- **`R1`'s inverted-verdict consequence is traced, not driven.** The mechanism
  (`stdout=None` → `TypeError` → caught → `FAIL:`) is read off `developer/tools.py:382-389`;
  a run of developer mode against a build emitting a non-ASCII byte would confirm
  it, and is the honest way to close `E-2`.
- **The `nt8-riskguard` and `nt8-mcp-bridge` consumers were not re-run.** O64–O67
  are taken as the operator filed them.
- **`§C`'s parallelism proposal (`S4`) is a sketch**, deliberately ranked below
  everything else and explicitly not costed.
