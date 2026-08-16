# Agent Loop Third Review — Independent Code Audit

> **Purpose.** A third, independent pass over `src/agent_loop/` (read in full on
> 2026-08-16, at `v0.6.7` / `374dc36`), cross-referenced against `BACKLOG.md`
> (O1–O67) and both prior reviews. This addendum records findings the prior
> reviews did **not** raise, confirms a short list they did, and proposes a
> consolidated plan that drives the loop to fix itself.

The prior reviews are:
- [`AGENT_LOOP_CRITICAL_REVIEW.md`](./AGENT_LOOP_CRITICAL_REVIEW.md) — design/conceptual
- [`AGENT_LOOP_INDEPENDENT_ENGINEERING_REVIEW.md`](./AGENT_LOOP_INDEPENDENT_ENGINEERING_REVIEW.md) — code-level audit with action matrix
- [`AGENT_LOOP_WORK_BREAKDOWN_AND_THROUGHPUT_REVIEW.md`](./AGENT_LOOP_WORK_BREAKDOWN_AND_THROUGHPUT_REVIEW.md) — work-breakdown, throughput, reliability; **two findings measured by running the code**

The work-breakdown review's headline finding (W1, measured) is the largest gap
in the system and is folded into the consolidated plan below as **Wave 0.5**.
Its W1–W7, R1–R6, S1–S5 ids are preserved and cross-referenced.

---

## New findings (not in O1–O67 or prior reviews)

### N1. The "arbiter ≠ reviewer" separation is a documented guarantee that is never enforced

**Severity: High for correctness — it is the loop's central thesis.**

The README states: *"the arbiter must not be the same model as any reviewer."*
`arbiter.adjudicate` and `loop.review_panel` both accept a model string with no
cross-check. A profile or `agent_loop.config.json` that sets `arbiter.model` to
the same value as a `reviewers` entry silently collapses the separation of
detection from adjudication — the exact failure mode the arbiter was built to
prevent (an adversarial reviewer adjudicating its own findings).

`run_ticket` receives `implementer`, `reviewers`, and `arbiter_model` as three
independent parameters. Nothing compares them. The invariant is stated in prose
and in the README, and is unenforced in code.

**Recommendation:** refuse at `run_ticket` entry when `arbiter_model` is in
`reviewers`, and validate the same condition in `config.get()` against the
resolved role models. Fail closed with a message naming both roles.

### N2. `save_settled` rewrites the entire store on every save and is not concurrency-safe

**Severity: Medium for correctness, grows with history.**

`memory.py:131-138`: `save_settled` reads every existing line, appends new ones,
writes all lines to a temp file, then `os.replace`. This is O(N) per save and the
store grows monotonically. The sibling `save_feedback` (line 261-265) correctly
moved to a line-buffered append and documents *why* — *"copying the whole store
to a temp file per finding made recording a round quadratic in the store's size
for no benefit"*. `save_settled` was not updated.

Worse, the read-modify-write is **not safe under concurrent tickets**. Two
processes can both read the file, both append, and the second `os.replace` wins,
losing the first's append. `os.replace` is atomic per-replace, not per-append.
The module docstring (line 24) claims "atomic file writes (os.replace)" — that
is true of the final rename and false of the append semantics.

**Recommendation:** switch `save_settled` to the same line-buffered append +
dedup-on-read pattern `save_feedback` already uses. The dedup check can move to
`load_settled` (which already deduplicates by key).

### N3. Two divergent graph-consumption paths with inconsistent output parsing

**Severity: Medium — silent empty context on format drift.**

`context.py` has **two separate code paths** that consume graph output, and they
disagree:

| path | caller | parsing |
|---|---|---|
| `_build_context_via_mcp` (line 161) | `build_context_slice` (loop.py:701) | raw string splitting on `"name"` and `":"` and `","` (lines 188–235) |
| `_graph_traces` (line 609) | `build_intent_context` (plan/brainstorm/docs) | calls `_is_useful_trace` (JSON-aware), then `str(res)[:400]` |

`_build_context_via_mcp` parses trace output by looking for the literal
substring `"name"` in each line, splitting on `":"`, then on `","`, then
stripping quotes. If the MCP server changes its text rendering (it returns
human-readable text, not structured JSON), this silently produces empty lists.
`_is_useful_trace` (line 583) exists to check whether a graph answer carries
real data, but `_build_context_via_mcp` does **not call it** — it does its own
ad-hoc line parsing with no failure detection.

The consequence: `build_context_slice` (the patch-mode path) can silently inject
zero context while `build_intent_context` (the plan/brainstorm path) correctly
detects the same server's output as useful. A regression in the MCP server's
text format breaks patch-mode context injection with no diagnostic.

**Recommendation:** route both paths through one consumption function that uses
`_is_useful_trace` and a shared parser. Delete the ad-hoc string-splitting in
`_build_context_via_mcp`.

### N4. MAJOR/BLOCKER asymmetry: the thrashing detector counts MAJORs the arbiter rejects

**Severity: Medium — false NOT_CONVERGING stops.**

`Finding.blocking` (loop.py:104) is `True` for both `BLOCKER` and `MAJOR`.
`convergence.append((len(blocking), ...))` at loop.py:991 uses `f.blocking`, so
the convergence history counts MAJOR + BLOCKER.

`arbiter.thrashing` (arbiter.py:436) detects "no convergence" from that history.
But the arbiter's SHIP rule (`_blocker_indices`, arbiter.py:249) only treats
`BLOCKER` as blocking. So a reviewer that files repeated **MAJOR** findings —
which the arbiter consistently REJECTS — inflates the convergence count and can
trigger a `NOT_CONVERGING` stop on a ticket that is actually converging (the
arbiter is rejecting the noise, the implementer is fixing the signal).

The asymmetry is undocumented. The arbiter contract says SHIP requires "no
upheld findings AND no reviewer filed a BLOCKER" — only BLOCKER. But the
thrashing detector uses a broader set.

**Recommendation:** either (a) make the thrashing detector count only UPHELD
findings (not all `blocking` ones), or (b) align `Finding.blocking` with
`_blocker_indices` by splitting it into `is_blocker` and `is_major`. Option (a)
is cheaper and more correct: thrashing is "the arbiter cannot converge," so it
should be keyed on what the arbiter upholds, not on what the reviewers file.

### N5. The arbiter's diff is silently truncated at 60K chars with no marker

**Severity: Medium — wrong adjudications on large patches.**

`arbiter.py:241`: `patch_diff[:60000]`. A multi-region ticket against a 113-line
method (the CM2 case from the handover) can exceed 60K chars of diff. The
truncation is **silent** — no `[... truncated ...]` marker is inserted. The
arbiter sees a partial diff with no indication that it is incomplete and can rule
on findings about code it cannot see.

Compare: `compaction.py` inserts `[COMPACTED: ... chars pruned]` markers, and
`context.py:140` inserts `... (truncated to token budget)`. The arbiter's diff
truncation is the one place where silent truncation can change a verdict.

**Recommendation:** insert a visible `[\n... diff truncated at 60000 chars; ...N chars omitted ...]`
marker at the cut point, and tell the arbiter in the prompt that the diff may be
truncated so it should ESCALATE if a finding references code past the marker.

### N6. The "reviewers must be different families" guarantee is unenforced

**Severity: Medium — identical reviewers provide zero additional signal.**

The README states: *"different model families review concurrently; the worst
verdict wins."* `review_panel` accepts any `Sequence[str]`. A config that lists
`["glm-5.2", "glm-5.2"]` runs the same model twice — two identical reviews
provide no more signal than one, and the worst-verdict rule is meaningless.

Combined with N1, the entire separation-of-powers architecture (independent
detection → independent adjudication) rests on operator discipline with no code
enforcement.

**Recommendation:** refuse at `run_ticket` entry when `len(set(reviewers)) !=
len(reviewers)`. A weaker check (same family, different model) is possible but
the strong check is cheaper and the README already promises it.

### N7. The panel deadline is not a hard wall-clock bound — the `with` block exits after hung threads

**Severity: Medium for reliability.**

`loop.py:453-463`: the `ThreadPoolExecutor` `with` block calls
`as_completed(timeout=deadline_secs)`. On timeout, `fut.cancel()` is called on
incomplete futures. But `Future.cancel()` on an already-running future is a
**no-op** (it returns `False`) — the thread keeps running. The `with` block's
`__exit__` calls `pool.shutdown(wait=True)`, which **blocks until all threads
finish**.

Net effect: if one reviewer hangs on an HTTP request with a 900s provider
timeout, the panel deadline (1800s) fires, UNREACHABLE votes are collected, but
the `return PanelResult(...)` at line 497 blocks inside the `with` block's
`__exit__` until the hung thread's HTTP timeout fires. Total wall-clock: up to
**1800s + 900s = 45 minutes**, not 1800s.

The prior review E7 discussed worst-verdict amplification but did not identify
this `with`-exit blocking. It is the same shape as the T2 2h03m hang that
motivated the deadline: a timeout that bounds the set of calls but not the
cleanup.

**Recommendation:** use `pool.shutdown(wait=False, cancel_futures=True)` (Python
3.9+) on timeout, or drop the `with` block and call `shutdown` manually after
collecting votes, or run each reviewer in a `daemon=True` thread so the process
can exit without joining them.

### N8. `terminal_ledger_record` labels an implementer failure as a "gate" failure

**Severity: Low — misleading reporting, not a correctness issue.**

`loop.py:517-519`: `failed_gate_names` returns
`sorted({r.get("stage", "") for r in rounds if not r.get("ok", True)} - {""})`.
When the implementer is unreachable (loop.py:818), the round record carries
`stage="implement"`. The ledger then records `gate: "implement"`, and the report
counts it as a gate failure. "implement" is a stage, not a gate — the gate
ladder is protected → static → lint → compile → test → lock-scope.

**Recommendation:** either exclude non-gate stages from `failed_gate_names`
(keep only the gate names), or record the stage separately from the gate field.

### N9. Block parser tolerates `>>` but review/arbiter parsers require `>>>`

**Severity: Low — latent parser inconsistency.**

`loop.py:47`: `BLOCK_RE` uses `>{2,}` for both opener and closer — a model that
emits `>>` instead of `>>>` still parses. The comment (line 41-45) explains
why: kimi-k2.7-code closed a block with `>>` on every retry.

But `parse_review` (loop.py:162) and `arbiter._section` (arbiter.py:182) use
literal `>>>` for `<<<VERDICT>>>` / `<<<FINDINGS>>>` / `<<<RULINGS>>>` markers.
A model that drops one `>` from a BLOCK closer (accepted by `BLOCK_RE`) may also
drop one from a VERDICT closer (rejected by `parse_review`), producing
`UNPARSEABLE` on the same model that successfully emitted its blocks.

This is a latent inconsistency: the block parser was hardened against one model's
habit, but the review/arbiter parsers were not hardened the same way.

**Recommendation:** either harden `parse_review` and `arbiter._section` with
`>{2,}` (accept `>>` closers), or document why the block parser alone needs the
tolerance. The safe direction is to harden all three — a missing `>` is a
rendering typo, not a semantic error.

---

## Confirmations of prior-review findings (verified in current code)

These findings from the prior reviews were re-verified against the current
source at `374dc36` and are **still open**:

| Prior ref | Finding | Verified at | Status |
|---|---|---|---|
| Eng-review E2 | MCP `stderr=PIPE` never drained → deadlock | `mcp_client.py:69` | **Open.** `stderr` is still `subprocess.PIPE`, no drain thread. |
| Eng-review E3 | Compaction never refuses oversized pinned content | `compaction.py:120-130` | **Open.** No admission check; `_mechanical_summary` returns history unchanged when `prior` is empty. |
| Eng-review E5 | Memory stores re-read in full each call | `memory.py:156, 281, 305` | **Open.** `load_settled`, `load_rejected_findings`, `load_upheld_findings` all do full-file reads. |
| Eng-review E1 | Graph enrichment serial, blocking readline deadline | `context.py:186-235`, `mcp_client.py:148` | **Open.** 9 serial RPCs per region; `readline()` blocks past the 120s deadline. |
| Eng-review §2 | Untracked file leaks on revert | `workspace.py:130-134` | **Open.** `revert` does `git checkout --` only; `stage_new_files` intent-adds but revert doesn't clean untracked. |
| Critical review §1 | TDD gate enforces "human-authored" proxy | `test_mode.py:23-41` | **Open.** `TEST_SYSTEM` prompt is the only independence mechanism; no path-isolation check. |
| Eng-review P0 | `expect_green` string matching brittle | `gates.py:484-493` | **Partially addressed** by `names_match` word-boundary regex, but O65 (validate against test sources) is still open. |

---

## Consolidated issue table (all three reviews + backlog cross-ref)

| ID | Source | Severity | Issue | Target |
|---|---|---|---|---|
| N1 | this review | High | Arbiter≠reviewer unenforced | `loop.py:604`, `config.py` |
| N2 | this review | Medium | `save_settled` full-rewrite, not concurrency-safe | `memory.py:82-142` |
| N3 | this review | Medium | Two divergent graph-consumption paths | `context.py:161-247` |
| N4 | this review | Medium | MAJOR/BLOCKER asymmetry in thrashing detector | `loop.py:991`, `arbiter.py:436` |
| N5 | this review | Medium | Arbiter diff silently truncated at 60K | `arbiter.py:241` |
| N6 | this review | Medium | Reviewers-must-differ unenforced | `loop.py:604` |
| N7 | this review | Medium | Panel deadline not hard-bound (`with` exit blocks) | `loop.py:453-463` |
| N8 | this review | Low | "implement" stage labeled as gate failure | `loop.py:517` |
| N9 | this review | Low | Block parser `>{2,}` vs review/arbiter `>>>` | `loop.py:47,162`, `arbiter.py:182` |
| E2 | eng-review | High | MCP stderr deadlock | `mcp_client.py:69` |
| E3 | eng-review | High | Compaction no admission control | `compaction.py:120` |
| E5 | eng-review | Medium | Memory stores full-file re-read | `memory.py:156,281,305` |
| E1 | eng-review | High | Graph enrichment serial + blocking deadline | `context.py:186`, `mcp_client.py:148` |
| E-§2 | eng-review | Medium | Untracked file leaks on revert | `workspace.py:130` |
| C-§1 | critical-review | High | TDD independence proxy wrong | `test_mode.py` |
| C-§2 | critical-review | High | No atomic evidence ledger | `loop.py` |
| C-§4 | critical-review | Medium | Brittle prompt-grammar dependency | `loop.py`, `arbiter.py` |
| E-P0a | eng-review | High | `expect_green` string matching brittle | `gates.py:484` |
| E-P0b | eng-review | High | Full test suite per ticket | `gates.py:160` |
| E-P1a | eng-review | Medium | Reasoning token budget collision | `providers.py:63` |
| E-P2 | eng-review | Medium | No region/line-count bounds in plan mode | `plan_mode.py:51` |
| O64 | backlog | Defect | Test-first gate blocks additive work | `loop.py:668-686` |
| O65 | backlog | Defect | `expect_green` never validated against sources | `cli.py --list` |
| O66 | backlog | Defect | Region resolves to wrong code, prints OK | `regions.py` |
| O67 | backlog | Defect | `PANEL_UNREACHABLE` reads as verdict | `loop.py:979` |
| W1 | work-breakdown (measured) | **Critical** | Later part cannot see earlier part's work; promote→uncommitted, worktree@HEAD | `workspace.py`, `loop.py:688` |
| W2 | work-breakdown | High | `depends_on` written by planner, read by nothing | `plan_mode.py`, `cli.py` |
| W3 | work-breakdown | High | Runner has no plan semantics: no order, no stop-on-fail, no rollback | `cli.py:592-616` |
| W4 | work-breakdown | Medium | `--mode test` is single-ticket; TDD half does not decompose | `cli.py:210-215` |
| W6 | work-breakdown | Medium | Nothing bounds work-package size | `plan_mode.py` |
| W7 | work-breakdown | Medium | No evidence ledger per work package (plan has no identity after write) | `loop.py:548` |
| R1 | work-breakdown (measured) | High | Encoding gate `glob` not `rglob` — inspects 26/29 files, misses `developer/` (2 unpinned captures); inverts a build verdict | `test_subprocess_capture_encoding.py:65,102`, `developer/tools.py:378,396` |
| R6 | work-breakdown | High | No `--preflight` smoke path; consumer runs lost before first model call | (new mode) |
| S1 | work-breakdown | Medium | Full suite runs 5× per ticket (20× for a 4-part plan) | `loop.py:659,851` |
| S2 | work-breakdown | Medium | Ladder scopes compile/lint by file but tests never use it; two-phase tests (acceptance first, full suite on pass) recover 60–75% | `gates.py:496-505` |
| S3 | work-breakdown | Medium | Baseline recomputed per ticket from identical commit; cacheable on `(base_commit, test_cmd)` | `loop.py:659` |

---

## Consolidated plan — driving the loop to fix itself

The agent loop is itself a consumer of its own design: it runs tickets against
its own source. The plan below is structured as **tickets the loop can execute
on itself**, sequenced by dependency and risk. Each ticket is sized to pass the
loop's own gates (one region, one failing test first, one focused validation
command).

### Sequencing principle

The ordering follows three rules, all drawn from the handover's hard-won lessons:

1. **Mechanical/safety fixes before behavioral ones** — a deadlock fix has no
   test-failure ambiguity; a TDD-proxy redesign does.
2. **Single-region, single-concern tickets first** — the handover says "a
   113-line region is too big for this loop." Each ticket below touches one file.
3. **Self-bootstrapping** — the first wave of fixes harden the loop's own
   reliability so later waves can be run *by the loop against itself* with
   confidence.

### Wave 0 — reliability of the loop itself (no model judgment needed)

These are deterministic fixes the loop can apply without a panel review, because
they are mechanical correctness issues with clear failing tests:

| Ticket | Fix | File | New findings |
|---|---|---|---|
| **T-REL1** | Drain MCP stderr in a background thread, or inherit it to a log file | `mcp_client.py` | E2 |
| **T-REL2** | `save_settled` → line-buffered append (match `save_feedback` pattern) | `memory.py` | N2 |
| **T-REL3** | Compaction admission check: refuse/split when pinned head alone exceeds budget | `compaction.py` | E3 |
| **T-REL4** | `parse_review` + `arbiter._section` use `>{2,}` closers (match `BLOCK_RE`) | `loop.py`, `arbiter.py` | N9 |
| **T-REL5** | Arbiter diff truncation inserts visible marker | `arbiter.py` | N5 |
| **T-REL6** | Panel `with` block → `shutdown(wait=False, cancel_futures=True)` on timeout | `loop.py` | N7 |

**Why first:** each has an unambiguous red test (deadlock under full stderr,
lost append under concurrency, oversized prompt not refused, `>>` closer
rejected, silent truncation, deadline exceeded). None requires a model to judge
"better." They can be developer-mode tickets with TDD.

### Wave 0.5 — the plan runner (the requested capability, measured-broken)

This is the work-breakdown review's headline finding (W1, measured): the loop
plans a decomposed chain it cannot execute. `promote()` writes uncommitted;
`open_workspace` checks out `HEAD`; part 2 dies with `RegionError: file does not
exist`. `depends_on` is emitted by the planner and read by nothing (W2). The
runner has no plan semantics — no dependency order, no stop-on-fail, no
rollback (W3). The plan has no identity after `plan.json` is written (W7).

| Ticket | Fix | File | Findings |
|---|---|---|---|
| **T-PLAN1** | New mode `--mode run-plan --plan plan.json`: topological sort on `depends_on`; refuse unknown id or cycle before first run; stop chain on non-promotable part; commit each promoted part to a **plan branch** (scratch, discardable) so the next worktree's `HEAD` contains it; rollback = delete the branch | new `run_plan_mode.py`, `cli.py` | W1, W2, W3, W7 |
| **T-PLAN2** | Plan-level evidence ledger: `plan_id` in the ledger, one manifest listing each part's verdict, tests, and commit | `loop.py:548`, `run_plan_mode.py` | W7 |
| **T-PLAN3** | `--mode test` accepts a whole plan and generates per-part tests with the part's own spec (not the feature-level `--defect`) | `cli.py:210-215`, `test_mode.py` | W4 |

**Why here (not later):** this is the capability the user is asking for, and it
is blocked by a **measured** defect, not a design opinion. The commit-per-part
choice is load-bearing — a git worktree can only be created at a commit, so
promoting into the uncommitted tree is what makes W1 unfixable. The plan branch
is never the user's branch and is deleted on abandonment, so nothing lands on the
user's branch until the whole plan is green. This also gives rollback and
per-part bisection for free.

**The workaround that exists today** is undocumented: `git commit` between every
part. Nothing in the README, `plan_mode.py`, or the printed output says so, and
the failure mode is a `RegionError` that reads as a bad anchor.

**Proven closed by:** a 3-part plan where part 2 inserts into a file part 1
creates and part 3 into a file part 2 creates, run end to end with no human step,
plus a test asserting the runner refuses a `depends_on` naming an unknown id and
one asserting the chain stops after a failed part.

### Wave 0.6 — the encoding gate's subject (one character, inverts a verdict today)

The work-breakdown review's R1 (measured): `v0.6.7`'s encoding gate uses
`SRC.glob("*.py")`, not `rglob`. It inspects 26 of 29 files and misses
`developer/`, which holds two unpinned captures. Consequence is worse than the
original defect: a dead reader thread leaves `stdout=None` with `returncode==0`,
so `_build` raises `TypeError` on the slice, the handler catches it, and a
successful build is reported as `FAIL:`.

| Ticket | Fix | File | Finding |
|---|---|---|---|
| **T-ENC1** | `glob` → `rglob` in the encoding gate; pin `encoding="utf-8", errors="replace"` on `developer/tools.py:378,396`; derive the positive control from the file count, not a literal `8` | `test_subprocess_capture_encoding.py:65,102`, `developer/tools.py` | R1 |
| **T-ENC2** | Sweep: every gate in the repo prints the count of what it inspected; the region a check inspects is stated, not silently shrunk | all test files | R1 (class) |

**Why here:** one character, inverts a verdict today, and the class (gates that
silently shrink their subject) is the third instance across four repos. The
camouflage note from the work-breakdown review applies: the `charmap`
`UnicodeDecodeError` in the 61s suite run is the gate deliberately driving the
hazard as a control — a real occurrence would now be camouflaged by an expected
one. Worth knowing so nobody chases the wrong thing.

### Wave 1 — invariant enforcement (guard the architecture's promises)

| Ticket | Fix | File | New findings |
|---|---|---|---|
| **T-INV1** | Refuse `arbiter_model in reviewers` at `run_ticket` entry + config validation | `loop.py`, `config.py` | N1 |
| **T-INV2** | Refuse duplicate reviewers (`len(set) != len`) | `loop.py` | N6 |
| **T-INV3** | `failed_gate_names` excludes non-gate stages (`implement`, `review`) | `loop.py` | N8 |
| **T-INV4** | Thrashing detector counts UPHELD findings only, not all `blocking` | `loop.py`, `arbiter.py` | N4 |

**Why second:** these are invariant checks the loop should have enforced from
day one. They are small, have clear red tests (run with arbiter==reviewer →
refused; run with dup reviewers → refused), and they protect every later wave.

### Wave 2 — backlog defects the loop can self-fix (O64–O67)

| Ticket | Fix | File | Backlog |
|---|---|---|---|
| **T-BLG1** | Test-first gate: distinguish "suite doesn't build" from "tests are green"; allow scaffold-stub pattern for `op=create` | `loop.py:668-686` | O64 |
| **T-BLG2** | `--list` validates `expect_green` against failing-test output (set-difference both directions) | `cli.py` | O65 |
| **T-BLG3** | `--list` warns when a capitalized identifier in `spec`/`context` is not declared in any region of the same file | `regions.py` or new lint | O66 |
| **T-BLG4** | `PANEL_UNREACHABLE` → `PANEL_OUTAGE` with member + reason; drop member instead of stopping run | `loop.py:949-980` | O67 |

**Why third:** these are the consumer-reported defects, already analyzed in
`BACKLOG.md`. They need the loop running reliably (Wave 0) and its invariants
enforced (Wave 1) before they're worth fixing — otherwise the fix-ticket itself
hits the same traps.

### Wave 3 — operational improvements (larger, need panel review)

| Ticket | Fix | File | Prior ref |
|---|---|---|---|
| **T-OPS1** | Graph enrichment: concurrent preflight with wall-clock budget + cache by `(commit, symbol, graph_version)` | `context.py`, `mcp_client.py` | E1 |
| **T-OPS2** | Unify the two graph-consumption paths; route through `_is_useful_trace` + shared parser | `context.py` | N3 |
| **T-OPS3** | `revert()` prunes untracked files created by `create`/`write_test` regions | `workspace.py` | E-§2 |
| **T-OPS4** | Memory stores: bounded rolling index, retrieve by relevance not recency | `memory.py` | E5 |
| **T-OPS5** | Focused test command per ticket (`focused_test_cmd`), full suite at promotion only | `gates.py`, `profile` | E-P0b |
| **T-OPS6** | Plan mode: enforce region/line-count upper bounds (max 3 regions, 150 lines/ticket) | `plan_mode.py` | E-P2 |

**Why last:** these change behavior the loop's users depend on. They need panel +
arbiter review, and the panel/arbiter must be reliable (Wave 0–1) for that
review to mean anything.

### Wave 4 — design-level (requires measurement, not just code)

These are the critical-review's conceptual findings. They are **not code tickets
yet** — they need a measurement or a decision before they become tickets:

| Topic | What to decide/measure | Prior ref |
|---|---|---|
| TDD independence | Replace "human-authored" proxy with path-isolation check (test generated from spec, implementation excluded) | C-§1 |
| Evidence ledger | Structured per-task JSON ledger: `(ticket, criterion, command, stdout, pass_ts)` | C-§2 |
| Prompt grammar | Move from `<<<MARKER>>>` text protocol to structured JSON output where the provider supports it | C-§4 |
| Reasoning budget | Model reasoning as a planned resource; validate before dispatch, not patch after failure | E-P1a |

### How to drive the loop to fix itself

The bootstrapping sequence:

1. **Apply Wave 0 by hand** (or via developer mode with `--allow-unapproved`).
   These are mechanical; a human review of each diff is faster than a panel
   round and the fixes harden the loop for the next waves.

2. **Apply Wave 0.5 (the plan runner) as a hand-written feature first**, then
   run the loop against it. This is the requested capability and it is blocked
   by a measured defect (W1). The commit-per-part design decision is load-bearing
   and should be reviewed by a human before the loop's own panel reviews it.
   Once the plan runner exists, the loop can execute decomposed plans against
   its own source — which is what makes Waves 1–3 self-bootstrapping.

3. **Apply Wave 0.6 (encoding gate) by hand** — one character (`glob`→`rglob`)
   plus two pinned captures. Do this before running any developer-mode ticket,
   because the inverted-verdict bug (R1) would make the loop report its own
   successful build as a failure.

4. **Run Wave 1 as patch-mode tickets** against the loop's own source. The
   `python-tvdownloadohlc` profile already works for this repo. Each ticket:
   - one region, one file
   - `expect_green` names a test that asserts the invariant (e.g.
     `test_refuses_arbiter_same_as_reviewer`)
   - the loop's own panel + arbiter reviews the change
   - **if the plan runner (Wave 0.5) is in place, these can be a 4-part plan
     that the loop executes itself**

5. **Run Wave 2 as patch-mode tickets** — same pattern. These are the consumer
   defects; fixing them on the loop itself means the next consumer run won't
   hit them.

6. **Wave 3 as patch-mode tickets**, one per file, with the panel reviewing.
   These are larger diffs; if a ticket exceeds ~80 lines of region text, split
   it (the handover's own lesson from CM2).

7. **Wave 4 stays human-driven** until a measurement exists. The TDD
   independence redesign especially needs a decision on what "path-isolated"
   means operationally before it can be ticketed.

### Verification gates for each wave

| Wave | Red test shape | Green test shape |
|---|---|---|
| 0 | deadlock/replay under full stderr; lost append under concurrency; `>>` closer rejected; silent truncation; deadline exceeded by hung thread | drained stderr completes; append survives; `>>` accepted; marker present; deadline enforced |
| 0.5 | part 2 worktree does not see part 1's promoted file (measured); `depends_on` naming unknown id accepted; chain continues after failed part; `--mode test` takes only one ticket | 3-part plan runs end-to-end with no human step; unknown id refused; chain stops on failure; plan branch discarded on abandon; `--mode test` accepts whole plan |
| 0.6 | encoding gate inspects 26/29 files; `developer/tools.py` captures unpinned; successful build reported as FAIL under non-ASCII byte | gate inspects 29/29; captures pinned; build succeeds; positive control derived from file count |
| 1 | arbiter==reviewer accepted; dup reviewers accepted; "implement" in gate list; thrashing fires on rejected MAJORs | all refused at entry; gate list excludes stages; thrashing counts upheld only |
| 2 | O64: additive ticket rejected; O65: bad `expect_green` accepted; O66: wrong region prints OK; O67: outage reads as verdict | additive ticket allowed with stub; bad string refused; wrong region warns; outage named with member+reason |
| 3 | serial enrichment >N seconds; untracked file orphaned; full suite runs per round; 450-line ticket accepted | concurrent enrichment bounded; orphan pruned; focused tests run; oversized ticket refused |
| 4 | (measurement-driven) | (measurement-driven) |

### Risk callouts

- **Wave 0 T-REL1 (stderr drain):** the drain thread must not change the
  process's exit semantics. Use `daemon=True` or a bounded queue.
- **Wave 0 T-REL3 (admission control):** refusing an oversized ticket is a
  behavior change. The refusal message must name the split strategy (reduce
  regions, not "your ticket is too big").
- **Wave 0.5 T-PLAN1 (plan branch):** the commit-per-part choice is
  load-bearing. The plan branch must never be the user's branch, must be
  deleted on abandonment, and must not conflict with the user's uncommitted
  work. `promote()`'s all-or-nothing, deliberately-not-`--3way` semantics must
  survive — the plan runner composes parts, it does not change how a single part
  lands. The `--feature` whole-plan review must also survive — the runner must
  not become a reason to review parts individually (§D of the work-breakdown
  review).
- **Wave 0.5 T-PLAN1 (stop-on-fail):** a failed part must stop the chain, not
  skip to the next. The exit code must reflect the plan's verdict, not just
  count non-promotable tickets (W3).
- **Wave 0.6 T-ENC1 (camouflage):** the `charmap` `UnicodeDecodeError` in the
  61s suite is the gate deliberately driving the hazard as a control. A real
  occurrence would now be camouflaged by an expected one — worth knowing so
  nobody chases the wrong thing.
- **Wave 1 T-INV1/T-INV2:** existing consumer configs that happen to set
  arbiter==reviewer will break on upgrade. The refusal message must name the
  config key to change.
- **Wave 2 T-BLG4 (drop member):** dropping a malfunctioning reviewer changes
  the panel's worst-verdict dynamics. The solo-REJECT downgrade (O63) already
  handles the 1-reviewer case; verify it still holds when a 2-reviewer panel
  drops to 1.
- **Wave 3 T-OPS5 (focused tests):** a focused test command that misses the
  changed symbols is a false green. The ticket must verify the focused
  selection includes the acceptance tests.

---

## Summary

The prior two reviews were accurate on design-level concerns (TDD proxy, task
atomicity, context architecture, prompt-grammar brittleness) and on the
mechanical hazards (MCP deadlock, token estimation, memory recency). The
work-breakdown review found the largest gap in the system by **measuring it**:
the loop plans a decomposed chain it cannot execute (W1). This third pass found
**nine new issues** the prior reviews missed, all in code that was read but not
cross-checked: an unenforced arbiter≠reviewer separation (the loop's central
thesis), a concurrency-unsafe settled-decisions store, two divergent
graph-consumption paths, a MAJOR/BLOCKER asymmetry in the thrashing detector,
silent 60K diff truncation, an unenforced reviewers-must-differ rule, a panel
deadline that blocks on `with`-exit, a stage mislabeled as a gate, and a
parser-hardening inconsistency between the block parser and the review/arbiter
parsers.

The consolidated plan now sequences **31 fixes** across 7 waves (0, 0.5, 0.6,
1, 2, 3, 4), sized so the loop can execute the first four waves against its own
source. Wave 0 (6 mechanical reliability fixes) should be applied first, by hand
or developer mode. Wave 0.5 (the plan runner) is the requested capability and
should be hand-written and reviewed before the loop's panel reviews it. Wave 0.6
(one-character encoding gate fix) must land before any developer-mode ticket
runs, because the inverted-verdict bug (R1) would make the loop report its own
successful build as a failure.