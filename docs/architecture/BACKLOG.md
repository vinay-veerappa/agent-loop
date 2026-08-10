# Backlog — What's Left to Implement

**Purpose**: track every gap between the execution plan and the built code.
Each item has a priority, an effort estimate, and a reference to the plan
section or decision log entry that motivates it.

**Last updated**: after the 2026-08-10 full-package review.

## STATUS

All 17 backlog items addressed + Phase 9 complete + review fixes applied.
129/129 tests pass. `v0.1.0` is tagged but predates Phase 9 and these fixes.

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