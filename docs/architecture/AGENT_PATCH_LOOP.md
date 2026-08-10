# Agent Patch Loop — formalised implement → gate → review → apply cycle

**Status**: **proven.** Closed T2, T3, T4 and T5 of the RiskGuard P0 phase end to end on
2026-08-06 (suite went 353/3-failing → 356/0). Live use surfaced seven defects in the loop itself:
three during T2 (§4.6, §4.7, the panel-validity bug in §4.1) and four during T3–T5 (§7), all fixed.
**Predecessor**: `scripts/agent_loop/ollama_patch_loop.py` — superseded, retained only so
in-flight artifacts stay readable. Three of its gates were defective (§4); do not use it.

> **The loop was not used for Phases B and C** (2026-08-07, defects `P2-28`, `P1-20`, `P1-37`,
> `P1-10`, `P1-35`, `P1-11`, `P1-15`). Those were written by hand, test-first. Two reasons, both
> worth knowing before reaching for the loop again: under test-first development the *test* is the
> hard part and `*Tests.cs` is a protected path the implementer cannot reach by construction; and
> each fix was a handful of lines whose difficulty was entirely in deciding what to change, not in
> writing it. The loop earns its cost on tickets with substantial mechanical edits behind a
> settled decision — not on small, high-judgement changes.
**Driving work**: [RISKGUARD_HARDENING_HANDOVER.md](RISKGUARD_HARDENING_HANDOVER.md).
**If something is broken, start at §8** (symptom → cause → fix).

```powershell
# confirm every ticket still resolves (free; run before paying for anything)
.\.venv\Scripts\python.exe -m scripts.agent_loop --list

# offline end-to-end check of the loop itself (free, ~2 min, no models)
.\.venv\Scripts\python.exe -m scripts.agent_loop.selftest

# run one ticket. Does NOT apply: it stops at ARBITER_SHIP for human sign-off.
.\.venv\Scripts\python.exe -m scripts.agent_loop --ticket T3 --arbiter glm-5.2:cloud

# promote a candidate you have read and accepted (see §9 for the checklist)
.\.venv\Scripts\python.exe -m scripts.agent_loop --ticket T3 `
    --resume-raw logs/agent_loop/T3/r2_impl_raw.txt --allow-unapproved --apply

# clean up worktrees left behind by a crashed run
.\.venv\Scripts\python.exe -m scripts.agent_loop --prune
```

> **Two rules that are not optional.**
> 1. **`ARBITER_SHIP` is a recommendation, not an outcome.** Nothing is applied until a human runs
>    `--apply`. Read the candidate first.
> 2. **A green gate ladder is not a closed defect.** The test gate only proves *no regression*.
>    T5 reached `ARBITER_SHIP` with its own acceptance test still red (§7.5).

---

## 1. What this is

A batch loop that applies a *ticket* — a defect description plus a set of source regions — by
having one model rewrite those regions, gating the result mechanically, having a panel of
different models review it adversarially, and having a stronger arbiter rule on their findings.

It is not an interactive pair-programmer. The unit of work is a ticket, the output is either an
approved patch or a pile of artifacts for a human arbiter, and every decision point in between is
recorded.

Unanimous APPROVE was the original bar for applying automatically. In practice it is unreachable
(§4.7) and the arbiter replaced it, so **in normal operation the loop ends at `ARBITER_SHIP` and a
human promotes**. Unanimous APPROVE still short-circuits to `APPROVE` when it happens; it just
rarely does.

### Why not just use an existing tool

Surveyed before rebuilding:

| Tool | Why it doesn't replace this |
|---|---|
| [Aider](https://aider.chat/docs/usage/lint-test.html) | Owns the write cycle and has the lint/test retry loop, but is an interactive pair-programmer: no ticket file, no verdict semantics, no unanimous-approve gate, no arbiter step. |
| [ng/adversarial-review](https://github.com/ng/adversarial-review), [alecnielsen/adversarial-review](https://github.com/alecnielsen/adversarial-review), [formin/multi-model-review](https://github.com/formin/multi-model-review) | All **review-only** — they critique code something else already wrote. None owns implement→gate→apply. |
| [LiteLLM](https://github.com/BerriAI/litellm) | Would replace `providers.py` wholesale, but is a large transitive dependency for ~3 calls per round. Rejected in favour of a ~200-line shim; revisit if provider count grows. |
| [tree-sitter](https://tree-sitter.github.io/) | Evaluated for `regions.py` and **rejected** — see §4. |

What is genuinely ours: ticket-scoped regions, a gate ladder whose mechanical results override
model verdicts, unanimous-approve-to-apply, and an orchestrator directive that outranks reviewers.

---

## 2. Module map

```
scripts/agent_loop/
  cli.py         entry point: python -m scripts.agent_loop
  loop.py        round driver, review panel, arbitration
  arbiter.py     rules on reviewer findings; non-convergence detector
  gates.py       the mechanical gate ladder
  workspace.py   git-worktree isolation, run lock, baseline freeze
  regions.py     locate + splice source regions
  providers.py   multi-provider chat shim
  profiles.py    domain prompts, gate config, settled decisions
  selftest.py    offline end-to-end exercise with model calls stubbed
  tickets_p0.json  the RiskGuard/Copier P0 tickets
  ollama_patch_loop.py   superseded predecessor - do not use
```

### `workspace.py`

Every ticket runs in its own `git worktree`, sharing the repo's object store. The live tree is
never written to; an approved patch is copied back in an explicit final step. This removes the
hazard described in §4.5 rather than documenting it — and it makes `git checkout --` *safe*,
because it is now scoped to a directory that holds nothing a human authored.

- **Run lock** is hand-rolled rather than `filelock`, for one reason: it records the holder's PID
  and treats a lock whose process is gone as stale. A crashed run must not leave a permanent lock.
- **`capture_baseline`** refuses to run against a dirty worktree — a baseline must describe
  unmodified code.
- **`export_patch`** writes a readable unified diff for the arbiter. `final_blocks.json` is
  JSON-escaped C# and unreadable; the arbiter is the last gate and deserves better.
- `--prune` cleans up worktrees left by a crashed run.

### `loop.py`

The round driver. Its two behavioural differences from the predecessor are both consequences of
the T2 post-mortem in §4.1: **a reviewer that did not answer has not voted**, and **the panel
carries a wall-clock deadline** rather than relying on a per-request timeout.

### `profiles.py`

Everything domain-specific — prompts, `build_cmd`/`test_cmd`, the lock name, protected paths.
Pointing the loop at a different codebase is a config change, not a fork.

`Profile.settled` carries decisions the arbiter has already made, injected into *every* review
round. The predecessor required a human to remember `--orchestrator-note` by hand, and the same
three false positives were re-litigated across rounds because nobody did.

### `providers.py`

One `chat()` over three backends, selected by a `backend:model` prefix. A bare name defaults to
`ollama`, so existing ticket files and CLI flags keep working.

```
ollama:kimi-k2.7-code:cloud      anthropic:claude-opus-5      openai:gpt-oss-120b
```

Returns a uniform `Completion` carrying text, token counts, stop reason, elapsed seconds and a
computed `cost_usd`. Two properties matter more than the transport:

- **`ProviderError` is distinct from a model answering.** Transport failure after retries, an
  Anthropic `stop_reason: "refusal"`, or a missing key all raise. A reviewer that could not be
  reached has *not voted*, and callers must not score it as dissent. This is the direct fix for
  the T2 failure in §4.
- **`temperature` is dropped on models that reject it.** Anthropic returns 400 for
  `temperature`/`top_p`/`top_k` on Opus 5, Sonnet 5, Fable 5 and Opus 4.7+. The loop asks for
  `temperature=0.1` for implementer determinism; sending it to those models would fail 100% of
  calls.

Retries use jittered exponential backoff on 408/409/429/5xx only — a 400 is our bug and fails
identically on retry.

Pricing table (USD per 1M in/out) is in `PRICING`; Ollama cloud models are subscription-billed and
cost 0 per token here.

### `regions.py`

Anchors on a declaration, never a line number, so a ticket written today still resolves after
unrelated commits land. Extent is found by brace matching over a comment/string stripper.

`guard_unsupported_syntax` refuses any file containing a C# verbatim string (`@"..."`) or a block
comment (`/* */`) — the two constructs the stripper cannot parse. This converts a silent bad
splice into a loud, actionable error. Neither construct exists in the addons today.

`apply()` splices bottom-up per file so earlier spans stay valid, and skips blocks identical to the
original so an unchanged region never dirties the file.

### `gates.py`

See §3.

---

## 3. The gate ladder

Cost-ascending, so a patch that leaked a marker or invented a symbol never reaches a paid reviewer.
**Every gate here is deterministic, and where a gate and the panel disagree, the gate wins.**

| # | Gate | Catches | Overrides APPROVE |
|---|---|---|---|
| 0 | **protected** | ticket regions that would let the patch edit its own grader | yes — refuses to run |
| 1 | **static** | missing/empty blocks, non-ASCII, unbalanced braces or `#if/#endif`, changed leading indentation, leaked markers | yes |
| 2 | **compile** | every invented symbol, wrong C# version, `#if` damage | yes |
| 3 | **test** | behavioural regressions, against a frozen baseline | yes |
| 4 | **lock-scope** | `Flatten`/`Cancel`/`Submit`/`CreateOrder` reachable under `lock (_stateLock)` | yes |
| 5 | panel | two reviewers of different families — **detection only** | — |
| 6 | **arbiter** | rules on the panel's findings; only upheld ones block | — |
| 7 | human | ships | — |

### Gate 0 — protected paths (anti reward-hacking)

The implementer is told to make the gates pass. Nothing in the loop's *shape* stops a ticket from
handing it the verifier itself. The literature is blunt about where that leads: verifiers are
[seldom robust and are gamed by deleting failing tests or monkey-patching the
verifier](https://arxiv.org/html/2606.26300v2), with [o3 reward-hacking in 30.4% of RE-Bench
runs](https://arxiv.org/pdf/2605.21384). So the verifier is made unreachable by construction:

```
DEFAULT_PROTECTED = ("*Tests.cs", "*.csproj", "scripts/agent_loop/*", "logs/agent_loop/*baseline*")
```

All five current tickets pass; regions aimed at `RiskGuardAddOnTests.cs`, the `.csproj`, or
`gates.py` are refused before any model is called.

### Gate 3 — test, against a frozen baseline

The suite is **not green and is not meant to be**: T4 and T5 have failing tests waiting that only
go green when those tickets land. A pass/fail gate would reject every candidate. What matters is
the *set* of failures.

- `[FAIL] <message>` lines are parsed into a set; `RESULTS: Passed = N, Failed = M` confirms the
  runner actually finished. A truncated run that happens to show no new failures proves nothing,
  so a missing `RESULTS` line fails the gate.
- The baseline is captured **once, before the first candidate is applied**, and never recomputed
  mid-run — otherwise a patch that breaks a test would simply widen the baseline and pass.
- Failures not in the baseline are regressions and fail the gate. Baseline failures that disappear
  are reported as progress — this is T4/T5's acceptance criterion, now machine-checked.

Current baseline (353 passed / 3 failed):

| Failing assertion | Defect |
|---|---|
| Exit copy is clamped to the follower's actual position (expected ≤ 1, got 5) | P0-5 |
| No copy is submitted to a RiskGuard-locked follower (got 1 order(s)) | P0-8 |
| Sub-one-contract micro→mini conversion is skipped rather than floored to 1 | P0-6 |

### Gate 4 — lock scope

Domain-specific and deliberately able to override an APPROVE: calling the broker under the state
lock is how this addon deadlocks with real money on the line, and reviewers have waved it through.

Implemented as an **ordered event scan** (lock keyword / open / close / risky call), not per-line
depth arithmetic — see §4 for why that distinction cost the project a working gate.

### Gates 5–7 — detection, adjudication, and shipping are three different jobs

The original design conflated the first two, and it does not work. Reviewers are told to *"assume
the implementer is confident and wrong"*, which makes them good at **detection** and structurally
incapable of **adjudication**: an adversarial reviewer has no stopping rule, so it always produces
something. Requiring unanimous APPROVE from two of them is not a high bar — on a region large
enough to keep offering new surface it is an unreachable one. §4.7 has the evidence.

**The arbiter** (`arbiter.py`) sees what neither reviewer does — the ticket, the patch, the
mechanical gate results, and *both* reviewers' findings together — and rules on each:

| Ruling | Meaning |
|---|---|
| `UPHELD` | Real, caused by this patch, blocks. Must state the concrete sequence that loses money or leaves a position unprotected. "Could be clearer" does not qualify. |
| `REJECTED` | The claimed mechanism does not hold, contradicts a gate, is already handled, or restates a settled decision. |
| `OUT_OF_SCOPE` | Real but pre-existing or another ticket's problem. This patch must fix its own defect without adding new ones — not everything wrong with the file. |

Then it recommends `SHIP`, `REVISE`, or `ESCALATE`. **Only upheld findings are fed back**, with an
explicit instruction not to make unrelated edits — feeding all of them is what drove the rewrite
churn that manufactured the next round's findings.

Its authority is bounded three ways, and deliberately:

- **It cannot overturn a mechanical gate.** Compile errors, test regressions and lock-scope
  violations are facts, not opinions.
- **It cannot ship.** `ARBITER_SHIP` writes the patch and a rationale, then stops. Promotion needs
  a human running `--allow-unapproved --apply`. On an addon that moves real money, a model does not
  get the last word on naked-position risk.
- **It cannot fake it.** A `SHIP` that skipped findings, or that contradicts its own `UPHELD`
  rulings, is downgraded to `ESCALATE` rather than trusted.

It also nominates recurring rejections for `Profile.settled`, so that list starts generating itself
instead of being hand-maintained.

**`arbiter.thrashing()`** stops a run that cannot converge: consecutive rounds with zero finding
overlap *and* no fall in blocking count means the implementer is complying, the reviewers are not
repeating themselves, and more rounds will not help. Verified against T2's real numbers
(13 → 14 → 13, zero overlap) with no false positive on a converging run (13 → 8 → 3).

---

## 4. Findings that motivated the rebuild

Each of these was verified against the real repo, not inferred.

### 4.1 The review panel could not approve anything (T2, 2026-08-06)

`parse_review("")` returns `{'verdict': 'REVISE', 'blocker_count': 0}` — an empty response is
scored as a dissenting vote, indistinguishable from a real one.

| Round | glm-5.2 | deepseek-v4-pro |
|---|---|---|
| 2 | REVISE, 3 blockers (3.7 KB) | **0 bytes** |
| 3 | REVISE, 4 blockers (6.1 KB) | **0 bytes** |
| 4 | **HTTP 502** | **HTTP 502** |

`unanimous_approve` requires *all* reviewers to say APPROVE, so the gate was closed from round 1
regardless of candidate quality. Round 4 had both reviewers 502 and the `except` fabricate
`REVISE`, so the final candidate was never reviewed by anything. ~2.5 hours and four implementer
rounds of cloud spend on a ticket with no reachable pass state.

There was also a **2h03m gap** between `r4_build` (16:16) and `r4_review` (18:19) despite
`timeout=900`, so the panel needs its own wall-clock deadline, not just a per-request one.

**Fixed.** `parse_review` now returns `UNPARSEABLE` for an empty or marker-less response and
`UNREACHABLE` for a transport failure; neither is a vote. A panel missing any vote is **invalid** —
the round is not decided, the candidate is kept, and the run stops with `PANEL_UNREACHABLE` plus a
`--resume-raw` hint, rather than being silently vetoed. `review_panel` also bounds the whole set of
calls with `panel_deadline` (default 1800s), not just each request.

Both are covered by `selftest.py`: scenario 3 replays the empty-reviewer case and scenario 4 the
double-502 case, and both must yield `PANEL_UNREACHABLE`.

### 4.2 The lock-scope gate was inert for 88% of its targets

```
ORIGINAL gate on Allman (brace on next line)  -> 0 flags  MISSED
ORIGINAL gate on K&R    (brace on same line)  -> 1 flag   caught

RiskGuardAddOn.cs: 28 Allman-style lock(_stateLock), 4 K&R-style
```

After matching `lock (_stateLock)`, the old code evaluated `depth <= depth_at_lock` before the
opening brace on the *next* line arrived — closing the scope before it opened, so nothing inside
was examined. This codebase is Allman-braced throughout.

**Fixed**: ordered event scan, 9/9 cases correct (single-line, multi-line, nested, deep nesting,
calls outside the scope, calls inside strings, a different lock object).

### 4.3 tree-sitter was not worth its dependency

Evaluated as a replacement for the hand-rolled brace matcher, on the theory that it would fix two
stripper bugs (verbatim strings, block comments).

- All **18** regions across T1–T5 resolve **identically** under both locators. Zero differences.
- `RiskGuardAddOn.cs` and `TradeCopierEngine.cs` contain **zero** verbatim strings and **zero**
  block comments. Both bugs are latent, not live.

Rejected. `guard_unsupported_syntax` covers the latent case at no dependency cost. Revisit if a
second language is ever targeted, or if either construct appears.

### 4.4 `logs/ollama_loop/summary.json` is not a ledger

`main()` overwrites it wholesale per invocation. It currently records T1 as
`applied: false / MAX_ROUNDS_EXHAUSTED` even though T1 is committed at `d94d5521` — the resumed run
that actually landed it was never recorded. **Do not trust it.** Per-ticket `result.json` is
reliable; the run-level summary is not. `loop.py` will write an append-only JSONL ledger instead.

### 4.5 `git checkout --` as the revert mechanism is a data-loss footgun

The build gate applies a candidate, builds, then reverts with `git checkout --` — destroying any
uncommitted work in the same files. The handover has to warn "between tickets you must commit"
purely because of this.

**Fixed** by [git-worktree isolation](http://aq.dev/guides/git-worktrees-for-ai-coding-agents/),
now the standard isolation primitive for parallel agents. The loop runs in its own checkout, the
live tree is never written to, and promotion is an explicit final step. Verified: editing the
addon inside the worktree leaves `git status` on the live repo empty.

The "commit between tickets" rule in the handover no longer applies to the new tool.

### 4.6 A reasoning reviewer looked exactly like a dead one

`deepseek-v4-pro` returned an empty review on every T2 round in *both* loops. It is not flaky: it
is a reasoning model that returns chain of thought in `message.thinking` and the answer in
`message.content`, and it was spending its entire output budget on the former — 34k chars of
thinking, zero content. Two bugs made this undiagnosable: `_call_ollama` accepted `max_tokens` and
never sent it (so the budget was whatever the server defaulted to), and only `content` was read.

Raising the budget only moved the wall — reasoning grew with candidate size (55k chars on the
round-1 candidate, 98k on round 2, hitting a 24k-token ceiling exactly). Measured on the round-2
review prompt:

| | time | content | thinking | tokens | result |
|---|---|---|---|---|---|
| think on | 159s | 0 | 90,421c | 24,000 (capped) | no verdict |
| think off | **21s** | 10,655c | 0 | 2,721 | REVISE, 10 findings |

**Fixed**: `num_predict` is now sent; reviewers default to `think=False` (their output contract is
a structured verdict, so deliberation is spent and discarded); empty content with non-empty
thinking raises a `ProviderError` naming the sizes and the budget to raise. `think: "low"` is not
honoured — it reasons at full length.

### 4.7 Unanimous APPROVE from adversarial reviewers is unreachable

T2, three rounds against the 168-line `ExecuteAction`, both reviewers voting:

```
round 1: 11 distinct findings
round 3: 13 distinct findings
overlap:  0
```

Every round-1 finding was fixed. All 13 in round 3 were new. The implementer was complying and the
reviewers were not repeating themselves — each rewrite simply exposed different ground, and the
prompt said *"Apply every required change"*, so false positives drove rewrites that generated the
next round's findings. The loop was manufacturing its own work.

**Fixed** by separating detection from adjudication — the arbiter rung above.

### 4.8 Reviewers reviewed blind, and re-litigated settled decisions

Reviewers saw only BEFORE/AFTER of each region — not whether the patch compiled, not whether tests
passed, and not the decisions the arbiter had already made. §5 of the handover exists purely as a
human-maintained list of false positives reviewers kept raising.

**Fixed**: the review prompt now carries a "Mechanical gates already passed" summary, and
`Profile.settled` is injected into every round automatically.

---

## 5. Deferred — test-strategy follow-ups

Not loop architecture, so not blocking the rebuild, but the highest-value next moves for the
RiskGuard work itself:

- **Property-based stateful testing** ([CsCheck](http://anthonylloyd.github.io/blog/2024/07/07/cscheck-happy-state)
  has stateful and parallel testing, C#-first). "A position is never uncovered beyond the grace
  window" is a *property*, not an example. P0-1 and P0-4 are both sequence bugs that example tests
  missed and a state-machine generator would find mechanically.
- **Mutation testing** (Stryker.NET) — the mechanical answer to "does my suite have teeth", which
  is exactly what commit `ff72e574` answered by hand.
- **Record/replay cassettes** for the loop's own model calls. `selftest.py` now covers the driver
  with stubs and the parsers with real-world fixtures (§10.1), but a cassette of a full live run
  would catch prompt-level regressions that neither reaches.
- **`expect_green` on tickets**, so the test gate can require a named test to flip rather than
  merely require no regression (§7.5). Highest-value item in this list.

---

## 6. Conventions

- **Stage explicit paths. Never `git commit -a`.** Two unrelated background processes commit to
  this repo; unrelated commits landed between every pair of commits in this effort.
- The tool pins to `.venv` (Python 3.12). The predecessor was being run under `C:\Python314`,
  which is a separate interpreter with a different package set.
- Artifacts land in `logs/agent_loop/<TICKET>/` — one file per round per stage, plus
  `result.json`. Everything a human arbiter needs to second-guess a verdict is on disk.
  (The predecessor used `logs/ollama_loop/`; those artifacts are frozen.)

---

## 7. Defects found in live use — and the one bug behind most of them

§4 is the post-mortem that motivated the rebuild. This section is the post-mortem of the *rebuilt*
loop, from landing T2–T5. Read it before debugging anything: four of these five were the same bug
wearing different clothes.

### The pattern: strict format parsing silently discarding valid content

**A model got the punctuation wrong; the loop threw away the content and reported the wrong
cause.** Every instance cost real rounds, and every one was invisible in the summary output.

| § | What was discarded | How it presented | Cost |
|---|---|---|---|
| 7.1 | Arbiter `RATIONALE` + `SETTLED` | Silence — sections simply empty | 11 settled decisions lost across two T2 rounds |
| 7.2 | An implementer block closed with `>>` | `"EvaluatePnLRules: missing from model output"` | **3 rounds and the whole T3 ticket** |
| 7.3 | Arbiter rulings written without `[ ]` | A clean `SHIP` became `ESCALATE` | One wasted adjudication |
| 7.4 | The resumed candidate was never persisted | A `promote:` hint naming the wrong file | Nearly promoted unreviewed code |

The general rule now: **marker punctuation is not what the gates exist to check.** Parsers accept
`>{2,}`, optional brackets, and mismatched/missing END tags. If you add a parser, make it tolerant
in the same way, and make the *failure message name the content*, not the format.

### 7.1 The arbiter silently discarded its own rationale and settled list

`_section()` required an exactly-matching END tag. glm-5.2 closed `<<<RATIONALE>>>` with
`<<<END SETTLED>>>` and omitted the `<<<SETTLED>>>` opener entirely — on **both** T2 rounds — so
both sections parsed as empty and the loop printed nothing to say so. Fixed (`4d7d9557`): the
parser tolerates a misnamed terminator (run to the next marker) and a missing opener (take the body
before the closer).

Why it mattered: `SETTLED` is the mechanism that stops reviewers re-litigating known false
positives. It was being thrown away every single time.

### 7.2 A missing angle bracket cost an entire ticket

T3 round 1 passed every gate; the arbiter upheld two findings. Rounds 2, 3 and 4 then all died on
the **static** gate with `EvaluatePnLRules: missing from model output`.

The block was not missing. kimi-k2.7-code closed it with `>>` instead of `>>>` and — given
feedback naming a block it had just emitted — reproduced the identical output twice more. **r2, r3
and r4 were byte-identical.** Fixed (`3bc4dfff`): `BLOCK_RE` and the NOTES pattern accept `>{2,}`.

This is the canonical example of the whole failure mode: a correct patch, rejected three times,
with the gate pointing at the one thing that was not wrong.

### 7.3 Arbiter rulings dropped over bracket style

T3 round 2's arbiter ruled on all eight findings and recommended `SHIP`, but wrote
`- REJECTED #1: ...` without the square brackets `_RULING_RE` demanded. All eight parsed as
unruled, and the (correct, deliberate) SHIP-with-unruled guard downgraded it to `ESCALATE`.
Fixed (`4de6c6b5`): brackets and emphasis characters are skipped on both sides. A ruling is
identified by the leading `-`, the verdict keyword and the `#n`.

### 7.4 The promote hint named a file the arbiter never reviewed

**The dangerous one.** On `--resume-raw` the loop read the candidate but never wrote it to
`rN_impl_raw.txt`, while every `resume with` / `promote:` hint is built from the round number. The
round-1 artifact therefore stayed as whatever an *earlier* run left there.

On T3 the loop resumed a good candidate, arbitrated it, recommended SHIP — and printed
`--resume-raw .../r1_impl_raw.txt`, which still held a candidate from six minutes earlier carrying
two upheld findings, one of them a naked-position defect. Following the hint would have promoted
unreviewed code into an addon that flattens live funded accounts. Fixed (`10b32c5a`).

Caught only because the code being reviewed did not match the implementer's own notes. **Keep
verifying that the file you promote is the one the arbiter saw** — `md5sum` it against the
candidate whose `rN_arbiter.txt` you read.

### 7.5 The test gate cannot tell "no regression" from "defect closed"

T5 reached `ARBITER_SHIP` with its own acceptance test — the entire point of the ticket — still
failing. The gate compares against a frozen baseline, that failure *was* in the baseline, so
"no regressions" was true and useless.

Not yet fixed. The fix is an `expect_green` list on the ticket that the test gate must observe
flipping. Until then: **if a ticket has a failing test waiting, check it by name.**

Related, and worth knowing: T5's test could never have passed. It built a locked RiskGuard but
never assigned `RiskGuardAddOn.Instance` (production only does that in `State.Configure`), so the
copier saw no guard at all. A test that cannot observe its own subject reads exactly like a fix
that does not work.

---

### 7.6 `--resume-raw … --apply` applied a candidate nobody had read (P1-56, 2026-08-10)

The promote command in §9 was run with `--resume-raw` pointing at a reviewed, `ARBITER_SHIP`
candidate. What landed in the live tree was a **different, unreviewed candidate**.

`--resume-raw` seeds round 1 with that raw — and then gates *and reviews* it like any other round.
On this run the panel returned `REVISE`, so the loop did what it is designed to do: it ran **round 2
with a fresh implementer call**, that round reached `ARBITER_SHIP`, and `--apply` shipped round 2.
The flag reuses a candidate as a *starting point*; it does not pin one.

That mattered, because round 2 was worse than the candidate that had been read. It set
`countAttempt = (pass == 0)` so re-drive passes reached the broker without counting an attempt —
making a bound of 3 into effectively 9 submissions — and it restored a `WorkingStop = null` on the
failure paths that reintroduced the defect the ticket existed to fix. Both changes passed compile,
the full suite including both acceptance tests, and the lock-scope gate.

**Two lessons, in order of importance:**

1. **§9 step 3 is not ceremony.** It is the only thing standing between a green ladder and a live
   defect, and this is the second time it has caught one (§7.4 was the first).
2. **To promote an exact candidate, do the splice yourself** — `loop.parse_blocks` on the raw you
   read, `regions.apply` over `regions.extract`, then diff the working tree against the `final.patch`
   you reviewed (`diff --strip-trailing-cr`, because the patch export and `git diff` disagree on line
   endings). That is deterministic and reuses the loop's own region machinery, so the splice is
   identical to the one that was gated.

A `--pin-raw` flag that skips review and applies verbatim would make this safe by construction, and
is worth adding. Until then the ladder cannot tell you *which* candidate it blessed — only that
something passed.

---

## 8. Debugging playbook

### 8.1 Where to look

```
logs/agent_loop/<TICKET>/
  00_implement_prompt.md        exact prompt sent to the implementer (round 1)
  rN_impl_raw.txt               raw implementer output for round N  <- the candidate
  rN_build.txt                  compiler output
  rN_tests.txt                  one-line test gate summary
  rN_review_<model>.txt         each reviewer verbatim
  rN_arbiter.txt                rulings, recommendation, rationale, settled
  final.patch                   unified diff of the last gated candidate  <- read this
  result.json                   per-round stage records; the machine-readable truth
logs/agent_loop/ledger.jsonl    append-only, one line per run
```

**Trust order when they disagree**: `result.json` > per-round artifacts > stdout summary.
`result.json` records the stage that actually failed and its detail string.

### 8.2 First moves

```powershell
# What stage failed, and why, for every round?
.\.venv\Scripts\python.exe -c "import json; r=json.load(open('logs/agent_loop/T3/result.json')); print(r['final_verdict']); [print(' round',d['round'],d['stage'],d['ok'],d['summary'],'|',(d.get('detail') or '')[:200]) for d in r['rounds']]"

# Did the model actually emit the blocks the static gate says are missing?
Select-String -Path logs/agent_loop/T3/r2_impl_raw.txt -Pattern '<<<'

# Do the parsers agree with your eyes?
.\.venv\Scripts\python.exe -c "from scripts.agent_loop.loop import parse_blocks; b,n=parse_blocks(open('logs/agent_loop/T3/r2_impl_raw.txt',encoding='utf-8').read()); print(sorted(b), len(n))"

# Same for an arbiter artifact
.\.venv\Scripts\python.exe -c "from scripts.agent_loop.arbiter import _section,_RULING_RE; t=open('logs/agent_loop/T3/r2_arbiter.txt',encoding='utf-8').read(); print(len(list(_RULING_RE.finditer(_section(t,'RULINGS')))),'rulings;', len(_section(t,'RATIONALE')),'c rationale')"
```

### 8.3 Symptom → cause → fix

| Symptom | Most likely cause | What to do |
|---|---|---|
| `X: missing from model output`, but `<<<BLOCK id="X">>>` is in the raw file | Marker punctuation (§7.2) | Run the `parse_blocks` one-liner. If it parses now, the candidate is fine — resume from it. If not, loosen the pattern. |
| Same round repeats with identical output | The model cannot act on the feedback because the feedback is wrong | Diff consecutive `rN_impl_raw.txt`. **Byte-identical rounds mean the gate is lying**, not that the model is stuck. |
| `PANEL_UNREACHABLE` | A reviewer raised `ProviderError`, or returned empty | Check `rN_review_*.txt` size. 0 bytes on a reasoning model = budget spent on `thinking` (§4.6); the loop already sets `think=False`. Otherwise transport — retry. |
| Arbiter `ESCALATE: recommended SHIP but did not rule on [...]` | Ruling lines did not parse (§7.3) | Read `rN_arbiter.txt`. If the rulings are visibly there, it is a parser bug, not an omission. |
| Arbiter rationale empty in output | Mismatched END tag (§7.1) | Should be fixed; re-check `_section`. |
| `ARBITER_SHIP` but the ticket's acceptance test is still red | The test gate only checks regressions (§7.5) | Do not promote. Check the named test. |
| Verdict good, but promoted code looks wrong | Stale `rN_impl_raw.txt` (§7.4) | `md5sum` the file against the candidate you reviewed. |
| `TICKET_REJECTED` | A region targets the verifier (gate 0) | Correct — the ticket is malformed. Fix the region, do not relax the gate. |
| Worktree left behind after a crash | Normal | `--prune`. |
| Run refuses to start, complains about a lock | Stale lock from a crashed run | The lock records a PID and self-clears when the process is gone; if not, delete it. |
| `capture_baseline` refuses | Live tree dirty | Commit or stash. A baseline must describe unmodified code. |

### 8.4 The rule that would have saved the most time

**When a gate says the model got the format wrong, check whether the content is there before
spending another round.** Three of the four §7 defects would have been caught in thirty seconds by
one `grep '<<<'` on the raw artifact.

---

## 9. Promotion checklist

The loop stops at `ARBITER_SHIP` on purpose. Before `--apply`:

1. **Read `rN_arbiter.txt`.** Not the summary line — the rulings. Reviewers contradict each other
   on load-bearing facts; on T2 one read a state machine correctly and the other asserted the
   opposite. Verify the mechanism against the code.
2. **Distrust proposed fixes especially.** Two reviewer "required changes" during this phase would
   have created live defects: one reintroduced the exact defect the previous round upheld, another
   would have aborted an auto-stop whenever the position scaled up, leaving it naked.
3. **Confirm the candidate is the one that was reviewed** (§7.4). **`--resume-raw … --apply` does not
   do this for you** — see §7.6. It is a *fresh run seeded with that raw*, not "promote what I read":
   the seeded round is re-reviewed, and a `REVISE` there produces a new implementation which is what
   `--apply` then lands. To promote an exact candidate, splice it yourself and diff the result:

   ```powershell
   # blocks = loop.parse_blocks(<the raw you reviewed>); regions.apply(regions.extract(REPO, ticket["regions"]), blocks)
   # then: diff --strip-trailing-cr <(git diff <file>) <the final.patch you reviewed>
   ```
4. **Confirm the acceptance test flipped**, by name (§7.5).
5. **Read `final.patch`.** The blast radius is the region set, but the consequences are not.
6. Promote with `--resume-raw <candidate> --allow-unapproved --apply`, then **stage explicit
   paths** and commit.
7. Re-run the suite in the live tree afterwards. The gates ran in a worktree.

Both defects that reached the live tree this phase were found at step 5, not by the panel:
T4's exit rounding and T3's out-of-region session reset.

---

## 10. Changing the loop safely

### 10.1 Testing a change

```powershell
.\.venv\Scripts\python.exe -m scripts.agent_loop.selftest   # 11/11, ~2 min, free
.\.venv\Scripts\python.exe -m scripts.agent_loop --list     # all 18 regions still resolve
```

`selftest.py` runs the real driver with `chat()` stubbed, against a real worktree, a real build and
a real test run. It covers: the worktree and baseline freeze, the gate ladder, panel validity
(empty response and `ProviderError` are *not* votes), all three arbiter recommendations, and gate 0
refusing a ticket aimed at the verifier.

> **Know what the selftest does not prove.** Its canned model output is *perfectly formatted* —
> `_arbiter_body()` emits exact `- [VERDICT] #n:` lines and correct END tags. **All four §7 parser
> defects passed the selftest 8/8 while broken**, because the selftest never feeds the parsers
> anything a real model would actually produce. Scenario 9 (`parser fixtures`) closes this by
> replaying the real malformed artifacts under `logs/agent_loop/`. If you touch `BLOCK_RE`,
> `_RULING_RE` or `_section`, that is the scenario that protects you — and add a fixture whenever a
> new malformation shows up in the wild.

### 10.2 Invariants not to break

- **Mechanical gates outrank model opinion.** Where a gate and the panel disagree, the gate wins.
  The arbiter may not overturn a gate.
- **A model never ships.** `--apply` is a human action.
- **The baseline is frozen once**, before the first candidate. Recomputing it mid-run lets a patch
  that breaks a test widen the baseline and pass.
- **Gate 0 is by construction, not instruction.** Never let a ticket reach the test file, the
  csproj, or `scripts/agent_loop/*`.
- **A reviewer that did not answer has not voted.** Empty and `ProviderError` are not dissent.
- **The live tree is never written to** except by the explicit final apply step.

### 10.2b Maintaining `settled` — retire entries, don't just add them

`profiles.py`'s `settled` tuple is injected verbatim into **every review round**. It exists because
the panel re-raises the same false positives indefinitely. That makes it powerful and therefore
dangerous: an entry that has since been settled the *other* way does not merely go stale, it
actively instructs reviewers to approve reintroducing a closed defect.

This happened. `settled` carried *"Orphan-cancel under `_stateLock` STAYS (tracked as P1-35)"* for
as long as P1-35 was deferred — correct at the time. When P1-35 was **fixed** on 2026-08-07, that
line would have told the panel to wave through a patch putting the broker call back under the lock.

**So: when you close a defect that appears in `settled`, rewrite that entry in the same commit.**
Usually it inverts — from "this is out of scope, don't raise it" to "this is now done *this* way,
don't propose undoing it". Mirror every change in the handover's §5, which is the human-readable
copy of the same list.

A related habit worth keeping: prefer settled entries that state the *invariant and its reason*
over ones that state a *scope decision*. "Simulated accounts are identified by `Provider`, never by
name, because names are user-chosen" stays true after the fix lands. "X is out of scope" does not.

### 10.3 Adding a ticket

Append to `scripts/agent_loop/tickets_p0.json`: `id`, `title`, `defect`, `spec` (numbered
imperatives), `context` (what must be preserved, and what neighbouring tickets already changed),
and `regions` (`id`, `file`, `anchor`, `note`). Then:

```powershell
.\.venv\Scripts\python.exe -m scripts.agent_loop --list   # regions must all say OK
```

Two lessons from this phase: **anchor on a declaration, never a line number**, and **make sure the
region set can actually reach everything the spec asks for**. T3's spec item 1 required an edit at
two sites that were not in any region, so the loop could not have complied — it flagged this in its
notes and was ignored until review caught it.

### 10.4 Adding a gate

Add to `gates.py` returning a `GateResult`; wire it in `loop.py` in cost order. Set `feedback` to
text the *implementer* can act on — it is fed into the next round verbatim, so a misleading
`feedback` string costs a full round (§7.2). Then add a selftest scenario proving it fires *and*
that unchanged source still passes it.

### 10.5 Pointing the loop at another codebase

Add a `Profile` in `profiles.py`: prompts, `build_cmd`, `test_cmd`, `lock_name`, `protected`,
`settled`. Nothing domain-specific lives in the driver. The lock-scope gate and the C# region
stripper are the two genuinely C#-shaped pieces; `regions.py` refuses files containing verbatim
strings or block comments rather than mis-splicing them.

---

## 11. Mode: `review` — adversarial review of code that is already written

**Built 2026-08-07.** `--mode review` runs the panel and arbiter over a **committed diff**. No
implementer, no regions, no worktree, no apply path. `scripts/agent_loop/review_mode.py`.

```
# smallest useful invocation
python -m scripts.agent_loop --mode review --review-base HEAD~1

# scoped, with intent, and with the true gate state fed to the panel
python -m scripts.agent_loop --mode review \
  --review-base 76137575~1 --review-head HEAD \
  --review-paths scripts/ninjatrader/addons/TradeCopierEngine.cs \
  --review-intent "P0-9: mirror the leader's protective stop, anchored to the follower's fill" \
  --review-verify
```

### Why it exists

`patch` mode's central guarantee is that **the grader is written by a different party than the one
being graded** — gate 0 makes `*Tests.cs` unreachable to the implementer. Hand-written work has no
such guarantee. One author writes the change *and* its tests, so the tests encode exactly the cases
that author already thought of, and the suite goes green for the same reason the bug got written.

That is not hypothetical. `P0-9`'s bracket replication shipped in `76137575` with `Math.Abs` where
a **signed** offset was required, so a leader trailing its stop into profit mirrored onto the
*losing* side of the follower's entry. It survived a green 515-test suite, a clean net48 compile
and a 20/20 falsifiability check, because all three artifacts were authored by the party that made
the mistake. A human question found it.

### What it did on its first run

Reviewing the `P0-9` diff: **24 findings, 3 upheld, 8 rejected, 13 out of scope.** Two of the three
upheld were real defects the author's own tests did not cover, and both are now fixed:

- A stop whose `Submit` threw, or which the broker rejected moments later, left `WorkingStop` null
  with a valid offset and **nothing re-triggered submission** — the follower stayed naked for the
  life of the position.
- The `OrderUpdate` reporting that rejection *was being received and discarded*, because the
  handler returned early for any account with no relationships, which every follower is.

**The third upheld finding was wrong**, and the arbiter upheld it anyway: it claimed a 10-point ES
stop becomes "10 follower-points" on MES. Every pair in the conversion matrix (NQ/MNQ, ES/MES,
YM/MYM, CL/MCL, GC/MGC, RTY/M2K) tracks the same underlying at the same price with the same tick
size; only the dollar multiplier differs, and quantity scaling handles that. **Read the rulings.
The arbiter is not a rubber stamp and it is not an oracle** — same lesson as section 4.7.

> **Fixing an upheld finding can introduce the defect a rejected finding warned about.** Adding
> re-submission created exactly the reject-to-resubmit flood that finding #13 described and the
> arbiter had rejected *on the grounds that no such loop existed*. That was true before the fix and
> false after it. The bound (`MaxBracketStopAttempts`) is why. The first attempt at the bound was
> also wrong — it reset the counter on `Submit` success, but the failure mode is rejection *after*
> a successful submit, so the bound was unreachable; the test caught it at 21 submissions.

### Properties worth preserving

- **Advisory by construction.** It reports and never edits, the same way `ARBITER_SHIP` never
  ships. Exit code is 0 when a verdict was *produced*, not when the code is approved — a non-zero
  exit would invite wrapper scripts to treat it as a gate it is not.
- **Findings come from `Vote.finding_list`**, which `parse_review` already produced with severities
  attached. Re-parsing the text would flatten everything to one severity and silently break the
  arbiter's BLOCKER/MINOR weighting.
- **`MAX_DIFF_CHARS` (90k) refuses an over-large range** rather than reviewing a truncated diff. A
  review that does not fit in context returns confident nonsense.
- **The prompt tells the panel the tests are author-written.** That single instruction is what
  redirects it from "restate what the tests cover" to "name a case they do not".

---

## 12. Backlog — one harness, several modes

Recorded 2026-08-07 so it is not re-derived. **The conclusion: we do not need different loops.**
Only two things vary by scenario; everything else is already shared and scenario-independent.

| Varies | Already shared |
|---|---|
| **Change model** — how a candidate is expressed and applied | worktree isolation + run lock (`workspace.py`) |
| **Gate ladder** — what "correct" means | provider shim, panel, arbiter, thrashing detection |
| | protected paths, frozen-baseline test diffing, ledger + artifacts |

The only thing blocking new-feature work is narrow: `regions.py` anchors on an **existing
declaration** and splices a replacement over it (`SUPPORTED_SUFFIXES = (".cs",)`). New code has no
declaration to anchor to. That is a change-model limit, not an architectural one.

### 12.1 `create` mode — NOT BUILT

For "add a feature", "write a new module". The change model becomes *write whole file at a declared
path*; `regions.py` drops out entirely.

- `check_protected_paths` **inverts**: from "these regions are off limits" to "you may only create
  under these declared paths, and never `*Tests.cs`". The anti-reward-hacking guarantee has to
  survive that inversion — it is the whole reason the mode would be safe.
- `check_static` mostly transfers (ASCII, balanced braces, no leaked markers).
- `check_compile` and `check_tests` are unchanged, and are what make it worth doing at all.
- Ticket gains `creates: [paths]` in place of `regions`.
- Open question: how a `create` ticket declares acceptance tests when the file under test does not
  exist yet. Probably the same `expect_green` contract with the tests written first by the
  operator — which is already the rule.

The smaller of the two remaining pieces; mostly plumbing.

### 12.2 Research / backtest mode — do NOT build this by extending the patch ladder

**This is a genuinely different kind of task, and the existing gates would give false assurance.**

`patch` works because its terminal signal is **binary and adversarially robust**: tests pass or
they do not. A backtest's output is a **number**. The failure mode is not a compile error, it is a
plausible-but-wrong Sharpe, and the reward-hacking surface is far larger:

- look-ahead / future leakage
- overfitting a parameter sweep (easy to do accidentally, and sweeps are ADR-022 standard here)
- quietly shifting the date range, instrument set or cost model until the number improves
- reporting in-sample as if it were out-of-sample

Worse, **gate 0's trick does not transfer.** You can make `*Tests.cs` unreachable; you cannot make
the *data* unreachable, and the model can always reshape the question. An implementer told
"improve this backtest" against a gate of "it ran without error" **will** find the overfit — that
is the optimiser working correctly.

The honesty gates this needs are already written down as ADRs and would mechanise directly:

| Rule | Source |
|---|---|
| Fully vectorised, no `for` in calculation paths | ADR-017 |
| Only `PropFirmSimulator` for prop viability; never per-trade % as daily P&L | ADR-021 |
| Metrics as price-percentage, not absolute points | ADR-002 |
| Intraday positions exit by 16:00 ET | ADR-020 |

Add look-ahead detection and a **holdout the loop may touch exactly once**, and there is a real
research ladder. Until those exist, pointing an LLM loop at backtesting produces confident garbage.

**So: two gate ladders, one harness.** Share `workspace` / `providers` / `arbiter`; do not reuse
the patch ladder for research, because passing it would mean nothing there while reading as
assurance.

### 12.3 Smaller items

- **`review` mode has no test of its own.** `selftest.py` covers the parsers; the new module is
  exercised only by having been run. A fixture diff with a known planted defect would make it
  falsifiable the same way the acceptance tests are.
- **`--review-verify` runs build+test in the live tree**, not a worktree. Harmless today because it
  only reads, but it is against the discipline `patch` mode follows.
- **Findings are not deduplicated across reviewers**, so the same defect from two models reaches
  the arbiter twice and inflates the count. Cosmetic now; it would distort the thrashing signal if
  review mode ever becomes multi-round.
- **`P2-38`** (`McpBridgeAddOn`'s name-prefix hole) is still unreachable by any mode, because that
  file is excluded from the test build. The `P1-21` precedent — move the logic to a testable class
  rather than argue about coverage — applies.
