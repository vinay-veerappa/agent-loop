# Improvement Research & Ideas

**Date**: 2026-08-21
**Status**: Research complete, implementation deferred

This document captures the research, brainstorming, and measurement data from
the arbiter redesign session, plus the broader improvement ideas identified by
reviewing all 32 consumer findings against the current research literature.

---

## 1. The Arbiter Problem

### The measured failure

The shipped arbiter (deepseek-v4-pro, then mistral-large-3) caught 0-2/5 correct
findings on the labelled O3 corpus. The best model (mistral) caught 2/5. The
worst (deepseek-v4-pro, qwen3.5, glm-5.2 with think=True) caught 0/5.

The problem was **false negatives** (correct findings rejected), not false
positives (wrong findings upheld — zero across all 42 runs in the sweep).

### Root cause: the task, not the model

The old arbiter prompt asked "is this finding correct?" — a semantic judgment
that requires understanding intent. LLMs cannot do this reliably, so they
default to conservatism and reject everything. The 2026 Springer study
("Are LLMs Reliable Code Reviewers?") measured this as **systematic
overcorrection**: when asked to judge + explain + propose a fix, LLMs become
overly conservative and reject correct implementations.

### The fix: inverted arbiter (implemented 2026-08-21)

The arbiter's job changed from "uphold correct findings" to "reject
demonstrably wrong findings." The burden of proof reversed: a finding must be
proven WRONG to be dropped, not proven correct to be kept.

Five concrete rejection criteria:
1. Contradicts a mechanical gate
2. Code doesn't exist in the patch
3. Out of scope (named in ticket's scope block)
4. Restates a settled decision
5. Mechanism doesn't hold

**Measured improvement** (O3 corpus, 5 correct findings, 3 reps each):

| Model | Old (uphold) | New (reject) | FP | Time |
|---|---|---|---|---|
| deepseek-v4-flash | 0.0/5 | 5.0/5 | 0 | 1s |
| deepseek-v4-pro | 0.0/5 | 5.0/5 | 0 | 2s |
| glm-5.2 | 0.7/5 | 5.0/5 | 0 | 3s |
| kimi-k3 | 0.3/5 | 5.0/5 | 0 | 2s |
| qwen3.5 | 0.0/5 | 5.0/5 | 0 | 6s |
| minimax-m3 | N/A | 4.7/5 | 0 | 59s |
| kimi-k2.7-code | 2.3/5 | 3.7/5 | 0 | 2s |
| mistral-large-3 | 2.0/5 | 3.0/5 | 0 | 7s |
| gemini-3.7-flash-high | N/A | 3.0/5 | 0 | 39s |

Zero false positives across all 42 runs. Default arbiter: `qwen3.5:cloud`
(5.0/5, 6s, perfectly stable, different family from glm and deepseek).

### Approaches tested and rejected

**Serial gauntlet** (SentinelOne's approach): reviewers run serially, each
must AGREE to keep a finding. WORSE than the current arbiter because it's a
one-way ratchet — a correct finding missed by one reviewer is gone forever.
2-reviewer gauntlet caught 1/5; 3-reviewer caught 2/5. The agreement
requirement can only remove findings, never recover them.

**Grounded arbiter** (full file context): COUNTERINTUITIVELY WORSE. More
context gave the arbiter more surface to rationalize rejections. mistral went
from 3/5 to 0/5 with full file context. The problem is the task ("is this
correct?"), not the context.

### Key references

- "Are LLMs Reliable Code Reviewers? Systematic Overcorrection in Requirement
  Conformance Judgement" (Springer, 2026) — measured the overcorrection bias
  and proposed the Fix-guided Verification Filter
- "Building an Adversarial Consensus Engine" (SentinelOne, 2026) — serial
  consensus pipeline with active rejection mandate
- "LLM-as-a-Judge in Multi-Agent Systems" (Medium, 2026) — "a judge is only
  as good as the artifacts it can inspect and the rubric it is asked to apply"
- "Emergence of Biased Consensus in Multi-Agent LLM Debate" (NeurIPS 2025) —
  multi-agent debate AMPLIFIES individual biases
- "Reliability without Validity" (arXiv 2026) — the consistency-bias paradox:
  the most reproducible judges are among the most biased

---

## 2. Self-Verification by the Implementer

### The idea

Ask the implementer to verify its own output against the ticket spec before
submitting to the gates — not "is this good?" but "did you change anything
you weren't asked to?"

### What the research says

**Naive self-review makes output WORSE.** Huang et al. (2023) proved that
without external feedback, LLMs "fix" correct answers into wrong ones more
often than they fix actual errors. The Self-Refine paper (2023) showed
modest gains on objective tasks but degradation on creative tasks, at 3x
token cost.

**What works is verification — giving the model NEW information it didn't
have during generation.** Every "self-correction" success is actually a
verification success:
- Reflexion (91% on HumanEval) fed **test execution output** back — not
  introspection
- CRITIC used **external tools** to verify claims — removing tools eliminated
  gains
- Constitutional AI used **principles as an external rubric** — constrained
  verification, not open-ended introspection

The conclusion: "Reflection without verification is an LLM talking to itself
in a mirror, confidently repeating its hallucinations in slightly more
grammatical sentences."

### The design that would work for our loop

A **targeted self-verification step** between block parsing and the gate
ladder:

1. After `parse_blocks(raw)`, before `check_static`, build a verification
   prompt with:
   - The ticket spec (what was asked)
   - The original region text (what was there before)
   - The model's blocks (what it produced)
   - Three factual questions:
     - "Did you change any line you were NOT asked to change?"
     - "Did you guess at any symbol you could not see in the provided regions?"
     - "Did you modify any comments, whitespace, or formatting you were not
        asked to touch?"

2. If the model says "yes, I changed things I shouldn't have" → re-emit
   with only the intended changes (one round, hard cap)

3. If the model says "no" → proceed to the gates as normal

**Why this works**:
- The original region text is an **external reference** — the model compares
  its output against something it didn't have when it wrote the replacement
- The ticket spec is an **external rubric** — "did you change what you were
  asked to change?" is a constrained question, not "is this good?"
- The questions target the **measured failure modes** (CF-25: comment
  rewrites, CF-31/32: symbol guessing, CF-25: non-ASCII stripping)
- It's **one round, not iterative** — the research says cap at one round

**When NOT to use it**:
- No external feedback signal (the Huang et al. rule)
- Open-ended "is this good?" questions
- More than one refinement round (3x cost, diminishing returns)

### Alternative: Best-of-N sampling

The research recommends this even more: generate 3 independent candidates,
run the gates on each, pick the best. "Generating 5 candidates and picking
the best often outperforms taking 1 candidate and refining it 5 times, at
similar total token cost."

For our loop: generate 3 patches in parallel (3 implementer calls), run
static+compile on each, pick the one that passes all gates. If none pass,
pick the one that got furthest. This is the "anti-reflection" approach.

### Key references

- "Large Language Models Cannot Self-Correct Reasoning Yet" (Huang et al.,
  2023) — self-correction without external feedback hurts accuracy
- "Self-Refine: Iterative Refinement with Self-Feedback" (Madaan et al.,
  2023) — modest gains on objective tasks, degradation on creative, 3x cost
- "The Research on LLM Self-Correction" (Vadim's blog, 2026) — comprehensive
  review separating verification from introspection
- "Self-Correcting Code Generation Using Small Language Models" (arXiv,
  2025) — SETS framework combining sampling, self-verification, self-correction

---

## 3. Mutation Testing Gate

### The idea

After the test gate passes, inject deliberate faults (mutations) into the
patched code and run the acceptance tests. If a mutant survives (tests still
pass), the acceptance tests do not detect that behavior change — they are
structurally blind to that code path.

### Why it matters

CF-32's core insight: "a guessed symbol the acceptance tests do not
discriminate ships unverified." The acceptance tests set the snapshot field
directly and never exercised the population path. The patch read
`account.Positions` instead of `state.Positions` — functionally wrong, but
every test passed because the tests didn't exercise the population code.

A mutation gate would have caught this: mutate the data source
(`state.Positions` → `account.Positions`), run the tests, observe the mutant
survives, warn "acceptance tests do not detect this behavior change."

### The research

- "All Smoke, No Alarm: Oracle Signals in Agent-Authored Test Code"
  (arXiv, 2026) — 86,156 test-file patches across 2,807 repos, 80.2% contain
  weak or no explicit oracle signals. "Coding agents generate test structure
  far more reliably than they generate oracle logic."
- "Mutation Testing for AI-Generated Code" (Augment, 2026) — mutation score
  is a stronger quality signal than line coverage. AI-generated tests can
  reach high coverage while killing far fewer mutants.
- Google: generated almost 17 million mutants across 760,000 code changes,
  surfacing 2 million to developers during code review.
- Atlassian: production workflow instructs the LLM to implement tests, rerun
  mutation tests, and verify whether coverage improved.

### The design

1. After the test gate passes, apply a set of mutations to the patched code
   (e.g., change `==` to `!=`, swap `state.Positions` to `account.Positions`,
   negate a condition)
2. Run the focused acceptance tests against each mutant
3. If a mutant survives, warn: "acceptance test(s) did not detect mutation
   at `<line>` — the test may not discriminate this behavior"
4. The warning is advisory (does not block) — the operator decides whether
   the surviving mutant is a real gap or an equivalent mutant

### The consumer already has this

The `nt8-riskguard` repo uses mutation batteries anchored on exact source
strings. The loop could integrate with the consumer's existing mutation
framework rather than generating its own mutations.

---

## 4. Pre-Flight Scope Check

### The idea

After `regions.apply()` and before the static gate, diff the applied changes
against the region boundaries. If any changed line falls outside a region's
`[start_line, end_line]` range, fail: "the patch modified lines outside the
ticket's regions."

### Why it matters

The comment-drift gate (CF-25) catches comment-only changes, but a general
"did this patch touch lines outside every region" check doesn't exist. The
model could change code outside its regions and no gate would notice — the
static gate checks block shape, not whether the block matches the region
boundaries.

### The research

- Autonoma (2026): "the blast radius of an agent PR is wider per
  line-of-intent than an equivalent human change"
- Codacy (2026): "a deterministic enforcement layer between generation and
  acceptance" — verify every change as a diff against region boundaries

### The design

`regions.apply()` already knows the original line ranges. After applying,
`ws.diff()` shows what actually changed. Compare the diff hunks against the
region boundaries:
- Every changed line in the diff must fall within a region's
  `[start_line, end_line]` range
- A line that changed outside every region → fail with "patch modified
  lines outside the ticket's regions at `<file>:<line>`"

This is a deterministic check — no model call, no judgment, just comparing
line numbers.

---

## 5. Test Oracle Strength Checking

### The idea

Parse the acceptance test source and classify assertions by strength:
- **No assertion**: the test runs code but doesn't check the result
- **Weak assertion**: checks non-null, type, truthiness
- **Strong assertion**: checks specific values, comparisons, exceptions

Warn when all assertions are weak — the test executes code but doesn't
verify behavior. This catches "test theater": tests that look like tests
but don't actually test anything.

### Why it matters

The 2026 oracle signal study found 80.2% of agent-authored test patches
contain weak or no explicit oracle signals. CF-32's acceptance tests had
assertions, but they set the snapshot field directly — the assertions were
strong but the test was structurally blind to the population path.

### The research

- "All Smoke, No Alarm" (2026) — strong oracle signals correlate with
  higher merge likelihood after controlling for PR complexity
- "Human Oversight for AI-Generated Test Artifacts" (ITEA, 2026) —
  "coverage can suggest progress even when expected results are wrong,
  oracles are weak, or assertions merely confirm the same flawed assumption
  used to generate the code"

### The design

A static analysis gate that parses the acceptance test source (already
extracted by `extract_test_sources`), identifies assertion patterns
(`assertEqual`, `assertIsNotNone`, `assert result is not None`, etc.),
classifies each as weak/strong, and warns when all assertions are weak.
This is language-specific but deterministic.

---

## 6. Deterministic Verification Layer

### The idea

The research (Codacy, 2026) argues: "the same system that generates your
code should not be the one to review it. A deterministic enforcement is
required." The loop already has deterministic gates (static, compile, test,
lock-scope, comment-drift), but the panel still has veto power over patches
that pass every deterministic gate.

The architectural shift: make the deterministic gates the PRIMARY
verification layer, with the LLM panel as a secondary semantic layer. A
patch that passes every deterministic gate AND the self-verification step
can proceed to the panel — but the panel's findings are filtered by the
inverted arbiter (reject-only), not by an uphold-gate.

### Why it matters

The inverted arbiter already moved toward this model (reject-only, not
uphold-gate). The remaining gap: the panel can still block a patch that
passes every deterministic gate with findings the arbiter keeps. The
deterministic gates are facts; the panel's findings are opinions. Facts
should outrank opinions.

### The research

- Codacy (2026): "deterministic where it matters: clear pass/fail
  conditions for security rules, quality thresholds, and policy
  enforcement"
- "Compiled AI: Deterministic Code Generation for LLM-Based Workflow
  Automation" (arXiv, 2026) — four-stage validation pipeline (Security,
  Syntax, Execution, Accuracy) with regeneration on failure
- Anthropic (2026): "Demystifying evals for AI agents" — deterministic
  tests, LLM rubric, static analysis, state checks, tool-call verification
  as separate evaluation layers

---

## 7. Remaining Open Items from Consumer Findings

All 32 CF findings have fix commits. These are the ones with known residuals
or partial fixes:

| Finding | Status | Residual |
|---|---|---|
| CF-1 | Partially fixed (20→5 warnings) | SCOPE may still warn under specific conditions |
| CF-4 | Partially fixed | `--selftest` CLI flag absent; `python -m agent_loop.selftest` exists |
| CF-5 | Fixed, re-opened, fixed again | `git describe` records the real version; verify on next consumer run |
| CF-8 | Fixed | Em dashes in console output — verify on cp1252 console |
| CF-9 | Fixed | Refused run writes result.json — verify on next refused run |
| CF-11 | Fixed | Submodules populated when `.gitmodules` exists — verify on submodule repo |

### Backlog items still open

- **O21**: "A self-authored acceptance test can cover half a fix" — the
  red phase guarantees the gate CAN fail, but does not guarantee the test
  covers the change. Mutation testing (item 3 above) is the durable fix.
- **O7**: "Whole rungs are still unexercised end to end" — some modes have
  never been run against a real consumer repo (docs mode, brainstorm mode).

---

## 8. Summary: Priority Order

| Priority | Improvement | Effort | Impact | Status |
|---|---|---|---|---|
| DONE | Inverted arbiter | Medium | 0-2/5 → 5/5 | Implemented, committed, pushed |
| HIGH | Self-verification step | Low | Catches CF-25/31/32 at source | Designed, not implemented |
| HIGH | Mutation testing gate | Medium | Catches "test theater" (CF-32 core) | Designed, not implemented |
| MEDIUM | Pre-flight scope check | Low | Catches out-of-scope edits | Designed, not implemented |
| MEDIUM | Oracle strength checking | Low | Catches weak assertions | Designed, not implemented |
| MEDIUM | Deterministic-first architecture | High | Facts outrank opinions | Architecture decision, not started |
| LOW | Best-of-N sampling | High | Anti-reflection alternative | Research only |
| LOW | Remaining CF residuals | Low | Polish | Verify on next consumer run |