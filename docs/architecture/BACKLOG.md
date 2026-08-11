# Backlog — What's Left to Implement

**Purpose**: track every gap between the execution plan and the built code.
Each item has a priority, an effort estimate, and a reference to the plan
section or decision log entry that motivates it.

**Last updated**: after the 2026-08-10 full-package review.

## STATUS

All 17 backlog items addressed + Phase 9 complete + review fixes applied.
**318/318 tests pass on Python 3.12 and 3.14** (re-verified on both, session 4).
Latest tag: **`v0.4.0`**, and `main` is pushed. **tvDownloadOHLC still pins and
has installed `v0.3.0`** — re-pin to get any of O15-O19, O24, O29 or O30.

Tag hazards: `v0.1.0` predates Phase 9 and all review fixes. `v0.2.0` carries
the O9 defect and **cannot run on Python < 3.13 at all**. Use `v0.3.0` or later;
`v0.4.0` is the first tag verified green on 3.12 *and* 3.14.

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

#### O1. `promote()` cannot handle two tickets that touch one file — CLOSED (`a4052e6`)

**Fixed** by the first option below: `promote()` builds a patch for exactly the
files being promoted (new OPTIONAL `paths` argument to `diff()`, so
`export_patch`'s no-arg call is untouched) and applies it with `git apply`.
Non-overlapping edits to one file now compose. 7 acceptance tests in
`tests/acceptance/test_o1_promote_composes.py`.

Three things worth carrying forward:

* **NOT `--3way`.** The loop's own patch used it. On a genuine conflict `--3way`
  does not refuse — it merges, writes **conflict markers into the live file**, and
  only *then* returns non-zero, so `promote` raised having already corrupted the
  target. It also implies `--index`, staging the result behind the user's back.
  Plain `git apply` is all-or-nothing. If anyone revisits this, read
  `test_edits_inside_one_context_window_refuse_rather_than_merge` first.
* **Known safe limit:** two edits closer than git's 3 lines of hunk context each
  carry the other's lines as context, so the second is refused rather than
  composed. A refusal the caller can act on, not data loss.
* **The original acceptance-test fixture was wrong** — its two functions were one
  line apart, i.e. inside one context window, so the "non-overlapping" case it
  claimed to test could never have composed. Fixture now models F4/F5 (two
  functions far apart). A test that cannot pass is as bad as one that cannot fail.

Original defect text follows.

#### O1 (original). `promote()` cannot handle two tickets that touch one file — HIGH

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

#### O2. `replay` mode does not hold the prompt constant — CLOSED (`973f370` + `f3fda21`)

**Fixed.** The loop now records the fully rendered review prompt
(`r{N}_review_prompt.md`, written after graph context and learning feedback are
appended — the exact bytes the panel sees) and the rendered arbiter prompt
(`r{N}_arbiter_prompt.md`, via a new `Adjudication.prompt` set on all four return
paths). Replay re-sends both verbatim; `arbiter.adjudicate` gained
`prompt_override` because `build_prompt` cannot reconstruct the original (the
ticket, diff and round history are not recoverable from a corpus — replay was
passing the literals `"replay"`/`"replay"`/`""`). Artifacts go to
`ticket_dir/replay/`, so a replay no longer overwrites the corpus it measures.

Three things worth carrying forward:

* **A corpus with no recorded prompt is refused**, not approximated, and without
  spending a model call. An unfaithful comparison is worse than none: it looks
  like a measurement. The F1-F6 corpus predates recording and correctly refuses.
* **Recording must use `write_text_verbatim`.** A test caught `Path.write_text`
  translating `
`→`
` on Windows, which would have made the recorded prompt
  differ from the sent prompt on *every line* while looking identical in a diff
  viewer — byte-for-byte fidelity that wasn't.
* **Exit codes are three-valued now**: `2` = could not measure, `1` = measured and
  a verdict flipped, `0` = measured and stable. It was `0 if flipped == 0 else 1`,
  which ignored errors — and since a legacy corpus now errors by design, that
  would have made replay a green CI gate measuring nothing. The corpus printout
  no longer labels an errored ticket `same` for the same reason.

Also closed the rest of O8's replay bullets: `--replay-dir` now exists (the
module docstring documented it from the start) and the six unused imports are gone.

#### O2 (original). `replay` mode does not hold the prompt constant — HIGH

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

#### O3. `report` gate-failure distribution reads a field the ledger never writes — CLOSED (`cf88846`, session 3)

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

#### O6. The panel did not earn its cost on this run — ANSWERED (it does); see the later session-3 section

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

#### O7. Whole rungs are still unexercised end to end — MODES DISCHARGED (session 4); rungs still open

**The four unrun modes have now been run** — `plan`, `test`, `brainstorm`,
`review` — see the session-4 section below (O31-O35). All four execute; three
produce output that is not what it appears to be. `developer` and `docs` were
discharged in session 3.

**Still open under this item:** the RUNGS, not the modes. Nothing below this
paragraph has been exercised — compaction, the settled-decisions store,
`APPROVE_PARTIAL`, `PANEL_UNREACHABLE`, `NOT_CONVERGING`. The arbiter has since
run for real (and got it wrong — O20).

Original text follows.

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

#### O12. Per-model token budgets in the registry are dead configuration — CLOSED (`2fbf1b6` + config.py)

`ModelConfig.max_tokens` was read in exactly **one** place in the package
(`compaction.py`). Every other call site hardcoded a literal that happened to
agree with the registry entry beside it:

| caller | literal | registry says |
|---|---|---|
| `loop.py` implementer | `48000` | `48000` — **fixed**, now reads the registry |
| `plan_mode.py` | `24000` | ignored |
| `test_mode.py` | `16000` | ignored |
| `docs_mode.py` | `8000` | ignored |
| `brainstorm_mode.py` | `8000` | ignored |

This is not cosmetic. O1's first loop run died `IMPLEMENTER_UNREACHABLE` because
kimi spent 125,070 chars on reasoning and emitted empty content; the provider's
own remedy ("raise max_tokens") was **unreachable without editing loop.py**.
Added `ModelRegistry.max_tokens_for(model, role, fallback)`, which prefers an
exact model-name match — a `--implementer <other-model>` override must not inherit
whichever model is registered first for the role — then the role default, then the
literal. Kimi's registered budget is now 96000, and the round-3 implement call
used 52,139 output tokens against 222,413 chars of reasoning, so the old 48000
ceiling was genuinely the binding constraint.

**Now fully closed** by `config.py`, which is the single definition of every
tunable. All five mode literals are gone; `models.DEFAULT_REGISTRY` is built from
the config rather than repeating it; `providers.chat`'s transport defaults come
from it; and the three copies of the 1800s panel deadline and the four copies of
the round limit are one value each.

The `ModelConfig.think` audit is done too, and it found a live hazard: every mode
called `chat()` with `think` unset, which omits the field and leaves the MODEL's
default in force — ON for a reasoning model. So `docs` and `brainstorm` were
running an 8000-token budget shared with an unbounded reasoning prefix, which is
the 48000 failure in miniature. Every role and mode now declares `think`
explicitly, and anything that thinks is budgeted for reasoning plus answer.

Two static guards (`test_no_budget_literals_at_call_sites`,
`test_no_panel_deadline_literals_at_call_sites`) fail the build if a literal
returns; both were mutation-checked.

#### O13. `--max-rounds 3` was not enough for a two-region ticket — OBSERVATION

O1 ran the full three rounds and ended `ARBITER_NEVER_RAN` — the panel never saw
a patch, because the test gate failed every round. Round 1 introduced a
regression, round 2 fixed the regression but lost the capability, round 3
regained the capability and reintroduced a *different* regression. The loop was
oscillating between the capability and the guarantee, which is a signal the
ticket should have been split or the rounds raised, not that the model was
incapable: round 3's patch was architecturally correct and needed one flag
removed.

Worth noting the gate ladder did its job perfectly here — it refused three
patches, one of which would have silently corrupted files with conflict markers.
**And the panel still never ran**, so this ticket adds nothing to O6's open
question about reviewer value.

#### O11. Consumer pin and install — CLOSED

`agent_loop` was not installed in the tvDownloadOHLC venv (every documented
command raised `ModuleNotFoundError`), and `requirements.txt` pinned `@v0.1.0`,
14 commits and ~25 known defects behind. Now pinned to and installed at
**v0.2.2**. Note `v0.2.0` is a **poisoned tag**: it carries the O9 defect, so it
is unusable on any Python below 3.13. Do not pin it.

#### O14. `test_graph_freshness_marker_round_trip` is flaky — LOW

Observed failing once in a full-suite run on Python 3.12 and passing both in
isolation and on the next full run, so it is a flake rather than a regression.

Mechanism: `check_graph_freshness` compares `newest_source_mtime > last_indexed`,
and `mark_graph_fresh` records `time.time()` immediately after the test writes
`a.py`. When the filesystem's mtime granularity rounds the write up into the same
tick as the marker, the comparison reports `stale` where the test asserts
`fresh`. Pre-existing and unrelated to the config work.

Fix when convenient: have the test advance the marker deliberately rather than
racing the clock. A flaky gate is worse than a missing one — it teaches people to
re-run instead of read.

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

### 2026-08-10 (session 3) — developer mode made to work, O3 closed: O15-O23

Developer mode had **never produced a patch**. Pointing it at O3 found that out,
which is the same lesson as O10: "never been run" was hiding a total failure,
not a risk of one. O3 itself is now closed, by developer mode, test-first.

#### O15. Developer mode dropped native tool calls — CLOSED (`5c6b091`)

`kimi-k2.7-code:cloud` — the *configured default implementer for this mode* —
answers by populating `message.tool_calls` and leaving `content` empty.
`_call_ollama` read only `content` and `thinking` and discarded the tool calls,
so every turn looked blank. With `think=True` (developer mode's setting) the
empty content tripped the reasoning-budget guard and the run died
`IMPLEMENTER_UNREACHABLE`; with `think=False` the driver said "Continue" to a
model that had already answered, until it ran out of turns.

`Completion` now carries `tool_calls`, normalised to `[{"name", "args"}]`, for
ollama and OpenAI. Anthropic never emits `tool_use` without a `tools` array,
which this shim does not send.

The driver renders native calls into the text protocol for the artifact and the
history turn, but **dispatches from `out.tool_calls`** — a namespaced name
(`functions.read_file`) would not survive the text protocol's `\w+` name regex,
which is this same silent-drop defect one layer down. A test pins that.

#### O16. The provider misdiagnosed empty content as budget exhaustion — CLOSED (`5c6b091`)

The guard fired on "empty content AND any thinking" and blamed the token budget
unconditionally. It reported a 48000-token exhaustion for a response that
generated **21 tokens** and stopped voluntarily, and advised raising
`max_tokens` — which could not have helped. It now checks for a tool call
first, and only claims truncation on `done_reason=length` or a genuinely spent
budget. A diagnostic that points at the wrong cause is worse than none: this one
sent a real debugging session to look at budgets.

#### O17. Turn exhaustion reported no state at all — CLOSED (`bd3c296`)

Spending every turn without `<<<DONE>>>` fell out of the loop with
`result["verdict"]` still `""`. The first working run did fifteen turns of
correct exploration and reported `verdict:` blank; every branch after the loop
keys off `verdict == "DONE"`, so it was indistinguishable from a run that did
nothing. Now `MAX_TURNS_EXHAUSTED`. `result["turns"]` was also a permanently
empty list and is now populated.

The loop's own arbiter rules name "a verdict or state that LIES" and "a silent
zero that hides a broken mechanism from its own logs" as upheld findings. It was
doing both to itself, in two places.

#### O18. Developer mode was not test-first — CLOSED (`08ad362`)

**The finding that matters most from this session.** Developer mode could edit
source with no test that fails first, so its gate ladder could only ask "does
this compile and break nothing?" — a question a patch that fixes nothing also
answers correctly.

Demonstrated, not theorised: developer mode's first O3 patch **compiled, passed
all 232 tests, and was a no-op.** It read `r.get("gate")` from a round record
whose field is named `stage`, so the branch it added could never fire. Every
gate was green because nothing in the suite tested the thing being fixed —
which is the same reason the defect existed. A reviewer caught it. The gates
could not.

There is now a RED phase before explore, default on
(`modes.developer.require_failing_test`, skipped for a profile with no
`test_cmd`):

* a `write_test` tool restricted to the profile's `test_sources` — the inverse
  of `_edit_file`'s protected check, so the red phase cannot reach source;
* the test is run immediately and must produce a NEW failure; one that passes
  against unfixed code is rejected with that reason and the phase does not
  advance;
* once red it is recorded as `acceptance`, folded into the frozen baseline,
  locked read-only, and required green by the final gate via `expect_green`;
* `<<<DONE>>>` in the red phase is refused; DONE without acceptance becomes
  `NO_FAILING_TEST`.

Two implementation traps, both found by the tests rather than by review:

1. The driver appended `edit_file` to the offered set **unconditionally**
   ("calling it is what moves the model from explore to edit"). Left alone, the
   model could skip straight to editing and the phase would have been
   decorative.
2. `git diff` omits untracked files, so the exported patch carried the fix
   **without the test proving it**. Fixed with `git add -N`.

**What this does NOT buy**, stated because it is easy to over-read: TDD makes
the gate ladder *able* to refuse. It does not make the model write a good test.
The acceptance criteria are self-chosen, and a test covering one side of a
two-sided fix still unlocks editing. See O21.

#### O19. The model could not see WHY a test failed — CLOSED (`679962c`)

`_run_tests` returned only the NAMES of failing tests. On the first TDD run the
model wrote three acceptance tests that shelled out to
`python -m agent_loop --mode report --repo <path>`. There is no `--repo` flag;
argparse **abbreviation-matched it to `--report-last`**, which takes an int, so
every call died with `invalid int value` and the tests were red for a reason no
fix could ever change. The model then spent sixty turns trying to satisfy them,
at one point editing `report.py`'s column padding from `{gate:<15}` to
`{gate:<16}` because a test asserted exact spacing.

It was not being stupid, it was blind: the argparse error explaining the failure
was never shown to it, and every tool result was truncated to 2000 chars — which
also silently halved its 100-line `read_file` windows, hence the redundant
re-reading.

Now: failure output is returned, per-tool result budgets (8000 diagnostic /
4000 otherwise), and `CONFIRMED RED` shows the failure output and says that a
test failing for any reason other than the defect can never pass and should draw
`<<<ESCALATE>>>` rather than edits.

The gate held throughout — `MAX_TURNS_EXHAUSTED`, no patch, nothing applied. The
system refused to ship a bad fix. It just took seventy turns to say so.

#### O3. Gate-failure distribution — CLOSED (`cf88846`)

Produced by developer mode with the red phase active, then verified and extended
by hand. `run_ticket` records the failing gate name(s) structurally from
`RoundRecord.stage`; the report counts that field; legacy entries are excluded
**visibly**. On this repo it now prints `review 6 / protected 1 / test 1` with
187 legacy entries excluded, in place of the double-counted `test 8 /
protected 8`.

Read O21 before trusting the generated tests that came with it.

#### O20. The arbiter is not a reliable adjudicator — MITIGATED (`f97492f`); see the measured table in the later session-3 section

Two failures in one session, on the same defect:

1. **A fabricated warrant.** It rejected the one correct reviewer finding — that
   the terminal branch was a no-op — on the grounds that *"the test suite
   passing proves that `RoundRecord` carries a `gate` field."* The suite proves
   nothing of the sort: there was no test over that path at all, which is
   precisely why O3 existed. It invented evidence and dismissed the only finding
   that mattered.
2. **Contradictory rulings.** On the pre-TDD patch it UPHELD that recording a
   gate for any failing round is wrong. On the TDD patch, which does the same
   thing, it REJECTED all six findings and ruled SHIP. Same substantive issue,
   opposite verdicts.

This is the first real arbiter data ever collected — before this session it had
never run outside `selftest` fakes (O7) — and it is not encouraging.

A hypothesis, not a conclusion: the arbiter is the only role whose job is
inference over evidence, and the only role configured `think=False`. The config
comment justifying that cites a real measurement (thinking ON burned the budget
before emitting findings), so the fix is not simply flipping the flag — per
config.py's own note, `think=True` requires raising `max_tokens` in the same
edit. Measure before changing.

Until then: **do not read `ARBITER_SHIP` as "reviewed".** Same standing advice
as O6, now with a second kind of evidence behind it.

#### O21. A self-authored acceptance test can cover half a fix — MEDIUM, OPEN

The generated tests for O3 covered only the report READER. They hand-build
ledger entries and feed them to `run_report`, so **deleting the write site in
`loop.py` entirely left every one of them green** — two of three writer
mutations survived. The TDD gate proved the reader worked; nothing proved the
writer did.

Mitigated for O3 by hand: `failed_gate_names` and `terminal_ledger_record` are
now named functions specifically so the writer is testable, in place of a
comprehension buried at the end of a 600-line function, and all five writer
mutations are now caught.

Not mitigated in general. The red phase guarantees the gate CAN fail; it does
not guarantee the test covers the change. The durable fix is to review the
acceptance test against the diff's blast radius before it locks — a design
change, not a patch, and it wants a deliberate decision.

#### O22. The default panel has one member — MEDIUM, OPEN

`ModelRegistry.register` APPENDS for a role, and its docstring says the panel
"is deliberately several reviewers from different families" and that overwriting
"silently reduced the panel to one member". But `Config.roles` is a
`Mapping[str, RoleSettings]` keyed by role NAME, so exactly one reviewer is
expressible and `registry_from_config` can only ever register one. The design
intent and the schema contradict each other.

Consequence: any run that does not pass `--reviewers` gets a one-member panel
and the loop's own "one reviewer is not a panel" warning. Every command in
HANDOVER §5 passes `--reviewers` explicitly, so nobody has been running the
default — which is why this survived.

Verified:

```
default reviewers : ['glm-5.2:cloud'] <- panel size 1
default arbiter   : deepseek-v4-pro:cloud
```

The fix wants a schema decision (a `reviewers` list distinct from single-model
roles), so it is recorded rather than patched.

#### O23. Small, from this session — LOW, OPEN

* `--keep-worktree` is ignored on error paths, so the runs most worth a
  post-mortem are exactly the ones whose worktree is deleted.
* Developer mode creates its worktree in a **sibling directory of the repo**
  (`../agentloop-DEV-<pid>`), not under `logs/`. HANDOVER §6 trap 7 says
  otherwise, and `--prune` may not find it.
* `ARBITER_SHIP` exits non-zero: `cli._developer` returns 0 only for
  `verdict == "DONE"`, so a successful arbiter-override run reports failure to
  CI.
* The implementer ROLE is budgeted `max_tokens=96000`, but developer mode runs
  it under the MODE budget of 48000. Harmless today — per-turn output is small —
  but the two numbers are unrelated by accident rather than by decision.

### 2026-08-10 (session 3, later) — the arbiter measured, models catalogued: O20 partly closed, O24-O26

#### O20. Arbiter reliability — MITIGATED (`f97492f`), not closed

Measured rather than argued. The session produced a **labelled** test case: glm-5.2
raised six findings on the O3 patch, five verified correct by hand (four of them
fixed in `2c326ca`). Metric: of those five, how many does an arbiter uphold?
Corpus frozen at `tests/fixtures/arbiter_bench/`, n=2 per arm.

| model | size | correct upheld | ruling |
|---|---|---|---|
| **mistral-large-3** | 675B | **3/5 on all 4 runs** | REVISE |
| gemma4:31b | 32B | 1/5, 1/5 | REVISE |
| glm-5.2 | 756B | 0/5, 1/5 | REVISE |
| qwen3.5 | 397B | 0/5, 0/5 | REVISE, SHIP |
| qwen3.5:397b | 397B | 0/5, 0/5 | SHIP |
| minimax-m3 | — | 0/5, 0/5 | SHIP |
| deepseek-v4-flash | 304B | 0/5, 0/5 | SHIP |
| deepseek-v4-pro | 1.6T | 0/5, 0/5 | SHIP |

Four results worth keeping:

1. **The benchmark validated itself.** deepseek-v4-pro (the shipped default)
   reproduced its live failure exactly and deterministically — SHIP on a patch
   with five real defects, twice, upholding none. O20 was never a fluke.
2. **Size does not predict adjudication quality.** A 32B model beat a 1.6T one
   and both Qwens. Do not pick an arbiter by parameter count.
3. **`think=True` did not help and is not a budget artifact** — both arms
   returned complete parseable recommendations at 64000. On glm it was strictly
   WORSE, flipping REVISE to SHIP on both runs.
4. **Role competence does not transfer.** glm-5.2 is excellent at GENERATING
   findings (five correct) and poor at ADJUDICATING the same ones (≤1 upheld).
   It stays on the panel; it was not promoted.

Arbiter is now `mistral-large-3:675b-cloud`. **Still open**, hence mitigated
rather than closed: 3/5 is an improvement, not a solution, and mistral misses
both findings about TEST quality — the class that let a tautological assertion
through. Do not read its REVISE as a thorough review.

Also fixed here: `adjudicate` hardcoded `think=False` while config separately
declared `think=False` for the arbiter. They agreed by coincidence — the exact
failure config.py exists to end — so the config flag did nothing.

#### O6. Does the panel earn its cost — ANSWERED: yes

O6 recorded that two reviewers produced zero findings across six F1-F6 patches.
On the O3 patch **glm-5.2 produced five correct findings**, including a
tautological assertion in a generated acceptance test that had already been
committed. The panel is not the weak link; the arbiter was. Fixed in `2c326ca`:

* the generated test's `out.count("test") == 0 or ...` assertion, which passed
  whenever the header appeared after the first "test" anywhere in the report;
* `Counter.most_common()` leaving ties in insertion order — same data in a
  different ledger order printed a different report;
* a **third** `append_ledger` site: `review_mode.py` writes an entry with no
  gate BY DESIGN (it runs no gate ladder), so every review-mode entry was
  counted as "written before the field existed" forever.

#### O24. The compactor read a tenth of what it claimed to summarise — CLOSED (`4546db7`)

Phase 4b fires only once the pruned history exceeds `round_input_token_budget`
(40000 tokens = 160000 chars). At that exact moment `_llm_summary` cut each
prior message to 2000 chars and the whole prompt to 20000 — about an eighth —
and labelled the result `[PRIOR ROUNDS SUMMARY (LLM compacted)]` as though it
covered everything. The implementer's next round then reasons from an account of
"what was tried and rejected" that silently omits most of it, which is how a
loop re-proposes a patch the panel already refused.

The input budget is config now (`loop.compactor_input_token_budget`, 48000),
sized above the trigger. When it still does not fit, the OLDEST messages are
dropped WHOLE — half a finding reads as complete and the implementer cannot
tell. Coverage is stated in the label. `except Exception: return None` reported
nothing, so a working compactor and a broken one looked identical; failures are
printed now.

This code has still never run in a real loop (O7): everything converges in
round 1. The tests use a 160000-char history because the defect is invisible at
toy sizes.

#### O27. Which model should compact — ANSWERED: the cheapest one (`82cab53`)

Metric: eight tagged rejections planted across a history at real Phase 4b scale
(216105 chars, above the 160000 trigger); how many does the summary carry
forward? Losing one is the failure that matters — the next round proposes it
again and burns a round rediscovering the rejection. Corpus at
`tests/fixtures/compactor_bench/`.

| model | size | rejections carried |
|---|---|---|
| glm-5.2 | 756B | 7/8, 7/8 |
| deepseek-v4-flash | 304B | 7/8, 7/8 |
| gemma4:31b | 32B | 7/8, 7/8 |
| qwen3.5 | 397B | 7/8, 7/8 |

Identical across a 20x size range, so compaction is not where capability buys
anything. The single miss in every arm is the OLDEST rejection, correctly
dropped by the input budget — which independently confirms the drop-oldest-whole
behaviour and the `covers 14/16 messages` label from O24 on a real-scale input.

The compactor stays `glm-5.2`: the measurement licenses a switch, it does not
motivate one. `gemma4:31b` and `deepseek-v4-flash` are marked suited, so either
is a one-line config change if latency or quota starts to matter.

**The first version of this benchmark was wrong, and it is the most useful thing
in this entry.** It scored literal tag presence, so `gemma4:31b` scored 0/8 —
while its summary said *"Widening the protected-path glob: Rejected because it
would allow the patch to edit its own tests"*. It was measuring tag-COPYING, not
faithfulness, and it disqualified a model that was doing the job correctly. A
benchmark is a measuring instrument and needs its own validity check: read the
raw output of the worst-scoring arm before believing the ranking.

Note also that this benchmark grades free prose, which makes it inherently
softer evidence than the arbiter one (O20), which grades structured `UPHELD #N`
rulings the model is required to emit. Two benchmarks, two levels of trust.

#### O28. The arbiter search is exhausted; the MEASUREMENT is now the bottleneck

Eighteen configurations, fourteen distinct models, four services. **Mistral-large-3
still wins at 3/5, and 3/5 is not good.**

| | best score |
|---|---|
| ollama (8 models, 13 configs) | **mistral-large-3, 3/5 on all 4 runs** |
| Gemini direct API (3 models) | 0/5 |
| agy CLI (5 models) | gemini-3.1-pro-high, 2/5 then 1/5 |
| GitHub Models | service retired (HTTP 410) |

Results that should stop further model shopping:

* **`claude-opus-4-6-thinking` ruled SHIP twice**, upholding none of five real
  defects — worse than a 32B gemma4. Adjudication ability is not a function of
  capability, and this is now the third independent demonstration.
* **Reasoning effort did not matter.** `gemini-3.6-flash-high` via agy scored
  0/5, identical to the same model at Google's default effort via the direct
  API. That retires the caveat that the direct-API arms measured the wrong
  setting — they did not.
* **`gemini-3.1-pro-high` was the best non-mistral arm and still lost**, at 2/5
  then 1/5 — and unstable across reps.

**The bottleneck is the corpus, not the pool.** The whole ranking rests on ONE
patch and ONE finding set. Mistral's 3/5 may be a property of that case rather
than a capability, and nothing in eighteen runs can distinguish those. A second
labelled corpus is worth more than a fifteenth model, and developer mode now
produces suitable material every time it runs: a patch, reviewer findings, and
a verifiable answer about which findings were correct.

Until that exists, `ARBITER_SHIP` remains what O20 says it is — not a review.

#### O25. No catalogue of what each model IS — CLOSED (`89c7c22`)

`MODEL_CATALOG` in config.py: parameters, context window, modalities, thinking
and native-tool-call support, cost — harvested from `ollama show`, not
estimated — plus `suited` roles, which is a CLAIM and marked MEASURED only where
it is one. Four guards fail the build when config and catalogue disagree.

Harvesting settled a question the config could not express: **mistral-large-3
has no thinking capability at all**, so the arbiter's `think=False` is the only
valid setting rather than a trade-off. A guard now rejects `think=True` on any
model that cannot think.

Costs: ollama cloud models are billed by SUBSCRIPTION, so `0.0` means "not
metered per token", NOT free. The Anthropic entries carry real per-token prices
so that switching a role to one is visibly a cost change.

#### O26. A mode could not choose its own model — CLOSED (`89c7c22`)

`ModeSettings` had `max_tokens` and `think` but no `model`, so every non-patch
mode ran on the implementer — a CODE-specialised model — including `docs`,
which writes prose, and `brainstorm`, which enumerates approaches. The right
answer was unexpressible.

`ModeSettings.model` added, empty meaning "inherit the implementer". **Every
mode still inherits**: the mechanism was missing, the evidence for changing any
particular assignment is not. Guards reject a mode pinning a model that is
uncatalogued, unsuited, or cannot think while `think=True`.

Two traps recorded while testing this:

* `main()` reloads config from disk unconditionally, so a test that calls
  `config.set_active()` before `main()` proves nothing — it is discarded. The
  first version of the CLI test did exactly that and passed while measuring a
  stale default. Drive it through a real `--config` file.
* All mode budgets fit their models' context windows; the implementer ROLE is
  budgeted 96000 while developer MODE runs it at 48000 (O23).

### 2026-08-10 (session 4) — the 3.12 gate before tagging: O29-O30, O14 closed

Session 3 left one explicit precondition for tagging: run the suite on the
consumer's Python 3.12, not just the dev 3.14. Doing it produced **9 failed,
293 passed** and two defects, both of the O9/O10 shape — green on the machine
they were written on, broken on the machine that uses them.

#### O29. The suite shelled out to whatever `python` PATH resolved to — CLOSED

Eight `test_developer_tdd` tests built `test_cmd="python -m pytest tests/ -q"`.
`Workspace.run` uses `shell=True`, so `python` was resolved by PATH, not by the
interpreter running the suite. Under the 3.12 venv that found a 3.14 install
with no pytest, and every one of them failed with `No module named pytest` —
the runner never reached RESULTS, which reads as "the harness is broken", not
"you have the wrong interpreter".

The instance is eight tests; the class is twenty call sites across six files,
including the `{files}` compile-gate cases and three `python -c` commands that
happened to survive only because any interpreter can `print`. All now go through
`tests/_interp.py`'s `PY_EXE` (`pythonpath` gained `tests`). Quoted, because
`sys.executable` can contain spaces; concatenated rather than f-stringed,
because several commands carry the literal `{files}` the gate substitutes.

One trap worth recording: `test_defect_regressions.py` already had a module-level
`PY` — a `Profile`. The first rename collided with it and produced
`TypeError: unsupported operand type(s) for +: 'Profile' and 'str'`. Hence
`PY_EXE`.

#### O14. `test_graph_freshness_marker_round_trip` is flaky — CLOSED

It fired in the 3.12 run. The mechanism recorded above was right: the test wrote
`a.py` and then let `mark_graph_fresh` race `time.time()` against it. It now ages
the source by 60s deliberately, so the assertion no longer depends on mtime
granularity.

#### O30. `python -m agent_loop.selftest` cannot run from an installed package — CLOSED for diagnosis

HANDOVER §5 says to run the selftest first after any change, and the consumer
venv is exactly where someone does that. `REPO = Path(__file__).resolve().parents[2]`
is the checkout root only in a checkout; installed it resolves to `<venv>/Lib`,
and the first read died with

```
FileNotFoundError: ...\.venv\Lib\tickets\phase1_state_machine.json
```

which reads as a missing ticket file rather than the wrong kind of install. This
is not fixable by packaging the ticket: the selftest extracts regions from
`src/agent_loop/` and runs the loop against the repo's own source, so it is
inherently a checkout-only check. It now returns 2 with a message naming the
package location, where it looked, and `pytest tests/` as the check that does
work against an install.

**Not yet in a released tag.** The consumer venv holds v0.3.0, so the traceback
above is still what it does there until the next tag is cut and pinned.

**State after this session:** 303 passed on **both** 3.12 and 3.14; selftest
12/12 on 3.12 from the checkout. The new guard was mutation-checked
(`return 2` → `return 0` kills the test).

### 2026-08-10 (session 4, later) — O7 discharged: the four unrun modes: O31-O35

O7 said `plan`, `test`, `brainstorm` and `review` had never been run, and that
the base rate for "never been run" was three-for-three completely broken. All
four were smoke-run through `main(argv)` against this repo, with a live panel.

**The base rate broke: all four RUN.** What they do instead is worse in one
respect — three of them produce confident output that is not what it appears to
be, which no crash would have hidden.

| mode | runs? | finding |
|---|---|---|
| `brainstorm` | yes | O31 — recommends an approach having never read a line of the code |
| `review` | yes | O32 — points the reader at the prompt and calls it the findings (FIXED) |
| `plan` | yes | O33 — produces a correct ticket that **nothing downstream can read** |
| `test` | yes | O34 — confirms a red test "(correct)" when it is red for the wrong reason |

Static checks first, which cost nothing: every one of the four CLI wrappers
passes arguments that match its `run_*` signature, so the O10 defect class
(`_docs` calling `run_docs` positionally against a mismatched signature) is not
present here. The defects below are all behavioural.

#### O33. Plan mode's output cannot be consumed by anything — CLOSED

**Fixed in two halves**, because either alone leaves a trap:

* `cli.load_tickets()` is now the ONE loader both call sites use. It accepts the
  wrapper `{"tickets": [...]}`, a bare list, or a single bare ticket object —
  that last shape being every `plan.json` already on disk — and raises
  `TicketFileError` naming the file and the expected shapes instead of a
  `KeyError` from inside a dict subscript. Both CLI paths return exit 2 with a
  message.
* Plan mode writes the canonical wrapper.

12 acceptance tests, driving `main(argv)` for the two wired paths. Verified on
the real artifact: the bare `plan.json` from the O7 run now lists.

**A mutation survived the first pass** and is worth recording: deleting the
per-ticket `id` validation left all ten tests green, because
`test_an_object_without_an_id_is_not_mistaken_for_a_ticket` is caught by the
SHAPE branch and never reaches the per-ticket check. A twelfth test covers a
well-formed wrapper containing a malformed ticket. That is O21 in miniature,
found by mutation and not by reading.

Original defect text follows.

#### O33 (original). Plan mode's output cannot be consumed by anything — HIGH

Plan mode's entire purpose is to turn a defect into a ticket the loop can run.
It writes `logs/agent_loop/PLAN/plan.json` as a **bare ticket object**
(`{id, title, defect, spec, regions, expect_green}`). Both consumers expect a
**wrapper**, `{"tickets": [...]}`:

```
cli.py:140  tickets = spec["tickets"]   # --mode test  -> KeyError: 'tickets'
cli.py:471  tickets = spec["tickets"]   # --tickets    -> KeyError: 'tickets'
```

So `--mode plan` → `--mode test` and `--mode plan` → the loop both die on an
unhandled `KeyError`, with a traceback rather than a message. The documented
pipeline has never worked end to end. Wrapping the object by hand is the
workaround; the fix is to make plan emit the wrapper (and/or have the loader
accept either shape and say which it got).

Smaller, same area: the generated ticket's `expect_green` named
`tests/test_review_mode.py::...`, but this repo's tests live in
`tests/acceptance/` and the profile declares `test_sources=("tests/acceptance/*.py",)`.
Plan's prompt never shows the model `profile.test_sources`, so it invents a path
that the test-first machinery is not allowed to write to.

#### O34. "failing at baseline (correct)" is not evidence — HIGH, OPEN

Test mode generated an acceptance test, ran it, and printed

```
[test-first] 1 test(s) failing at baseline (correct)
```

The test was red because its own stub returned a `dict` where `review_panel`
returns a `PanelResult`, so it died at `panel.votes` — **before reaching a single
one of its assertions**. It could never have passed, against fixed or unfixed
code.

The gate counts failures. It cannot distinguish "red because the defect is
there" from "red because the test is broken", and it prints `(correct)` either
way. This is O19 and O21 again in a third location, and it is the same root as
the memory-worthy lesson from session 3: *the gates cannot refuse what the suite
cannot observe*. A red phase satisfied by a broken test is a red phase that
proves nothing.

Worth noting how cheaply it was confirmed: read the failure, do not trust the
label. HANDOVER §6 trap 9 says exactly this and it still cost a run.

**And it is not only the model's tests.** The hand-written replacement was red
for the wrong reason twice — first `Finding(author=...)` and `Vote(counted=...)`,
neither of which exists (`counted` is a derived property), then a count assertion
`"1" in line` that passed against the UNFIXED code because the pytest temp path
contains a `1`. Both were caught by reading the failure and by mutating the fix.

#### O32. Review mode labelled the prompt as the findings — CLOSED

`review_mode.py:243` printed `findings -> <art>/review_prompt.txt`.
`review_prompt.txt` is the INPUT — the rendered prompt with the whole diff
appended (37KB on the run that exposed this). Following the label hands the
reader a copy of their own diff, from which the only available conclusion is that
the review found nothing, while the findings sit unread in
`r1_review_<model>.txt` beside it.

Now prints the count and the files the findings are in, with the prompt on its
own line labelled as what it is. Three acceptance tests, both mutations killed
(empty the file list; drop the count).

#### O31. Brainstorm mode has never seen the codebase — MEDIUM, OPEN

`run_brainstorm` builds its entire prompt from the defect text plus four profile
fields:

```python
prompt += f"Language: {profile.language}
"
prompt += f"File suffixes: {', '.join(profile.file_suffixes)}
"
prompt += f"Build: {profile.build_cmd or '(none)'}
"
prompt += f"Test: {profile.test_cmd or '(none)'}

"
```

No graph context, no source, no regions — `in=264` tokens on a live run.

**Correction (while fixing O33):** brainstorm is not alone. `plan_mode.py:23`
imports `build_context_slice` and **never calls it** — the import is dead, and
plan mode builds its prompt from the same four profile fields. That is why the
live plan run was `in=319` tokens. Only docs mode actually injects context
(`_build_graph_context`). So the mode whose entire job is to LOCALISE a defect
in the codebase has never been shown any of it — and it still produced a
correctly-anchored ticket, which says more about the defect description it was
given than about the mode.

It still returned a recommendation that reads as authoritative and happened to
match O22's recorded fix — because the *defect description* named `Config.roles`,
not because it read `config.py`. That is the failure mode: an approach
recommendation with no evidentiary basis is indistinguishable, in the output,
from one with a good basis. Give it the graph slice plan mode already builds, or
document it as a rubber duck.

#### O35. `python` in a PROFILE is resolved by PATH, same as O29 — LOW, OPEN

O29 fixed the tests. The class extends past them: `profiles/self.py` uses
`build_cmd="python -m py_compile {files}"` and `test_cmd="python -m pytest ..."`,
and the consumer's `python-tvdownloadohlc` profile does the same. These run in
the worktree during real runs, so PATH decides which interpreter establishes the
frozen baseline.

**Not broken today** — verified: bare `python` (3.14) and the repo venv (3.12)
both give `1 failed, 64 passed` on the consumer's suite, which is the documented
baseline. It is a hazard, not a break: the day the two interpreters' installed
packages diverge, the frozen baseline silently changes under the loop.

Interesting provenance — minimax raised exactly this in its `<thinking>` during
the O29 review, reasoned that production profiles were out of scope, and emitted
`NONE`. The finding was correct and the self-censorship was not.

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