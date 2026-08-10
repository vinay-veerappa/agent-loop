# Roadmap — What to Build Next

**Purpose**: record the agreed sequence of work after Phase 9, the rationale
for each item, and — equally important — what was considered and rejected.

**Created**: 2026-08-10, after the Claude review of Phase 9.

---

## Context

The agent-loop package (v0.1.0) has 9 phases complete, 129 tests passing,
and a 3-model cross-review done. The loop bootstrapped itself: it ran a
ticket against its own source, passed all gates, and both reviewers
unanimously approved.

The question now is: what makes this an instrument instead of an artifact?

---

## What we are building (agreed sequence)

### 1. `report` command over existing JSONL — HALF DAY

**What**: a `agent-loop report` command that reads `ledger.jsonl` and
`learning_feedback.jsonl` and prints:
- Cost per ticket (input, output, cache-read, total USD)
- Rounds per ticket (distribution: 1-round, 2-round, 3-round, 4-round)
- Gate-failure distribution (which rung catches the most, which never fires)
- Per-reviewer upheld/rejected/out-of-scope rates
- Per-ticket verdict distribution (APPROVE, ARBITER_SHIP, ESCALATE, etc.)

**Why now**: the data is already on disk. The JSONL format is structured.
Nothing reads it back except `build_learning_context`, which uses 5 entries
for prompting. A report command makes the loop's behavior measurable.

**What this enables**: "is the second reviewer worth its cost?" becomes
answerable. A reviewer that only ever restates what the first one found is
pure cost, and right now you'd never know.

**Key metric**: per-reviewer upheld findings the *other* reviewer missed.
This is the marginal value of the second reviewer. A reviewer that always
says APPROVE has a perfect cost-to-finding ratio — but zero marginal value.
The metric must reward finding things the other reviewer missed and the
arbiter upheld.

**Caveat**: the arbiter's rulings are not ground truth. The arbiter is a
model, not an oracle. An UPHELD finding means "the arbiter thinks this is
real," not "this is actually real." The only ground truth is: does the
patch lose money? You can't measure that in a loop. So the calibration
metric is: *do arbiter-upheld findings correlate with tickets that converge
faster?* That's a correlation measurable from the ledger.

**Files touched**: new `src/agent_loop/report.py`, `cli.py` (add `--mode
report`).

---

### 2. Replay corpus from existing artifacts — DAY

**What**: a `--replay <dir>` flag that re-runs the panel and arbiter against
recorded implementer outputs. The loop already writes `r{N}_review_*.txt`,
`r{N}_arbiter.txt`, `r{N}_impl_raw.txt` to disk. A replay command would:
1. Load the recorded implementer output from `r{N}_impl_raw.txt`
2. Run the current panel (with current prompts) against it
3. Run the current arbiter against the panel's findings
4. Compare the new verdict to the recorded verdict

**Why now**: the 129 tests pin mechanics. They say nothing about whether a
prompt change makes the arbiter better. A replay corpus turns prompt changes
from vibes into measurements. Freeze a dozen real tickets with known
outcomes. Now "I changed the arbiter prompt" has an answer that isn't
guesswork.

**What this enables**: regression-testing the loop's *judgment*. Change the
arbiter prompt, run the replay corpus, see which tickets flip verdict.

**Files touched**: `loop.py` (add replay path), `cli.py` (add `--replay`
flag).

---

### 3. Prompt caching in `_call_anthropic` — ~20 LINES

**What**: add `"cache_control": {"type": "ephemeral"}` to the system message
and the last user message in the Anthropic request. On cache hit, input
tokens bill at 10% (cache_read vs cache_creation).

**Why now**: the system prompt + implement prompt + region source are stable
across all rounds of a ticket. That's a textbook cacheable prefix. On Opus
that's a 90% cut on the largest part of every round-2+ call. ~20 lines.

**How it works per provider**:
- **Anthropic**: explicit `cache_control` blocks in the request. Mark which
  message parts are cacheable. On cache hit, input tokens bill at 10%.
  This is the big one — ~20 lines in `_call_anthropic`.
- **Ollama (cloud models)**: no explicit cache control needed. The Ollama
  server auto-caches the prompt prefix. `num_ctx` and the prompt prefix are
  already stable across rounds (same system + same history structure). No
  code change needed — it's already happening server-side.
- **OpenAI-compatible**: `prompt_cache` is automatic on the server side for
  requests with the same prefix. No explicit cache control needed. Already
  happening.

**Files touched**: `providers.py` (`_call_anthropic`).

---

### 4. Linter rung between static and compile — SMALL

**What**: a new gate rung that runs the profile's linter command (if
configured) between the static gate and the compile gate. The linter catches
style violations, unused imports, and simple type errors that the model
produces — before they reach the compiler or the panel.

**Why now**: every finding a linter can make is a finding you're paying a
model to make and an arbiter to adjudicate. Cheap, deterministic, and it
shrinks the panel's surface. The linter runs before compile (cheaper) and
before tests (faster feedback).

**Profile field**: `lint_cmd` (optional). If not set, the rung is skipped
(same pattern as `build_cmd` and `test_cmd`).

**Files touched**: `gates.py` (add `check_lint`), `profiles.py` (add
`lint_cmd`), `loop.py` (add rung to gate ladder).

---

### 5. `Finding.signature` fix — SMALL

**What**: replace the crude "first eight alphabetic words, lowercased"
signature with an arbiter-based same-finding check. The thrashing detector
currently uses `Finding.signature` to detect overlap between rounds. If a
reviewer rewords the same finding, the signature changes, overlap drops to
zero, and the thrashing detector fires — escalating a ticket that's
actually converging.

**The fix**: ask the arbiter "is finding #3 the same as last round's #7?"
instead of comparing word signatures. The arbiter already sees every
finding; asking it for same-finding rulings makes convergence detection
meaningful.

**Why now**: this is a real bug in thrashing detection. A reviewer that
rephrases its findings (which models do) defeats the convergence check.

**Files touched**: `arbiter.py` (add same-finding ruling to
`ARBITER_SYSTEM`), `loop.py` (use arbiter same-finding rulings in
`_history_note`).

---

### 6. Bounded region escalation — MEDIUM

**What**: let the implementer emit a `<<<NEED-REGION file=... anchor=...
why=...>>>` request. The loop resolves it, re-checks it against protected
paths, and re-prompts. This lets the implementer say "I discovered I also
need to edit this function" without aborting the ticket.

**Why now**: a region is a unique-anchor text span. The implementer cannot
add a method, touch an import, or edit a second place it discovers it
needs. This is the design's hard dead end. Bounded escalation removes it
while keeping the region contract and the gate-0 guarantee.

**Files touched**: `loop.py` (add NEED-REGION parsing and re-extraction),
`regions.py` (add dynamic region addition).

---

### 7. Mutation gate on the patched region — MEDIUM

**What**: after the test gate passes, run a mutation testing tool scoped to
the patched region only. The mutation gate directly answers the question the
test gate can't: do the acceptance tests actually constrain the new code?

**Why now**: mutation testing beat review on the NT8 addons — it found a
real defect, two unreachable guards, and a lying harness. That's the
established evidence standard, and it's currently outside the loop.

**Caveat**: even scoped to one region, running a mutation tool adds 30-60
seconds per round. On a 4-round ticket, that's 2-4 minutes of added latency.
The payoff is real but the cost is not trivial.

**Profile field**: `mutation_cmd` (optional). If not set, the rung is
skipped.

**Files touched**: `gates.py` (add `check_mutation`), `profiles.py` (add
`mutation_cmd`), `loop.py` (add rung after tests).

---

## What we are NOT building (and why)

### Tree-sitter for region extraction — NOT NOW

**What was considered**: replace the hand-rolled brace matcher and indent
finder in `regions.py` with a tree-sitter AST parse.

**Why not now**: the graph (`codebase-memory-mcp`) already uses tree-sitter
internally. But the loop's `regions.py` can't call the graph's tree-sitter
because the graph is an MCP server, not a Python library. Adding
tree-sitter as a Python dependency to `agent-loop` would give accurate
region extraction for all languages — but the replay corpus (item 2) will
tell us whether region extraction is actually a problem in practice. If
10% of tickets fail because the brace matcher breaks, that's worth fixing.
If 2% fail, it's not worth the dependency. Measure first.

**The codebase-memory-mcp graph already uses tree-sitter**. That's how
`trace_call_path` and `search_graph` work. The graph is the source of truth
for code structure. The loop's `regions.py` is a fallback for when the graph
is unavailable. Adding tree-sitter to `regions.py` would duplicate the
graph's tree-sitter — two sources of truth for the same data.

**Decision**: don't add tree-sitter to `regions.py` until the replay corpus
shows it's needed. The `guard_unsupported_syntax` function refuses files it
can't parse (e.g., C# files with `@"verbatim"` strings). This is a known
limitation, not a bug.

---

### Secret redaction in prompts — NOT NEEDED

**What was considered**: a `--redact` flag that masks string literals before
sending source to cloud models.

**Why not**: the user confirmed this is not needed for this codebase. The
NT8 RiskGuard source is not secret. The trading logic is in the indicators
and strategies, not in the AddOn's risk-guard code.

**Decision**: not building. If the user's requirements change, this is a
small addition (~50 lines in `providers.py`).

---

### More modes — NOT NOW

**What was considered**: adding more modes beyond the 7 existing (patch,
review, plan, test, developer, brainstorm, docs).

**Why not**: seven modes is already a lot. The `brainstorm` and `docs`
modes are thin — they're single-pass generators without the gate ladder.
Consolidating existing modes earns more than adding new ones. The
genuinely novel thing is separating detection from adjudication plus a
cost-ascending deterministic ladder. Nothing else fills that niche.
Broadening dilutes it.

**Decision**: don't add more modes. Focus on making the existing modes
measurable (report), regression-testable (replay), and cheaper (caching,
linter).

---

### Auto-chaining review → plan → test → patch — NOT NOW

**What was considered**: automatically chaining modes so a defect flows
through plan → test → patch without human intervention.

**Why not**: compounding LLM stages compounds error. A bad plan produces a
bad test, which produces a bad patch. The human between review and plan is
doing real work — they're checking the plan's regions against the actual
defect. Removing that check would make the loop less reliable, not more.

**Decision**: don't auto-chain. The human is the final gate.

---

### Parallel tickets — NOT NOW

**What was considered**: running multiple tickets in parallel using separate
worktrees.

**Why not**: the repo-global run lock serializes tickets. Worktrees would
allow parallel execution, but promote conflicts (two tickets patching the
same file) and a shared learning store (two tickets writing to
`learning_feedback.jsonl` concurrently) make the payoff smaller than it
looks. The atomic writes in `memory.py` prevent corruption, but the
learning context would be inconsistent between parallel tickets.

**Decision**: don't add parallel tickets. Serial execution is simpler and
the learning feedback is more coherent.

---

### A general agent framework — NOT NOW

**What was considered**: generalizing the loop into a framework for any
agent task (not just software engineering).

**Why not**: the loop's value is in its domain-specific design: the gate
ladder, the adversarial panel, the adjudicating arbiter, the settled-
decisions cache. Generalizing would dilute these features. The package is
already language-agnostic (via profiles) and provider-agnostic (via
providers.py). That's the right level of generality.

**Decision**: don't generalize. The loop is a software engineering tool, not
a general agent framework.

---

## Why not add tree-sitter on top of codebase-memory-mcp?

**The question**: "why do we need tree-sitter on top of codebase-memory-mcp?"

**The answer**: we don't.

The graph (`codebase-memory-mcp`) already uses tree-sitter internally to
build the AST. That's how `trace_call_path` and `search_graph` work. The
graph is the source of truth for code structure.

The loop's `regions.py` uses a hand-rolled brace matcher + indent finder
for region extraction. This is a fallback for when the graph is unavailable
(or not yet indexed). It's not the primary source of truth — the graph is.

Adding tree-sitter to `regions.py` would:
1. Duplicate the graph's tree-sitter (two sources of truth for the same
   data)
2. Add a native dependency (tree-sitter requires compiled language
   grammars)
3. Solve a problem we haven't measured yet (how often does the brace
   matcher actually fail?)

The `guard_unsupported_syntax` function refuses files it can't parse (e.g.,
C# files with `@"verbatim"` strings). This is a known limitation, not a
bug. The replay corpus (item 2) will tell us how often this happens in
practice.

**Decision**: don't add tree-sitter to `regions.py`. Use the graph (via
MCP) as the primary source of truth for code structure. Use the brace
matcher as a fallback. If the replay corpus shows the brace matcher fails
>10% of the time, revisit this decision.

---

*End of roadmap. Update as items are completed.*