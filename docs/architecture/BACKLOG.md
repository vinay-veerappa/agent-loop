# Backlog — What's Left to Implement

**Purpose**: track every gap between the execution plan and the built code.
Each item has a priority, an effort estimate, and a reference to the plan
section or decision log entry that motivates it.

**Last updated**: after phase 8 completion (commit `b5d6bf4`).

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

### 1.3 Phase 4b LLM summarization
- **Where**: `src/agent_loop/compaction.py:49`
- **Status**: mechanical only — uses `_mechanical_summary()`, not a
  compactor model from the registry
- **Plan ref**: §3 Phase 4b, §9.3 token efficiency rule 2
- **Effort**: small
- **Fix**: when `history_token_count > round_input_token_budget`, call the
  compactor model from `DEFAULT_REGISTRY` (the "compactor" role) to
  summarize prior rounds. Fall back to `_mechanical_summary()` when no
  compactor is registered or the call fails.

### 1.4 PANEL_REJECT signal
- **Where**: `src/agent_loop/loop.py` (arbiter call site)
- **Status**: not implemented — REJECT goes to arbiter with the same prompt
  as REVISE, no "rethink, don't tweak" distinction
- **Plan ref**: §2 Issue 5, §4 "Not states (internal signals)"
- **Effort**: small
- **Fix**: when the panel's worst verdict is REJECT, pass a flag to the
  arbiter prompt builder indicating the panel rejected the approach (not
  just the details). The arbiter's feedback to the implementer should say
  "rethink the approach" not "fix this line." The terminal state is
  whatever the arbiter recommends — PANEL_REJECT is a signal, not a state.

---

## 2. Missing wiring (components built but not connected)

### 2.1 Developer mode panel + arbiter
- **Where**: `src/agent_loop/developer/driver.py`
- **Status**: not wired — the driver runs the gate ladder (compile, test)
  but does NOT run the panel + arbiter after the edit phase. The docstring
  says it should; the code doesn't.
- **Plan ref**: §5 Developer mode spec, "Panel + arbiter: Same."
- **Effort**: medium
- **Fix**: after the edit phase completes (DONE) and the gate ladder
  passes, build a review prompt from the diff and call `review_panel()`.
  If the panel does not unanimously approve, call `arbiter.adjudicate()`.
  The review prompt for Developer mode uses the diff (not regions) — same
  as `review_mode.py` but scoped to the files the LLM edited.

### 2.2 Reviewer prompt graph context
- **Where**: `src/agent_loop/loop.py` (review prompt builder)
- **Status**: not wired — graph context is injected into the implementer
  prompt only (by design), but the reviewer prompt should also get
  callers/types context to check "will this break callers?"
- **Plan ref**: §3 Phase 3, decision log "Context injected into
  implementer prompt only, not reviewer/arbiter"
- **Effort**: medium
- **Fix**: inject a smaller context slice (callers + types only, not
  callees — the reviewer doesn't need to know what the code calls, it
  needs to know what depends on it) into `build_review_prompt()`.
  Budget: half of `context_token_budget` (default 1500 tokens).

### 2.3 Plan mode settled-decisions injection
- **Where**: `src/agent_loop/plan_mode.py`
- **Status**: not wired — plan mode doesn't call `inject_settled()` to
  include prior adjudication precedents in the plan review
- **Plan ref**: §3 Phase 6, "Feedback loop" section
- **Effort**: small
- **Fix**: call `inject_settled(profile.settled, repo)` at the start of
  `run_plan()` and pass the effective settled list to the arbiter.

### 2.4 Plan mode compaction
- **Where**: `src/agent_loop/plan_mode.py`
- **Status**: not wired — plan mode doesn't call `compact_history()`
  between rounds
- **Plan ref**: §3 Phase 4 (compaction applies to all multi-round modes)
- **Effort**: small
- **Fix**: call `compact_history(history, rnd, profile)` before each
  implementer call in `run_plan()`, same as `loop.py` does.

### 2.5 Per-role token accounting in ledger
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

### 5.1 `selftest.py` runs against the new package
- **Status**: not verified — the selftest was copied from tvDownloadOHLC
  and may reference old paths (`scripts/agent_loop/` instead of
  `src/agent_loop/`)
- **Effort**: small
- **Fix**: run `python -m agent_loop.selftest` and fix any path issues

### 5.2 `verify_backfill_reverts.py` runs against the new package
- **Status**: not verified — same as above
- **Effort**: small

### 5.3 `review_mode.py` uses the generalized profile
- **Status**: not verified — may still reference hardcoded C# patterns
  instead of the profile's `lock_name`, `risk_calls`, etc.
- **Effort**: small
- **Fix**: audit `review_mode.py` for any remaining hardcoded patterns
  and replace with profile field references

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