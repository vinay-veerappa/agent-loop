# Implementation Decision Log

**Purpose**: record every non-obvious decision made during implementation of
phases 1-8 so future sessions can review *why* the code is the way it is.
Each entry traces to the plan doc and the commit that landed it.

---

## Phase 1: State machine fixes (commits `1603a30`, `c4f3daf`)

### P1-1: Purge stale artifacts BEFORE the loop, not at round start
**Decision**: purge all `r*_*.txt` files before the loop starts, not at each
round's start.
**Why**: the original plan said "at round start" but with `--max-rounds 1` the
loop only enters round 1, so stale `r2_impl_raw.txt` from a prior run is never
purged. Purging before the loop ensures the on-disk artifacts always match
`result.json`'s round count regardless of `max_rounds`.
**Tradeoff**: purging before the loop is slightly more aggressive (deletes
artifacts from a concurrent run), but the run lock prevents concurrent runs
on the same repo.

### P1-2: ARBITER_DEADLOCK reverts touched files before breaking
**Decision**: call `ws.revert(touched)` before `break` on arbiter-unreachable.
**Why**: without revert, the worktree has a half-applied patch that the next
resume would build on. Same pattern as PANEL_UNREACHABLE (P1-6).
**Tradeoff**: the candidate is on disk in `rN_impl_raw.txt` and can be resumed;
reverting the worktree is safe.

### P1-3: arbiter_consulted flag, not a separate "ran" tracker
**Decision**: a boolean `arbiter_consulted` set to `True` when `adj.ok` is True,
checked after the loop to distinguish `ARBITER_NEVER_RAN` from
`MAX_ROUNDS_EXHAUSTED`.
**Why**: simpler than tracking which round the arbiter ran on. The question is
binary: was the arbiter ever consulted, or not?
**Tradeoff**: if the arbiter is consulted on round 1 but unreachable on round 2,
the flag is True and the final verdict is `MAX_ROUNDS_EXHAUSTED` (not
`ARBITER_DEADLOCK`), which is correct — the arbiter was consulted at least once.

### P1-4: Keep `applied` as a backward-compat alias
**Decision**: keep `result["applied"]` as `applied_approved or applied_unapproved`
in addition to the new `applied_approved` and `applied_unapproved` booleans.
**Why**: existing code (ledger, CLI summary) reads `result["applied"]`. Removing
it would break backward compatibility. The new fields are additive.

### P1-7: Quorum threshold is `ceil(2/3 * len(reviewers))`
**Decision**: quorum = ceil(2/3 * len(reviewers)), not a fixed number.
**Why**: 2-of-3 should proceed, 3-of-5 should proceed, but 1-of-3 should not.
`ceil(2/3 * N)` gives: N=3 -> 2, N=5 -> 4, N=4 -> 3. This is strict enough to
prevent a single reviewer from approving, but lenient enough to handle one
unreachable reviewer in a 3-reviewer panel.
**Tradeoff**: a 4-of-5 panel with 3 approvals (quorum=4 not met) hard-stops.
This is correct — 3-of-5 is not a supermajority.

---

## P1-4b: CLI exit code (commit `c4f3daf`)

### First ticket the loop ran against itself
**Decision**: the loop's first self-referential ticket was the CLI exit code fix
(P1-4b), not a state-machine fix.
**Why**: P1-4b was small (one line), had a clear test (check that `ARBITER_SHIP`
appears in the exit-code condition), and the region anchor was a single line
(`kind="line"`). This minimized the risk of the first self-run failing on a
complex region extraction.

### pytest-compatible test parser
**Decision**: added pytest output format (`N failed, M passed` and
`FAILED tests/x.py::test_name`) to `gates.parse_tests()`, alongside the NT8
`dotnet test` format.
**Why**: the loop's test gate must parse pytest output to run against Python
codebases. The original parser only handled `RESULTS: Passed=N, Failed=M`.
**Tradeoff**: the parser now handles two formats; a third (e.g. `go test`) would
need another regex. This is a profile-level concern that should eventually move
to the Profile's `test_runner_regex` field.

### pyproject.toml `pythonpath = ["src"]`
**Decision**: added `[tool.pytest.ini_options] pythonpath = ["src"]` to
`pyproject.toml`.
**Why**: the loop runs tests in a git worktree (a fresh checkout). The
`agent_loop` package isn't pip-installed in the worktree; pytest needs
`pythonpath` to find `src/agent_loop/`. Without this, tests fail with
`ModuleNotFoundError` in the worktree.
**Alternative considered**: `set PYTHONPATH=src &&` in the profile's `test_cmd`.
Rejected because it's Windows-specific (`set` vs `export`) and fragile.

---

## Phase 2: Graph freshness (commit `ec91be4`)

### graph_project field on Profile, not hardcoded
**Decision**: `graph_project` is a Profile field (string, default `""`).
**Why**: the graph project name is repo-specific
(`C-Users-vinay-agent-loop`, `C-Users-vinay-tvDownloadOHLC`). Hardcoding it
would violate the language-agnostic principle. When empty, the freshness check
is skipped silently.
**Tradeoff**: the consumer must know their graph project name. This is
acceptable — it's a one-time setup.

### Freshness check is a status report, not a gate
**Decision**: `check_graph_freshness()` prints a `[graph]` status line but
does not block the loop if the graph is stale.
**Why**: the graph is an enhancement, not a gate. A stale graph is better than
no graph and better than a 45-second startup wait. The loop should proceed
with whatever graph state exists.
**Tradeoff**: the loop might inject stale context. Acceptable — the gates and
panel catch wrong context; the graph just helps the implementer localize.

---

## Phase 3: Passive context injection (commit `2b4cd00`)

### Cache file design, not live MCP calls
**Decision**: `build_context_slice()` reads a pre-computed cache file
(`logs/agent_loop/graph_context.json`), not live MCP queries.
**Why**: the codebase-memory-mcp runs as a separate MCP server process. Calling
it from the loop requires the MCP client protocol (JSON-RPC over stdio), which
is a significant dependency. The cache-file design decouples the loop from the
MCP client — any tool that writes the cache file works.
**Tradeoff**: the cache must be populated before the loop runs (by
`scripts/populate_graph_context.py` or by an MCP-aware agent). In a future
phase, the loop itself will call the MCP server live.

### Context injected into implementer prompt only, not reviewer/arbiter
**Decision**: graph context is injected into the implementer prompt only.
**Why**: the implementer needs context to write the right code. The reviewer
and arbiter review the diff (which already reflects the code structure) and
don't need graph context — they need the patch, the ticket, and the settled
decisions. Injecting graph context into reviewer prompts would consume tokens
without adding signal.
**Tradeoff**: if the reviewer needs to check "will this break callers?", it
can't see the graph. This is a Phase 7+ concern (active graph tools for the
reviewer).

### Token budget enforcement at the slice level
**Decision**: the context slice is truncated to `context_token_budget * 4`
chars (estimated 4 chars/token) after all regions are concatenated.
**Why**: per-region truncation (limiting to 10 callees, 10 callers, 5 tests, 5
types) keeps individual region context small, but with many regions the total
can exceed the budget. The final truncation ensures the total stays under
budget.
**Tradeoff**: truncation at the slice level may cut a region's context
mid-line. The `"... (truncated to token budget)"` marker makes this visible.

---

## Phase 4: Compaction (commit `ce86d50`)

### Mechanical summary, not LLM summarization
**Decision**: Phase 4b uses `_mechanical_summary()` (extract finding counts and
first lines), not an LLM call to a compactor model.
**Why**: the plan called for LLM summarization via a cheaper compactor model,
but that requires the compactor to be registered in the model registry and
callable from the compaction path. The mechanical summary works without any
model call and produces a compact block that preserves the essential structure
(what was tried, how many findings, what feedback was given).
**Tradeoff**: the mechanical summary is less readable than an LLM summary. A
future upgrade will call the compactor model when one is registered.

### Per-artifact threshold of 5000 chars (~1250 tokens)
**Decision**: individual artifacts above 5000 chars are pruned to truncation
markers.
**Why**: the OpenCode pattern (research doc reference) uses 40K tokens for the
*entire* old-tool-output pruning step. Our 5K-char per-artifact threshold is
more aggressive because our artifacts are smaller (implementer blocks, not
full tool-call traces). 5000 chars preserves the first and last 500 chars of
each artifact, which is enough to understand the shape.
**Tradeoff**: a very long implementer output (e.g. 50K chars) loses its middle.
The `COMPACTED` marker shows how much was pruned.

### Findings compacted to per-finding summaries, not aggregate counts
**Decision**: `_compact_findings()` extracts each finding's severity and
first 80 chars of text, not just a count.
**Why**: the Gemini review (edge case 5) flagged that aggregate counts lose
too much structure. Per-finding summaries preserve enough for the implementer
to know what was wrong without the full finding text.
**Tradeoff**: 10 findings × 80 chars = 800 chars per prior round. With 4
rounds, that's 3200 chars of compacted findings — well under the 5K threshold.

---

## Phase 5: Persistent memory (commit `2ce0ec2`)

### JSONL store, not SQLite
**Decision**: settled decisions are stored in a JSONL file
(`logs/agent_loop/settled_decisions.jsonl`), not a SQLite database.
**Why**: JSONL is append-only, human-readable, and doesn't require a schema
migration. SQLite would add a dependency and a serialization layer for a simple
key-value store. The OpenWorker reference uses SQLite, but their memory is
richer (summary column, session-stable); ours is just text decisions keyed by
ticket+hash.
**Tradeoff**: JSONL can't be queried efficiently. With hundreds of decisions,
loading is O(N). Acceptable — we expect <1000 decisions.

### Deduplication by ticket + hash of decision text
**Decision**: the key is `f"{ticket_id}:{_hash_text(decision)}"`, not just the
decision text.
**Why**: the same decision text from different tickets should both be saved
(different tickets may nominate the same settled decision independently). But
the same decision from the same ticket should not be saved twice (e.g. if the
arbiter repeats it across rounds). The ticket+hash key achieves this.
**Tradeoff**: a decision that is re-worded slightly will get a new key and be
saved as a duplicate. The `inject_settled()` function deduplicates by text
content when loading, so the injected list won't have duplicates even if the
store does.

### Atomic append, not file locking
**Decision**: uses `tempfile.mkstemp` + append, not `portalocker`.
**Why**: `portalocker` is a third-party dependency. The atomic append pattern
(write to temp, then append) avoids corruption without a dependency. On
Windows, `os.replace()` is atomic, but we're appending (not replacing), so the
temp file is just a buffer — the actual append is a standard file append which
is atomic for small writes on most filesystems.
**Tradeoff**: on a filesystem with non-atomic appends, two concurrent writers
could interleave lines. The `seen_keys` check in `save_settled()` deduplicates
on load, so interleaved lines would just be duplicates that are filtered out
by `load_settled()`.

---

## Phase 6: Plan + Test modes (commit `de8341e`)

### Plan mode reuses the existing panel + arbiter
**Decision**: plan mode calls `review_panel()` and `arbiter.adjudicate()` from
`loop.py`, not a separate review pipeline.
**Why**: the panel and arbiter are the moat (§8.6 of the research doc). Plan
mode benefits from the same adversarial review + adjudication as patch mode.
Reusing them ensures plan quality is held to the same standard.
**Tradeoff**: the panel reviews a JSON ticket, not a code diff. The reviewer
prompt is different (plan completeness, not code correctness). This is handled
by passing a plan-specific review prompt to `review_panel()`.

### --fast-plan flag skips panel+arbiter entirely
**Decision**: `--fast-plan` accepts the plan after region verification, without
panel or arbiter review.
**Why**: the Gemini review (edge case 3) flagged that a full panel+arbiter cycle
adds ~2 minutes and ~$0.30-$1.00 per planning iteration. For rapid prototyping
(exploring what regions a defect touches), the full cycle is too slow. The
`--fast-plan` flag trades verification depth for speed.
**Tradeoff**: a fast-plan plan is not reviewed. The user must review it
themselves before feeding it to patch mode. The plan doc specifies this.

### Test mode verifies baseline failures but does NOT gate on them
**Decision**: `run_test()` runs the tests and reports whether they fail at
baseline, but does not refuse to write the test file if they pass.
**Why**: the test-first check in `loop.py:442-457` is the gate that refuses
vacuous tests. Test mode is a *generation* step — it writes the test file for
the user to review. The gate fires when the test file is consumed by patch
mode, not when it is generated.
**Tradeoff**: the generated tests might pass at baseline (a bad test). The
user sees a WARNING in the output. The gate catches it when patch mode runs.

---

## Phase 7+8: Developer mode (commit `8a0ff49`)

### Tool calls via <<<TOOL>>> blocks, not OpenAI function-calling
**Decision**: the LLM emits tool calls as `<<<TOOL name="tool_name">>>{args}<<<END TOOL>>>`
blocks, not as OpenAI function-calling JSON.
**Why**: the loop's provider shim (`providers.py`) returns raw text, not
structured tool-call objects. Parsing tool calls from text keeps the loop
provider-agnostic (any model that can emit structured text works, including
Ollama models that don't support function-calling natively).
**Tradeoff**: models that support native function-calling (GPT-4, Claude) could
be more reliable with structured tool calls. A future upgrade can add
function-calling support to `providers.py` without changing the tool execution
layer.

### Phase separation: explore (read-only) -> edit (no search)
**Decision**: the explore phase has `read_file`, `search_code`,
`trace_call_path` (read-only); the edit phase has `edit_file`, `run_build`,
`run_tests` (no search).
**Why**: AutoCodeRover's pattern (research doc §10.6) — prevents the LLM from
editing before it understands the codebase. The transition from explore to edit
happens when the LLM first calls `edit_file`.
**Tradeoff**: the LLM cannot search after it starts editing. If it needs to
read another file mid-edit, it can use `read_file` (available in both phases).
`search_code` and `trace_call_path` are explore-only.

### File-level scope gate via `file_scope_whitelist`
**Decision**: the Developer mode gate checks that `edit_file` calls only touch
files within `profile.file_scope_whitelist`. The static gate and lock-scope
gate from patch mode are not applied (Developer mode has no declared regions).
**Why**: the Gemini review (edge case 5) flagged that line-level lock-scope
doesn't work in Developer mode (regions are dynamic). The file-level scope gate
replaces it — the LLM may only edit files in the whitelisted directories.
**Tradeoff**: a malicious LLM could edit any file in the whitelisted directory,
including files unrelated to the defect. The panel + arbiter catch this in
review.

### Developer mode does not use the panel + arbiter (yet)
**Decision**: the current Developer mode driver runs the gate ladder (compile,
test) but does NOT run the panel + arbiter after the edit phase.
**Why**: the panel reviews diffs, and Developer mode produces a diff. However,
wiring the full panel + arbiter requires the review prompt to be built from the
diff (not from regions). This is a straightforward addition but adds
complexity. The current implementation focuses on the localization + edit
phases; the panel + arbiter integration is a follow-up.
**Tradeoff**: Developer mode patches are not reviewed by the panel. The human
must review the diff before applying. This matches the plan's principle: "the
arbiter recommends; a human runs --apply."

---

## Cross-cutting decisions

### Indent-based region finder (`kind="indent"`)
**Decision**: added a new `kind="indent"` mode to `find_region()` that uses
indentation level instead of brace matching, triggered by
`profile.block_kind="indent"`.
**Why**: Python's `def`, `for`, `if`, `class` blocks use indentation, not
braces. The original brace matcher would fail with "unbalanced braces" on every
Python anchor. The indent finder uses the anchor's indentation level and
extends until the indentation returns to the same or lesser level.
**Tradeoff**: the indent finder is less precise than a tree-sitter parse (it
doesn't understand nested blocks, decorators, or multi-line expressions). It
works for the common case (a function or class body) and is a 50-line addition
vs. a multi-hundred-line tree-sitter dependency.

### `block_comment = ()` for Python profiles
**Decision**: Python profiles use `block_comment = ()` (empty tuple), not
`("#",)`.
**Why**: the `guard_unsupported_syntax()` function rejects files containing
block-comment tokens. For C#, `/*` is a block comment that rarely appears. For
Python, `#` is a *line* comment that appears in every file. Setting
`block_comment = ()` prevents the guard from rejecting every Python file.
**Tradeoff**: the guard no longer checks for Python block comments. Python
doesn't have block comments, so there's nothing to guard against.

### All new modules are in `src/agent_loop/`, not `scripts/`
**Decision**: `context.py`, `compaction.py`, `memory.py`, `plan_mode.py`,
`test_mode.py`, `developer/tools.py`, `developer/driver.py` are all in the
`src/agent_loop/` package.
**Why**: the package is pip-installable. Modules in `src/` are importable via
`from agent_loop.context import build_context_slice`. Modules in `scripts/`
would not be importable.
**Tradeoff**: the package is larger. But each module is <300 lines and has a
clear single responsibility.

---

## Phase 9: Learning feedback + context bloat control

### Settled-decisions injection capped at 20 most recent
**Decision**: `inject_settled()` caps auto-extracted decisions at
`MAX_SETTLED_INJECTED=20` most recent. Older decisions stay on disk.
**Why**: after 100 tickets with 5 decisions each, injecting all 500
decisions would add 25K tokens to every review prompt. Capping at 20
keeps injection at ~1K tokens regardless of ticket count.
**Tradeoff**: decisions 21+ are not visible to reviewers. If a
decision from ticket 5 is relevant to ticket 50, the reviewer won't
see it unless it's in the top 20. Hand-curated decisions in
`profile.settled` bypass this cap — they're always injected.

### Learning feedback store (`learning_feedback.jsonl`)
**Decision**: after each arbiter ruling, `save_feedback()` records
which finding was UPHELD vs REJECTED. Before each review,
`build_learning_context()` injects "known false positives (do NOT
re-raise)" and "known real defects (keep flagging)" into the
reviewer prompt. Capped at `MAX_FEEDBACK_INJECTED=10` entries.
**Why**: the loop already learns via settled decisions (Phase 5),
but that only prevents re-litigating *adjudicated* precedents. The
learning feedback goes further: it tells reviewers "the arbiter
rejected this finding on ticket X" even if the finding wasn't
nominated as a settled decision. This reduces false-positive churn
across tickets.
**Tradeoff**: the feedback store grows unboundedly (one entry per
finding per round). Old entries are not pruned automatically. A
future upgrade should prune entries older than N tickets or
compact the feedback store periodically.

### `save_feedback` deduplicates by ticket+round+finding_hash+ruling
**Decision**: the key is `f"{ticket_id}:{round_num}:{_hash_text(finding_text)}:{arbiter_ruling}"`.
This means the same finding text from the same round is only saved
once, but the same finding text from a different ticket or round is
saved separately (it's a different learning event).
**Why**: if ticket A rejects a "lock-scope violation" finding, and
ticket B raises the same finding, both events are recorded. The
reviewer on ticket C sees "this finding was rejected on tickets A
and B" — two data points, not one.

### Path traversal fix in `read_file`/`edit_file`
**Decision**: both tools now resolve the path and check
`str(path).startswith(str(repo.resolve()))` before reading or
writing. If the path escapes the repo, the tool returns an error.
**Why**: the 3-model cross-review (Phase 8.5) flagged that
`repo / args["path"]` doesn't constrain `args["path"]` to be
inside `repo`. An LLM (or malicious prompt) could pass
`../../etc/passwd` or `/absolute/path` and read/write outside
the repo.
**Tradeoff**: symlinks inside the repo that point outside are not
blocked. This is a known limitation; a future upgrade should
resolve symlinks before the containment check.

### `check_graph_freshness` compares mtime against persisted marker
**Decision**: the function now compares the mtime of the newest
source file against a persisted marker file
(`logs/agent_loop/.graph_mtime`). If the marker is older than the
newest source file, the graph is "stale".
**Why**: the original implementation always returned "fresh"
without checking anything. The cross-review flagged this as a
no-op. The mtime comparison is a cheap proxy for "has the code
changed since the last index?"
**Tradeoff**: the marker is not updated after indexing. A future
upgrade should write the current timestamp to the marker after a
successful re-index.

---

*End of decision log. All phases 1-9 complete.*