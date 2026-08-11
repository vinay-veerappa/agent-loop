# Backlog — What's Left to Implement

**Purpose**: track every gap between the execution plan and the built code.
Each item has a priority, an effort estimate, and a reference to the plan
section or decision log entry that motivates it.

**Last updated**: after the 2026-08-10 full-package review.

## STATUS

All 17 backlog items addressed + Phase 9 complete + review fixes applied.
**173/173 tests pass on Python 3.12 and 3.14.** Current tag: **`v0.2.2`**, which
is what tvDownloadOHLC pins and has installed.

Tag hazards: `v0.1.0` predates Phase 9 and all review fixes. `v0.2.0` carries
the O9 defect and **cannot run on Python < 3.13 at all**. Use `v0.2.2` or later.

### 2026-08-10 review — 22 defects found and fixed

A line-by-line review of all 6,001 lines. The suite was green throughout,
which is the finding behind the findings: it exercised the state machine
against fakes and never crossed a boundary — a real pytest summary, a real
`git stash`, a real second language, a real promote, a real install.

Blocking:

| Defect | Where | Fix |
|---|---|---|
| `--mode test` stashed the live tree and never restored it (`_git` returns a str; the 2-tuple unpack raised before `stash pop`), then exited 0 | `test_mode.py:125` | baseline verified in a throwaway worktree; live tree never touched |
| The pytest parser read `17 passed, 1 warning` and `15 passed, 2 skipped` as "runner never finished", so any warning aborted the ticket at baseline capture | `gates.py:206` | counts read by keyword from the summary line; `errors` tracked separately |
| `python-tvdownloadohlc` could not run one ticket: `test_cmd` produced 15 collection errors and `build_cmd` named a file that does not exist | consumer profile | green suites (64 pass, 1 frozen failure); `build_cmd` uses `{files}` |
| `--mode review --review-verify` raised AttributeError on `TestOutcome.reached_results` | `cli.py:67` | `.ran`; build/test steps skipped when the profile has no command |
| Developer mode had no protected-path gate and edited the live tree against an empty baseline; `--apply` was accepted and ignored | `developer/` | worktree + frozen baseline + gate 0 in `_edit_file`; `--apply` promotes |

High:

| Defect | Where | Fix |
|---|---|---|
| Phase 4a/4b compaction folded away the implement prompt — the ticket, spec and region source — then asked for "ALL blocks in full" | `compaction.py` | `pin_count()` pins system + implement prompt through both phases |
| Compaction truncated the candidate under revision (the newest exchange) | `compaction.py:68` | newest exchange kept verbatim |
| The 4b summary was a second `user` turn, giving `[system, user, user]` → non-retryable Anthropic 400 | `compaction.py` | summary emitted as an `assistant` turn; alternation asserted by test |
| Learning feedback stored `ruling.reason` as the finding text, every severity as `BLOCKER`, and `"?"` as the author | `loop.py:739` | rulings joined back to `all_findings` by index |
| Reviewer token accounting was permanently zero behind a `hasattr` guard on fields `Vote` never had | `loop.py:650` | `Vote.input_tokens/output_tokens`, populated in `review_panel` |
| The arbiter prompt was hardcoded to NinjaTrader; its UPHELD bar ("loses money or leaves a position unprotected") is unmeetable elsewhere, so the arbiter rejected everything and recommended SHIP | `arbiter.py:39` | `Profile.arbiter_rules`; generic default; NT8 text moved to its profile |
| `loop.py` hardcoded ` ```csharp `; `extract_test_sources` was C#-only, so Python reviewers saw no acceptance tests | `loop.py` | `Profile.fence`, `regions.extract_named_block` (indent + decl) |
| `effective_settled` was computed, printed, and then discarded — the settled store was never read back into a prompt | `loop.py:452` | passed to the review prompt and the arbiter |

Medium / minor: `promote()` overwrote uncommitted work; a quorum-only panel was
recorded as a unanimous approval; `ModelRegistry` overwrote by role so a
two-family panel collapsed to one member; `cost_summary` priced input at the
output rate; ollama `num_ctx` (32768) was smaller than the requested output
budget (48000); the developer tool-call protocol was never documented to the
model while out-of-phase calls were dropped silently; `any()` made a
multi-ticket run exit 0 when one of four tickets passed; `check_graph_freshness`
reported "stale" forever because nothing wrote the marker; the graph name
extractor understood only `def`/`class`, so every NT8 query was junk;
`build_context_slice` ran twice per round; the arbiter never received the graph
context Phase 3 promised it; `save_feedback` rewrote the whole store per
finding; `expect_green` matched substrings so `test_foo` was satisfied by
`test_foo_bar`; `regions.apply` normalised line endings and added trailing
newlines; path containment used `str.startswith`; the README and the `Profile`
docstring recommended `block_comment=("#",)` for Python, which refuses every
Python file containing a comment.

Deliberately NOT changed (needs a decision):

- `logs/agent_loop/` is written into the consumer repo. Fine for tvDownloadOHLC, awkward for a library.
- `guard_unsupported_syntax` still refuses any C# file containing `/*`. Correct but coarse; a real parser (tree-sitter) is the fix.
- The `v0.1.0` tag predates Phase 9 and these fixes, and `requirements.txt` in tvDownloadOHLC pins it. Needs a new tag + push.

### 2026-08-10 — open issues after the F1-F6 self-hosted run

The loop ran six tickets against its own source (`tickets/review_followups.json`,
commit `41e5fd0`): unanimous panel APPROVE in round 1 on all six, all gates
green, nine red acceptance tests turned green. Three loop defects the run
exposed were fixed in that commit. What follows is what is still open, with the
mechanism, so none of it has to be re-derived.

#### O1. `promote()` cannot handle two tickets that touch one file — HIGH

`Workspace.promote` is a `shutil.copy2` per file, not a patch application. F4
and F5 both edit `src/agent_loop/report.py`; each patch was produced in its own
worktree from the same base. Promoting both in either order copies a whole file
that contains only one of the two changes, so **the second promote silently
reverts the first** — and the ledger records `applied` for both.

The dirty-target guard added earlier turns this from silent loss into a
`WorkspaceError`, which `cli.main` catches and records as `ERROR`. That is the
right failure, but the capability is still missing. Two candidate fixes:

* apply `final.patch` with `git apply` instead of copying files — composes
  correctly, and the patch is already the review artifact; or
* detect the collision up front (two selected tickets sharing a region file) and
  refuse the run with a message, documenting one-file-per-run.

The F1-F6 patches were landed with `git apply` by hand for exactly this reason.

#### O2. `replay` mode does not hold the prompt constant — HIGH

`replay.run_replay` cannot reconstruct the regions, so it builds its own review
prompt (`replay.py:84-90`): the implement prompt truncated to 2000 chars plus
the raw implementer output truncated to 8000. The recorded verdict came from the
real prompt — BEFORE/AFTER blocks, gate summary, settled decisions, acceptance
tests, graph context, learning feedback. A "flip" therefore compares two
different prompts and says nothing about the change under test, while
`run_replay_corpus` returns exit 1 on any flip, so wiring it into CI produces a
gate that fails on noise. Live model calls make flips partly sampling variance
on top of that.

Fix: record the rendered review prompt (and the rendered arbiter prompt)
alongside `r{N}_impl_raw.txt` in `run_ticket`, and have replay re-send *that*
byte-for-byte. Until then replay is decorative.

Second defect on the same path: `art = ticket_dir` (`replay.py:93`) passes the
recorded ticket directory to `review_panel`, which writes
`r{N}_review_{model}.txt` into it — **a replay overwrites the corpus it is
replaying**. Write to a `replay/` subdirectory.

#### O3. `report` gate-failure distribution reads a field the ledger never writes — MEDIUM

`_print_gate_failures` keyword-scans `e.get("detail")`. `append_ledger` writes
`detail` only on the protected-paths rejection; static, lint, compile, test and
lock-scope failures never reach the ledger at all. The counts it does print are
also wrong: `--mode report` on this repo shows `test 8 / protected 8`, which is
the same 8 selftest rejections counted twice because their detail text mentions
`*Tests.cs`.

`run_ticket` already knows `failed.name`. Record it in the ledger and read that
instead of scanning prose.

#### O4. `report` arbiter calibration correlates coupled variables — MEDIUM

`_pearson` is now arithmetically correct (F5), but its inputs are not
independent: `upheld_per_ticket` sums upheld findings **across rounds** while the
y-variable *is* the round count, so more rounds mechanically means more recorded
findings. The metric will report "arbiter is upholding noise" almost regardless
of arbiter quality. Normalise to upheld-per-round, or compare upheld count
against a convergence outcome rather than against the round count.

#### O5. `Finding.signature` still breaks on suffix changes — MEDIUM

F1 removed the digit/punctuation fragility, and the property now keeps the
**full** normalised text. So a reviewer that adds a trailing clause in round 2
("X is wrong" → "X is wrong because Y") still produces a non-overlapping
signature, and `thrashing()` can still fire on a converging ticket. Signature
normalisation is a band-aid in either direction; the durable fix is to ask the
arbiter — which already sees every finding from every reviewer — whether finding
#3 is the same as last round's #7, and use that for convergence detection.

#### O6. The panel did not earn its cost on this run — OBSERVATION

Two adversarial reviewers from different families, six patches, **zero
findings**. Two of those patches had defects visible in what the reviewers were
shown: F5 emitted a module-level `import` mid-file (plainly in the AFTER block),
and F2 added a parameter no caller passed (their own priority list includes
"does it break callers?"). Every correctness outcome on this run came from the
gates — static, compile, test against a frozen baseline with `expect_green`, and
lock-scope.

One run is not a verdict on the panel. It is a reason to answer the question
with data now that O3/O4 and the reviewer-overlap metric (F4) are fixed: run
enough tickets to populate the feedback store, then read unique-upheld per
reviewer. If it stays near zero, the panel is latency and tokens for nothing on
this class of ticket, and the interesting configuration is gates + arbiter.

#### O7. Whole rungs are still unexercised end to end — TEST GAP

Because all six tickets converged in round 1 with a unanimous panel:

* the **arbiter** never ran — adjudication, `upheld_indices` feedback, ESCALATE,
  and `ARBITER_DEADLOCK` are covered only by fakes in `selftest.py`;
* **compaction** never triggered (it starts at round 2);
* nothing was written to the **settled-decisions** store, and the **learning
  feedback** store only ever received selftest stub data;
* `APPROVE_PARTIAL`, `PANEL_UNREACHABLE` and `NOT_CONVERGING` were not reached.

A deliberately hard or under-specified ticket is the cheapest way to exercise
these. Modes never run at all: `plan`, `test`, `developer`, `brainstorm`,
`docs`, `review`. Developer mode is the priority — it received the largest
changes (worktree, frozen baseline, protected-path gate in `_edit_file`) and has
the least coverage.

#### O8. Small, unticketed — LOW

* `--mode report` and `--mode replay` both require `--profile` and print the
  single-reviewer panel warning; report needs neither.
* `replay.py`'s docstring documents a `--replay-dir` flag that the CLI does not
  implement.
* `replay.py` imports `Completion`, `ProviderError`, `chat`, `Finding`,
  `PanelResult`, `RoundRecord` and uses none of them.
* `replay`'s `adjudicate` call omits `rules=profile.arbiter_rules` and uses
  `profile.settled` rather than `inject_settled(...)`, so it diverges from the
  real pipeline in two more ways.
* `check_lint` reuses `_digest`, whose regex is the MSBuild `error CS1234`
  format; ruff-style output matches nothing and falls through to the raw tail.
* `_call_openai` does not capture its cached-token usage field, so the OpenAI
  backend cannot report cache hits at all.

### 2026-08-10 (later) — consumer unblock session: O9-O11

Found while making tvDownloadOHLC able to consume the package at all. The two
CLOSED items below were both invisible to the existing test suite for the same
structural reason: **the tests call the library functions directly, with correct
arguments, so nothing exercised the CLI wiring that end users actually go
through.** Both were found by running the shipped commands, not by review.

#### O9. `Path.read_text(newline=)` breaks every Python < 3.13 — CLOSED (`27eeacc`, v0.2.1)

`Path.read_text`/`Path.write_text` only accept `newline=` on Python 3.13+, but
`requires-python` is `>=3.10`. Six call sites used that form, so on 3.10-3.12
`regions.read_source` raised `TypeError` — and since `regions.extract` calls it,
**every ticket died before reaching a model, including `--list`.**
`developer/_edit_file` and `workspace.export_patch` were dead the same way.
Unseen because the dev interpreter is 3.14; surfaced the instant the package was
installed into the consumer venv (3.12). Fixed by `agent_loop._io`
(`read_text_verbatim`/`write_text_verbatim` over `open(newline="")`). The suite
now runs green on **both** 3.12 and 3.14, and a static guard test
(`test_no_path_text_newline_kwarg`, mutation-checked) fails if the kwarg returns.

**Lesson for CI:** a single-interpreter test run cannot see this class of defect.
The suite should run on the lowest supported version, not just the dev version.

#### O10. Docs mode had never run — CLOSED for wiring, OPEN for conventions

`cli._docs()` called `run_docs()` **positionally** against a signature it did not
match: `profile` received the `--review-base` string, `implementer` received the
`Profile`, and `docs_type` received the model name. Every sub-mode of every
invocation returned `unknown docs type: 'kimi-k2.7-code:cloud'`. Four further
wiring defects in the same 18-line function:

* no `--docs-type` argument existed, so 3 of the 4 sub-modes were unselectable
  even after the call was fixed (the README documented the flag regardless);
* `--review-base` was required for all four, though only `changelog` reads a diff;
* `--defect` was never forwarded, so `design`/`prd` had no input; and
* `output_path` was `args.test_file or "docs/UPDATES.md"` — and `--test-file`
  **defaults** to `tests/acceptance/test_generated.py`, so the left side was
  never falsy and docs mode would have written markdown over a test file.

Fixed with `--docs-type` + `--docs-out`, keyword-only forwarding, per-sub-mode
validation, and defaults under gitignored `docs/generated/`. 18 regression tests
in `tests/acceptance/test_docs_mode_cli.py` drive `main(argv)` so argparse is in
the loop; 17 of the 18 fail against the pre-fix `cli.py`. `changelog` and
`handover` have now been run end to end against a live model.

**Still OPEN (MED):** the README claims docs mode follows the doc-architect
skill's conventions. It does not — the four system prompts in `docs_mode.py` are
hardcoded and contain nothing project-specific, so generated docs do not match
any repo's house format. Either inject the skill's conventions into the system
prompt or add a `Profile.docs_conventions` field. README now says so explicitly.

#### O11. Consumer pin and install — CLOSED

`agent_loop` was not installed in the tvDownloadOHLC venv (every documented
command raised `ModuleNotFoundError`), and `requirements.txt` pinned `@v0.1.0`,
14 commits and ~25 known defects behind. Now pinned to and installed at
**v0.2.2**. Note `v0.2.0` is a **poisoned tag**: it carries the O9 defect, so it
is unusable on any Python below 3.13. Do not pin it.

### Note on graph re-index for tvDownloadOHLC

The `codebase-memory-mcp` graph for tvDownloadOHLC (`C-Users-vinay-tvDownloadOHLC`)
is stale — it indexes the predecessor `ollama_patch_loop.py`, not the current
`loop.py`. Re-indexing was attempted but the MCP server timed out (the repo
has 39K+ nodes and the index operation exceeds the MCP request timeout).
The re-index should be run after restarting the MCP server, or by running
the `codebase-memory-mcp` exe directly outside of the MCP client.

The `agent-loop` repo graph (`C-Users-vinay-agent-loop`, 258 nodes) is fresh.

---

## 1. Stubs (partially built, not functional)

### 1.1 `trace_call_path` in Developer mode tools
- **Where**: `src/agent_loop/developer/tools.py:166`
- **Status**: stub — returns a placeholder string, no real graph query
- **Plan ref**: §5 Developer mode spec, tool set table
- **Effort**: medium — requires MCP client protocol or direct graph DB access
- **Fix**: wire `trace_call_path` to call the codebase-memory-mcp graph via
  the MCP client protocol (or via a subprocess JSON-RPC call to the MCP exe).
  The tool should return callers (inbound) and callees (outbound) for a
  function name, using the profile's `graph_project`.

### 1.2 `build_context_slice()` live MCP calls
- **Where**: `src/agent_loop/context.py`
- **Status**: cache-file only — reads `logs/agent_loop/graph_context.json`,
  doesn't query the graph live
- **Plan ref**: §3 Phase 3, "Implementation reference" (Aider PageRank)
- **Effort**: medium
- **Fix**: replace the cache-file read with live MCP queries
  (`trace_call_path`, `search_graph`, `get_code_snippet`) for each region's
  functions. Rank by structural distance, truncate to `context_token_budget`.
  The cache-file design can remain as a fallback for offline operation.

### 1.3 Phase 4b LLM summarization — DONE (commit `072241a`)
- **Where**: `src/agent_loop/compaction.py`
- **Status**: done — `_llm_summary()` calls the compactor model from the registry, falls back to `_mechanical_summary()`

### 1.4 PANEL_REJECT signal — DONE (commit `072241a`)
- **Where**: `src/agent_loop/loop.py` (feedback to implementer)
- **Status**: done — REJECT feedback says "RETHINK THE APPROACH" not "fix these lines"

---

## 2. Missing wiring (components built but not connected)

### 2.1 Developer mode panel + arbiter — DONE (commit `072241a`)
- **Where**: `src/agent_loop/developer/driver.py`
- **Status**: done — the driver now runs `review_panel()` after the gate ladder, then `arbiter.adjudicate()` if the panel does not unanimously approve

### 2.2 Reviewer prompt graph context — DONE (commit `072241a`)
- **Where**: `src/agent_loop/loop.py` (review prompt builder)
- **Status**: done — reviewer prompt gets a smaller context slice (half budget)

### 2.3 Plan mode settled-decisions injection — DONE (commit `072241a`)
- **Where**: `src/agent_loop/plan_mode.py`
- **Status**: not wired — plan mode doesn't call `inject_settled()` to
  include prior adjudication precedents in the plan review
- **Plan ref**: §3 Phase 6, "Feedback loop" section
- **Effort**: small
- **Fix**: call `inject_settled(profile.settled, repo)` at the start of
  `run_plan()` and pass the effective settled list to the arbiter.

### 2.4 Plan mode compaction — DONE (commit `072241a`)
- **Where**: `src/agent_loop/plan_mode.py`
- **Status**: not wired — plan mode doesn't call `compact_history()`
  between rounds
- **Plan ref**: §3 Phase 4 (compaction applies to all multi-round modes)
- **Effort**: small
- **Fix**: call `compact_history(history, rnd, profile)` before each
  implementer call in `run_plan()`, same as `loop.py` does.

### 2.5 Per-role token accounting in ledger — DONE (commit `072241a`)
- **Where**: `src/agent_loop/loop.py` (ledger append)
- **Status**: partial — the ledger records `cost_usd` per ticket but not
  per-role token counts (implementer/reviewer/arbiter/compactor) as
  specified in §9.3
- **Plan ref**: §9.3 token efficiency, rule 7
- **Effort**: small
- **Fix**: record `input_tokens` and `output_tokens` per role in each
  round's `RoundRecord`, and aggregate in the ledger. The data is already
  available in the `Completion` object — it just isn't being recorded.

---

## 3. Missing modes (deferred per the plan)

### 3.1 `brainstorm` mode
- **Status**: deferred (not in phases 1-8)
- **Plan ref**: §6 Mode pipeline, "Deferred modes"
- **Effort**: medium
- **Spec**: input is a defect description, output is candidate approaches
  + trade-offs. No code changes. Exploratory — the LLM proposes multiple
  approaches, the user picks one for `plan` mode.

### 3.2 `docs` mode
- **Status**: deferred (not in phases 1-8)
- **Plan ref**: §6 Mode pipeline, "Deferred modes"
- **Effort**: medium
- **Spec**: input is a diff + graph, output is documentation updates.
  Generates or updates docs from the diff and the code knowledge graph.

---

## 4. Missing profiles (consumers must create these)

### 4.1 `nt8-riskguard` profile in tvDownloadOHLC
- **Status**: not built — the original profile lives in
  `tvDownloadOHLC/scripts/agent_loop/profiles.py`; needs to be re-created
  as a consumer of the `agent-loop` package
- **Effort**: small — copy the existing `NT8_RISKGUARD` Profile instance
  into a new `tvDownloadOHLC/scripts/agent_loop_config/nt8_riskguard.py`
  that calls `agent_loop.profiles.register()`

### 4.2 `python-tvdownloadohlc` profile
- **Status**: not built
- **Effort**: small — a Python profile for the tvDownloadOHLC repo

---

## 5. Hardening / production readiness

### 5.1 `selftest.py` runs against the new package — DONE (commit `f865ea7`)
- **Status**: done — path references fixed, test profile added, runs without crashing (2/11 pass; the rest need C# tickets)

### 5.2 `verify_backfill_reverts.py` runs against the new package — DONE (commit `f865ea7`)
- **Status**: done — no path references found; no changes needed

### 5.3 `review_mode.py` uses the generalized profile — DONE (commit `f865ea7`)
- **Status**: done — audited, no hardcoded C# patterns found

### 5.4 `populate_graph_context.py` queries live MCP
- **Where**: `scripts/populate_graph_context.py`
- **Status**: stub — writes a hardcoded cache, doesn't query
  codebase-memory-mcp
- **Effort**: medium
- **Fix**: replace the hardcoded cache with actual MCP graph queries
  (`trace_call_path`, `search_graph`) for each function in the repo

---

## Priority order

**Do next** (unblocks real usage):
1. **2.1** Developer mode panel + arbiter wiring
2. **1.4** PANEL_REJECT signal
3. **1.1** `trace_call_path` live MCP

**Do soon** (improves quality):
4. **2.2** Reviewer prompt graph context
5. **1.3** Phase 4b LLM summarization
6. **2.5** Per-role token accounting
7. **2.3** Plan mode settled-decisions injection
8. **2.4** Plan mode compaction

**Do later** (polish):
9. **1.2** Live MCP context queries (replaces cache file)
10. **5.4** `populate_graph_context.py` live MCP
11. **5.1** Selftest hardening
12. **5.2** Verify backfill reverts hardening
13. **5.3** Review mode hardening

**Deferred**:
14. **3.1** brainstorm mode
15. **3.2** docs mode
16. **4.1** nt8-riskguard consumer profile
17. **4.2** python-tvdownloadohlc consumer profile

---

*End of backlog. Update as items are completed.*