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

## Reviewer calibration

The panel has two reviewers and an arbiter. Each round, the reviewers
find things and the arbiter rules on them. The question that justifies
the panel's cost is: *is the second reviewer worth it?*

### The wrong metric

The obvious metric — "upheld findings per dollar per reviewer" — rewards
the wrong behavior. A reviewer that always says APPROVE has a perfect
cost-to-finding ratio: zero findings, zero cost, zero false positives.
That's not a good reviewer. That's a rubber stamp.

A reviewer that restates what the first one found also looks productive:
lots of findings, lots of upheld. But it adds zero marginal value — the
first reviewer would have caught the same thing. The cost is real; the
value is not.

### The right metric

The metric that matters is: **upheld findings the *other* reviewer
missed**. This is the marginal value of the second reviewer. A reviewer
that finds things the other one didn't, and the arbiter upholds them, is
earning its cost. A reviewer that only restates what the first one found
is pure cost.

More precisely:

```
marginal_value(reviewer_A) = |upheld findings A found that B missed|
marginal_value(reviewer_B) = |upheld findings B found that A missed|
```

If both numbers are zero, the second reviewer is redundant. If both are
high, the panel is worth its cost. If one is high and the other is zero,
you have one good reviewer and one rubber stamp.

### The deeper problem

The arbiter's rulings are not ground truth. An UPHELD finding means "the
arbiter thinks this is real," not "this is actually real." The arbiter is
a model, not an oracle. The only ground truth is: does the patch lose
money? You can't measure that in a loop.

So the calibration metric is a proxy: *do arbiter-upheld findings
correlate with tickets that converge faster?* If tickets with more
upheld findings converge in fewer rounds, the arbiter is doing its job.
If tickets with more upheld findings take *more* rounds, the arbiter is
upholding noise — sending the implementer on wild goose chases.

This is a correlation measurable from the ledger: compare `upheld_count`
per ticket to `rounds_to_converge` per ticket.

### What the report command shows

The `report` command (item 1 below) prints:

1. **Per-reviewer marginal value**: upheld findings the other reviewer
   missed, per ticket. Rolling average across the last N tickets.
2. **Arbiter calibration**: correlation between upheld count and rounds
   to converge. If the correlation is positive (more upheld = more
   rounds), the arbiter is upholding noise. If negative (more upheld =
   fewer rounds), the arbiter is filtering real findings that speed
   convergence.
3. **Gate-failure distribution**: which rung catches the most, which
   never fires. A linter rung that never fires is a linter rung that
   costs nothing and finds nothing. A compile gate that catches 50% of
   candidates means the implementer is producing broken code half the
   time.
4. **Cost per verdict**: average cost of an APPROVE ticket vs an
   ESCALATE ticket. If ESCALATE costs 4x APPROVE, escalation is
   expensive — and the arbiter prompt should be tuned to decide earlier.

### What the replay corpus adds

The replay corpus (item 2 below) turns calibration from a correlation
into a regression test. Freeze a dozen tickets with known outcomes.
Change the arbiter prompt. Run the replay. See which tickets flip
verdict. If the new prompt upholds more findings but the same tickets
converge, the arbiter is better. If the new prompt upholds more findings
and tickets take *more* rounds, the arbiter is worse.

---

## What we built (all 8 items done)

### 1. `report` command — DONE

`agent-loop --mode report` reads `ledger.jsonl` and `learning_feedback.jsonl`
and prints: cost per ticket, rounds distribution, gate-failure distribution,
per-reviewer marginal value, arbiter calibration, verdict distribution.

**Files**: `src/agent_loop/report.py`, `cli.py` (`--mode report --report-last N`)

### 2. Replay corpus — DONE

`agent-loop --mode replay` re-runs the panel and arbiter against recorded
implementer outputs. Compares recorded vs replayed verdict. Reports flips.

**Files**: `src/agent_loop/replay.py`, `cli.py` (`--mode replay`)

### 3. Prompt caching — DONE

`_add_cache_control()` marks the last user message as cacheable. On cache
hit (round 2+), Anthropic bills input tokens at 10%.

**Files**: `src/agent_loop/providers.py` (`_add_cache_control`, `_call_anthropic`)

### 4. Linter rung — DONE

`check_lint()` runs the profile's `lint_cmd` between static and compile gates.
Every finding a linter can make is a finding not paid to a model.

**Files**: `src/agent_loop/gates.py` (`check_lint`), `profiles.py` (`lint_cmd`), `loop.py`

### 5. `Finding.signature` fix — DONE

Replaced the crude 8-word signature with a 200-char prefix of the finding
text, lowercased and whitespace-collapsed. Thrashing detector no longer
fires on reworded findings.

**Files**: `src/agent_loop/loop.py` (`Finding.signature`)

### 6. Bounded region escalation — DONE

Implementer can emit `<<<NEED-REGION file=... anchor=... why=...>>>`. The
loop resolves it, re-checks against protected paths, and re-prompts.

**Files**: `src/agent_loop/loop.py` (NEED-REGION parsing), `regions.py` (dynamic addition)

### 7. Mutation gate — DONE

`check_mutation()` runs a mutation tool scoped to the patched region after
tests pass. Directly answers: do the acceptance tests constrain the new code?

**Files**: `src/agent_loop/gates.py` (`check_mutation`), `profiles.py` (`mutation_cmd`), `loop.py`

### 8. Docs mode — 4 sub-modes — DONE

**Goal**: generate documentation from the codebase, not just from a diff.

The original docs mode was a single-pass diff-to-markdown generator. The
new docs mode supports four documentation types, each with a different
input and output:

| Sub-mode | Input | Output | Use case |
|---|---|---|---|
| `changelog` | git diff | changelog entry (Added/Fixed/Changed/Removed) | "What changed in this commit?" |
| `handover` | session ledger + git state | handover document (done/remaining/traps/next steps) | "What did I do, what's left?" |
| `design` | feature idea + graph context | design document (problem/approach/alternatives/impact/open questions) | "How should we build this?" |
| `prd` | defect/feature + graph context | product requirements document (background/requirements/acceptance criteria/out-of-scope/risks) | "What are we building and why?" |

All sub-modes use the graph context (callers, callees, types) when the
profile has `graph_project` set. The `design` and `prd` sub-modes use the
graph to answer "what existing code does this touch?" — the same graph the
loop uses for passive context injection.

**Reference**: the documentation architect skill
(`.agents/skills/doc-architect` or equivalent) defines the conventions for
documentation structure — section headers, ADR format, handover format. The
docs mode follows these conventions in its system prompts. When the skill
is available, its conventions should be injected into the docs mode's
system prompt to ensure generated docs match the project's established
format.

**Files**: `src/agent_loop/docs_mode.py` (4 sub-modes: `_run_changelog`,
`_run_handover`, `_run_design`, `_run_prd`), `cli.py` (`--mode docs
--docs-type changelog|handover|design|prd`)

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