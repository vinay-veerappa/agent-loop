# Agent Loop — Architecture

> **Purpose.** This document is the authoritative reference for the agent-loop
> system's design, control flow, components, and workflows. It is written to be
> unambiguous: every step has a file location, every decision has a reason, and
> every component boundary is explicit.
>
> **State.** This doc reflects the code at `main` after Waves 0–4 and the
> fifth review fixes (commit `e091196` + R5-1 through R5-7). The plan runner
> (Wave 0.5) is documented in [PLAN_RUNNER.md](./PLAN_RUNNER.md). See
> [AGENT_LOOP_FIFTH_REVIEW.md](./AGENT_LOOP_FIFTH_REVIEW.md) for the review
> that found the issues fixed in this commit.

---

## Table of contents

1. [What the loop is](#1-what-the-loop-is)
2. [The core loop (patch mode)](#2-the-core-loop-patch-mode)
3. [Gate ladder](#3-gate-ladder)
4. [Review panel](#4-review-panel)
5. [Arbiter](#5-arbiter)
6. [Memory and learning](#6-memory-and-learning)
7. [Compaction](#7-compaction)
8. [Graph context](#8-graph-context)
9. [Worktree isolation](#9-worktree-isolation)
10. [Profiles](#10-profiles)
11. [Configuration](#11-configuration)
12. [Modes](#12-modes)
13. [The model registry](#13-the-model-registry)
14. [Providers and transport](#14-providers-and-transport)
15. [Tickets and regions](#15-tickets-and-regions)
16. [Ledger and artifacts](#16-ledger-and-artifacts)
17. [Invariants enforced in code](#17-invariants-enforced-in-code)
18. [Review history and known findings](#18-review-history-and-known-findings)

---

## 1. What the loop is

agent-loop is a language-agnostic AI agent loop for software engineering. Its
central thesis is the **separation of detection from adjudication**: an
adversarial panel of different model families reviews a candidate patch
concurrently, and a separate arbiter model rules on each finding. Only upheld
findings go back to the implementer. This is the design choice that
distinguishes it from a single-reviewer loop.

The loop runs in rounds:

```
implement → gate ladder → review panel → arbiter → apply or revise
```

- The **implementer** generates a candidate patch from a ticket spec.
- The **gate ladder** runs deterministic mechanical checks (static, lint,
  compile, test, lock-scope). Gates are facts; the panel cannot override them.
- The **review panel** runs concurrently across different model families. The
  worst verdict wins. A reviewer that did not answer has not voted.
- The **arbiter** rules on each reviewer finding: UPHELD, REJECTED, or
  OUT_OF_SCOPE. Only UPHELD findings go back to the implementer. The arbiter
  cannot ship; it recommends, and a human runs `--apply`.
- **Apply** promotes the patch to the live working tree, or the loop reverts
  and feeds the upheld findings back for another round.

**What the loop is not.** It is not a chatbot, an IDE plugin, or a
general-purpose coding assistant. It is a batch pipeline driven by ticket JSON
files, producing patches and a ledger. Every decision lands in
`logs/agent_loop/<TICKET>/` and `logs/agent_loop/ledger.jsonl`.

---

## 2. The core loop (patch mode)

**File:** `src/agent_loop/loop.py`, function `run_ticket`.

### Entry

`run_ticket(repo, ticket, profile, implementer, reviewers, ...)` is the entry
point. It:

1. **Resolves config.** `max_rounds` and `panel_deadline` default to config
   values (0 = "ask config").

2. **Invariant checks** (Wave 1):
   - Refuses if `arbiter_model` is in `reviewers` (N1). The arbiter must be a
     different model from any reviewer — separation of detection from
     adjudication is the loop's central thesis.
   - Refuses if `reviewers` contains duplicates (N6). Different model families
     review concurrently; identical reviewers provide zero additional signal.
   - Both return `CONFIG_REJECTED` with a message naming the config key to
     change.

3. **Protected paths** (gate 0): refuses if any region file matches a
   protected pattern (`test_*.py`, `conftest.py`, gate scripts, etc.). This
   prevents the patch from editing the code that grades it.

4. **Graph freshness check:** reports whether the codebase-memory-mcp graph is
   fresh. Reports only; does not re-index.

5. **Settled decisions injection:** loads auto-extracted settled decisions
   from prior tickets and combines them with hand-curated ones from the profile.
   Capped at 20 most recent (~1K tokens).

6. **Open worktree:** creates a disposable git worktree at `HEAD`. The live
   working tree is never written to during a round.

7. **Baseline capture:** runs the full test suite once to freeze the
   expected-failure set. The baseline is immutable for the run. A dirty
   worktree or a suite that doesn't produce a parseable summary is refused.
   For a feature ticket (`op=create`), the message distinguishes "suite doesn't
   build" from "tests are green" (T-BLG1, O64) and explains the scaffold-first
   workaround.

8. **Test-first check:** every `expect_green` name must be in the baseline
   failure set. A name that is not failing means either a typo (vacuous gate)
   or the test passes without the fix (doesn't test the defect). Refused.

9. **Region extraction:** resolves the ticket's region anchors against the
   worktree source. Each region has an `op`: `create` (new file), `insert`
   (add after anchor), or `replace` (rewrite anchored block).

10. **Graph context slice:** builds a ranked, token-budgeted context slice
    from the codebase-memory-mcp graph (callers, callees, tests). Built once
    per ticket, reused by implementer, reviewer, and arbiter prompts.

### Round loop

For each round 1..max_rounds:

```
Round N:
  1. (if N > 1) Compact history (Phase 4)
  2. IMPLEMENT: call implementer model with history
     → parse blocks from response
  3. GATE LADDER (cheapest first):
     a. static gate — shape, indentation, markers, braces
     b. (if static OK) apply regions to worktree
     c. lint gate (optional)
     d. compile gate
     e. test gate (focused first if focused_test_cmd set, then full suite)
     f. lock-scope gate (optional, for languages with locks)
     → any gate fails: revert, feed feedback, continue to next round
  4. (if all gates pass) REVIEW PANEL:
     → concurrent reviewers, worst verdict wins
     → panel invalid (unreachable) → quorum rescue or PANEL_OUTAGE
     → unanimous APPROVE → final=APPROVE, break
  5. (if panel not unanimous) ARBITER:
     → rules on each finding (UPHELD/REJECTED/OUT_OF_SCOPE)
     → ESCALATE → final=ESCALATED, break
     → SHIP → final=ARBITER_SHIP, break (human sign-off required)
     → REVISE → thrashing check → revert, feed upheld findings, continue
  6. Track convergence (UPHELD findings only, N4)
```

### Post-loop

After the round loop:

1. **Determine final verdict:** `MAX_ROUNDS_EXHAUSTED`, `ARBITER_NEVER_RAN`,
   or the verdict from the last round.

2. **Select candidate:** the last candidate that passed every gate, not
   necessarily the last round's candidate (O55). A later round can be worse.

3. **Export:** write `final_blocks.json` and `final.patch` for human review.

4. **Promote:** if the verdict is promotable (`APPROVE`, `APPROVE_PARTIAL`,
   `ARBITER_SHIP`) and `--apply` is set, promote the patch to the live working
   tree via `git apply` (all-or-nothing, deliberately not `--3way`).

5. **Ledger:** append a terminal record to `logs/agent_loop/ledger.jsonl`.

### Final verdicts

| Verdict | Meaning | Promotable? |
|---|---|---|
| `APPROVE` | Unanimous panel approval | Yes |
| `APPROVE_PARTIAL` | Quorum-only approval (one reviewer unreachable) | Yes (unapproved) |
| `ARBITER_SHIP` | Arbiter recommends SHIP; human sign-off required | Yes (unapproved) |
| `REVISE` | Panel/arbiter found issues; loop continues | No |
| `REJECT` | Panel rejected the approach | No |
| `ESCALATED` | Arbiter cannot rule safely | No |
| `NOT_CONVERGING` | Thrashing detected (3 rounds, no overlap, not falling) | No |
| `PANEL_OUTAGE` | No quorum of reviewers answered | No |
| `ARBITER_DEADLOCK` | Arbiter unreachable | No |
| `IMPLEMENTER_UNREACHABLE` | Implementer model failed | No |
| `MAX_ROUNDS_EXHAUSTED` | Ran all rounds without convergence | No |
| `ARBITER_NEVER_RAN` | Max rounds exhausted without arbiter consultation | No |
| `TICKET_REJECTED` | Protected paths or bad expect_green | No |
| `CONFIG_REJECTED` | Invalid model configuration (arbiter==reviewer, dups) | No |

---

## 3. Gate ladder

**File:** `src/agent_loop/gates.py`

Gates are deterministic mechanical checks. They run cheapest first, so a patch
that doesn't compile never costs a test run, and one that fails tests never
costs a reviewer. Each gate returns a `GateResult` with `ok`, `summary`,
`detail`, and `feedback` (the text fed back to the implementer on failure).

| Gate | Name | What it checks | Cost |
|---|---|---|---|
| 0 | `protected` | Region files don't match protected patterns | Free |
| 1 | `static` | Shape: blocks present, non-empty, indentation matches, no leaked markers, balanced braces (decl languages), balanced #if/#endif | Free |
| 1.5 | `lint` | Profile's linter command (optional, runs between static and compile) | Cheap |
| 2 | `compile` | Profile's build command with `{files}` substitution | Medium |
| 3 | `test` | Profile's test command against frozen baseline; `expect_green` must be green, no regressions | Expensive |
| 4 | `lock-scope` | No risk calls inside locks (optional, for languages with lock primitives) | Free |

### Two-phase tests (S2, T-OPS5)

When `profile.focused_test_cmd` is set and the ticket has `expect_green`, the
test gate runs in two phases:

1. **Focused:** run only the acceptance tests (seconds). If they fail, the
   round is over — skip the full suite.
2. **Full:** run the full suite only when the focused tests pass, to catch
   regressions.

This preserves the regression guarantee (the full suite still gates every
promotable candidate) and recovers 60–75% of rounds that fail on the acceptance
tests.

---

## 4. Review panel

**File:** `src/agent_loop/loop.py`, function `review_panel`.

### How it works

The panel runs reviewers concurrently via `ThreadPoolExecutor`. Each reviewer
receives the same prompt: ticket, gate summary, settled decisions, acceptance
tests, before/after regions, and graph context. The worst verdict wins.

### Verdict parsing

`parse_review` extracts the verdict from `<<<VERDICT>>>` blocks. Tolerates `>>`
closers (not just `>>>`), matching the block parser's `>{2,}` tolerance (N9).
An empty or unparseable response is `UNPARSEABLE`, never a silent `REVISE`.

A reviewer returning more than 60 findings is `UNPARSEABLE` (repetition, not
review). The cap is configurable via `loop.max_findings_per_reviewer`.

### JSON fallback (C-4, R5-3)

When the text protocol parser finds no `<<<VERDICT>>>` marker, it tries to
parse the response as JSON before returning `UNPARSEABLE`. A model that
returns `{"verdict": "APPROVE", "findings": [...]}` is a valid review, not
an unparseable one. The text protocol remains primary; JSON is a fallback.

The extractor uses balanced-brace matching (`_extract_balanced_braces`),
not a regex, because a regex with `[^{}]*` cannot match nested JSON objects
(a review with a findings array contains inner braces). The walker tracks
brace depth and skips braces inside string literals.

### Solo-REJECT downgrade (O63)

A single reviewer's `REJECT` is downgraded to `REVISE`. REJECT drives a
different branch (discard the approach, re-emit every block), and that should
not rest on one voice. A corroborated REJECT (2+ reviewers) still rethinks.

### Quorum rescue (T-BLG4, O67/R2)

If the panel is invalid (one or more reviewers unreachable), but a quorum
(≥ ceil(2/3)) answered, the panel proceeds with the surviving reviewers' worst
verdict. The unreachable reviewers are dropped with a loud line naming them
and their error. Only when no quorum is reached does the run stop with
`PANEL_OUTAGE`.

### Panel deadline (T-REL6, N7)

The panel deadline (`panel_deadline_secs`, default 1800s) is a hard wall-clock
bound. The `ThreadPoolExecutor` is NOT in a `with` block (whose `__exit__`
calls `shutdown(wait=True)`, blocking on hung threads). Instead,
`shutdown(wait=False, cancel_futures=True)` is called manually after collecting
votes, so a hung reviewer cannot stall the ticket past its own deadline.

### Invariant enforcement (T-INV1/2, N1/N6)

At `run_ticket` entry:
- Refuses if `arbiter_model` is in `reviewers` — separation of detection from
  adjudication.
- Refuses if `reviewers` contains duplicates — different families review
  concurrently.

---

## 5. Arbiter

**File:** `src/agent_loop/arbiter.py`

### What it does

The arbiter sees what neither reviewer does: the ticket, the patch, the
mechanical gate results, both reviewers' findings together, the convergence
history, and the graph context. It rules on each finding:

- **UPHELD** — real, caused by this patch, blocks. Only these go back.
- **REJECTED** — wrong. The mechanism does not hold, contradicts a gate, or
  restates a settled decision.
- **OUT_OF_SCOPE** — real but pre-existing or belonging to another ticket.

Then recommends: SHIP, REVISE, or ESCALATE.

### Authority bounds

- Cannot overturn a mechanical gate. Compile errors, test regressions, and
  lock-scope violations are facts.
- Cannot ship. It recommends; a human runs `--apply`.
- Cannot dismiss a BLOCKER and recommend SHIP. If a BLOCKER was filed and the
  arbiter rejects it, the recommendation is forced to ESCALATE so a human
  confirms.

### Convergence tracking (N4)

The thrashing detector counts **UPHELD** findings only, not all blocking
findings filed by reviewers. A reviewer filing repeated MAJORs the arbiter
consistently REJECTS should not trigger `NOT_CONVERGING` — the arbiter is
rejecting the noise, the implementer is fixing the signal.

Thrashing fires when: 3 consecutive rounds, zero overlap between consecutive
rounds, and the count is not falling. The message says to split the ticket.

### Diff truncation (T-REL5, N5)

The arbiter's diff is truncated at 60K chars with a visible
`[DIFF TRUNCATED: N chars omitted]` marker. The arbiter is told to ESCALATE if a
finding references code past the marker.

---

## 6. Memory and learning

**File:** `src/agent_loop/memory.py`

### Settled decisions (Phase 5)

The arbiter nominates settled decisions in its `<<<SETTLED>>>` output. These
are auto-extracted, deduplicated by `ticket_id:hash`, and persisted to
`logs/agent_loop/settled_decisions.jsonl` (line-buffered append, T-REL2/N2).

At the start of each ticket, the most recent 20 decisions are loaded and
injected into the review prompt. Hand-curated decisions from the profile take
precedence; auto-extracted ones are advisory. Older decisions stay on disk for
auditability but are not injected.

### Learning feedback (Phase 9)

After each round, the loop records what the arbiter ruled on each finding:
`save_feedback(repo, ticket_id, round, reviewer_model, finding_text, severity,
ruling)`. This is a line-buffered append to `learning_feedback.jsonl`.

At the start of each ticket, the most recent 10 REJECTED and 10 UPHELD findings
are injected into the reviewer prompt as "known false positives" and "known
real defects." The loop gets smarter with every ticket.

### Concurrency safety (T-REL2, N2)

`save_settled` uses a per-file `threading.Lock` around the line-buffered
append. On Windows, concurrent line-buffered appends to the same file are NOT
atomic (NTFS does not guarantee atomic appends), so the lock is required.
Deduplication is on read (`load_settled`), so a duplicate append is harmless.

---

## 7. Compaction

**File:** `src/agent_loop/compaction.py`

### Why

The implementer's history grows unboundedly across rounds. Each round adds the
implementer's raw output, reviewer findings, arbiter rulings, and feedback. By
round 4, the history can exceed 400K tokens.

### How

Phase 4a: prune verbose old outputs above 5000 chars to truncation markers
that preserve per-finding structure (reviewer name, severity, one-line summary,
arbiter ruling).

Phase 4b: if the pruned history still exceeds `round_input_token_budget`
(default 40K tokens), replace prior rounds with a mechanical summary. Only
pay for an LLM summary if the mechanical one still doesn't fit.

### Pinned content

Two things are never compacted:
- The system prompt and the IMPLEMENT PROMPT (history[0] and history[1]) —
  every round ends "re-emit ALL blocks in full," so losing the ticket spec and
  region source is fatal.
- The newest exchange — the candidate under revision and the feedback about it.

### Admission check (T-REL3, E3)

If the pinned head alone exceeds the budget, `CompactionError` is raised before
the provider call. No compaction can help — the pinned content is never
removable. The message names the actionable fix: reduce region size or split
the ticket.

---

## 8. Graph context

**File:** `src/agent_loop/context.py`

### What it does

Queries the codebase-memory-mcp graph for each region's callees, callers,
tests, and types. Returns a ranked, token-budgeted context slice (default
3000 tokens) injected into the implementer, reviewer, and arbiter prompts.

Built once per ticket and reused by all three roles. The reviewer gets half
the budget so it can check "does this break callers?" without crowding out the
diff it is there to read.

### Freshness check

`check_graph_freshness` compares the mtime of the newest source file against a
persisted marker. Reports `fresh`, `stale`, `no-project`, or `error`. Does not
re-index — that takes minutes and should not be a silent side effect.

### MCP client (T-REL1, E2)

The MCP client spawns a stdio-based server process. A **daemon thread**
continuously drains stderr so the 64KB OS pipe buffer cannot fill and deadlock
the client on `stdout.readline()`. The drain captures the last 500 lines for
diagnostics. Without this, a chatty MCP server blocks on its next stderr write
and the client blocks on stdout waiting for a response that never arrives.

---

## 9. Worktree isolation

**File:** `src/agent_loop/workspace.py`

### Why

The predecessor applied candidates directly to the live tree and reverted with
`git checkout --`, which destroys uncommitted work. A worktree removes the
hazard: the loop gets its own checkout sharing the repo's object store, the
live tree is never written to, and the dangerous `git checkout --` becomes safe
because it is scoped to a throwaway directory.

### Run lock

`run_lock` is an exclusive advisory lock at `logs/agent_loop/.runlock`. It
records the holder's PID and treats a lock whose process is gone as stale
(reclaims it). Two loops or a loop and a human running `dotnet build` racing
the same files silently corrupts both.

### Revert (T-OPS3, E-§2)

`revert()` restores tracked files AND removes untracked files created by
`create`/`write_test` regions. The old revert only did `git checkout --`, which
left orphaned files in the worktree. Now checks `git show HEAD:<file>` — if the
file is not in HEAD, it was created by this run and is removed.

### Promote

`promote()` applies the worktree's diff to the live repo via `git apply`.
Deliberately NOT `--3way`: a 3-way merge writes conflict markers into the live
file and then returns non-zero. All-or-nothing: `git apply --check` first, then
`git apply`. Refuses to promote over uncommitted changes in the target files.

---

## 10. Profiles

**File:** `src/agent_loop/profiles.py`

A Profile carries everything language-specific and domain-specific the loop
needs. The loop driver, gates, and region extractor contain zero
language-specific strings.

| Field | Purpose | Example |
|---|---|---|
| `language` | Fence label, test path derivation | `"python"`, `"csharp"` |
| `file_suffixes` | Which files the graph indexes | `(".py",)`, `(".cs",)` |
| `line_comment` | Comment syntax | `"#"`, `"//"` |
| `block_comment` | Block comment delimiters (empty = no block comments) | `("/*", "*/")`, `()` |
| `block_kind` | How blocks are delimited | `"decl"` (braces), `"indent"` (Python) |
| `build_cmd` | Compile command, with `{files}` substitution | `"python -m py_compile {files}"` |
| `test_cmd` | Full test suite command | `"python -m pytest tests/ -q"` |
| `focused_test_cmd` | Focused test command with `{tests}` (S2) | `"python -m pytest {tests} -q"` |
| `lint_cmd` | Linter command (optional) | `"ruff check {files}"` |
| `protected` | Paths the patch cannot edit | `("test_*.py", "conftest.py")` |
| `test_sources` | Where test files live | `("tests/test_*.py",)` |
| `context_token_budget` | Graph context budget | `3000` |
| `round_input_token_budget` | Per-round input budget | `40000` |
| `max_region_lines` | Max lines per region (W6) | `150` |
| `lock_name` | Lock primitive name (optional) | `"_stateLock"` |
| `risk_calls` | Risk calls to flag under locks | `(".Flatten", ".Cancel")` |
| `file_scope_whitelist` | Developer mode file scope | `("src/",)` |
| `graph_project` | codebase-memory-mcp project name | `"C-Users-vinay-agent-loop"` |
| `implementer_rules` | Domain rules for the implementer | `"You are a senior..."` |
| `reviewer_priorities` | Domain rules for reviewers | `"You are adversarial..."` |
| `arbiter_rules` | What "blocks" means in this codebase | `"Blocking means..."` |
| `settled` | Hand-curated settled decisions | `("Always release the lock...",)` |

### Adding a language

Adding Python or TypeScript support is a new Profile, not a fork. The loop
driver, gates, region extractor, and arbiter contain zero language-specific
strings.

---

## 11. Configuration

**File:** `src/agent_loop/config.py`

Every tunable number lives in one place: `config.py`. The rule: a tunable
number appears as a literal exactly once, here, with the reason it has its
value. Call sites read it; a test (`test_config_central.py`) fails the build
if a literal reappears at a call site.

### Override

Override without editing the package by copying `agent_loop.config.example.json`
to `agent_loop.config.json` and deleting everything you are not changing:

```json
{
  "roles": {"reviewer": {"model": "kimi-k3:cloud"}},
  "modes": {"docs": {"max_tokens": 48000}},
  "loop":  {"max_rounds": 6}
}
```

Resolution: `--config PATH` → `$AGENT_LOOP_CONFIG` →
`./agent_loop.config.json` → built-in defaults. Unknown keys are a hard
error.

### Budgets and thinking

On a reasoning model, chain-of-thought is spent from the **same budget as the
answer**. `think=None` leaves the model's own default in force (ON). A budget
sized for the expected output therefore becomes a budget shared with an
unbounded reasoning prefix. Every role and mode declares `think` explicitly.
Anything with `think: true` is budgeted for reasoning **plus** answer. If you
turn thinking on, raise the budget in the same edit.

---

## 12. Modes

| Mode | Input → Output | Flag | File |
|---|---|---|---|
| `patch` | ticket JSON → patched code | `--mode patch` (default) | `loop.py` |
| `review` | existing diff → panel verdict | `--mode review --review-base HEAD~1` | `review_mode.py` |
| `plan` | defect → ticket JSON (panel+arbiter reviewed) | `--mode plan --defect "..."` | `plan_mode.py` |
| `test` | defect + ticket → failing acceptance tests | `--mode test --defect "..." --tickets plan.json` | `test_mode.py` |
| `developer` | defect → patched code (autonomous localize+edit) | `--mode developer --defect "..."` | `developer/driver.py` |
| `brainstorm` | defect → candidate approaches + trade-offs | `--mode brainstorm --defect "..."` | `brainstorm_mode.py` |
| `docs` | codebase → documentation (4 sub-modes) | `--mode docs --docs-type changelog\|handover\|design\|prd` | `docs_mode.py` |
| `run-plan` | plan JSON → executed chain (implementing) | `--mode run-plan --plan plan.json` | `run_plan_mode.py` (new) |

### Patch mode (default)

The core loop described in §2. One ticket, one file, one round loop.

### Review mode

Reviews an existing diff (no implementation). Runs the panel + arbiter against
a diff between `--review-base` and `--review-head`. Reports; never edits.

### Plan mode

Decomposes a defect or feature into ticket JSON. For features (`--feature`),
emits ordered parts with `depends_on`, `op` (create/insert/replace), and
`expect_green` per part. The plan is reviewed whole by the panel+arbiter, not
part-by-part (reviewing part 1 of 4 misses whether the parts compose).

### Test mode

Generates failing acceptance tests from a defect + ticket. The tests must fail
at baseline (the test-first check enforces this). For a C# project, the test
file path is derived from the profile's `test_sources`, not hardcoded to Python.

### Developer mode

Autonomous localization + edit. Three phases: RED (write a failing test),
EXPLORE (read-only search), EDIT (edit + build + test). Phase separation
prevents editing before understanding. The test is read-only once it goes red.

### Docs mode

Generates documentation from the codebase. Four sub-modes: changelog (from a
diff), handover (from session state), design (from a feature + graph), prd
(from a defect + graph). Uses graph context when `graph_project` is set.

### Run-plan mode (implementing)

Executes a decomposed plan. See [PLAN_RUNNER.md](./PLAN_RUNNER.md) for the full
design. Creates a scratch branch, commits each promoted part, stops on failure,
and writes a plan-level manifest.

---

## 13. The model registry

**File:** `src/agent_loop/models.py`

Declarative mapping from role to model. The registry resolves which model
plays which role, with what budget. Per-model `max_tokens` is read from the
registry, not hardcoded at call sites — `ModelRegistry.max_tokens_for(model,
role, fallback)` is the single entry point.

The arbiter must not be the same model as any reviewer (enforced in code, N1).
The panel must have at least two members from at least two families (enforced
at import time, `check_panel_policy`).

---

## 14. Providers and transport

**File:** `src/agent_loop/providers.py`

Thin multi-provider chat shim. No third-party dependencies (uses
`urllib.request`). Supports `ollama`, `anthropic`, `openai`, `gemini`,
`github`, and `agy` backends.

Every backend returns the same `Completion` dataclass with `text`, `cost_usd`,
`input_tokens`, `output_tokens`, `secs`, `usage_line`. Transport failures raise
`ProviderError` and are distinguishable from a model that answered.

### Encoding (T-ENC1, R1)

Every subprocess capture pins `encoding="utf-8", errors="replace"`. Without an
explicit encoding, `text=True` decodes as cp1252 on Windows, and one non-ASCII
byte kills the reader thread — leaving `stdout=None` with `returncode==0`, so
a successful build is reported as `FAIL`. The encoding gate
(`test_subprocess_capture_encoding.py`) uses `rglob` (not `glob`) to inspect
all source files and derives the positive control from the actual file count.

### Anthropic prompt caching

Cache breakpoints are inserted at `turns[0]` (system + implement prompt with
verbatim source regions) and the latest user turn. `turns[0]` is byte-identical
across rounds, yielding ~80% cost reduction on input tokens for rounds 2+.

---

## 15. Tickets and regions

### Ticket JSON

```json
{
  "id": "T1",
  "title": "Fix the off-by-one in parse_date",
  "defect": "parse_date returns the wrong day for leap years.",
  "spec": "Fix the leap year check to handle Feb 29.",
  "regions": [
    {"id": "PARSE_DATE", "file": "src/dates.py", "anchor": "def parse_date"}
  ],
  "expect_green": ["test_parse_date_leap_year"]
}
```

### Regions

Each region has an `op`:
- **`replace`** (default): rewrite the anchored block.
- **`insert`**: add new code after the anchor; the anchored code stays.
- **`create`**: the file does not exist yet; the whole file is written.

### `--list` validation (T-BLG2/3, O65/O66)

`--mode patch --tickets plan.json --list` validates:
- Every region resolves against the current tree (prints OK/FAIL with line
  numbers).
- `expect_green` strings match actual test failures (set difference both
  directions: a string matching no failure = vacuous gate; a failure no string
  claims = forgotten criterion).
- Capitalized identifiers in `spec`/`context` that are not found in the
  region's file are warned about (the model's entire view of the file is the
  region; an invisible type gets a plausible guess).

---

## 16. Ledger and artifacts

### Per-ticket artifacts

`logs/agent_loop/<TICKET>/`:
- `00_implement_prompt.md` — the rendered implementer prompt
- `rN_impl_raw.txt` — implementer's raw response per round
- `rN_review_<model>.txt` — reviewer responses per round
- `rN_arbiter.txt` — arbiter response per round
- `rN_arbiter_prompt.md` — the rendered arbiter prompt (for replay)
- `rN_review_prompt.md` — the rendered review prompt (for replay)
- `rN_build.txt`, `rN_tests.txt`, `rN_lint.txt` — gate outputs
- `final_blocks.json` — the exported blocks
- `final.patch` — the exported diff for human review
- `result.json` — the full result dict

### Ledger

`logs/agent_loop/ledger.jsonl` — append-only, one line per ticket run. Each
record carries: `ticket`, `verdict`, `applied`, `rounds`, `cost_usd`, `gate`
(the distinct gates that failed, if any; excludes non-gate stages like
`implement` and `review`, N8), and `evidence`.

The **evidence** field (Wave 4.1, R5-4) records what was PROVEN, not just
that the run finished:
- **Promotable tickets:** `gate_ladder` — the mechanical gate summaries
  (static, compile, test, lock-scope) from the round that cleared every
  gate. This is the evidence that the patch compiles and passes tests, not
  the panel's opinion.
- **Failed tickets:** `blocked_by` and `block_summary` — which gate blocked
  and its summary.
- **Token usage:** per-role input/output tokens from the final round.

Thread-safe via a per-file `threading.Lock` (R5-6), same pattern as
`save_settled` (N2). Concurrent plan parts or parallel ticket runs on
Windows cannot interleave writes and corrupt the JSONL.

### Plan manifest

`logs/agent_loop/<plan_id>/plan_manifest.json` — one record per plan, listing
each part's verdict, the commit it landed at, and its acceptance tests. The
plan runner commits each promoted part to a scratch branch
(`agent-loop/plan-<id>`) and soft-resets the user's branch back so the
commit lives only on the plan branch (R5-1). Each part's worktree is based
at the plan branch HEAD, so part 2 sees part 1's promoted code (R5-2).

---

## 17. Invariants enforced in code

These guarantees were previously documented but not enforced. As of Waves 1–2
and the fifth review fixes, they are checked at entry:

| Invariant | Where enforced | Finding |
|---|---|---|
| Arbiter ≠ any reviewer | `run_ticket` entry | N1 |
| No duplicate reviewers | `run_ticket` entry | N6 |
| Panel ≥ 2 members, ≥ 2 families | `config.py` import time | O22 |
| Failed gates exclude non-gate stages | `failed_gate_names` | N8 |
| Thrashing counts UPHELD only | `loop.py` convergence tracking | N4 |
| Compaction refuses oversized pinned head | `compact_history` admission check | E3 |
| MCP stderr drained (no deadlock) | `mcp_client.start` | E2 |
| Panel deadline is hard (no `with`-block blocking) | `review_panel` | N7 |
| Parser tolerates `>>` closers | `parse_review`, `arbiter._section` | N9 |
| Arbiter diff truncation has visible marker | `_truncate_diff` | N5 |
| Encoding captures pin `utf-8, errors=replace` | encoding gate (rglob) | R1 |
| `save_settled` uses line-buffered append + lock | `save_settled` | N2 |
| `append_ledger` uses line-buffered append + lock | `append_ledger` | R5-6 |
| `revert()` prunes untracked files | `workspace.revert` | E-§2 |
| Test-first gate distinguishes causes | `run_ticket` baseline capture | O64 |
| `--list` validates expect_green | `cli._list` | O65 |
| `--list` warns on undeclared identifiers | `cli._list` | O66 |
| Quorum rescue drops member, not stop | `review_panel` quorum check | O67/R2 |
| Region size ceiling in plan mode | `_validate_feature_plan` | W6 |
| Plan runner commits to scratch branch, not user's | `_commit_to_branch` | R5-1 |
| Plan runner: part N sees part N-1's work | `run_ticket` `base_ref` param | R5-2 |
| JSON fallback uses balanced-brace extraction | `_extract_balanced_braces` | R5-3 |
| Evidence ledger records gate ladder, not panel verdict | `terminal_ledger_record` | R5-4 |
| Reasoning budget warning goes to stderr | `providers.chat` | R5-5 |

---

## 18. Review history and known findings

Four independent reviews have been conducted against this codebase:

1. **[AGENT_LOOP_CRITICAL_REVIEW.md](./AGENT_LOOP_CRITICAL_REVIEW.md)** — design
   and prompt architecture. Identified the TDD independence proxy, the lack of
   atomic task items, context fragility, and prompt-grammar brittleness.

2. **[AGENT_LOOP_INDEPENDENT_ENGINEERING_REVIEW.md](./AGENT_LOOP_INDEPENDENT_ENGINEERING_REVIEW.md)**
   — code-level audit. Identified the MCP stderr deadlock, token estimation
   inaccuracy, compaction admission gap, memory recency poisoning, and the full
   test suite per ticket.

3. **[AGENT_LOOP_THIRD_REVIEW.md](./AGENT_LOOP_THIRD_REVIEW.md)** — independent
   code audit. Found 9 new issues (N1–N9): unenforced arbiter≠reviewer,
   concurrency-unsafe `save_settled`, divergent graph-consumption paths,
   MAJOR/BLOCKER asymmetry, silent diff truncation, unenforced
   reviewers-must-differ, panel deadline not hard-bound, stage mislabeled as
   gate, parser-hardening inconsistency. Carries the consolidated 7-wave plan.

4. **[AGENT_LOOP_WORK_BREAKDOWN_AND_THROUGHPUT_REVIEW.md](./AGENT_LOOP_WORK_BREAKDOWN_AND_THROUGHPUT_REVIEW.md)**
   — work breakdown, throughput, reliability. Measured W1 (later part cannot
   see earlier part's work) and R1 (encoding gate inspects 26/29 files).
   Identified the missing plan runner, `depends_on` as dead machinery, no
   stop-on-fail, no rollback, and no evidence ledger per plan.

5. **[AGENT_LOOP_FIFTH_REVIEW.md](./AGENT_LOOP_FIFTH_REVIEW.md)** — code-level
   audit after Waves 0–4. Found 7 issues (R5-1 through R5-7): plan runner
   commits to user's branch (BLOCKER), plan runner part 2 doesn't see part
   1's work (BLOCKER), JSON fallback regex can't match nested objects,
   evidence ledger records panel verdict not gate ladder, reasoning budget
   warning not thread-safe, `append_ledger` has no lock, ARCHITECTURE.md
   §18 stale. All 7 fixed.

### Backlog

[BACKLOG.md](./BACKLOG.md) tracks O1–O67. The `## STATUS` block is the one line
everybody reads and the one nobody updates — verify currency by checking
`git log --oneline` against the last recorded tag.

### What's open

| Wave | Items | Status |
|---|---|---|
| 0 | T-REL1 through T-REL6 (mechanical reliability) | **Done** |
| 0.6 | T-ENC1 (encoding gate), T-ENC2 (sweep) | **Done** (ENC2 is the gate itself) |
| 1 | T-INV1 through T-INV4 (invariant enforcement) | **Done** |
| 2 | T-BLG1 through T-BLG4 (backlog defects O64–O67) | **Done** |
| 3 | T-OPS3, T-OPS5, T-OPS6 (operational) | **Done** |
| 0.5 | T-PLAN1 through T-PLAN3 (plan runner) | **Done** (R5-1/R5-2 fixed in fifth review) |
| 4 | TDD proxy, evidence ledger, JSON output, reasoning budget | **Done** (R5-3 through R5-6 fixed in fifth review) |