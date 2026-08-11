# Backlog — What's Left to Implement

**Purpose**: track every gap between the execution plan and the built code.
Each item has a priority, an effort estimate, and a reference to the plan
section or decision log entry that motivates it.

**Last updated**: 2026-08-11, session 4 (O7 modes, O14, O23, O29-O34).

## STATUS

All 17 backlog items addressed + Phase 9 complete + review fixes applied.
**470/470 tests pass on Python 3.12 and 3.14** (re-verified on both, session 4).
Latest tag: **`v0.4.0`**, and `main` is pushed — but `main` has moved on past that
tag, so **cut `v0.5.0` before re-pinning anything**
(`git log --oneline v0.4.0..HEAD` for how far).
**tvDownloadOHLC still pins and has installed `v0.3.0`**; re-pinning is what
delivers O15-O19, O23, O24, O29-O34 and O36 to the consumer.

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

#### O4. `report` arbiter calibration correlates coupled variables — CLOSED

The x-variable is `upheld_per_round` now, not the sum across rounds, so it is no
longer mechanically coupled to the y-variable. The printed line also carries the
caveat that it is measuring an arbiter with two known bad rulings (O20), because a
number with a known confound gets read as a measurement unless it says otherwise.

Original text follows.

#### O4 (original). `report` arbiter calibration correlates coupled variables — MEDIUM

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

#### O8. Small, unticketed — CLOSED (two entries were already stale)

* **`check_lint` reused the MSBuild digest** — the real one of the six. `_DIAG`
  matches `error CS1234`, so on ruff or eslint output nothing matched and
  `_digest` fell through to `output[-4000:]`, handing the model a raw tail to find
  the errors in. New `gates.lint_digest()` covers ruff, eslint, gcc/clang/tsc and
  MSBuild. Kept SEPARATE from `_digest` rather than widening it: the compile gate
  wants MSBuild's shape specifically, and broadening that regex would make the C#
  digest start matching prose. Same reasoning as keeping `op` out of `kind`.
* **`--mode report` demanded `--profile` and printed the one-member panel
  warning.** It reads a ledger; there is no profile to honour and no panel to be
  one-membered. Dispatched before both checks now.
* **`replay` diverged from the real pipeline in two ways** — it omitted
  `rules=profile.arbiter_rules` and read `profile.settled` instead of
  `inject_settled(...)`. This is not cosmetic: replay exists to hold everything
  constant but one variable, so adjudicating under a different contract than the
  run being replayed makes every flip it reports meaningless. That was O2's whole
  point, reintroduced one argument at a time.
* ~~`replay.py` documents a `--replay-dir` flag the CLI does not implement~~ —
  **STALE.** The flag exists (`cli.py`, `nargs="*"`). The test now asserts the
  useful invariant instead: every flag the docstring names must be a real
  argument. Worth recording because the first version of that test asserted the
  flag's ABSENCE and would have had someone "fix" correct code.
* ~~`replay.py` imports six names it never uses~~ — **STALE**, already cleaned up.
* `_call_openai` does not capture its cached-token usage field. **Still open**,
  and deliberately: no `OPENAI_API_KEY` is set, so the fix cannot be verified
  against the real response shape — and this session's evidence is that a guess
  at a response format is exactly what produces a confident wrong reading (O24,
  O34). Left for whoever has a key.

**Two mutations survived the first pass**, both my own tests' fault:
the `check_lint` test asserted `"F401" in feedback`, which the `output[-4000:]`
fallback also satisfies on a short fixture — so reverting to the MSBuild digest
stayed green. It now uses 800 lines of chatter and asserts the noise is ABSENT.
And a mock returned `{"tickets": 0}` where `run_report` is annotated `-> int`,
which made `main()` return a dict and the test blame the code for it.

Original text follows.

#### O8 (original). Small, unticketed — LOW

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

#### O14. `test_graph_freshness_marker_round_trip` is flaky — CLOSED (session 4)

**Closed in the session-4 section below.** The test now ages `a.py` by 60s instead
of racing `mark_graph_fresh`'s clock. This header was left saying "LOW" when the
fix landed, so an audit grepping for unclosed items found it and would have had
someone redo the work — the reason every other closed item states its status in
the header it is most likely to be read by.

Original text follows.

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

#### O22. The default panel has one member — CLOSED

**The schema call was answered by a policy**, stated by the user: *"we should
always have at least two doing the review preferably from different view points."*
That is stronger than the schema question, because it says what the DEFAULT must
be, not just what the schema must be able to express. So it is encoded three ways:

1. **The schema can express it.** `RoleSettings.extra_members` names members beyond
   `model`, and `registry_from_config` registers every one. `ModelRegistry` already
   stored `role -> [config]` and appended — the capability existed and was
   unreachable because that loop registered one config per role.
2. **The shipped default IS two, from different families:** `glm-5.2:cloud` +
   `minimax-m3:cloud`. Both measured, not guessed — glm produced five correct
   findings on the O3 patch, and on the O29 review minimax raised a correct point
   glm did not (that production profiles also hardcode `python`, which became O35).
   That is the marginal value of a second viewpoint, observed rather than assumed.
3. **A static guard, `check_panel_policy`, runs at import** and fails the build if
   the default ever drops below two members or two families. This is why: O22
   survived because every documented command passes `--reviewers` explicitly, so
   nobody ever ran the one-member default and nothing complained. Two of the five
   mutations against this fix produce a **collection error** rather than a test
   failure — the guard refusing to let the package load.

`models.model_family()` supplies the definition of "viewpoint" — the vendor stem,
lowercased. Deliberately crude: a hand-maintained mapping goes stale the next time
a model ships, and crude fails SAFE here, because two names it cannot tell apart
are reported as one family, which warns rather than staying silent. `agy:` and the
other backend prefixes are stripped first: a transport is not a viewpoint, and two
agy-routed Claudes are one family.

The CLI now warns on a same-family panel as well as a one-member one, for runs that
override the default.

**Why `extra_members` and not a full `members` list** — the first attempt made
`members` the full set, which SHADOWED `model`, so overriding `roles.reviewer.model`
in a config file was silently ignored. That is precisely the failure this module's
docstring warns about ("a typo that is quietly ignored is worse than no config
file"), and an existing test caught it. `model` stays the single primary truth.

22 tests, five mutations killed.

Original text follows.

#### O22 (original). The default panel has one member — MEDIUM

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

#### O23. Small, from this session — CLOSED

All four addressed. Two were real code defects, one was **half wrong in a way
that mattered**, and one was documentation.

* **`--keep-worktree` was silently dropped.** Not "ignored on error paths" as
  filed — `run_developer` had no such parameter at all, so the flag never reached
  developer mode under any outcome. (`open_workspace` honours `keep` in a
  `finally`, so the patch-loop path was always correct on errors; that is now
  pinned by a test that raises inside the context manager.) Threaded through.
* **The exit code disagreed with the driver's own definition of success.**
  `cli._developer` returned 0 only for `DONE`, while `driver.py:544` already used
  `("APPROVE", "ARBITER_SHIP", "DONE")` for its `apply` decision — so a run could
  apply its patch and report failure to CI in the same breath. O23 named only
  `ARBITER_SHIP`; **a unanimous panel `APPROVE` was affected too**. The predicate
  is now one constant: `loop.PROMOTABLE` and `loop.DEVELOPER_PROMOTABLE`, used at
  all three sites.
* **The worktree claim was half wrong, and the correction found a real defect.**
  Worktrees are siblings of the repo for **every** mode, not just developer mode
  (`repo.parent / f"agentloop-{ticket}-{pid}"`) — HANDOVER §6 trap 7 said
  `logs/`, and that is fixed. But "`--prune` may not find it" turned out to be
  **true for a reason nobody had guessed**, observed live: after a real test-mode
  run, `agentloop-<TICKET>-testgen-42184` sat on disk while `--prune` reported
  "pruned 0 worktree(s)". The directory was **empty and unregistered** —
  `git worktree remove --force` had deleted the contents but could not remove the
  directory (Windows held a handle), and the `git worktree prune` that follows
  then dropped the registration. `list_stale` asks git, so nothing could see it
  again. That is not just clutter: `open_workspace` **refuses to start when its
  path exists**, the path carries the pid, and pids get recycled — so a later run
  of the same ticket fails with "worktree path already exists" and `--prune` will
  not fix it. `list_stale` now also scans for `agentloop-*` siblings git does not
  know about, and `prune` removes an orphan **only when empty**; a non-empty one
  is reported and left, because it may be the post-mortem. Verified by pruning
  the real orphan.
* **The two budget numbers are now unrelated by decision, not by accident.**
  config.py records why developer MODE's 48000 deliberately overrides the
  implementer ROLE's 96000: the role budget sizes a patch-loop turn that re-emits
  whole regions, a developer turn emits one tool call, and the smaller ceiling
  bounds a fifteen-turn run.

**A mutation survived twice here, in different ways**, and both are worth
recording:

1. Deleting `keep=keep_worktree` from the driver's own `open_workspace` call left
   every test green, because they all stub `run_developer` and prove only that
   the flag ARRIVES. Forwarding is half a fix. A test that runs the real
   `run_developer` and looks on disk closes it.
2. Deleting the non-empty-orphan check also left everything green — because
   `Path.rmdir()` refuses a non-empty directory anyway. **rmdir is the safety;
   the check only makes the message true.** The test now asserts the message, and
   that is the honest statement of what the code is for.

Original text follows.

#### O23 (original). Small, from this session — LOW

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

#### O34. "failing at baseline (correct)" is not evidence — CLOSED

**Fixed.** `gates.failure_kinds(raw)` reads the exception types a run ended its
failures with, and `gates.reached_an_assertion(kinds)` is three-valued:

* **True** — at least one failure was an assertion. The test ran and disagreed.
* **False** — failures were identified and none was an assertion, so every one of
  them died before testing anything.
* **None** — nothing identifiable. Reported as UNKNOWN, not as a refusal. The NT8
  profile's runner prints `[FAIL] Suite.Test` and no exception at all, and a
  check that fails every run on a runner it does not understand gets turned off.

Applied in both places the blind spot exists, and **deliberately differently**:

| | behaviour | why |
|---|---|---|
| test mode | **refuses** — sets `result["error"]`, so the CLI exits non-zero | one-shot, reports to a human, and the test file is still on disk. `cli._test`'s own comment already said tests that were never confirmed red "are not yet evidence" |
| developer mode red phase | **warns**, loudly, to stdout and to the model | iterative, and the model cannot override a gate. A crash-defect's test legitimately fails with the exception the defect raises; refusing it would strand the run in the red phase burning every remaining turn. Loud and escapable beats correct-and-stuck |

The word `(correct)` is gone. Both paths now print what the failures actually
were.

**The classifier is validated against real pytest, not against a fixture of what
pytest is assumed to look like.** 16 of the 27 tests shell out to a real runner
across `--tb=short/long/line/no` for four cases: a broken scaffold
(`AttributeError`), a bare `assert` (which carries **no exception name anywhere**
in pytest's output — the common case, and omitting it would have misclassified
most genuine acceptance tests), an assert with a message, and `pytest.raises`
that does not raise (`Failed: DID NOT RAISE`). That precaution is O24's lesson:
the first compactor benchmark produced a confident, published, wrong ranking
because it measured what its author assumed the output looked like.

All four implementation mutations killed.

Original defect text follows.

#### O34 (original). "failing at baseline (correct)" is not evidence — HIGH

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

#### O31. Plan and brainstorm had never seen the codebase — CLOSED

**Fixed** by `context.build_intent_context(repo, profile, intent)`, wired into
plan and brainstorm, with docs mode's private duplicate now delegating to it.

**The mechanism is the FILESYSTEM, and that is the whole design decision.** Docs
mode's `_build_graph_context` returns "" unless `codebase-memory-mcp` is live, so
reusing its shape would have "fixed" O31 only on machines running the graph
server. Here the tree is searched for definitions of the symbols the request
names, and the graph is added on top when available.

Why the unused import was NOT simply an oversight: `build_context_slice` takes
`regions`, and regions are what plan mode exists to PRODUCE. There was nothing to
pass it. The missing piece was context keyed on the REQUEST.

Measured before and after, same defect text, live model:

| | in tokens | grounding |
|---|---|---|
| before | 264 | none — inferred `Config.roles` from the prompt text |
| first attempt | 289 | still none, and that is the finding below |
| after | 367 | cites `Mapping[str, RoleSettings]` and `_DEFAULT_ROLES`, neither of which was in the request |

**Four things the measurement caught that review would not have:**

1. **The first version was strict and therefore useless.** `_looks_like_code`
   required an underscore or a lower-to-upper transition, which rejects every
   single-word class name — `Config`, `Vote`, `Finding`. On the live run it found
   nothing and added 25 tokens. **Recall matters more than precision here, for a
   structural reason: a candidate that is not real finds no definition and is
   dropped, so the filesystem is the filter.** Being strict loses the name
   silently.
2. **The graph injected its own failures as findings.** `trace_call_path` answers
   `{"error":"function not found"}` — a 200-OK JSON body, not a string starting
   with `ERROR`, which was all the code checked. Three of those went into the
   prompt under the heading "Call paths", which does not merely waste tokens: it
   tells the model those symbols do not exist.
3. **Test files outranked production code.** `roles` matched
   `roles = dict(base.roles)` in two test files and pushed the real
   `roles: Mapping[str, RoleSettings]` out of the two-hit budget, because `rglob`
   is alphabetical. `_iter_sources` now yields production files first.
4. **A column-0 assignment pattern missed the field the request pointed at.**
   `Config.roles` is a dataclass field, indented inside the class body.

Eight mutations, all killed — but **one survived the first pass and it is the
third instance of the same shape this session**: replacing the `if not parts:
return ""` guard with `pass` left everything green, because the test that
exercises "nothing found" returns at an EARLIER guard (nothing extracted) and
never reaches it. Two guards that can return the same value need a test each. See
O33 for the first instance.

Original defect text follows.

#### O31 (original). Brainstorm mode has never seen the codebase — MEDIUM

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

#### O35. `python` in a PROFILE is resolved by PATH, same as O29 — CLOSED for this repo

`profiles/self.py` uses `sys.executable` now, quoted, with `{files}` escaped
through the f-string. The consumer's `python-tvdownloadohlc` profile still uses
bare `python` and is **left alone deliberately** — it lives in the tvDownloadOHLC
repo, where another session has been committing, and it is verified working today
(3.14 and the 3.12 venv agree on the baseline). Fix it there, not from here.

Original text follows.

#### O35 (original). `python` in a PROFILE is resolved by PATH, same as O29 — LOW

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

### 2026-08-11 — asked for by the user: plan mode must plan FEATURES: O36

#### O36. Plan mode can only plan a defect fix — CLOSED (entry point + change model)

**Questions 3 and 4 are both answered, by the user, not inferred:**

> "I do expect a feature to get broken down into smaller parts."
> "A feature should also go through the same TDD cycle."

Question 4 therefore needs no new answer: the acceptance criterion for a feature is
the SAME one. `--feature` emits an ORDERED list of parts, each with its own
`expect_green`, and `_validate_feature_plan` **refuses a plan whose part has no
acceptance tests** — otherwise feature mode would be the one path into the loop
that skips the check the loop exists to apply. The O34 exception is what lets those
tests be red for the right reason before the code exists.

`--feature` and `--defect` are mutually exclusive: they select different system
prompts and produce different output shapes (an ordered list vs one ticket), so
silently preferring one would make the other a no-op the caller cannot see. A
defect plan still returns a single ticket; a one-part feature still returns a list,
because a caller that has to branch on "one or many" will get it wrong.

**The ordering consequence, handled:** part 2 legitimately inserts into a file part
1 creates. Validating the whole plan against the tree as it is now would reject
every feature that builds on itself, so validation walks the parts in order and
carries forward the files earlier parts create. Scoped, though — a path no earlier
part creates must still exist, or a typo sails through — and two parts may not
create the same file.

**Live, end to end, twice.** The first run is the finding:

* It returned four well-formed ordered parts, ops correct, `expect_green` on each
  — and created every file under a **`patchgate/` package that does not exist**.
* Cause: O31's context is keyed on SYMBOLS, and a feature request names none.
  `extract_intent_symbols` returned `[]` and the context was `""`, so nothing had
  told the model the code lives in `src/agent_loop/`. **That is structural, not
  tuning:** for a defect the thing complained about is already there to find; for a
  feature the question is "where does new code GO", and only the layout answers it.
* Fixed by `context.build_layout_context()`, injected in feature mode: directories
  with file counts, a sample of real paths, where tests must live, and the
  `file_scope_whitelist` when the profile sets one (a profile that may only edit
  `scripts/` must not be shown the rest of the repo as a home for new files).
* Re-run: **zero mentions of `patchgate`**; the plan targets `src/agent_loop/cli.py`
  plus three new modules under the real package. It then failed validation on one
  wrong anchor (`parser = argparse.ArgumentParser(` where the code says `ap = `) and
  ran out of rounds at `--max-rounds 2`. The validator working, and O13's lesson
  again: two rounds is not enough for a four-part plan.

32 tests across the two O36 files.

Original text follows.

#### O36 (original). Plan mode can only plan a defect fix — MEDIUM

**Answered question 3, and the answer was neither of the two options as written.**
The requirement settled it: *"a new feature can be completely new files or adding
something to an existing file. Both should be accommodated."* Routing to developer
mode cannot do that — `_edit_file` returns `ERROR: file not found`, so **developer
mode cannot create a source file either**; only `write_test` creates files, and
only under `test_sources`. Neither option was free.

So the region model grew an **operation**, per region:

| `op` | file | anchor | the model returns |
|---|---|---|---|
| `replace` (default) | must exist | must resolve | the region's new body |
| `create` | must **not** exist | none | the whole file |
| `insert` | must exist | must resolve | only the code to add |

`op` is deliberately **not** folded into `kind`. `kind` is the LOCATOR strategy
(decl/indent/line) consumed by `find_region`; the operation is a different axis,
and one field with two meanings is the ambiguous helper this repo avoids.

Because `op` is per-region, **one ticket mixes all three** — a new module, a hook
into an existing file, and a signature change at the call site. That is the
requirement met directly, and multi-step ordering is already expressible as a list
of tickets (the O33 loader accepts one).

**`insert` earns its keep twice.** It is also the fix for §6 trap 4, "the region
model cannot add a module-level function", which is a pre-existing limitation that
already bit F1-F6 on DEFECT work: F5 emitted a mid-file `import` because its
region began at a `def` and it had no legal alternative.

**Three things that had to come with it:**

* `Workspace.stage_new_files()` — `diff()` is `git diff`, which ignores untracked
  files, so a created file would be **absent from its own patch** and `promote`
  would land a change referencing a module the patch does not add. The red phase
  learned this once with a new test file; this is that fix generalised to source.
  All four apply sites in `loop.py` now go through `_apply_regions`.
* Per-op prompt text. Left implicit, `create` gets a fragment instead of a file and
  `insert` gets the anchored block re-emitted, duplicating it.
* `lines_1based` said `1-0` for a create region (start=0, end=-1), which would
  have gone into the prompt and `--list` as though it were a real range.

**The interaction that would have shipped O36 broken — O34.** A feature's first
red test imports something not yet written, so it fails with
`ImportError`/`AttributeError`. O34's rule reads that as "died in its own
scaffolding, proves nothing" and **refuses in test mode** — so the gate added three
commits earlier would have rejected every feature on arrival. Now
`reached_an_assertion(kinds, feature=True)` accepts the "this name is not there"
family, and `gates.is_feature_ticket(ticket)` derives it from `op: create` so the
caller does not declare it twice. Deliberately narrow: a `TypeError` in a stub is a
broken test whether the work is a feature or a fix. Same output, opposite meaning,
and only the ticket knows which job it is.

27 tests. Six mutations, all killed — including widening the exception to
`TypeError`, which is the mistake that would make it meaningless.

**STILL OPEN — the entry point.** The change model can express a feature; plan mode
still cannot ask for one:

* `run_plan(repo, defect_description, ...)` and `--defect`; `PLAN_SYSTEM` says
  "analyze the defect, localize it in the codebase". A `--feature` sibling wants a
  prompt that asks for new files and emits `op` per region.
* Plan emits ONE ticket. A feature is usually several with an order, and the
  loader already accepts a list.
* Question 4 is still unanswered: what is the acceptance criterion for a feature?
  `expect_green` presumes tests that exist. Test mode can generate them, and the
  O34 exception now lets them be red for the right reason — but that path has not
  been run end to end.

Original text follows.

#### O36 (original). Plan mode can only plan a defect fix — MEDIUM

Requested directly: *"plan mode should also be capable of planning a complete
development idea, not only a defect fix."* Recorded here because it is a
capability gap, not a defect, and nothing in the backlog covered it.

Everything about the mode presumes a defect that already exists in the code:

* the signature is `run_plan(repo, defect_description, ...)`;
* `PLAN_SYSTEM` tells the model to "analyze the defect, localize it in the
  codebase using the context provided";
* the ticket schema is defect-shaped — `defect`, `spec`, `regions` — and
  **`regions` must resolve against the current tree**, which is checked before
  the panel ever runs (`regions.extract` → retry on `RegionError`);
* the CLI flag is `--defect`.

That last point is the load-bearing one. A feature's code **does not exist yet**,
so there is nothing for an anchor to resolve to, and a feature plan would be
rejected by the region check on every round until `max_rounds` ran out. The mode
cannot express "add a new module", only "change these existing lines" — which is
the same limitation HANDOVER §6 trap 4 records for the loop itself.

What a feature plan needs that this shape cannot carry: new files, an ordering
between steps, more than one ticket out of one request, and acceptance criteria
that are not "these named tests go from red to green in one edit".

**Sequencing note.** Do O31 first. Plan mode currently builds its prompt from the
defect text plus four profile fields and has never been shown the codebase
(`build_context_slice` is imported and never called), so it is a poor foundation
for the larger job. The context injection wants designing once, for both.

**Open design questions**, none of which should be guessed:

1. One mode with two entry shapes (`--defect` | `--feature`), or a separate
   `feature` mode? The gate ladder and panel are worth reusing either way.
2. Can a plan emit MORE than one ticket, with a declared order? The loader now
   accepts a list (O33), so the file format is already capable of it.
3. How does a region-based loop implement a ticket whose files do not exist?
   Either the ticket schema grows a "new file" kind, or feature tickets go to
   developer mode, which edits by tool call and has no region model.
4. What is the acceptance criterion for a feature? `expect_green` presumes tests
   that exist. Test mode can generate them, but for a feature it would be
   generating them against code that is not written.

Question 3 is the one that decides the shape of the rest.

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

### 2026-08-11 — found by the first real feature run through the loop: O37, O38

#### O37. `anchor not unique` feedback truncated every hit to the same string — CLOSED

**Found live, not by reading.** A `--feature` plan against the consumer's
`TradeCopierEngine.cs` was rejected with `anchor not unique (2 hits)` on
`public int CalculateFollowerQuantity(CopierRelationship rel,`. The file has two
overloads of that method, and `regions.py` previewed each hit as
`lines[i].strip()[:60]` — where the two signatures are still BYTE-IDENTICAL. So
the feedback asserted the hits differed while displaying the same string twice.

The model, given nothing to disambiguate with, lengthened the anchor with an
INVENTED parameter list (`string leaderSymbol`), which converted a recoverable
ambiguity into `anchor not found`. **Four of six plan rounds went on this**, and
the run ended `MAX_ROUNDS_EXHAUSTED` with an otherwise-correct 4-part plan.

**Same class as O8's lint digest: feedback the caller cannot act on is a gate
that only looks like one.** The fix computes the preview window from the hits —
past their longest common prefix, so the differing text is always visible — adds
line numbers, and when the lines are *truly* identical says so explicitly and
points at `re:`, because no amount of lengthening can separate them.

Seven mutations, all killed. One tightened a test of mine first: asserting the
two rendered hits merely *differed* was satisfied by the `L1:`/`L5:` prefixes
even with the 60-char truncation restored — the "assertion the fallback also
satisfies" shape again. It now compares the code text with the prefix stripped.

#### O38. A rejected plan was discarded entirely — CLOSED

Same run. `plan.json` is only written when a plan is APPROVED, so
`MAX_ROUNDS_EXHAUSTED` left nothing but `result.json` — discarding a 4-part plan
that was **one bad anchor** from usable, after a round-1 call that cost 377s.
The only recovery was to hand-parse `r6_plan_raw.txt`.

The last plan that PARSED is now written to `plan_rejected.json` in the same
wrapper shape as `plan.json`, so it can be fixed by hand and fed straight to
`--tickets`. Deliberately a different FILENAME: nothing downstream may mistake a
rejected plan for an approved one, and a test asserts `plan.json` is absent.

---

#### O39. Anchors are invented because the plan model has never seen the file — CLOSED

**The root cause behind BOTH failed feature runs**, and the reason O37 alone was
not enough. Plan mode asks for EXACT-MATCH anchors into existing files, and
supplies no file content: `build_intent_context` is keyed on symbols (right for a
defect, empty for a feature) and `build_layout_context` answers "where does new
code go" (O31), not "what text is in this file". So the model anchors from
memory. Live evidence:

* five rounds hunting `LoadCopierConfig` in a file whose method is
  **`LoadFromDisk`** — unguessable, and no amount of re-prompting fixes a guess
  about text that cannot be seen;
* `TranslateSymbol(..., CopierRelationship relationship)` where the file says
  `rel = null`;
* the O37 preview, truncated mid-identifier at `...FullNam`, COPIED as the next
  anchor and completed as `FullName, relationship)` where the file says `, rel)`.

`anchor not found` now carries real candidate lines from the file, ranked by
similarity, with line numbers. On the two anchors that killed run 2 the correct
line is offered — first, for the one that killed it at round 10. Truncated
previews are explicitly marked `...[TRUNCATED, not a copyable anchor]`, because
the model demonstrably treats preview text as copyable.

**What mutation testing changed in the fix itself, which is the durable part:**

* An identifier-match BONUS was deleted, not tested. It survived mutation, and on
  all three live anchors it produced byte-identical output. An untested weight
  that changes nothing is a knob for a later reader to mis-tune.
* Two overlapping noise guards were deleted down to one. `s in ("{", "}", "};")`
  was unreachable (all shorter than 4 chars) and the `len(s) < 4` floor that
  replaced it was redundant with the similarity floor. Neither was pinned by a
  test.
* The similarity floor (0.3) is now pinned AT ITS BOUNDARY. Two mutations
  survived because every other test used lines scoring ~1.0 or exactly 0.0 --
  **a threshold no test approaches can be changed to anything.**
* One of my own tests asserted an ORDERING that only the deleted bonus produced.
  Asserting on incidental heuristic output turns a heuristic into a requirement
  by accident; it now asserts only that both plausible lines are offered.

Also: `_candidates()` in the test file asserts the message is the not-found
variant first, because twice I chose a fixture anchor that was a literal
substring, got `anchor not unique`, and misread the resulting IndexError as a
code defect.

---

#### O40. A multi-line anchor is unsatisfiable and was reported as a plain miss — CLOSED

Run 3 of the feature plan kept this anchor for THREE consecutive rounds:

    "        public string TranslateSymbol(string rawSymbol, CopierRelationship rel = null)
        {"

`find_region` tests `anchor in line`, one line at a time, so an anchor containing
a newline can never match ANYTHING. `anchor not found` was true and useless: the
request is impossible, and no model can guess its way out of an impossible
request. Meanwhile O39's candidates were visibly working on the same plan --
three other anchors self-corrected to real text between rounds 3 and 4 -- so this
one anchor alone would have consumed the remaining six rounds.

The failure now says the anchor spans multiple lines, that anchors match one line
at a time, that `kind=decl` already expands from the opening line to the end of
the block (so the newline was never needed), names the opening line to use
instead, AND appends O39's candidates for that opening line -- because in the
observed case the opening line was ALSO wrong (`relationship` for `rel`), and one
round should not be spent per correction.

#### O41. Progress output never reached a piped log — CLOSED

The same run looked hung for 26 minutes. Python block-buffers a non-tty stdout,
so four rounds of progress lines were still in the buffer; the artifact file
mtimes were the only evidence the process was alive. **Anything long enough to
background is exactly what gets piped**, so this is the normal case. `main()` now
sets `line_buffering=True`. A test pins the premise (a piped child really is
block-buffered) as well as the fix, since the fix is worthless if the premise
ever stops holding.

---

#### O42. Plan/test shipped the budget the docs warned about — CLOSED

Run 4 died at round 1 with `IMPLEMENTER_UNREACHABLE`: **207,078 characters of
reasoning, empty content, eval_count=48000, done_reason=length.** Plan mode
shipped `max_tokens=48000, think=True`, and `agent_loop.config.example.json`
had been warning, in prose, that *"48000 with thinking on is the configuration
that returned 125,070 characters of reasoning and EMPTY CONTENT"*.

**A documented hazard that is also the default is not documented.** Plan and test
are now 96000, matching the implementer role, because their answer is a whole
ticket set or a whole test file re-emitted every round. `developer` keeps 48000
deliberately -- one tool call per turn, bounding a fifteen-turn run -- and a test
pins that CONTRAST so nobody raises it by analogy.

**Why no test caught this, which is the transferable part.**
`test_shipped_example_file_loads_and_equals_the_defaults` already asserts the
example equals `DEFAULTS`, and it passed: the machine-checkable VALUES agreed
perfectly. The contradiction was between the values and the PROSE sitting six
lines above them. Consistency tests pin what they can compare, and a warning
about a number is not comparable to that number. The example's comment now
records what the defaults are and why they differ from each other.

Also worth carrying: this is the third time in this run of work that a longer
input caused a failure that a shorter one did not. Run 3's round 1 spent 172k
chars of reasoning and squeaked in under the same 48000; run 4's brief was 1.8 KB
longer and did not. **A budget that is passing is not necessarily a budget that
fits.**

---

### 2026-08-11 — the first feature plan to reach the arbiter: O43, O44, O45

These three are one COMPOUNDING chain, and the chain is the finding. Run 5
printed:

    round 3: plan: 4 part(s), regions check OK
    [panel] REJECT  [glm-5.2=REVISE(6), minimax-m3=REJECT(7)]
    [arbiter] SHIP (upheld=0 rejected=18 out-of-scope=0)
    plan: logs/agent_loop/PLAN/plan.json (1 part(s))

Four parts validated. One part written. Nothing said three were dropped.

#### O43. Arbiter SHIP truncated a feature plan to its first part — CLOSED

The SHIP branch assigned `result["plan"] = ticket` -- the bare FIRST part -- while
the panel-approve and fast-plan branches both assign `tickets if feature else
ticket`. **Every arbiter-shipped feature plan since `--feature` existed was
silently truncated**, and it was never noticed because §12f recorded that the
full feature chain had never been run.

#### O44. A rejection from an earlier round survived into a success — CLOSED

`result.pop("error", None)` was missing from the SHIP branch, so `result.json`
reported `verdict: ARBITER_SHIP` beside round 2's anchor error. A reader takes
that error as the outcome.

#### O45. The panel only ever saw part 1 of an N-part plan — CLOSED

The review prompt rendered `json.dumps(ticket)` -- part 1 -- and, because the
feature branch set `regs = []`, an EMPTY "Resolved regions" section. So thirteen
findings were raised against a quarter of the plan, and both reviewers said so:
minimax's top blocker was *"there is no region that consumes `PerTickerRatios`
anywhere on the sizing path... a dictionary that nothing reads is dead weight; the
defect is not closed"*, and glm's was the same shape. **They were right about what
they were shown and wrong about the plan**, because parts F2-F4 -- the consumer,
the sizing change, the fail-closed branch -- were never in the prompt. Both also
blocked on the empty regions block.

**Why this chain is worth remembering.** O45 produced a review of a data model
with no consumer; the arbiter dismissed all 18 findings and shipped; O43 then
wrote exactly that data-model-only part to disk. The output on disk was the
precise failure the brief had named as forbidden ("PerTickerRatios and
CustomSymbolMappings are today parsed by NOTHING... do not repeat that shape") --
assembled by three harness defects, none of which is about sizing at all.

The panel now receives every part, is asked explicitly whether the parts COMPOSE,
and gets a per-part resolved-region list (line ranges for real anchors, `(new
file)` for creates, and `anchor deferred` where a later part targets a file an
earlier one creates).

**Ten mutations across these three, and one survivor is the recurring shape
again**: `"deferred" in prompt` was satisfied by the SENTENCE explaining what
deferred means, so removing the note stayed green. That is the third instance in
this stretch of an assertion the fallback also satisfies -- it is now the single
most reliable way I write a useless test.

**Still open, and not mine to close here: O20/O28.** The arbiter upheld 0 of 18
findings and shipped against a REVISE and a REJECT. Some of those findings were
substantive (ratio validation for NaN/negative, deep-copy semantics on
`ToRelationships`, the missing-key fallback, serialisation of a new dictionary
property into existing configs). An arbiter that ships against a unanimously
objecting panel is exactly the measurement O28 is supposed to provide and cannot,
on a corpus of one.

---

#### O46. A transport failure hid the reason and lied about the attempt count — CLOSED

A plan run died with

    qwen3.5:cloud failed after 3 attempts: HTTPError: HTTP Error 400: Bad Request

**Two falsehoods in one line.** `_retryable` already excludes 400 (correctly -- a
bad request fails identically forever), so exactly ONE call was made; the message
hardcoded `max_retries`. And `str(HTTPError)` is the status line only, so the body

    {"error":"max_tokens (96000) exceeds model's maximum output tokens (65536)
              for model qwen3.5"}

was discarded -- a complete, actionable diagnosis, thrown away. I read that
message myself and reported "the loop retried three times" before checking, which
is what a lying error costs.

`describe_exception()` now appends the body (bounded, and a body that cannot be
read is never a new failure), and the count is the number of attempts ACTUALLY
made, singular when it is one. Five mutations, all killed.

**Note the interaction with O42.** Raising plan mode to 96000 was right for the
kimi implementer, whose ceiling is above it, and it makes `qwen3.5` (65536)
unusable for plan mode without an override. A budget is only valid against a
specific model's ceiling, and nothing in the config expresses that relationship.
Not filed as a defect because auto-clamping would be guessing at limits the API
does not advertise until you exceed them -- but a consumer switching implementers
needs to know.

---

### 2026-08-11 — O28 gains its SECOND labelled case, and it indicts the arbiter

O28 has been blocked on labelling, not running: the corpus was one patch, one
finding set, one ruling. **Here is case 2, labelled by a human, from a real plan
review of a real trading addon.**

**The case.** Slice 1 of the copier ratio feature, planned by `qwen3.5:cloud`
(kimi was 503), reviewed by `glm-5.2` + `minimax-m3`, arbitrated by
`deepseek-v4-pro`. Round 4: `[panel] REVISE [glm=REVISE(7), minimax=REVISE(13)]`,
then `[arbiter] SHIP (upheld=0 rejected=26 out-of-scope=4)`.

**The arbiter was wrong, and the panel was right.** Human ruling on the two that
matter, both of which the arbiter rejected:

1. **glm, BLOCKER, CORRECT AND SHOULD HAVE BEEN UPHELD.** The plan's exit formula
   `Math.Sign(leaderQty) * Math.Min(Math.Abs(leaderQty), Math.Abs(currentFollowerPosition))`
   is signed, while every existing return from that method is an unsigned
   magnitude. glm stated the losing sequence exactly as this profile's
   `arbiter_rules` require: leader `+2`, follower `-3`, and the exit INCREASES the
   follower's position instead of reducing it. That is a position flip -- the same
   class as `P1-56`, which this addon was hardened against after it shipped live.
2. **minimax + glm, BLOCKER, CORRECT.** The plan invented an `ExtractRootSymbol`
   that "strips trailing non-alpha chars" while giving `MESU25 -> MES` as its
   example, which requires stripping an alpha character. minimax caught the
   internal contradiction; the platform's real format is space-separated
   (`"MES 03-26"`) and the file already parses it as `Split(' ')[0]`. A root parsed
   wrongly matches no rule, which under this slice refuses EVERY entry.

Also correctly raised and rejected: the entry clamp was written against
`MaxPositionSize` instead of the existing available-capacity clamp
(`MaxPositionSize - |position|`), and `TranslateSymbol` was to return `null` as a
"skip" signal with no region updating either live caller.

**So the score on case 2: the two-family panel found four real defects, at least
one of which loses money, and the arbiter upheld none of them and shipped.** It
had upheld 2 findings in round 2 and 3 in round 3, then upheld 0 in round 4 --
while the panel's finding COUNT went UP (glm 7 -> 10 -> 7, minimax 7 -> 8 -> 13).

**What this means for O20, concretely.** O20 is "mitigated, not closed" and was
waiting on O28's measurement to decide. Case 2 says the mitigation is not enough:
an arbiter that rejects a correctly-stated naked-risk finding is worse than no
arbiter, because the panel's REVISE was the right answer and the arbiter
overrode it. The cheapest change consistent with both cases is to make SHIP
unavailable when any counted reviewer returns a BLOCKER that the arbiter does not
address individually -- i.e. an unaddressed blocker forces ESCALATE. Not
implemented here; recorded so the decision rests on two cases instead of one.

**Method note for whoever extends the corpus.** These findings were labelled by
reading the plan against the FILE, not by reading the findings alone. Three of the
four are only visible if you know what the existing method returns -- which is
also why the plan contained them: the planner was given anchors and line numbers
but never the invariants of the code it was rewriting.

---

#### O47. Overlapping regions in one ticket passed every gate — CLOSED

Found by reading a shipped plan's line ranges by hand, which is the only reason it
was found at all.

The plan named four regions in one file. Region 1 anchored the whole of
`CalculateFollowerQuantity` (**429-534**) and region 2 a branch INSIDE it
(**441-462**); region 3 (**382-427**) contained region 4 (**404-424**). Two nested
pairs.

`apply()` splices per file bottom-up so earlier spans stay valid -- correct for
disjoint spans, and silently wrong for nested ones: the outer replacement rewrites
the same lines the inner one rewrites, so what lands depends on application order
and one edit is lost or duplicated.

**Everything passed it.** `regions.extract` resolved all four. `--list` printed
them. Both reviewers filed fifteen findings without mentioning it. The arbiter
shipped. It is an easy mistake for a model to make and a hard one to see, because
**each anchor is individually correct** -- there is nothing wrong with either
anchor, only with holding both at once.

`extract()` now rejects it, naming both spans and which one contains which, so it
fails like a bad anchor does -- before a model is asked to fill regions that cannot
both be applied. Same span in different FILES is still fine; adjacent spans are
still fine.

**One mutation survived and removed code rather than adding a test**: an explicit
`op == CREATE` skip could not change any outcome, because a create carries
`start=0/end=-1` and `b.start_line <= a.end_line` is unsatisfiable against it. The
skip is gone and the arithmetic is documented. That is the fourth inert guard this
stretch of work has deleted rather than tested.

---

#### O48. Test mode was Python-only, and its gate was vacuous — CLOSED

**The first `--mode test` run this package has ever had against a non-Python
profile.** §12i listed it as untried; it was broken four ways at once.

Against the C# NT8 profile (`test_sources = scripts/ninjatrader/addons/*Tests.cs`)
it wrote **`tests/acceptance/test_generated.py`** containing `import pytest` and
`from TradeCopierEngine import CopierRelationship`, i.e. Python importing a `.cs`
file as a module, calling `CalculateCopyQuantity` (the real method is
`CalculateFollowerQuantity`), and passing a C# `out` parameter by value. Then it
printed `[test-first] WARNING: tests pass at baseline` and exited 0.

1. **The path was a hardcoded Python default in two places** -- `run_test`'s
   signature AND `--test-file`, which was passed unconditionally so it overrode
   anything else. Now derived from `test_sources`, substituting the ticket id for
   the `*` so the file still matches the glob -- which is also what keeps it inside
   `protected`, so the implementer cannot edit the tests it must satisfy.
2. **`TEST_SYSTEM`'s output example was literally ```python / import pytest.** The
   prompt taught Python regardless of profile.
3. **"Code under test" was `src.splitlines()[:100]`** -- the head of a 2,700-line
   file whose regions are at 382-534, so the test writer never saw the method and
   invented a name for it. It is O39's lesson in a third place: the model was given
   coordinates and not content. Now the RESOLVED REGION TEXT, plus an existing test
   source as a style reference so the harness convention is visible.
4. **`expect_green` was labelled "Test names to use".** These strings are matched
   against the runner's FAILURE LINES, so on a harness that prints
   `[FAIL] <message>` they are assertion messages. Method names could never match,
   and the test-first check -- the one check between the loop and a fake gate --
   would have passed while verifying nothing.

Green-at-baseline is now an ERROR, not a warning, so the run exits non-zero rather
than printing a caution above `tests written to: <path>`.

**A pre-existing test caught a flaw in that refusal, which is worth recording.**
`not outcome.failures` is NOT "everything passed": a runner can report a failure
COUNT while printing no identifiable failure names, leaving the parsed set empty
for a suite that is red. Harmless as a warning, wrong as a refusal. The condition
now requires `outcome.failed == 0` as well.

**Eight mutations. Two survived and both were my tests' fault, the same shape as
before**: `"TheMethodUnderTest" in prompt` was satisfied by the TICKET JSON's
region anchor, which is pasted into the same prompt -- so emptying the regions
entirely stayed green. It now asserts on the method BODY, which only the region
text can supply. That is the fourth instance in this stretch of an assertion the
fallback also satisfies.

---

*End of backlog. Update as items are completed.*