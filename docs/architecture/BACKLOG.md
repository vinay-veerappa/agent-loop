# Backlog — What's Left to Implement

**Purpose**: track every gap between the execution plan and the built code.
Each item has a priority, an effort estimate, and a reference to the plan
section or decision log entry that motivates it.

**Last updated**: after Phase 9 (learning feedback + context bloat control).

## STATUS

All 17 backlog items addressed + Phase 9 complete. 77/77 tests pass.
Tagged `v0.1.0`.

### Phase 9: Learning feedback + context bloat control

| Item | Status |
|---|---|
| Settled-decisions injection capped at 20 most recent | Done |
| Learning feedback store (save_feedback / build_learning_context) | Done |
| Path traversal fix in read_file / edit_file | Done |
| check_graph_freshness compares mtime against marker | Done |

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