# Agent Loop v2 — Execution Plan

**Purpose**: turn the research in [AGENT_LOOP_RESEARCH.md](AGENT_LOOP_RESEARCH.md) into a
concrete execution plan. The current loop is documented in
[AGENT_PATCH_LOOP.md](AGENT_PATCH_LOOP.md) (829 lines, status: proven) and is not re-described
here. This doc covers: the current state machine, the issues found, the eight execution
phases, the new states, the Developer mode spec, and the mode pipeline.

**Status**: draft for review. No implementation has started.

---

## 1. Current state machine

The loop in `scripts/agent_loop/loop.py:480-665` runs this flow per round:

```
TICKET ENTRY
 +-- protected paths check (gate 0)
 |   +-- fail -> final=TICKET_REJECTED, return
 +-- baseline capture + test-first check
 |   +-- expect_green not failing -> final=TICKET_REJECTED, return
 +-- open worktree
 +-- extract regions

round start
 +-- IMPLEMENT (or resume-raw on r1)
 +-- parse blocks
 +-- GATE LADDER (cheapest first):
 |   +-- static      -- fail -> revert, feed feedback, continue
 |   +-- compile     -- fail -> revert, feed feedback, continue
 |   +-- test        -- fail -> revert, feed feedback, continue
 |   +-- lock-scope  -- fail -> revert, feed feedback, continue
 +-- PANEL (concurrent reviewers, wall-clock deadline)
 |   +-- invalid (unreachable)      -> final=PANEL_UNREACHABLE, BREAK
 |   +-- unanimous APPROVE           -> final=APPROVE, BREAK
 |   +-- else (REVISE/REJECT)        -> arbiter
 +-- ARBITER
 |   +-- ESCALATE   -> final=ESCALATED, BREAK
 |   +-- SHIP       -> final=ARBITER_SHIP, BREAK
 |   +-- REVISE     -> thrashing check
 |       +-- thrashing -> final=NOT_CONVERGING, BREAK
 |       +-- else     -> revert, feed upheld findings, continue
 +-- loop ends -> final=MAX_ROUNDS_EXHAUSTED

post-loop: if blocks valid and (APPROVE or allow_unapproved) and apply -> promote
```

### Current states (7)

| State | Meaning | Where set |
|---|---|---|
| `APPROVE` | Unanimous panel APPROVE; candidate applied in worktree | `loop.py:593` |
| `ARBITER_SHIP` | Arbiter recommends SHIP; human sign-off required | `loop.py:632` |
| `ESCALATED` | Arbiter cannot rule safely; human must decide | `loop.py:625` |
| `NOT_CONVERGING` | Thrashing detected; no overlap across 3 rounds | `loop.py:638` |
| `PANEL_UNREACHABLE` | A reviewer did not answer; panel invalid | `loop.py:587` |
| `MAX_ROUNDS_EXHAUSTED` | Loop ran all rounds without convergence | `loop.py:478` (initial) |
| `TICKET_REJECTED` | Ticket targets protected paths or bad expect_green | `loop.py:423,449` |

---

## 2. Issues found in review

Seven issues, found while reviewing T4/T5 artifacts and the loop source. None have been
fixed. All are about the loop's recorded state not matching what actually happened.

### Issue 1: Stale per-round artifacts (the T4/T5 bug)

**Symptom**: T4/T5 `result.json` says `final_verdict: MAX_ROUNDS_EXHAUSTED` + `applied: true`
+ only 1 round recorded, but `r2_*` artifacts exist on disk (including `r2_arbiter.txt` with a
SHIP verdict). A later resume run with `--max-rounds 1 --allow-unapproved --apply` overwrote
`result.json` while leaving the earlier run's `r2_*` files orphaned. The applied patch is from
r1's blocks, which the arbiter never saw, but the on-disk artifacts make it look like the
arbiter SHIP'd it.

**Root cause**: `loop.py` never cleans stale per-round artifacts at round start. `rN_impl_raw.txt`,
`rN_build.txt`, `rN_tests.txt`, `rN_review_*.txt`, `rN_arbiter.txt` from prior runs persist.

**Fix**: at the start of each round `N`, delete `r{N}_*` artifacts from any prior run. One
line in the round loop, before the implement step.

### Issue 2: Arbiter-deadlock fall-through

**Symptom**: `loop.py:602` guards with `if arbiter_model and all_findings`. If the arbiter is
unreachable (`adj.ok == False`), the loop prints a warning (`loop.py:620`) and falls through to
feeding ALL findings back — exactly the T2 failure mode the arbiter exists to prevent.

**Root cause**: no state for arbiter-unreachable. The loop silently reverts to pre-arbiter
behavior (every finding blocks).

**Fix**: add `ARBITER_DEADLOCK` state. When `adj` is not `ok`, break with
`final=ARBITER_DEADLOCK` instead of falling through.

### Issue 3: MAX_ROUNDS_EXHAUSTED is ambiguous

**Symptom**: `MAX_ROUNDS_EXHAUSTED` is the initial value of `final` (`loop.py:478`) AND the
fall-through after the loop. It covers two materially different cases: (a) every round ran
with arbiter revision each time, still not converging; (b) `--max-rounds 1` resume where the
arbiter was never consulted. A human reading the ledger cannot tell them apart.

**Fix**: split into `MAX_ROUNDS_EXHAUSTED` (arbiter was consulted at least once) and
`ARBITER_NEVER_RAN` (loop ended without the arbiter ever being consulted, e.g. `--max-rounds 1`
and panel returned REVISE).

### Issue 4: `applied: true` conflates approved and unapproved

**Symptom**: `result["applied"] = True` is set whenever `apply` and (`APPROVE` or
`allow_unapproved`). So `MAX_ROUNDS_EXHAUSTED + applied=true` is legal and means "we applied
a patch that was never approved." The ledger records this faithfully, but `cli.py:214` only
returns exit 0 on `final_verdict == "APPROVE"` — so `ARBITER_SHIP` with `--apply` returns exit 1,
which is wrong.

**Fix**: split `applied` into `applied_approved` (final was APPROVE) and
`applied_unapproved` (final was ARBITER_SHIP or MAX_ROUNDS + `--allow-unapproved`). Update
`cli.py` exit code to return 0 on both `APPROVE` and `ARBITER_SHIP` when applied.

### Issue 5: PANEL_REJECT gets the same path as REVISE

**Symptom**: if the panel's worst verdict is REJECT (not just REVISE), the loop still goes to
arbitration. But REJECT is stronger — a reviewer thinks the patch is fundamentally wrong, not
that it needs tweaks. There is no short-circuit; a REJECT gets the same REVISE feedback path
as a dissenting REVISE.

**Fix**: add `PANEL_REJECT` state. When the panel's worst verdict is REJECT, the arbiter still
runs (preserving the moat — detection separated from adjudication) but with a "rethink, don't
tweak" prompt that tells the implementer the approach is fundamentally wrong. If the arbiter
upholds a REJECT-level finding, the feedback is "rethink the approach" not "fix this line."
This keeps adjudication in the loop for the strongest negative signal rather than removing it.

### Issue 6: PANEL_UNREACHABLE discards the candidate silently

**Symptom**: on an unreachable panel, the loop breaks with the candidate still applied in the
worktree (`touched` from `loop.py:518` was never reverted). The resume hint points at
`rN_impl_raw.txt`, but the worktree has a half-applied patch.

**Fix**: before breaking on `PANEL_UNREACHABLE`, revert `touched` if any. The candidate is on
disk in `rN_impl_raw.txt`; the worktree should be clean.

### Issue 7: No quorum for partial panels

**Symptom**: `valid = all(v.counted for v in votes) and len(votes) == len(reviewers)`. A 2-of-3
panel where the two who answered both APPROVE is `valid=False` → `PANEL_UNREACHABLE` → break.
But a 2-of-3 unanimous APPROVE is different from a 0-of-3 panel.

**Fix**: add `PANEL_PARTIAL` state with quorum logic. If `>= ceil(2/3 * len(reviewers))`
answered and all are APPROVE, proceed as APPROVE. If quorum not met, break with
`PANEL_UNREACHABLE` as today.

---

## 3. The eight execution phases

**Phases are numbered in execution order.** The sequencing rationale in §8 explains why this
order. Dependencies: phase N depends on all phases before it unless noted. Phase 7 (active
tools) can be built in parallel with phases 4-6 but is not useful until phase 8.

### Phase 1: Fix the state machine

Fix all 7 issues from §2. Pure correctness, no new capability. Adds 4 new states (see §4).
Touches `loop.py`, `cli.py`.

**Exit criteria**: T4/T5 artifacts re-run produces a `result.json` that matches the on-disk
round artifacts. Arbiter-unreachable produces `ARBITER_DEADLOCK`, not silent fall-through.
`MAX_ROUNDS_EXHAUSTED` distinguishes "ran with arbiter" from "ran without".

### Phase 2: Re-index the graph

The `codebase-memory-mcp` graph is stale — it indexes `ollama_patch_loop.py` (the predecessor),
not the current `loop.py`/`arbiter.py`/`gates.py`/`review_mode.py`. Re-index the current tree.

**Graph freshness strategy**: lazy on first use. At loop start, check graph freshness (mtime
of latest indexed file vs latest `.py` in `scripts/agent_loop/`); if stale, re-index via
`codebase-memory-mcp index_repository`. Alternative: git hook on commit that touches
`scripts/agent_loop/*.py` or `scripts/ninjatrader/addons/*.cs`.

**Startup latency**: `index_repository` across 39,600+ nodes takes 15-45 seconds. Running this
inline at loop startup on stale detection could feel unresponsive. Mitigation: output clear
CLI progress feedback during re-index (`[Graph] Indexing codebase changes before ticket
start...` with a spinner or elapsed-time counter). If the latency proves unacceptable in
practice, the mtime check can run asynchronously — start the re-index in a background thread
and let the loop proceed with the stale graph for the first round (the graph is an enhancement,
not a gate; a stale graph is better than no graph and better than a 45-second startup wait).
The preferred path is synchronous-with-progress; async is the fallback.

**Exit criteria**: `trace_call_path("run_ticket")` returns callees in `loop.py`, not
`ollama_patch_loop.py`. `search_graph("adjudicate")` returns the arbiter function.

### Phase 3: Passive graph-augmented prompts (Aider-style)

Before each round, query the graph for each region's context and inject it into the
implementer, reviewer, and arbiter prompts. The LLM never calls graph tools; it receives richer
context. Matches our 0-tools pattern.

**What to inject per region**:
- Callees of the functions in the region (what does this code call?)
- Callers of the functions in the region (who depends on this code?)
- Tests that cover the region (what verifies this code?)
- Types/interfaces used in the region (what contracts does this code rely on?)

**Token budget**: high-centrality functions (e.g., `OnBarUpdate` or `OnExecution` in C# AddOns,
or core profiler handlers) have dozens of callers/callees. Passively dumping full AST
traversals could easily consume 20K+ tokens per prompt. Implement a strict token cap — max
3,000 tokens for injected graph context per prompt — using PageRank/usage frequency ranking
to truncate low-relevance callers before injection. The cap is a profile setting
(`context_token_budget`, default 3000) so it can be tuned per language/domain.

**How**: a new `context.py` module in `scripts/agent_loop/` that takes the regions and returns
a ranked context slice. Uses `codebase-memory-mcp trace_call_path`, `search_graph`,
`get_code_snippet`. These are **scaffold-side queries, not LLM-callable tools** — the loop
calls the graph MCP directly and injects the results into the implementer/reviewer/arbiter
prompts as context. The LLM never calls graph tools; it receives richer context. The slice is
added to `build_implement_prompt` and `build_review_prompt`.

**Exit criteria**: the implementer prompt for a ticket editing `OnExecution` includes the
callers of `OnExecution` and the tests that cover it, without any LLM tool calls.

**Implementation reference**: Aider's PageRank repo map (`github.com/Aider-AI/aider`,
`aider/repomap.py:365-574`) is the reference for the ranking algorithm. Key patterns: (1)
tree-sitter tags → NetworkX dependency graph → PageRank centrality; (2) identifiers
mentioned in the conversation get 10x boost, chat files 50x; (3) binary search fills the
token budget in rank order. We do not adopt Aider's tree-sitter (we already have
`codebase-memory-mcp` with its own graph); we borrow the ranking idea — weight the callees,
callers, tests, and types by their structural distance from the regions being edited. See
also [AGENT_LOOP_RESEARCH.md](AGENT_LOOP_RESEARCH.md) §6.3.

### Phase 4: Compaction (OpenCode two-phase)

Add round-level history pruning so long tickets do not crash or degrade.

**Phase 4a — prune verbose old outputs above a token threshold**: after round N, for each
prior round's implementer output, reviewer findings, and build/test logs that exceed a token
threshold (e.g., 5K tokens per artifact), replace the raw text with truncation markers that
preserve per-finding structure (reviewer name, finding severity, one-line summary, arbiter
ruling) — not just aggregate counts. For example: "[round 2: glm-5.2 APPROVE(0);
deepseek-v4-pro REVISE(4): #1 [BLOCKER] OnExecution lock-scope — UPHELD; #2 [MAJOR] race
condition — REJECTED; ...]". This matches the OpenCode pattern: prune only verbose outputs
above a threshold, preserve message structure, not all old rounds unconditionally.

**Phase 4b — LLM summarization**: if the pruned history still exceeds a token threshold
(e.g., 50K total), summarize rounds 1..N-1 via a cheaper model into a compact "what was tried
and rejected" block. Keep round N full.

**Exit criteria**: a 4-round ticket with rich reviewer output stays under 60K tokens of
history by round 4, with no loss of the latest round's detail.

**Implementation reference**: OpenWorker's `coworker/compaction.py` (in
`github.com/andrewyng/openworker`) is the most concrete compaction implementation to study
before building this. Key patterns to borrow: (1) checkpoint at each iteration top (between
tool turns and before a new turn); (2) usage signal captured per round-trip (`context_tokens`;
chars/4 estimate when never reported); (3) the summarizer runs off-loop through the normal
provider router — so the compactor model can be a cheap model from the registry (§9.2), not
the implementer or arbiter; (4) failure policy: retry once attended, auto-trim unattended;
(5) `CompactionState` persisted on the session record so reloads keep the compacted view.
Their "cap the compacted block's user-message list at 40 with an honest omitted count" is the
same idea as our per-finding truncation markers. See also the OpenCode two-phase pattern in
[AGENT_LOOP_RESEARCH.md](AGENT_LOOP_RESEARCH.md) §7.2.

### Phase 5: Persistent memory (auto-extract arbiter SETTLED)

The arbiter already nominates settled decisions in its `<<<SETTLED>>>` output. Nothing
persists them. A human reads the arbiter response and copies decisions into `profiles.py` by
hand.

**What to do**: after every arbiter response, parse the `SETTLED` section and write each
decision to a settled-decisions store (a JSONL file at
`logs/agent_loop/settled_decisions.jsonl`, keyed by ticket + hash of decision text to avoid
duplicates from minor wording changes). At the start of each ticket, load all settled
decisions and inject them into `profile.settled` alongside the hand-curated ones. The
hand-curated ones take precedence (they are reviewed); the auto-extracted ones are advisory
and marked as such.

**Concurrency**: if multiple agent loops run concurrently (or during automated batch ticket
testing), appending to `settled_decisions.jsonl` simultaneously could corrupt JSON lines. Use
file-locking (`portalocker` or atomic filesystem appends) when writing to the JSONL file.
Reads do not need locking (append-only JSONL is safe to read while another process appends).

**Exit criteria**: after running a ticket that produces arbiter SETTLED decisions, the next
ticket's review panel sees those decisions in its `settled` list without any human action.

**Implementation reference**: OpenWorker's `coworker/memory/` (in
`github.com/andrewyng/openworker`) is the memory V1 pattern to study. Key patterns to borrow:
(1) SQLite-backed with a summary column (in-place migration, same defensive parse as their
other grants); (2) knowledge is session-stable, the save switch is per-message; (3) "standing
instructions ride along" — their standing instructions map directly to our settled-decisions
injection into `profile.settled`. The Codex CLI background-extraction pipeline (two-phase:
extract from rollouts, then consolidate via sub-agent) is the more sophisticated reference for
later; our scoped version (extract `<<<SETTLED>>>` only) is simpler. See also
[AGENT_LOOP_RESEARCH.md](AGENT_LOOP_RESEARCH.md) §7.3.

### Phase 6: Plan + Test modes

Add two new modes that close the test-first loop. These are the priority per the user's
decision; `brainstorm` and `docs` modes are deferred to a later phase.

**`plan` mode**: input is a defect description. Output is a ticket JSON with regions
(file + lines + purpose) and acceptance test names (`expect_green`). The LLM uses the graph
(passive injection, phase 3) to localize the defect and propose regions. No code changes.
The output is reviewed by the panel + arbiter for completeness (did it find the right regions?
did it name the right tests?) before being promoted to a ticket file. This panel+arbiter
review of `plan` output is a new requirement specific to this loop — the research doc §11 has
been updated to reflect it. **Feedback loop**: if the panel+arbiter rejects the plan (REVISE
verdict), the plan agent revises and resubmits — same round loop as `patch` mode, but the
"blocks" are ticket JSON fields, not code regions. If the arbiter ESCALATES, the pipeline
halts for human review. Max rounds is the same as `patch` mode (default 4).

**Latency and cost**: running a full multi-model panel + arbiter cycle to verify a plan
ticket JSON adds ~2 minutes of latency and ~$0.30-$1.00 of API cost per planning iteration.
For rapid prototyping of defect tickets, support a `--fast-plan` flag that uses a single
reviewer model (from the registry §9.2) instead of the full panel + arbiter. The `--fast-plan`
path skips the arbiter entirely — a single APPROVE from the designated reviewer promotes the
plan. This trades verification depth for speed; the full panel+arbiter path is the default for
plans that will drive real patches.

**`test` mode**: input is a defect description + a ticket JSON (from `plan` mode). The ticket
JSON provides the regions and acceptance test names — `test` mode needs to know what to test
and where the code under test lives. Output is failing acceptance tests written to a test file.
The LLM uses the graph to find the test patterns and the code under test. The tests must fail
at baseline (the loop's test-first check, `loop.py:442-457`, already enforces this).

**Exit criteria**: `plan -> test` pipeline works end to end. A defect goes in; `plan` produces
a ticket JSON with regions and acceptance test names (reviewed by panel+arbiter); `test`
produces failing acceptance tests. (`patch` mode already exists and is not part of phase 6's
exit criteria — the full `plan -> test -> patch` pipeline is validated when `patch` consumes
the ticket JSON and makes the tests pass, but that is testing `patch`, not building it.)

### Phase 7: Active graph tools (for Developer mode)

Give the LLM graph-traversal tools for Developer mode. This is the Prometheus pattern, scoped
to our minimal tool set. See §5 for the Developer mode spec. Can be built in parallel with
phases 4-6 but is not useful until phase 8.

**Exit criteria**: Developer mode can localize a defect by querying the graph
(`search_code`, `trace_call_path`) without any pre-declared regions.

**Implementation reference**: Prometheus's Neo4j graph-traversal tools
(`github.com/ML4CODE/prometheus`, `graph_traversal.py:93-586`) is the reference for building
LLM-callable graph tools. Key patterns: (1) 11 tools (10 graph traversal + `read_file`)
query `FileNode`, `ASTNode`, `TextNode` entities; (2) tool names are structure-aware
(`search_class`, `search_method`) not text-matching; (3) the graph is built from tree-sitter
ASTs covering 20 languages. We do NOT adopt Prometheus's Neo4j (we use `codebase-memory-mcp`);
we borrow the tool naming and the `search_class`/`search_method` pattern for our
`search_code` tool. The Moatless Tools embedding-based retrieval (`code_index.py:57`, FAISS
via LlamaIndex) is the alternative if graph traversal proves insufficient — see
[AGENT_LOOP_RESEARCH.md](AGENT_LOOP_RESEARCH.md) §6.5.

### Phase 8: Developer mode

The autonomous localization+edit path. This is phase 8 and depends on phases 1, 2, 3, 4, 5,
and 7 (state machine honest, graph fresh, passive retrieval wired, compaction for long sessions,
memory for settled decisions, and active tools available). See §5 for the full spec.

**Exit criteria**: a defect description goes in; the LLM localizes via graph, edits via
tools, gates pass, panel approves (or arbiter SHIPs); a patched diff comes out. No human
localization.

**Implementation references**:
- **SWE-agent ACI** (`github.com/SWE-agent/SWE-agent`, `docs/background/aci.md`) — the
  agent-computer interface design principles: linter on edit, 100-line file viewer windows,
  custom search that lists match files succinctly, "tools for agents not humans." Our
  Developer mode tool set (§5) follows these. Read this before building the tools.
- **OpenWorker `coworker/permissions.py`** (`github.com/andrewyng/openworker`) — the safety
  hardening to study before building the tool gating. Key patterns: (1) argv-aware matching
  that rejects any command containing shell operators (`; & | > < backtick $ (` and newlines)
  before consulting the allowlist; (2) dropped language interpreters (python, node, npm)
  from the allowlist because allowlisting an interpreter allowlists arbitrary code
  (`python3 -c "..."`). Our Developer mode excludes `run_command` entirely; this reference is
  for validating that `run_build` and `run_tests` cannot be chained into arbitrary execution.
- **AutoCodeRover** (`github.com/AutoCodeRover/AutoCodeRover`) — the phased tool-scoping
  pattern: search agent has 8 read-only tools, no edit/execute; patch agent has no search
  tools. Our Developer mode adopt this: explore phase is read-only, edit phase has no search.
- **aisuite Agents API** (`github.com/andrewyng/aisuite`) — the `max_turns` tool-calling loop
  is the reference for the LLM-driven tool loop in Developer mode. We do NOT adopt the full
  Agents API (our loop is deliberately minimal); but reading how they handle tool-call
  parsing, `intermediate_messages` history, and `RequireApprovalPolicy` is worth doing before
  building the Developer mode loop.

---

## 4. New states (after phase 1)

Three new terminal states added to the state machine. `PANEL_REJECT` is an internal signal
(not a terminal state) that modifies the arbiter's prompt. `PANEL_PARTIAL` is metadata, not a
verdict. Stale-artifact purging is a round-start action. All three are described in the flow
diagram below but only the three terminal states appear in the state table.

| State | Meaning | When | Replaces |
|---|---|---|---|
| `ARBITER_DEADLOCK` | Arbiter unreachable; cannot adjudicate | `adj.ok == False` | Silent fall-through to all-findings-block |
| `ARBITER_NEVER_RAN` | Loop ended without arbiter being consulted | `--max-rounds 1` + panel REVISE | Part of `MAX_ROUNDS_EXHAUSTED` |
| `PANEL_PARTIAL` | Some reviewers answered, quorum met, all APPROVE — recorded as metadata, final is still `APPROVE` | `>= ceil(2/3 * len(reviewers))` answered | `PANEL_UNREACHABLE` for quorum-met cases |

**Not states (internal signals and actions)**:
- `PANEL_REJECT` — internal signal: when the panel's worst verdict is REJECT, the arbiter
  runs with a "rethink, don't tweak" prompt. The loop does not break; the arbiter still
  adjudicates. If the arbiter upholds a REJECT-level finding, the feedback tells the
  implementer to rethink the approach. The terminal state is whatever the arbiter recommends
  (REVISE, ESCALATE, SHIP), not PANEL_REJECT itself.
- Stale-artifact purge — round-start action, not a state.

### Updated state flow (after phase 1)

```
round start
 +-- ACTION: purge stale r{N}_* artifacts from prior runs (housekeeping, not a state)
 +-- IMPLEMENT (or resume-raw on r1)
 +-- parse blocks
 +-- GATE LADDER (same 5 rungs, fail -> revert, feed feedback, continue)
 +-- PANEL
 |   +-- unreachable, no quorum  -> final=PANEL_UNREACHABLE, revert touched, BREAK
 |   +-- quorum met, all APPROVE -> final=APPROVE (record PANEL_PARTIAL in metadata), BREAK
 |   +-- worst=REJECT            -> arbiter (with "rethink, don't tweak" prompt signal)
 |   +-- else (REVISE)           -> arbiter
 +-- ARBITER
 |   +-- unreachable (ok=False)  -> final=ARBITER_DEADLOCK, revert touched, BREAK
 |   +-- ESCALATE                -> final=ESCALATED, BREAK
 |   +-- SHIP                    -> final=ARBITER_SHIP, BREAK
 |   +-- REVISE                  -> thrashing check
 |       +-- thrashing           -> final=NOT_CONVERGING, BREAK
 |       +-- else                -> revert, feed upheld findings, continue
 +-- loop ends
     +-- arbiter consulted?      -> final=MAX_ROUNDS_EXHAUSTED
     +-- arbiter never consulted? -> final=ARBITER_NEVER_RAN

post-loop: if blocks valid and (APPROVE or allow_unapproved) and apply -> promote
           applied_approved = (final == APPROVE)
           applied_unapproved = (final == ARBITER_SHIP or ...) and allow_unapproved
```

**Note on PANEL_REJECT**: the arbiter still runs on a REJECT verdict, but with a "rethink,
don't tweak" prompt that tells the implementer the approach is fundamentally wrong, not that
the implementation needs tweaks. This preserves the moat (detection separated from
adjudication) while giving the arbiter the context that the panel rejected the approach, not
just the details. PANEL_REJECT is a signal that modifies the arbiter prompt; it is not a
terminal state. The terminal state is whatever the arbiter recommends.

---

## 5. Developer mode spec

The autonomous localization+edit path. This is phase 8 and depends on phases 1-4 (state machine
honest, graph fresh, passive retrieval wired, active tools available).

### Input

A defect description (same as `brainstorm` or `plan` mode input). No pre-declared regions.

### Tool set (minimal, per §10.4 of the research doc)

| Tool | Signature | Notes |
|---|---|---|
| `read_file` | `(path, start_line?, end_line?) -> str` | 100-line window default; scroll via start_line |
| `search_code` | `(pattern, file_pattern?, mode?) -> results` | Graph-augmented grep from codebase-memory-mcp; ranks by structural importance |
| `trace_call_path` | `(function_name, direction, depth?) -> callers/callees` | Graph traversal; answers "will this break callers?" |
| `edit_file` | `(path, old_str, new_str) -> result` | str_replace exact match; linter-on-edit (gate 1 runs before return) |
| `run_build` | `() -> result` | Profile's build_cmd; gate 2 |
| `run_tests` | `() -> result` | Profile's test_cmd; gate 3 |

**Deliberately excluded**: `run_command` (arbitrary shell), `browser`, `git` operations. This
repo has an AddOn that moves real money; tool scope matters.

### Control flow

```
defect -> explore phase (read-only: read_file, search_code, trace_call_path)
       -> edit phase (edit_file, run_build, run_tests in a generate-test-repair loop)
       -> gate ladder (same 5 rungs)
       -> panel (same)
       -> arbiter (same)
```

The explore phase is read-only (AutoCodeRover pattern: search agent cannot edit). The edit
phase has no search tools (AutoCodeRover pattern: patch agent cannot search). This phase
separation prevents the LLM from editing before it understands. The implementer's first turn
in the edit phase must include a brief plan (which files, which functions, what changes) in
its notes — this is not a separate phase with its own gates, just a structural requirement in
the edit-phase prompt so the LLM commits to an approach before generating edits.

### Gate ladder

Same 5 rungs: protected → static → compile → test → lock-scope. The static gate and
lock-scope gate both need adaptation for Developer mode.

**Static gate** (gate 1): in patch mode it checks that returned blocks match declared
regions (brace balance, ASCII, indentation, #if/#endif balance, no leaked markers). In
Developer mode there are no declared regions — the LLM edits files directly via `edit_file`.
The static gate runs on each `edit_file` call's result (the edited file content), checking:
- **ASCII only** in string literals and comments (same as patch mode)
- **Balanced braces** (same as patch mode)
- **#if/#endif balance** (same as patch mode)
- **No leaked `<<<BLOCK` markers** in the file (the LLM should not emit block markers in
  Developer mode)
- **Indentation preserved** relative to the edit's anchor line (the `old_str` match point)

These are the same checks as patch mode's gate 1, applied per-edit instead of per-block. The
compile and test gates are unchanged (they run on the worktree, not on blocks).

**Lock-scope gate** (gate 5) becomes a **File-Level Scope Gate** in Developer mode. In patch
mode, gate 5 scans for broker calls reachable inside `lock(_stateLock)` — a line-level check
bounded by the declared regions. In Developer mode, line regions are dynamic (the LLM
chooses what to edit), so the line-level lock-scope check is replaced by a file-level scope
check: the LLM may only edit files within the profile's `file_scope_whitelist` (a new profile
field, defaulting to the directories the profile governs, e.g., `scripts/ninjatrader/addons/`
for `nt8-riskguard`, `scripts/agent_loop/` for a python-loop profile). Edits to files outside
the whitelist are rejected by the gate before the compile gate runs. The protected-paths
check (gate 0) still applies — `*Tests.cs`, `*.csproj`, etc. remain unreachable. If the
profile has a `lock_name` (C#), the line-level lock-scope scan also runs on the edited file's
content (not on declared regions); if the profile has no `lock_name` (Python), this check is
skipped entirely.

### Panel + arbiter

Same. The panel reviews the diff (not regions). The arbiter rules on findings. The settled-
decisions cache applies. This is the moat and it is unchanged.

### Output

A patched diff in the worktree, exported via `ws.export_patch()`. Same promotion path as
patch mode: `ARBITER_SHIP` → human review → `--apply`.

---

## 6. Mode pipeline

```
                    +----------+
                    | brainstorm|  (new, DEFERRED - optional precursor, not in phases 1-8)
                    +----+-----+
                         | (optional)
                         v
                    +----------+
         defect -->|   plan   |  (new, phase 6) --> ticket JSON
                    +----+-----+
                         |
                         v
  defect + ticket -->+----------+
                    |   test   |  (new, phase 6) --> failing acceptance tests
                    +----+-----+
                         |
                         v
              +---------------+---------------+
              |                               |
              v                               v
        +----------+                    +----------+
        |  patch   |                    | developer|  (new, phase 8)
        | (current)|                    | (auto)   |
        +----+-----+                    +----+-----+
             |                               |
             +---------------+---------------+
                             |
                             v
                        +----------+
                        |  review  |  (current, review_mode.py)
                        +----+-----+
                             |
                             v
                        +----------+
                        |   docs   |  (new, DEFERRED - not in phases 1-8)
                        +----------+
```

- `plan` takes a defect description, produces a ticket JSON (regions + acceptance tests).
- `test` takes a defect + the ticket JSON from `plan`, produces failing acceptance tests.
- `developer` takes a defect, localizes and edits autonomously (phase 8).
- `patch` takes a ticket JSON (from `plan` or hand-written), produces patched code.
- `review` and `docs` apply to either path after the patch lands.

**Deferred modes**: `brainstorm` and `docs` are not in phases 1-8. `brainstorm` is an optional
precursor to `plan` (exploratory, no code changes). Both are future work after the core
pipeline (plan -> test -> patch/developer) is proven.

### Priority (from user decision)

**Plan + Test first** (phase 6). These close the test-first loop. Review mode already exists.
Brainstorm and docs are deferred. Developer mode is phase 8 (depends on everything before it).

---

## 7. Graph freshness strategy

The `codebase-memory-mcp` graph is stale right now. Two options:

| Strategy | Pros | Cons |
|---|---|---|
| **Lazy on first use** (recommended default) | No automation, no hook to maintain; re-indexes only when the loop runs and only if stale | Adds startup latency on first run after changes |
| **Git hook on commit** | Always fresh; no startup latency | Hook maintenance; re-indexes on every commit that touches .py, even if the loop is not run |

**Recommended**: lazy on first use. At loop start, check `index_status` for the project; if
the latest indexed file mtime is older than the latest `.py` in `scripts/agent_loop/` or
`scripts/ninjatrader/addons/`, call `index_repository` in fast mode before proceeding. This
keeps the graph fresh without any external automation. A git hook is a recommended
alternative for teams that want zero startup latency.

---

## 8. Sequencing rationale

Why this order (phase numbers match §3):

1. **Phase 1 (state machine) first** — every subsequent phase depends on the loop's recorded
   state being honest. If `result.json` can lie (T4/T5 bug), we cannot measure whether later
   phases help.

2. **Phase 2 (re-index) second** — phases 3 and 7 depend on the graph being fresh. Re-indexing
   is cheap and unblocks everything downstream.

3. **Phase 3 (passive retrieval) third** — cheapest graph win. No new tools, no new modes, just
   richer prompts. Immediate benefit to every existing ticket.

4. **Phase 4 (compaction) fourth** — before adding modes that produce more history, fix the
   one that can crash. Long tickets are the failure mode today.

5. **Phase 5 (memory) fifth** — the arbiter already produces SETTLED decisions; persisting
   them is a small change with compounding value across tickets.

6. **Phase 6 (Plan + Test) sixth** — closes the test-first loop. Depends on passive retrieval
   (phase 3) for localization in `plan` mode. Panel+arbiter review of `plan` output reuses
   the existing verification stack.

7. **Phase 7 (active tools) seventh** — only needed for Developer mode. Can be built in
   parallel with phases 4-6 but is not useful until phase 8.

8. **Phase 8 (Developer mode) last** — depends on everything: honest state machine (phase 1),
   fresh graph (phase 2), passive retrieval (phase 3), compaction (phase 4) for long
   autonomous sessions, memory (phase 5) for settled decisions across developer runs, and
   active tools (phase 7). Building it before the foundations would reproduce the field's
   failure modes on our moat.

---

## 9. Language agnosticism and model-by-capability

Two design principles that cut across all phases. Both are currently violated; both must be
fixed before the loop can serve any codebase other than the NT8 AddOn or any model mix other
than the hand-picked defaults.

### 9.1 Language agnosticism — the loop must not know what language it is patching

**Current state**: the loop is hardcoded to C# / NinjaTrader in five places:

| Where | What is language-specific | Should be |
|---|---|---|
| `profiles.py` `NT8_RISKGUARD` | `implementer_rules` mentions "C# 8.0", ".NET Framework 4.8", "_stateLock", "NinjaTrader 8 AddOn", "Account.Flatten/Cancel/Submit/CreateOrder" | All of this belongs in the profile, which is correct — but the profile is the ONLY one that exists |
| `profiles.py` `SUPPORTED_SUFFIXES` | `regions.py` only supports `.cs` files (`SUPPORTED_SUFFIXES = (".cs",)`) | Must be per-profile; a Python profile supports `.py`, a TS profile supports `.ts` |
| `gates.py` `check_lock_scope` | Scans for `lock(_stateLock)` and broker calls (`Flatten/Cancel/Submit/CreateOrder`) — C#-specific syntax and domain-specific calls | The gate itself is generic; the patterns must come from the profile (`lock_pattern`, `risk_calls`) |
| `gates.py` `check_static` | Checks `#if`/`#endif` balance — a C# preprocessor directive | Must be conditional on the profile's `preprocessor_directives` setting (C# has `#if/#endif`; Python has none; Go has `//go:build`) |
| `profiles.py` `protected` | `*Tests.cs`, `*.csproj` — C# test file patterns | Must be per-profile (`*Tests.py`, `*Test.ts`, etc.) |

**Principle**: the loop driver (`loop.py`), the gates (`gates.py`), the regions extractor
(`regions.py`), and the arbiter (`arbiter.py`) must contain zero language-specific strings.
Everything language-specific lives in a `Profile` and is injected at call time.

**What a language-agnostic Profile looks like**:

```python
@dataclass
class Profile:
    name: str
    # Language
    language: str                          # "csharp", "python", "typescript"
    file_suffixes: tuple                    # (".cs",) or (".py",) or (".ts", ".tsx")
    preprocessor_directives: tuple = ()     # ("#if", "#endif") for C#; () for Python
    block_comment: tuple = ("/*", "*/")    # for strip_code; ("#",) for Python
    # Build and test
    build_cmd: str = ""
    test_cmd: str = ""
    test_runner_regex: tuple               # (fail_line_re, results_re) — language-specific
    # Lock-scope gate (optional; only for languages with a lock primitive)
    lock_name: str = ""                    # "_stateLock" for NT8; "" for Python (gate skipped)
    lock_pattern: str = ""                # "lock\\s*\\(\\s*{lock_name}\\s*\\)" — compiled per profile
    risk_calls: tuple = ()                 # (".Flatten", ".Cancel", ...) for NT8; () for Python
    # File-level scope gate (Developer mode; see §5 Gate ladder)
    file_scope_whitelist: tuple = ()       # ("scripts/ninjatrader/addons/",) for nt8; Developer mode rejects edits outside
    # Protected paths and test sources
    protected: tuple = ()
    test_sources: tuple = ()
    # Context injection budget (Phase 3 passive retrieval)
    context_token_budget: int = 3000       # max tokens injected per prompt; tunable per language/domain
    # Per-round input budget (Phase 9.3 token efficiency)
    round_input_token_budget: int = 40000  # if prompt exceeds this, compaction runs before the call
    # Prompts and settled decisions
    implementer_rules: str
    reviewer_priorities: str
    settled: tuple = ()
```

**New profiles to add** (not all in one phase; added as modes and modes demand them):

| Profile name | Language | Build | Test | Lock-scope gate |
|---|---|---|---|---|
| `nt8-riskguard` (existing) | C# / NinjaTrader | `dotnet build` | `dotnet run` | yes (`_stateLock`) |
| `python-tvdownloadohlc` | Python | `python -m compileall` or ruff | `pytest` | no (Python has no lock primitive) |
| `typescript-web` | TypeScript | `tsc --noEmit` | `npm test` | no |
| `go-service` | Go | `go build` | `go test` | no |

**Phase placement**: this refactor is part of phase 1 (state machine fixes) because it touches
`gates.py`, `regions.py`, and `profiles.py` — the same files phase 1 already modifies. It is
not a separate phase; it is a constraint on how phase 1 is implemented.

### 9.2 Model-by-capability — match models to roles by capability and cost

**Current state**: models are configured as CLI flags with hardcoded defaults:

```python
--implementer  default="kimi-k2.7-code:cloud"   # strong coder, slow
--reviewers    default="glm-5.2:cloud,deepseek-v4-pro:cloud"  # two different families
--arbiter      default="glm-5.2:cloud"          # same as a reviewer (not ideal)
```

**Problems**:
1. The arbiter defaults to the same model as one of the reviewers (`glm-5.2:cloud`). The
   research doc §8.6 and the `cli.py:117` help text both say the arbiter "wants to be stronger
   than the panel and from a different family" — but the default violates this.
2. There is no per-role cost/capability specification. The user must know which model is good at
   which role and manually pass the right names. A wrong default wastes money (strong model on
   a cheap task) or quality (weak model on a hard task).
3. The `plan`, `test`, and `developer` modes will need different model profiles:
   - `plan` mode needs a strong reasoner (localization is hard)
   - `test` mode needs a strong coder (tests must compile and fail)
   - `developer` mode explore phase needs a strong reasoner; edit phase needs a strong coder
   - `compaction` (phase 4) should use a cheap model (the research doc §7.2 says "a cheaper
     compaction agent")
4. There is no concept of a "model registry" — a declarative mapping from role to model, with
   cost and capability metadata, so the loop can pick the right model per role without the user
   memorizing model names.

**Principle**: the loop should have a **model registry** that maps each role to a model based
on capability and cost. The user can override per-role, but the defaults are sensible.

**What a model registry looks like**:

```python
@dataclass
class ModelConfig:
    name: str                # "kimi-k2.7-code:cloud"
    role: str                # "implementer", "reviewer", "arbiter", "compactor", "planner", "explorer", "tester"
    capability: str          # "strong-coder", "strong-reasoner", "cheap", "fast"
    cost_per_1m_out: float   # USD; 0.0 for subscription models
    think: bool = False      # whether chain-of-thought is on by default for this role
    max_tokens: int = 24000 # default output budget for this role

MODEL_REGISTRY = {
    "implementer": ModelConfig("kimi-k2.7-code:cloud", "implementer", "strong-coder", 0.0, think=True, max_tokens=48000),
    "reviewer":    ModelConfig("glm-5.2:cloud", "reviewer", "fast", 0.0, think=False, max_tokens=24000),
    "arbiter":     ModelConfig("deepseek-v4-pro:cloud", "arbiter", "strong-reasoner", 0.0, think=False, max_tokens=24000),
    "compactor":   ModelConfig("glm-5.2:cloud", "compactor", "cheap", 0.0, think=False, max_tokens=8000),
    # Phase 6+ roles:
    "planner":     ModelConfig("deepseek-v4-pro:cloud", "planner", "strong-reasoner", 0.0, think=False, max_tokens=24000),
    "tester":      ModelConfig("kimi-k2.7-code:cloud", "tester", "strong-coder", 0.0, think=True, max_tokens=48000),
    # Phase 8 roles:
    "explorer":    ModelConfig("deepseek-v4-pro:cloud", "explorer", "strong-reasoner", 0.0, think=False, max_tokens=24000),
}
```

**Design rules** (enforced by the registry, not by instruction):
1. **The arbiter must not be the same model as any reviewer.** The registry rejects an arbiter
   that matches a reviewer in the same run. Different families catch different things; a
   shared family means the arbiter inherits the reviewer's blind spots.
2. **The compactor uses a cheap model, never the implementer or arbiter.** Compaction is
   mechanical summarization, not reasoning; spending arbiter-class tokens on it is waste.
3. **Roles can override per-ticket via CLI, but the registry validates the override.** A user
   can pass `--arbiter claude-opus-5` for a hard ticket; the registry accepts it (Opus is
   strong-reasoner, different family from the cloud reviewers). It rejects
   `--arbiter glm-5.2:cloud` if `glm-5.2:cloud` is already a reviewer.
4. **Capability and cost are visible in the ledger.** Every round records which model served
   which role, the token cost, and the capability tier, so the cost/quality tradeoff is
   auditable per ticket.

**Phase placement**: the model registry is part of phase 1 (it touches `cli.py` and
`providers.py`, which phase 1 already modifies). The registry replaces the hardcoded defaults
in `cli.py` and adds the validation rule (arbiter != reviewer). The per-mode roles (`planner`,
`tester`, `explorer`, `compactor`) are added as their phases land.

### 9.3 Token efficiency — minimize token usage as a first-class goal

**Why this is a principle, not a feature**: token usage is the dominant cost driver and the
dominant latency driver. A ticket that takes 4 rounds with 48K-token implementer outputs, 24K-
token reviewer findings, and 24K-token arbiter rulings consumes ~400K tokens per round — and
that is the *cheap* subscription path. On paid models (Anthropic, OpenAI) the same ticket can
cost $5-15. Token efficiency is not an optimization; it is a constraint that shapes every
phase.

**Current state**: the loop has no token budget enforcement. The implementer gets
`max_tokens=48000` per call; reviewers get 24000; the arbiter gets 24000. The implementer
prompt includes every prior round's raw output, every reviewer's findings, and every arbiter
ruling — unbounded growth across rounds. A 4-round ticket can exceed 400K tokens of history by
round 4. There is no per-round token budget, no history-trimming before the prompt is built,
no feedback to the implementer that its output is too large.

**Token-efficiency rules** (enforced by the loop, not by instruction):

1. **Per-round input budget.** The implementer prompt is capped at a token budget (default
   40K input, configurable via the profile's `round_input_token_budget`). If the prompt exceeds
   the budget, compaction (phase 4) runs *before* the implementer call, not after. The
   implementer never sees a 400K-token prompt.
2. **Per-role output budget enforcement.** The `max_tokens` in the `ModelConfig` (§9.2) is a
   hard cap, not a suggestion. A reviewer that returns 24K tokens of findings when 8K would
   do is wasting tokens; the reviewer system prompt asks for concise findings, and the
   `max_tokens` cap enforces it. The arbiter gets a tighter cap (16K) than the implementer
   (48K) because the arbiter's output is structured rulings, not reasoning.
3. **Differential truncation.** Not all history is equally valuable. The latest round's full
   exchange is always kept in full. Prior rounds are pruned (phase 4a) — verbose outputs above
   a threshold become truncation markers that preserve per-finding structure (reviewer name,
   severity, one-line summary, arbiter ruling) without the bulk. This is already in phase 4;
   the principle here is that it applies *before* every implementer/reviewer/arbiter call, not
   just at compaction time.
4. **Settled decisions are summarized, not repeated.** The `profile.settled` list (which can
   grow to 20+ entries) is injected into every review prompt as a compact bulleted list, not
   as the full text of each decision. A settled decision that is 3 sentences becomes 1 line.
   The full text lives in the settled-decisions store (phase 5); the prompt gets the summary.
5. **Graph context is capped (phase 3, already specified).** The `context_token_budget`
   (default 3000, §9.1) caps injected graph context per prompt. High-centrality functions with
   dozens of callers get ranked and truncated, not dumped.
6. **`--fast-plan` for plan mode (phase 6, already specified).** A single reviewer instead of
   a full panel + arbiter cuts plan verification from ~2 minutes + ~$0.30-1.00 to seconds +
   cents. Use for rapid prototyping; the full path is the default for real patches.
7. **Token accounting in the ledger.** Every round records per-role input tokens, output
   tokens, cache-read tokens, and cost. The ledger already records `cost_usd` per round; the
   enhancement is per-role granularity (implementer/reviewer/arbiter/compactor) so the cost
   driver is visible, not just the total. A ticket that spent 60% of its tokens on the
   implementer's chain-of-thought is a ticket where `think=False` or a smaller `max_tokens`
   would help.

**What this means for each phase**:

| Phase | Token efficiency impact |
|---|---|
| 1 (state machine) | Per-role token accounting in the ledger; per-role `max_tokens` in the registry |
| 3 (passive retrieval) | 3K-token cap on injected graph context (already specified) |
| 4 (compaction) | Prune verbose outputs above threshold before every call; LLM summarization via cheap model when history exceeds 50K |
| 5 (memory) | Settled decisions injected as 1-line summaries, not full text |
| 6 (plan+test) | `--fast-plan` flag for single-reviewer verification |
| 8 (Developer mode) | Explore phase is read-only (no token waste on premature edits); edit phase has a prompt-level plan requirement before edits (commits to approach, not 48K tokens of exploratory edits) |

**The goal**: a 4-round ticket on a hard defect should cost under 200K total tokens (input +
output across all roles), down from the current unbounded ~400K+. The compaction (phase 4)
and the per-round input budget are the two biggest levers; the token accounting in the ledger
makes the cost visible so we can see whether we hit the goal.

### 9.4 What this unlocks

- **Any language**: a `Profile` is a config file, not a fork. Adding Python or TypeScript
  support is a new profile, not a rewrite of `gates.py` or `regions.py`.
- **Any model mix**: the registry lets us swap a single model (e.g., when a new model drops)
  by changing one line, not by hunting through CLI defaults.
- **Cost visibility**: the ledger records per-role cost, so we can see whether the arbiter is
  worth its tokens or the compactor is overspending.
- **Capability matching**: a hard ticket can pass `--implementer claude-opus-5 --arbiter
  deepseek-v4-pro:cloud` knowing the registry validates the mix; a cheap ticket can use the
  defaults knowing they are sensible.
- **Token efficiency**: the per-round input budget and per-role output caps keep a 4-round
  ticket under 200K tokens, making the loop viable on paid models and faster on free ones.

---

## 10. Implementation references by phase

Each phase above has an inline "Implementation reference" section. This is the consolidated
index so nothing is lost. Read the referenced source before building the phase; the patterns
are worth borrowing, the code is not worth importing.

| Phase | Reference | What to study |
|---|---|---|
| 3 (passive retrieval) | Aider `aider/repomap.py:365-574` (`github.com/Aider-AI/aider`) | PageRank repo map: tree-sitter tags → NetworkX dependency graph → centrality ranking; conversation identifier 10x boost; binary search fills token budget |
| 4 (compaction) | OpenWorker `coworker/compaction.py` (`github.com/andrewyng/openworker`) | Off-loop summarizer via provider router; checkpoint at iteration top; usage signal per round-trip; failure policy (retry attended, auto-trim unattended); CompactionState persisted |
| 4 (compaction) | OpenCode two-phase surgical (`github.com/sst/opencode`) | Prune verbose old tool outputs >40k tokens first (preserve structure, truncation markers), then LLM summarization via cheaper compaction agent |
| 5 (memory) | OpenWorker `coworker/memory/` (`github.com/andrewyng/openworker`) | SQLite-backed with summary column; session-stable; per-message save switch; "standing instructions ride along" maps to settled-decisions injection |
| 5 (memory) | Codex CLI background extraction (`github.com/openai/codex`) | Two-phase: extract from rollouts (per-rollout model), consolidate via sub-agent; usage-ranked, stale pruned. The more sophisticated reference for later |
| 7 (active tools) | Prometheus `graph_traversal.py:93-586` (`github.com/ML4CODE/prometheus`) | 11 graph-traversal tools over Neo4j AST graph (20 languages); structure-aware tool names (`search_class`, `search_method`); we borrow naming, not Neo4j |
| 7 (active tools) | Moatless Tools `code_index.py:57` (`github.com/moatless-tools/moatless-tools`) | FAISS via LlamaIndex; the only embedding-based LLM-callable retrieval in the field. Alternative if graph traversal proves insufficient |
| 8 (Developer mode) | SWE-agent ACI (`github.com/SWE-agent/SWE-agent`, `docs/background/aci.md`) | Linter on edit; 100-line file viewer; custom search (succinct match list); "tools for agents not humans" |
| 8 (Developer mode) | OpenWorker `coworker/permissions.py` (`github.com/andrewyng/openworker`) | Argv-aware shell allowlist: reject operators (`; & | > < backtick $ (`); drop interpreters (python, node, npm) from allowlist. Validates that `run_build`/`run_tests` cannot be chained into arbitrary execution |
| 8 (Developer mode) | AutoCodeRover (`github.com/AutoCodeRover/AutoCodeRover`) | Phased tool-scoping: search agent has 8 read-only tools, no edit/execute; patch agent has no search tools |
| 8 (Developer mode) | aisuite Agents API (`github.com/andrewyng/aisuite`) | `max_turns` tool-calling loop; `intermediate_messages` history; `RequireApprovalPolicy`. We do NOT adopt the full API; we read it before building our minimal loop |
| 9.2 (model registry) | Codex CLI Guardian (`github.com/openai/codex`, `guardian.rs`) | Separate LLM (`gpt-5.4`) evaluates each tool call's risk on 0–100 scale, blocks >80. The only multi-model routing for safety. Reference for our arbiter-model-validation rule |

**What we do NOT adopt** (so future-us does not re-evaluate):
- **aisuite Chat Completions API** — our `providers.py` is zero-dependency and covers the same
  providers. Replacing it adds a dependency tree for no marginal benefit.
- **aisuite toolkits (files, git, shell)** — general-purpose; our Developer mode tool set is
  deliberately minimal. Their shell toolkit is exactly what we exclude.
- **OpenWorker `connectors/`** — Slack, Jira, Notion. Not our domain.
- **OpenWorker `inbox.py` / approval gating** — desktop-app approval flow. Our approval is
  `--apply`. Different surface.
- **OpenWorker `personas/`** — user-facing chat personalities. Our profiles are
  language/build/test configs. Different concept.

---

*End of AGENT_LOOP_V2_PLAN.md. The research is in
[AGENT_LOOP_RESEARCH.md](AGENT_LOOP_RESEARCH.md). The current loop is documented in
[AGENT_PATCH_LOOP.md](AGENT_PATCH_LOOP.md).*