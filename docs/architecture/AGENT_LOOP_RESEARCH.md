# Agent Loop v2 — State of the Field (Research)

**Purpose**: answer "what does a modern agent harness look like, what does graph engineering
offer, and where does our loop sit?" This is the **research** companion to
[AGENT_LOOP_V2_PLAN.md](AGENT_LOOP_V2_PLAN.md), which turns these findings into an execution plan.
The current loop is documented in [AGENT_PATCH_LOOP.md](AGENT_PATCH_LOOP.md) (829 lines,
status: proven) and is not re-described here.

**Sources cited**:
- "Inside the Scaffold: A Source-Code Taxonomy of Coding Agent Architectures" (Rombaut,
  arXiv:2604.03515v2, Apr 2026) — a qualitative taxonomy of 13 open-source coding agents at
  pinned commits. Pure taxonomy, no benchmark correlations. All file:line-anchored claims below
  trace to that paper unless another link is given.
- "Agent Harness: What It Is and How to Build One" (Puppygraph, Jul 2026) — the five-component
  harness anatomy.
- "Harness Engineering for AI Coding Agents" (Augment Code, Apr 2026) — the constraint →
  feedback → quality-gate layering, PEV loop, rules files.
- SWE-bench leaderboard (swebench.com), SWE-agent ACI docs, Aider repo-map docs, OpenHands
  source, Codex CLI source.

---

## 1. Executive summary

**Where we sit.** The agent loop in `scripts/agent_loop/` is a **phased, scaffold-driven
patch harness** with a five-rung gate ladder, a concurrent adversarial panel, and an adjudicating
arbiter that filters false-positive findings before they reach the implementer. Against the
13-harness taxonomy it sits in the same family as AutoCodeRover (phased, scaffold-driven) but
adds two things no harness in the study has: **multi-model adversarial review** and **arbitrated
adjudication of reviewer findings**. Those are the moat.

**Where we fall behind.** Four gaps, all in the taxonomy's "diverging frontier" zone where no
harness has settled and the design space is still open:

| Dimension | We have | Best in field | Gap size |
|---|---|---|---|
| **Retrieval** | None — sees only declared regions | Prometheus (Neo4j AST graph), Aider (PageRank repo map) | Largest |
| **Compaction** | None — history grows across rounds | OpenCode (two-phase surgical), Codex (pre/mid-turn) | Large |
| **Memory** | Ledger is append-only, never read back | Codex (background extraction), Cline (LLM-writable rules), Prometheus (multi-tier: Athena + Neo4j + PostgreSQL) | Medium |
| **Plan-execute** | None — implementer edits directly | AutoCodeRover (search→patch separated), PEV loop | Medium |

**One missing mode.** The current `patch` mode assumes a human has done **localization**
(finding which functions/lines to rewrite). That is the hard part; the LLM's job (rewrite text
blocks) is the easy part. A **`developer` mode** — where the LLM gets a defect, uses the graph
and tools to localize, then edits — closes this gap and is the primary consumer of the graph
retrieval we are missing. See §10.

**The graph we already have.** `codebase-memory-mcp` has indexed this repo: 39,609 nodes,
46,060 edges, `CALLS`/`USAGE`/`DEFINES`/`WRITES`/`CONTAINS`/`HANDLES`/`TESTS` edge types. That is
Prometheus-style infrastructure. Two problems: (1) it is stale — it still indexes the predecessor
`ollama_patch_loop.py`, not the current `loop.py`/`arbiter.py`; (2) the loop does not use it. The
graph is the cheapest high-leverage upgrade available; see §6 and §11.

**Should we adopt an existing harness?** No. The verification stack (panel + arbiter + settled
decisions + thrashing detection) is the hardest thing to build and the thing the field has not
solved. What we lack — retrieval, compaction, memory, plan-execute — is the easy part to add.
Extend, do not replace.

---

## 2. The taxonomy — three layers, twelve dimensions

The taxonomy paper analyses 13 open-source coding agents (SWE-agent, OpenHands, Codex CLI,
Gemini CLI, Cline, Aider, OpenCode, mini-swe-agent, AutoCodeRover, Agentless, Moatless Tools,
Prometheus, DARS-Agent) and classifies them along three layers and twelve dimensions. There is
no separate "verification" layer — verification is distributed across the control loop
(generate-test-repair), the tool interface (the `validate` capability), and tree-search
reviewers. The full table is in §4; the dimensions are listed here as a reference vocabulary.

### Layer 1 — Control Architecture

| Dimension | What it decides |
|---|---|
| 1.1 Control Loop Strategy | Topology: fixed pipeline / user-driven / sequential ReAct / phased / tree search |
| 1.2 Loop Driver | Who decides next: user, scaffold, or LLM — "arguably the most fundamental distinction" |
| 1.3 Control Flow Implementation | The code mechanism: imperative `while`, recursion, compiled graph, exception-based |

### Layer 2 — Tool and Environment Interface

| Dimension | What it decides |
|---|---|
| 2.1 Tool Set Design | Count and granularity of LLM-callable tools; converges on read/search/edit/execute + optional validate |
| 2.2 Edit and Patch Format | How LLM output becomes code; converging on `str_replace` exact-string matching |
| 2.3 Tool Discovery | Static / config-conditional / per-phase / per-turn rebuilt / dynamic (MCP) |
| 2.4 Context Retrieval Paradigm | How code is found: grep, repo map, AST search, knowledge graph, embeddings, hierarchical localization, SBFL |
| 2.5 Execution Isolation | Trust boundary: none / stateless subshell / platform sandbox / Docker / shadow git / shadow mode |

### Layer 3 — Resource Management

| Dimension | What it decides |
|---|---|
| 3.1 State Management | How conversation state is represented: destructive / flat-list / typed event log / graph-scoped / tree-structured / event-sourced |
| 3.2 Context Compaction | How finite windows are managed: none / rule-based truncation / structural isolation / token-budget / LLM summarization / summarization+verification / LLM-initiated |
| 3.3 Multi-Model Routing | One model for all steps vs. different models per role or per attempt |
| 3.4 Persistent Memory | What survives between sessions: nothing / pipeline resumability / config files / LLM-writable rules / full session persistence / background extraction / multi-tier |

### Cross-cutting themes

| Theme | What it covers |
|---|---|
| Sampling vs. iteration | Independent regeneration vs. re-prompt with error feedback |
| Sub-agent delegation | Whether and how a primary spawns secondary agents |
| Online vs. offline selection | During-search vs. final-output selection in tree-search |
| Ecosystem maturity | Fork-based vs. dependency-based reuse |

---

## 3. The five composable loop primitives

The paper's headline finding (§7): architectures are **continuous spectra, not discrete
categories**, and **11 of 13 agents layer multiple primitives**. Five primitives compose the
space; they are not mutually exclusive.

| Primitive | What it is | Where it appears |
|---|---|---|
| **ReAct** | thought → tool → observation cycle; LLM selects next action | SWE-agent, OpenHands, Codex, Gemini CLI, mini-swe-agent, Cline, OpenCode |
| **Generate-test-repair** | edit → run tests → re-prompt with error if fail | Aider (inner loop), OpenHands, Codex, Cline (implicit in all ReAct+execute agents) |
| **Plan-execute** | decompose → plan → execute against plan → verify alignment | AutoCodeRover (search→patch), Prometheus (graph nodes), OpenCode (`plan` sub-agent) |
| **Multi-attempt retry** | generate N candidates, select best | SWE-agent `RetryAgent` (iteration within + sampling across, reviewer selects) |
| **Tree search** | branch on choices, evaluate, backprop, select | Moatless (full MCTS with rewards), DARS-Agent (tree without reward, greedy critic) |

**Pure single-loop agents are rare** (§5.4): only mini-swe-agent (ReAct only) and Agentless
(fixed pipeline, no loop). The paper's conclusion: the ReAct loop is foundational but
insufficient on its own — layering a retry, test-repair, or planning primitive addresses failure
modes a single feedback loop cannot. Our loop composes **three**: generate-test-repair (gate
ladder), plan-execute (implicitly — the ticket IS the plan, the regions ARE the scope), and
multi-attempt retry (the round loop with arbiter filtering).

### Loop driver — the fundamental axis (§4.1.2)

Who decides what happens next matters more than the loop topology. Three camps:

- **User-driven** (Aider): LLM has 0 tools, user selects files, LLM emits text edits. Sidesteps
  localization entirely.
- **Scaffold-driven** (Agentless, AutoCodeRover): scaffold controls sequencing, LLM called at
  fixed points. Invests in retrieval infrastructure because the scaffold owns navigation.
- **LLM-driven** (SWE-agent, OpenHands, Codex, 9 of 13): LLM selects tools and controls
  exploration. Trusts the LLM to navigate with general-purpose tools (grep, find).

**Our loop is scaffold-driven.** The ticket pre-declares regions; the loop drives the
implementer, gate ladder, panel, and arbiter in a fixed sequence. This puts us in the
AutoCodeRover camp, and means retrieval infrastructure is ours to invest in — the scaffold
owns navigation but currently invests nothing in it.

> The retrieval-driver correlation (§4.2.4, §5.3): scaffold-driven agents tend to invest in
> retrieval infrastructure, while LLM-driven agents tend to trust the LLM to navigate with
> general-purpose tools. Our loop is scaffold-driven but has no retrieval — that is the gap.

---

## 4. Per-harness comparison

Synthesized from the taxonomy paper's per-dimension tables. Each harness's distinctive trait
only — the full table is in the paper.

### SWE-agent (Python, SWE-bench, 19k stars)
- **Control**: ReAct + multi-attempt retry (`RetryAgent`), imperative `while`, LLM-driven.
  Per-attempt model cycling through `agent_configs`.
- **Tools**: 3 default / 35 across 15 config-conditional bundles; `str_replace_editor`; 10
  output parsers for model-agnostic tool parsing.
- **Retrieval**: keyword/regex (grep/find); scaffold provides no code understanding.
- **Compaction**: rule-based truncation, 7 composable processors; `polling` parameter
  preserves prompt-cache prefix (undocumented interaction between compaction and API cost).
- **Memory**: none.
- **Distinctive**: config-conditional tool bundles + per-attempt model cycling + the polling
  cache trick.

### OpenHands (Python, SWE-bench, 70k stars)
- **Control**: ReAct, imperative `while`, LLM-driven.
- **Tools**: 9+ tools, `str_replace_editor`; only agent with a built-in BrowserGym headless
  browser; `request_condensation` tool (LLM can request compaction); MCP support.
- **Retrieval**: keyword/regex; no scaffold-side understanding.
- **State**: **event-sourced** — immutable `EventStream`; condensation inserts markers rather
  than deleting events, enabling full audit trail and replay after compaction.
- **Compaction**: LLM summarization; **most extensible condenser — 9 pluggable implementations
  composable into pipelines via registry**.
- **Isolation**: Docker — architecturally distinctive: a FastAPI action server runs *inside*
  the container, host-side controller talks via HTTP.
- **Memory**: none (notable for a 70k-star interactive tool).
- **Distinctive**: event-sourced state + in-container FastAPI server + built-in web browser +
  most extensible condenser registry.

### Codex CLI (Rust/TS, CLI, 72k stars)
- **Control**: ReAct, imperative `while` with async event channels, LLM-driven.
- **Tools**: ~20+; `apply_patch`; meta-tools for tool discovery (`tool_search`, `tool_suggest`);
  `request_permissions` tool lets the LLM ask for elevated sandbox access mid-session —
  "permissions are a negotiable resource"; MCP; sub-agent spawning with depth limit.
- **Tool discovery**: **per-turn dynamic rebuild** — `built_tools()` called for every sampling
  request; tool set "in principle different at every LLM call."
- **Retrieval**: keyword/regex.
- **State**: flat list + dual persistence (append-only JSONL rollout for replay + SQLite for
  queryable state); only flat-list agent supporting undo and thread rollback.
- **Compaction**: LLM summarization; **pre-turn and mid-turn** modes — unique awareness of
  compaction timing relative to conversation structure.
- **Isolation**: platform sandboxing (Bubblewrap+Landlock on Linux, Seatbelt on macOS).
- **Routing**: **most models of any agent** — primary + Guardian (`gpt-5.4` for safety
  evaluation, the only multi-model routing for safety not cost) + two memory-extraction models.
- **Memory**: **background extraction pipeline** (two-phase: extract from rollouts, then
  consolidate via sub-agent; usage-ranked, stale pruned).
- **Distinctive**: per-turn tool rebuild + Guardian LLM safety evaluator + pre/mid-turn
  compaction + per-rollout memory-extraction + LLM-negotiable permissions.

### Aider (Python, CLI, 43k stars)
- **Control**: **user-driven** outer loop (LLM has 0 callable tools); inner generate-test-repair
  autonomous for up to `max_reflections`; imperative `while`.
- **Tools**: **0 LLM-callable tools** (text-parsed edits); 13 registered edit formats as
  separate coder subclasses, selected by model capability.
- **Retrieval**: **PageRank repo map** — tree-sitter → NetworkX dependency graph → PageRank
  centrality; conversation identifiers 10x boost, chat files 50x; binary search fills token
  budget. **No other agent uses graph-theoretic relevance ranking.**
- **State**: destructive two-list; summarization overwrites `done_messages`.
- **Compaction**: LLM summarization, recursive hierarchical.
- **Isolation**: none (local shell) — relies on the user being present; "the human serves as
  the safety boundary."
- **Memory**: config file loading; tags cache persists AST analysis across sessions.
- **Distinctive**: 0 tools + PageRank repo map + 13 model-specific edit formats + user-as-
  safety-boundary + generate-test-repair as the only autonomous inner loop.

### mini-swe-agent (Python, baseline, 4k stars)
- **Control**: ReAct, imperative `while`, LLM-driven; **exception-based signaling**
  (`InterruptAgentFlow` hierarchy carries control messages).
- **Tools**: **1 tool — a single `bash`** — covers all four capability categories by delegating
  to shell; only agent that edits files directly via shell.
- **Retrieval**: keyword/regex (via shell).
- **State**: flat list, raw history sent as-is, no filtering — the minimal extreme.
- **Compaction**: **none — crashes on `ContextWindowExceededError`**.
- **Isolation**: stateless subshells.
- **Memory**: none.
- **Distinctive**: the deliberate minimal baseline — 1 bash tool, no compaction, no memory,
  exception-based control flow. Closest to a "pure" single-loop agent.

### OpenCode (TS, CLI, 135k stars — most stars)
- **Control**: ReAct, imperative `while`, LLM-driven; **layers a global publish-subscribe event
  bus** on top of the while loop; no other CLI agent uses an event bus.
- **Tools**: 18+ built-in; `edit` (string replace) and `apply_patch` (unified diff) — selects
  between them based on model capability; MCP; plugins; LSP integration; `skill` meta-tool
  loads user-defined workflows from filesystem; `batch` tool for grouping operations.
- **State**: **typed event log — SQLite-backed message/part hierarchy with 12 part types**;
  append-only messages with mutable part states; most granular state after OpenHands and the
  only one backed by a relational database.
- **Compaction**: **two-phase, the most surgical** — first prunes verbose old tool outputs
  >40k tokens (preserves message structure, replaces with truncation markers), then triggers
  LLM summarization via a dedicated compaction agent that can use a cheaper model.
- **Memory**: **full session persistence** in SQLite — all messages, tool outputs, token
  usage, costs; interrupted sessions resume with complete context.
- **Delegation**: role-based `task` tool spawns sub-agents (build, plan, explore, general),
  each with scaffold-enforced tool permissions — `plan` disables `edit`/`write` but keeps
  `bash`; `explore` enables read-oriented tools, denies write.
- **Distinctive**: pub/sub event bus + SQLite 12-part typed state + two-phase surgical
  compaction + per-sub-agent role-based model overrides + LSP integration + skill/batch
  meta-tools.

### Prometheus (Python, SWE-bench, 1k stars)
- **Control**: **LangGraph compiled state machine** — ≥4 levels of nested subgraphs; each LLM
  node has its own message list; per-node tool scoping; per-node model assignment; graph-
  scoped state reset at retry boundaries.
- **Tools**: per-node scoped — EditNode=5 tools, BugReproducingWriteNode=read_file only,
  BugFixVerifyNode=run_command only.
- **Retrieval**: **knowledge graph traversal** — Neo4j graph built from tree-sitter ASTs (20
  languages); 11 tools (10 graph traversal + `read_file`) query FileNode, ASTNode, TextNode.
- **Memory**: **multi-tier persistence** — Athena (semantic memory, HTTP API) + Neo4j (KG) +
  PostgreSQL (LangGraph checkpoints); memory-first retrieval with KG fallback.
- **Distinctive**: Neo4j AST graph + LangGraph state machine + multi-tier memory. A SWE-bench
  agent with persistence "more characteristic of an interactive tool, suggesting architectural
  ambitions beyond benchmark evaluation."

### Moatless Tools (Python, SWE-bench, 600 stars)
- **Control**: **full MCTS** — select/expand/simulate/backprop with rewards (−100..+100) and
  visit counts; pluggable selector; discriminator selects best finished trajectory.
- **Tools**: includes a separate `validate` capability category (only agent with this).
- **Retrieval**: **embedding-based semantic search** — FAISS via LlamaIndex; only agent with
  embedding-based retrieval as an LLM-callable tool.
- **Isolation**: **shadow-mode execution** — in-memory `FileContext`, no disk writes, so
  branching is free (contrasted with DARS-Agent's expensive Docker reset + replay).
- **Distinctive**: the cleanest separation of concerns — its `ActionAgent` (single-step
  executor) can be driven by either `AgenticLoop` (ReAct) or `SearchTree` (MCTS) with no
  changes to agent code. "The choice between sequential and tree-search exploration is a
  configuration decision rather than an architectural one."

### AutoCodeRover (Python, SWE-bench, 3k stars)
- **Control**: **phased loop** — optional reproducer → optional SBFL fault localization →
  search → patch. Search agent has **8 read-only tools, no edit/execute**; patch agent has
  **no search tools**. "Localization and repair are distinct tasks."
- **Retrieval**: AST-aware search (Python-only); plus **SBFL with Ochiai scoring** — the only
  agent that bridges classical fault localization with LLM-based repair.
- **Distinctive**: phased tool-scoping (search phase cannot edit, patch phase cannot search) +
  SBFL + secondary LLM call to parse tool selections into structured JSON.

### Agentless (Python, SWE-bench, 2k stars)
- **Control**: **fixed pipeline, no loop** — 10 stages of independent scripts connected by
  JSONL files on disk; no feedback between stages.
- **Retrieval**: **hierarchical localization** — file → class/function → line narrowing
  across stages; each level sees only what the previous identified.
- **Retry**: flat independent sampling — ~20 candidate patches generated in isolation,
  selected by majority vote. No tree, no interaction between candidates.
- **Distinctive**: the closest to a pure single-primitive agent. Sidesteps compaction by
  using single-turn calls with progressively-compressed prompts.

---

## 5. Harness engineering — the discipline

The term "harness engineering" (attributed to Mitchell Hashimoto, formalized by OpenAI's Ryan
Lopopolo, Feb 2026) names the discipline of designing the layer around a model that makes it
reliable. The Augment Code guide formalizes it as three reinforcing layers.

### The three layers

| Layer | What it does | Example |
|---|---|---|
| **Constraint (feedforward)** | Reduce the solution space before generation | Rules files (AGENTS.md), architectural lint, type systems, complexity limits |
| **Feedback (corrective)** | Return structured error signals so the agent self-corrects | Lint messages as prompts ("use `logger.info({...})` instead of `console.log`") |
| **Quality gate (enforcement)** | Prevent non-compliant code from merging | CI failures on violation, inline-disable rules disabled to prevent suppression |

> Over-constraining is a real failure mode. Complexity limits set too low flag legitimate
> refactoring. Start narrow, measure, then expand.

### PEV loop — Plan, Execute, Verify

The PEV pattern is a three-phase architecture that separates planning from execution and
enforces verification as a structured feedback loop. The distinction from generate-and-check
is architectural, not cosmetic:

| Dimension | Generate-and-check | PEV loop |
|---|---|---|
| Planning | None; LLM generates directly | Explicit decomposition with acceptance criteria |
| Execution scope | Unconstrained | Bounded by plan; harness gates fire on every tool call |
| Verification | Post-hoc only | Pre-execution + runtime + post-execution + plan alignment |
| Feedback signal | Binary pass/fail | Error messages with context looped back into reasoning |

Pre-execution gates: is this a known tool? are arguments valid? does this require approval? is
the path inside the workspace? Plan alignment gates: did the agent use existing auth middleware
or create a new one? did it follow the response format convention? — architectural questions
invisible to standard test runners.

### Agent = Model + Harness

LangChain's formulation. The model is a fixed artifact you call over an API; the harness is
code you own. A capable model in a thin harness behaves like a brilliant contractor with no
tools and no notes; in a well-built harness the same model can sustain a long task. The
harness, not the model, is increasingly where reliability is won or lost.

### Rules files (AGENTS.md)

The `AGENTS.md` spec (Aug 2025) is a shared cross-tool convention: OpenAI Codex uses
hierarchical `AGENTS.md` (Git root to CWD, 88 files across subcomponents in OpenAI's repo),
Claude Code uses `CLAUDE.md`, Cursor uses `.cursor/rules/*.mdc`. Three-tier boundary pattern
from GitHub's analysis of 2,500+ repos:

| Tier | Examples |
|---|---|
| Always | Log all notification delivery attempts; use UTC for scheduling |
| Ask First | Adding a new notification channel, changing retry intervals |
| Never | Send notifications without verified opt-in; modify the unsubscribe flow |

**Our repo already uses this**: `CLAUDE.md` at the repo root, `.agents/AGENTS.md` for
fail-fast rules. The loop's `profiles.py` carries the domain rules in `implementer_rules` and
`reviewer_priorities` — that is our constraint layer.

### Why rules files alone are insufficient

LLM compliance with instructions is **probabilistic, not deterministic**. Rules files must be
combined with deterministic outer-harness constraints (linters, CI gates) to be reliable at
scale. Our loop's gate ladder IS this combination: `profiles.py` is the constraint layer, the
gates are the deterministic enforcement.

---

## 6. Graph engineering for code

The taxonomy paper identifies **seven retrieval paradigms** (§4.2.4). Three involve graphs.

### 6.1 AST-aware search (AutoCodeRover, Moatless, DARS-Agent, Prometheus)

Parse source into ASTs, enabling `search_class`, `search_method` — structure-aware queries
("find all methods named `process`" rather than text matching). AutoCodeRover is Python-only;
Prometheus covers 20 languages via tree-sitter. This is the floor of graph engineering: AST
indices that answer structural queries.

### 6.2 Knowledge graph traversal (Prometheus — the standout)

Prometheus builds a **Neo4j graph from tree-sitter ASTs** (20 languages) with `FileNode`,
`ASTNode`, `TextNode` entities. The LLM gets **11 tools** (10 graph traversal + `read_file`)
that query the graph directly. Combined with **multi-tier memory** (Athena semantic memory +
Neo4j KG + PostgreSQL checkpoints) and **memory-first retrieval with KG fallback**. This is
the richest graph-augmented retrieval in the 13-harness study.

### 6.3 PageRank repo map (Aider — the only graph-theoretic relevance ranking)

Aider builds a **dependency graph** from tree-sitter tags, then applies **PageRank centrality**
to rank symbols. Identifiers mentioned in the conversation get 10x boost, chat files 50x.
Binary search fills the token budget in rank order. The ranked slice is injected into the
prompt — the LLM never calls graph tools; it just receives the most relevant context. This is
the cheapest path to graph-augmented retrieval and matches our 0-tools pattern.

> No agent combines Prometheus-style graph traversal (LLM-callable tools) with Aider-style
> PageRank ranking (passive injection). That combination is open.

### 6.4 What we already have

`codebase-memory-mcp` has indexed this repo. The graph has:

- **39,609 nodes** across 13 labels (Function 5,842; Method 5,087; Variable 9,856; Section
  11,896; Class 1,083; File 934; Module 3,243; Interface 722; Type 402; Folder 327; Enum 139;
  Route 77; Project 1)
- **46,060 edges** across 15 types (`CALLS` 18,932; `USAGE` 9,866; `DEFINES` 9,142;
  `DEFINES_METHOD` 5,087; `WRITES` 1,561; `CONTAINS_FILE` 934; `CONTAINS_FOLDER` 268;
  `HANDLES` 77; `TESTS` 42; `CONFIGURES` 36; `IMPORTS` 30; `HTTP_CALLS` 28; `RAISES` 6;
  `ASYNC_CALLS` 5; `THROWS` 1)
- MCP tools: `search_graph`, `trace_call_path` (inbound/outbound/both), `query_graph` (raw
  Cypher), `get_code_snippet`, `get_architecture`, `search_code` (graph-augmented grep)

This is Prometheus-class infrastructure already deployed. Two problems:

1. **Stale.** The graph still indexes `ollama_patch_loop.py` (the predecessor). A trace for
   `run_ticket` returns callees in `ollama_patch_loop`, not `loop.py`. A search for
   `adjudicate` returns 0 results. The graph was indexed before the new loop existed.
2. **Unused.** No graph-augmented prompts, no graph-traversal tools for the implementer, no
   PageRank-style context ranking. The loop operates blind while the graph sits idle.

### 6.5 The retrieval landscape, ranked

| Paradigm | Coverage | Tool calls needed | Token cost | Our fit |
|---|---|---|---|---|
| Keyword/regex (grep) | 8/13 agents | LLM calls grep | Low | What we would have if we added tools |
| PageRank repo map (Aider) | 1/13 | None (passive injection) | Medium | Best first step for us — matches our 0-tools pattern |
| AST-aware search | 4/13 | LLM calls search_class/search_method | Medium | Floor for Developer mode |
| Knowledge graph traversal (Prometheus) | 1/13 | 10+ graph tools | High | Target for Developer mode phase 2 |
| Embedding semantic search (Moatless) | 1/13 | FAISS query | Medium | Not needed — graph is better for code |
| Hierarchical localization (Agentless) | 1/13 | None (scaffold-driven) | Low | Compatible with our scaffold-driven nature |
| SBFL (AutoCodeRover) | 1/13 | Test execution + Ochiai | High | Valuable for bug-fix tickets; future |

### 6.6 The recommended path

**Phase 1 — passive injection (Aider-style).** Before each round, query the graph for the
callees and callers of each region's functions, the tests that cover them, and the types they
use. Inject the ranked slice into the implementer, reviewer, and arbiter prompts. The LLM
never calls graph tools; it just receives richer context. This is the cheapest, lowest-risk
upgrade and matches our 0-tools pattern.

**Phase 2 — active tools (Prometheus-style).** When Developer mode lands (§10), give the LLM
graph-traversal tools: `search_code`, `trace_call_path`, `list_callers`, `list_callees`,
`find_tests_for`. The LLM localizes by querying the graph. This is the target state for
autonomous localization.

---

## 7. Memory and state

The taxonomy identifies six state representations and seven compaction strategies. The field
is diverging — no consensus.

### 7.1 State representations

| Strategy | Agents | Notes |
|---|---|---|
| **Destructive** (overwrite old turns) | Aider | Two-list; summarization overwrites `done_messages` |
| **Flat list preserved** | SWE-agent, Codex, Gemini CLI, mini-swe-agent, DARS-Agent | History grows; filtered views for LLM |
| **Typed event log** | OpenCode | SQLite 12-part; append-only messages with mutable part states |
| **Graph-scoped** | Prometheus | Per-node message lists; reset at retry boundaries |
| **Tree-structured** | Cline | Recursive call stack grows with conversation |
| **Event-sourced** | OpenHands | Immutable `EventStream`; condensation inserts markers, not deletes |

**Our loop is flat-list preserved, in-memory only.** History grows across rounds; there is no
persistence, no filtering, no compaction. A long ticket (4+ rounds with rich reviewer output)
will eventually exceed the context window. This is the mini-swe-agent end of the spectrum.

### 7.2 Compaction strategies

| Strategy | Agents | Notes |
|---|---|---|
| **None (crash on overflow)** | mini-swe-agent | Confirms compaction is not optional beyond trivial tasks |
| **Rule-based truncation** | SWE-agent, DARS-Agent | Keep first + last N observations, elide rest; polling preserves cache prefix |
| **Structural isolation (prevention)** | Prometheus, AutoCodeRover | Per-node scoping + resets at retry boundaries |
| **Token-based selective inclusion** | Moatless | Greedy recent-first selection within token budget |
| **LLM summarization (scaffold-triggered)** | Aider, OpenHands, Gemini CLI, Codex, OpenCode | Auto at threshold |
| **LLM summarization + verification** | Gemini CLI | Summarize, then a "Probe" turn checks for information loss — only agent that validates its own compaction |
| **LLM-initiated compaction** | Cline | `condense` tool — LLM decides when |

**Our loop has none.** History accumulates across rounds unboundedly. The reference pattern
for us is **OpenCode's two-phase surgical**: (1) prune verbose old tool outputs (reviewer
findings, build logs) above a token threshold, replacing with truncation markers; (2) LLM
summarization of the pruned history via a cheaper model. This preserves the structure (the
arbiter still sees "round 2 produced 4 findings, all rejected") without the bulk.

### 7.3 Persistent memory

| Strategy | Agents | Notes |
|---|---|---|
| **None** | SWE-agent, OpenHands, AutoCodeRover, mini-swe-agent, DARS-Agent | All 5 are SWE-bench/benchmark agents |
| **Pipeline resumability** | Agentless | JSONL outputs enable `--skip_existing` |
| **Config file loading (static)** | Aider | `.aider.conf.yml`; tags cache persists AST analysis |
| **LLM-writable rules** | Cline, Gemini CLI | LLM actively writes persistent instructions (`new_rule`, `save_memory`) |
| **Full session persistence** | OpenCode | SQLite; interrupted sessions resume with complete context |
| **Background extraction pipeline** | Codex | Two-phase: extract from rollouts, then consolidate; usage-ranked, stale pruned |
| **Multi-tier** | Prometheus | Athena + Neo4j + PostgreSQL; memory-first retrieval with KG fallback |

**Our loop has a ledger but never reads it back.** The `settled` list in `profiles.py` is
hand-curated — a human reads the arbiter's `<<<SETTLED>>>` output and copies decisions into the
profile. The arbiter already nominates settled decisions; nothing persists them. This is the
Codex background-extraction pattern, scoped to our arbiter: auto-extract the arbiter's
`SETTLED` section and write it to a settled-decisions store that feeds future review rounds.

> The settled-decisions mechanism is ours and is not found in any of the 13 harnesses. The
> field's closest analog is Cline's LLM-writable rules, but those are general instructions;
> ours are specific adjudication precedents that prevent reviewers from re-litigating known
> false positives. Auto-persisting them is the lowest-hanging memory upgrade.

---

## 8. Verification across the field

The taxonomy has no dedicated verification layer. Verification is distributed across four
mechanisms. Our loop is the only harness that combines all four plus a fifth (arbitration).

### 8.1 Generate-test-repair (the control-loop primitive)

Grounded in Reflexion (Shinn et al. 2023). After edits, the scaffold runs linting and tests;
if either fails, re-prompts with error output. This is the loop inside our gate ladder:
compile → test → lock-scope, with feedback sent back to the implementer.

### 8.2 The `validate` tool category

A dedicated test-running or linting tool, separate from `execute`. Appears **only in Moatless
Tools**; all other agents subsume validation under `execute` (a `bash` tool that runs `pytest`).
Our loop has no LLM-callable tools, so this does not apply — the scaffold runs tests directly.

### 8.3 Tree-search reviewers / discriminators

- **SWE-agent `RetryAgent`**: multiple complete attempts, a **reviewer model** selects best.
- **DARS-Agent**: online LLM critic at each branch + **separately trained offline reviewer**
  evaluates finished patches.
- **Moatless**: MCTS value function online + separate **discriminator** re-evaluates all
  completed trajectories offline — "potentially selecting one that was not the most-visited."
- **Prometheus**: voting at the decision layer — calls advanced model 10 times on the same
  prompt to select among already-generated candidate patches, early stopping when vote lead
  exceeds remaining votes.

### 8.4 Plan-alignment verification

Not a distinct dimension in the taxonomy. Closest instantiations:
- **Prometheus** graph structure with conditional edges on `state["reproduced_bug"]` — scaffold
  enforces stage transitions based on state fields.
- **AutoCodeRover** phase separation: search agent has only read-only tools; patch agent has
  no search tools. Workflow-level constraint, not a runtime plan check.
- **OpenCode** role-based sub-agents: `plan` agent disables file-editing tools; `explore`
  agent denies write. Structural guardrailing via tool permissions.

### 8.5 Safety verification — Codex CLI Guardian

Codex CLI uses a separate LLM (`gpt-5.4`) — the **Guardian** — to evaluate each tool call's
risk with structured scoring on a 0–100 scale, blocking calls above threshold 80. "Only agent
in the corpus that uses an LLM to evaluate the safety of another LLM's actions."

### 8.6 Where our loop sits — ahead of the field

| Mechanism | We have | Field has |
|---|---|---|
| Generate-test-repair | Yes (gate ladder) | Aider, all ReAct+execute agents |
| Concurrent adversarial panel | **Yes — multi-model, worst-verdict-wins** | SWE-agent single reviewer; Prometheus 10x voting is candidate selection, not adversarial review |
| Adjudication of findings | **Yes — arbiter rules on each finding, only upheld go back** | None — no harness separates detection from adjudication |
| Settled-decisions cache | **Yes — prevents re-litigation across rounds** | None |
| Thrashing detection | **Yes — signature overlap across rounds, escalates on zero convergence** | None |
| Plan-alignment gate | No | Prometheus (graph edges), OpenCode (tool permissions) |
| Safety LLM | No | Codex Guardian |
| Tree search | No (multi-attempt only) | Moatless (MCTS), DARS-Agent (greedy tree) |

**The panel + arbiter + settled-decisions + thrashing detection combination is our moat.** No
harness in the 13-agent study separates detection (reviewers) from adjudication (arbiter), and
none carries adjudication precedents forward to stop re-litigation. The Reddit result we
found (GPT-5.2 reviewer + Claude Opus brainstormer → 80%→90% on SWE-bench) is a 2-agent version
of what we already have with richer machinery.

---

## 9. What the field has that we do not — the four gaps

### Gap 1: Retrieval (largest)

We see only declared regions. The implementer, reviewer, and arbiter all operate on the
ticket's pre-scoped regions with no visibility into the surrounding codebase. This means:
- The implementer cannot see callers of the functions it edits (may break them).
- The reviewer cannot check whether the patch is consistent with code outside the regions.
- The arbiter cannot see whether a "fix" introduces a regression in a caller.
- No Developer mode is possible — the LLM has no way to find code to edit.

The field has seven retrieval paradigms (§6.5). We need at minimum the Aider-style passive
injection (phase 3) and the Prometheus-style active tools (phase 4, for Developer mode).

### Gap 2: Compaction (large)

History grows unboundedly across rounds. A 4-round ticket with rich reviewer output can
exceed 100K tokens of history. The implementer's prompt includes every prior round's raw
output, every reviewer's findings, every arbiter ruling. This is the mini-swe-agent failure
mode — the loop will eventually crash or degrade.

The field's best pattern for us is OpenCode's two-phase surgical: prune verbose old tool
outputs above a token threshold (preserve structure, replace with truncation markers), then
LLM summarization via a cheaper model. This is phase 5 of the plan.

### Gap 3: Memory (medium)

The ledger records every run but is never read back. The `settled` list in `profiles.py` is
hand-curated — a human reads the arbiter's `<<<SETTLED>>>` output and copies decisions into the
profile. The arbiter already nominates settled decisions; nothing persists them.

The field's best pattern for us is Codex's background extraction, scoped to our arbiter:
auto-extract the `SETTLED` section from every arbiter response and write it to a settled-
decisions store that feeds future review rounds. This is phase 6 of the plan.

### Gap 4: Plan-execute (medium)

The implementer gets the defect + regions and edits directly. There is no planning step
where the implementer decomposes the problem, proposes an approach, and gets it reviewed
before generating code. This means a round can be wasted on an approach that was wrong from
the start.

The field's best pattern for us is AutoCodeRover's phased separation: a search/plan phase
(read-only tools, produces a plan) followed by a patch phase (no search tools, produces
code). The PEV loop is the general formulation. This is phase 7 of the plan.

---

## 10. Developer mode — the missing autonomous path

### 10.1 The gap

The current `patch` mode assumes a human has done localization — the ticket comes pre-regioned
with `file` + `lines` + `purpose` for each block. The LLM never explores the codebase, never
searches, never reads outside its declared regions. That is the Aider pattern (0 tools,
scaffold applies edits) — except Aider has the user doing localization interactively, while
our patch mode requires a human to pre-write the ticket JSON.

**That means a human must: (1) read the defect, (2) find which functions/methods are
involved, (3) identify the exact line ranges to rewrite, (4) write the ticket JSON. That is
the hard part. The LLM's job (rewrite text blocks) is the easy part. The current loop
automates the easy part and leaves the hard part to a human.**

### 10.2 What Developer mode is

A Developer mode is the autonomous path: the LLM gets a defect description (not a pre-regioned
ticket) and does the localization itself using the graph + tools. It then edits, and the same
gate ladder + panel + arbiter reviews the result.

```
defect → DEVELOPER (LLM explores, localizes, edits) → gate ladder → panel → arbiter → ship
```

| | `patch` (current) | `developer` (proposed) |
|---|---|---|
| Input | Ticket with pre-declared regions | Defect description only |
| Localization | Human does it | LLM does it, using graph + search |
| Tools | 0 (LLM emits text blocks) | read_file, search_code, trace_call_path, edit_file, run_build, run_tests |
| Scope | Bounded to declared regions | Multi-file, LLM decides |
| Graph use | None (sees only regions) | Critical — it is how the LLM navigates |
| Gates | Same 5-rung ladder | Same 5-rung ladder |
| Panel/arbiter | Same | Same |

### 10.3 Why Developer mode is the primary consumer of the graph

In patch mode, the LLM does not need the graph — it only sees its declared regions. In
Developer mode, the LLM *must* find the right code to edit, and the graph (call paths, callers,
tests, types) is what makes that tractable. The graph retrieval (§6) is not a nice-to-have for
Developer mode — it is the core mechanism.

### 10.4 The Developer mode tool set (minimal)

Following SWE-agent's ACI philosophy (custom tools for agents, not human tools):

| Tool | What it does | Why |
|---|---|---|
| `read_file` | Read a file, windowed (100 lines default, scroll) | SWE-agent found 100-line windows work best |
| `search_code` | Graph-augmented grep (search_code from codebase-memory-mcp) | Finds symbols, ranks by structural importance |
| `trace_call_path` | Who calls this function / what does it call | Graph traversal; answers "will this break callers?" |
| `edit_file` | Apply an edit (str_replace or unified diff) | Converging field standard |
| `run_build` | Run the profile's build command | Gate 2, but LLM-callable |
| `run_tests` | Run the profile's test command | Gate 3, but LLM-callable |

**Deliberately excluded**: `run_command` (arbitrary shell), `browser`, `git` operations.
This repo has an AddOn that moves real money; tool scope matters. The minimal set lets the
LLM localize + edit + verify without giving it a shell.

### 10.5 The ACI design principles (from SWE-agent)

1. **Linter on edit**: do not let the edit go through if the code is not syntactically correct.
   Our gate 1 (static) already does this.
2. **Custom file viewer, not `cat`**: display 100 lines per turn, with scroll and search.
3. **Custom search command**: succinctly list files with matches, not full context — showing
   more context about each match proved too confusing.
4. **Tools for agents, not humans**: the tools you design for an agent should not be the same
   tools you would design for a human.

### 10.6 Developer mode in the field

| Harness | Pattern | Lesson for us |
|---|---|---|
| SWE-agent | ReAct + config-conditional tools + RetryAgent | Per-attempt model cycling; reviewer selects best |
| OpenHands | ReAct + event-sourced + in-container FastAPI | Event-sourced state; browser for web lookup |
| Codex CLI | ReAct + per-turn tool rebuild + Guardian | LLM-negotiable permissions; safety LLM |
| AutoCodeRover | Phased: read-only search → no-search patch | Phase separation prevents the search agent from editing |
| OpenCode | Role-based sub-agents with scaffold-enforced permissions | `plan` disables edit; `explore` denies write |

**Our Developer mode should adopt**: AutoCodeRover's phase separation (explore first, then
edit), OpenCode's role-based tool permissions (the explore phase cannot edit), and SWE-agent's
ACI principles (100-line windows, linter on edit, custom search). The edit phase's first turn
must include a brief plan (which files, which functions, what changes) in its notes — this is
a prompt-level requirement, not a separate gated phase, keeping the design simpler than
AutoCodeRover's full search→patch split while still forcing the LLM to commit to an approach
before generating edits. **Our Developer mode should keep**: the gate ladder, the panel, the
arbiter, the settled-decisions cache — these are the moat and they apply unchanged to
Developer-mode output.

---

## 11. The full mode set

The loop should support the full SDLC. Each mode maps to a field pattern.

| Mode | Input → Output | Autonomous? | Field analog | Status |
|---|---|---|---|---|
| `brainstorm` | defect → candidate approaches + trade-offs | No code changes | Prometheus exploration nodes | New (DEFERRED) |
| `plan` | defect → ticket JSON (regions + acceptance tests), reviewed by panel+arbiter | No code changes | AutoCodeRover search phase; PEV plan | New (phase 6) |
| `test` | defect + ticket JSON (from `plan`) → failing acceptance tests | Writes only test files | Test-first development (manual today) | New (phase 6) |
| `developer` | defect → patched code, LLM localizes + edits | Yes (tools + graph) | SWE-agent, OpenHands, Codex | New (phase 8) |
| `patch` | ticket JSON (pre-regioned) → patched code | Semi (bounded by regions) | Aider (0 tools, scaffold applies) | Current |
| `review` | existing diff → panel verdict | No code changes | Already built (`review_mode.py`) | Current |
| `docs` | diff + graph → documentation updates | Writes only docs | Not in the 13-harness study | New (DEFERRED) |

**Note on `plan` mode**: the plan doc specifies that `plan` mode output (the ticket JSON) goes
through panel+arbiter review for completeness before being promoted to a ticket file. This is
a new requirement specific to this loop — the panel+arbiter stack, originally built for
patch review, is reused to verify that the plan found the right regions and named the right
acceptance tests. No field harness reviews a plan this way.

**Note on `test` mode input**: `test` takes a defect + a ticket JSON (from `plan` mode). The
ticket JSON provides the regions and acceptance test names — `test` mode needs to know what
to test and where the code under test lives. This is tighter than the original research-doc
formulation ("defect → failing acceptance tests") which omitted the ticket dependency.

**Deferred modes**: `brainstorm` and `docs` are not in phases 1-8 of the execution plan. They
are future work after the core pipeline (plan -> test -> patch/developer) is proven.

### The pipeline

```
                    +----------+
                    | brainstorm|  (optional, DEFERRED)
                    +----+-----+
                         | (optional)
                         v
                    +----------+
         defect -->|   plan   |  --> ticket JSON
                    +----+-----+
                         |
                         v
  defect + ticket -->+----------+
                    |   test   |  --> failing acceptance tests
                    +----+-----+
                         |
              +----------+----------+
              |                     |
              v                     v
        +----------+            +----------+
        |  patch   |            | developer|  (autonomous)
        +----+-----+            +----+-----+
             |                       |
             +-----------+-----------+
                         |
                         v
                    +----------+
                    |  review  |
                    +----+-----+
                         |
                         v
                    +----------+
                    |   docs   |  (DEFERRED)
                    +----------+
```

- `brainstorm` is an optional, deferred precursor to `plan` (exploratory, no code changes).
- `plan` takes a defect description, produces a ticket JSON (regions + acceptance tests).
- `test` takes a defect + the ticket JSON from `plan`, produces failing acceptance tests.
- `developer` takes a defect, localizes and edits autonomously (phase 8).
- `patch` takes a ticket JSON (from `plan` or hand-written), produces patched code.
- `review` is adversarial: existing diff → panel verdict. Already exists.
- `docs` is deferred. Generates or updates documentation from the diff + graph.

### Priority (from the user's decision)

**Plan + Test first.** These close the test-first loop: `plan` produces the ticket + tests,
`test` writes the failing tests, `patch` (or `developer`) makes them pass. Review mode already
exists. Brainstorm and docs are later.

---

## 12. Conclusions for this repo

### What to adopt

1. **Graph retrieval** — Aider-style passive injection first (phase 3), Prometheus-style
   active tools for Developer mode (phase 7). We have the graph; it is stale and unused.
2. **Compaction** — OpenCode's two-phase surgical pattern (phase 4). Prune verbose old
   outputs above a threshold, then LLM summarization via a cheaper model.
3. **Persistent memory** — auto-extract arbiter SETTLED decisions (phase 5). The arbiter
   already nominates them; nothing persists them.
4. **Plan + Test modes** — close the test-first loop (phase 6). `plan` produces a ticket JSON
   reviewed by panel+arbiter; `test` produces failing acceptance tests.
5. **Developer mode** — the autonomous localization+edit path (phase 8). Minimal tool set;
   SWE-agent ACI principles; AutoCodeRover phase separation.
6. **Language agnosticism** — the loop driver, gates, and region extractor must contain zero
   language-specific strings. Everything language-specific lives in a `Profile`. Adding
   Python or TypeScript support is a new profile, not a fork. See plan §9.1.
7. **Model-by-capability registry** — a declarative mapping from role (implementer, reviewer,
   arbiter, compactor, planner, explorer, tester) to model, with capability and cost metadata.
   The arbiter must not be the same model as any reviewer. The compactor uses a cheap model.
   See plan §9.2.
8. **Token efficiency** — minimize token usage as a first-class goal. Per-round input budget
   (default 40K; compaction runs before the call if exceeded), per-role output caps enforced
   via the registry, settled decisions injected as 1-line summaries, graph context capped at
   3K tokens. Target: a 4-round ticket under 200K total tokens, down from unbounded ~400K+.
   See plan §9.3.
6. **Plan + Test modes** — close the test-first loop (phase 7). Plan produces ticket JSON;
   Test produces failing acceptance tests.

### What to keep

1. **Panel + arbiter + settled-decisions + thrashing detection** — the moat. No harness in
   the field has this combination.
2. **Scaffold-driven, 0-tools (patch mode)** — matches Aider; safe and proven.
3. **The 5-rung gate ladder** — protected paths, static, compile, test, lock-scope. The
   deterministic backbone.
4. **The ticket model** — defect + regions + acceptance tests. The contract.

### What to skip

1. **Tree search (MCTS)** — Moatless and DARS-Agent. Too expensive for our use case; multi-
   attempt retry with arbiter filtering is sufficient.
2. **Embedding-based retrieval** — Moatless. The graph is better for code structure; embeddings
   add a second retrieval system without adding signal.
3. **Browser tool** — OpenHands. Not needed for our domain.
4. **Full autonomous shell** — OpenHands/mini-swe-agent. Too dangerous for a repo with a
   real-money AddOn; the minimal Developer tool set is the line.
5. **Background memory extraction from general rollouts** — Codex. Too broad; our scoped
   extraction (arbiter SETTLED only) is tighter and safer.

### The way forward

The research is done. The plan is in [AGENT_LOOP_V2_PLAN.md](AGENT_LOOP_V2_PLAN.md). The
sequencing rationale: fix the state machine first (so the loop's recorded state is honest),
then re-index the graph (so the graph is fresh), then wire passive retrieval (cheapest
graph win), then compaction (so long tickets do not crash), then memory (so adjudication
precedents persist), then Plan + Test modes (close the test-first loop), then Developer mode
(the autonomous path that depends on everything before it).

---

## References

- Rombaut, B. "Inside the Scaffold: A Source-Code Taxonomy of Coding Agent Architectures."
  arXiv:2604.03515v2, Apr 2026.
- Yang et al. "SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering."
  NeurIPS 2024. arXiv:2405.15793.
- Jimenez et al. "SWE-bench: Can Language Models Resolve Real-world Github Issues?" ICLR 2024.
- OpenAI. "Harness engineering: leveraging Codex in an agent-first world." Feb 11, 2026.
- LangChain. "The Anatomy of an Agent Harness." 2026.
- Puppygraph. "Agent Harness: What It Is and How to Build One." Jul 2026.
- Augment Code. "Harness Engineering for AI Coding Agents." Apr 2026.
- Aider. "Repository map." aider.chat/docs/repomap.html.
- SWE-agent. "Agent Computer Interface (ACI)." github.com/SWE-agent/SWE-agent/blob/main/docs/background/aci.md.
- SWE-bench leaderboard. swebench.com.

---

*End of AGENT_LOOP_RESEARCH.md. The execution plan is in
[AGENT_LOOP_V2_PLAN.md](AGENT_LOOP_V2_PLAN.md). The current loop is documented in
[AGENT_PATCH_LOOP.md](AGENT_PATCH_LOOP.md).*