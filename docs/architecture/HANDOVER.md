# HANDOVER — agent-loop

This file is the orientation layer: state, hazards, commands, and the traps that
cost real time. Open issues live in [BACKLOG.md](BACKLOG.md), keyed **O1-O36**;
that file is authoritative and this one summarises.

**This document has grown by accretion — sections are appended per session and
EARLIER SECTIONS ARE OFTEN SUPERSEDED.** §0's parallel-session hazard is history,
not current state. When two sections disagree, the higher-numbered one wins.

---

## START HERE — checkpoint, 2026-08-11 (session 4)

| | |
|---|---|
| `main` | see `git log --oneline -1`; working tree was clean at last commit, **nothing unpushed** |
| Tag | **`v0.4.0`** (`e780e29`), on origin. First tag verified green on 3.12 *and* 3.14. **Tests have moved on since the tag** — 470 now |
| Tests | **470 pass on both 3.12 and 3.14**; `selftest` 12/12 from the checkout |
| Consumer | tvDownloadOHLC **still pins and has installed `v0.3.0`**. Do not trust a number written here — it went stale three times in one session. Run `git log --oneline v0.3.0..HEAD \| wc -l` for how far behind the consumer is, and `git log --oneline v0.4.0..HEAD` for how stale the latest TAG is (non-empty means cut `v0.5.0` rather than pinning `v0.4.0`) |

**Closed in session 4:** O4, O7 (modes), O8, O14, O22, O23, O29-O36.

**Open — the complete list**, because two summaries in this session under-reported
it by quoting a short version:

| | |
|---|---|
| **O28** | Second labelled arbiter corpus. **The bottleneck is the LABELLING, not the running** — someone must judge which findings were actually correct. The corpus today is literally one case: `o3.patch` + one finding set + one shipped ruling. The 12-entry results JSON is 12 model CONFIGS against that one case, not 12 cases. |
| **O20** | *Mitigated, not closed.* Arbiter 3/5. **Cannot close before O28**, because O28 is the measurement it would need. |
| **O5** | `Finding.signature` breaks on suffix changes, so `thrashing()` can fire on a converging ticket. Its planned fix delegates dedup to the arbiter — **revisit that premise first**, given O20. |
| **O21** | Self-authored tests covering half a fix. **Six mutations survived a green suite this session**, all found by mutating and none by reading. Wants a design answer, not a patch. |
| **O10** | Closed for wiring, **open for conventions** — docs mode does not inject the house format, so generated docs need editing. |
| **O8 remainder** | The OpenAI cached-token field. Left deliberately: no key, so it cannot be checked against a real response shape, and guessing a response format is what produced O24's and O34's confident wrong readings. |

**O5, O20 and O28 are one cluster**, and the dependency runs one way: O5's fix
needs arbiter trust, O20 needs O28's measurement to close, and O28 needs human
labelling. So it is O28 first or none of the three.

**O13 is not a defect** — it is the observation that round budgets must scale with
ticket size, re-confirmed when the four-part feature plan exhausted
`--max-rounds 2`.

**Next, in the order argued for:**

1. **O28** — the arbiter ranking rests on ONE patch and ONE finding set. A second
   labelled corpus is worth more than a fifteenth model, and both developer mode
   and feature planning now produce suitable material every run.
2. **O21** — a self-authored acceptance test can cover half a fix. Not
   theoretical: **six mutations survived a green suite this session**, every one
   found by mutating and none by reading. Wants a design answer (review the test
   against the diff's blast radius before it locks), not a patch.

Also outstanding and cheap: **cut `v0.5.0` and re-pin the consumer to it.**
`v0.4.0` is already several fixes stale, so cutting the tag is the cheaper half of
the job. Until the consumer is re-pinned, O30 still throws its bare
`FileNotFoundError` in that venv, and none of O23/O31/O33/O34/O36 is present there.
Bump `__version__` and `pyproject.toml` together — a test pins them to each other.

**The one lesson session 4 kept re-learning**, in six separate items: *a check
that has only ever run on the machine, or against the double, or against the
assumed output format, it was written for is not a check.* Its corollary cost the
most and is now the local standard: **mutate the fix.** SIX mutations survived a
green suite this session:

* a half-covered `id` validation (O33),
* a flag that was forwarded but never honoured (O23),
* a guard whose real job turned out to be the error message, because `rmdir`
  already provided the safety (O23),
* an empty-context guard shadowed by an earlier return (O31),
* a lint assertion the `output[-4000:]` FALLBACK also satisfied, so reverting the
  fix stayed green (O8),
* and a mock returning a dict where the real function returns an int, which made
  `main()` return a dict and the test blame the code (O8).

**Two recurring shapes, and both are worth a reflex check.** *Two guards that can
return the same value, where the test only ever reaches the first* (three
instances). And *an assertion the fallback path also satisfies* — which is the
single most common way I wrote a useless test in this session.

---

## §0 READ FIRST (session 1-2 history — superseded, kept for provenance)

**Two sessions have been editing this repo in parallel.** This one (review +
tickets + caching) and another (report/replay/caching v1, roadmap, docs mode).
That produced three surprises worth knowing about:

1. Work committed by the other session under a **misleading message**. Commit
   `813053b "feat: Phase 9 — learning feedback + context bloat control"`
   actually contains the **22 review fixes** from the first half of this
   session, not Phase 9 (Phase 9 is `999658f`). `git log` will not find those 22
   defects; BACKLOG.md is the only searchable record.
2. A branch was created and then deleted under me. `review/followups-f1-f6` no
   longer exists; its four commits (`e241882`, `41e5fd0`, `0812578`, `643b93f`)
   are all ancestors of `main`.
3. Files were modified in the working tree mid-task by the other session
   (`docs_mode.py`, `test_brainstorm_docs_modes.py`), briefly leaving the suite
   red for reasons unrelated to the change in flight.

**Consequence for a new session: do not use `git add -A`.** Stage explicit
paths. Before committing, run `git status` and confirm every staged file is one
you actually touched. If two sessions will run concurrently again, split by
directory or serialise.

### State as verified at handover time

| | |
|---|---|
| `agent-loop` branch | `main`, working tree **clean** |
| `agent-loop` HEAD, tag, tests | **see START HERE at the top of this file** — these rows went stale three times in one session, so they now live in exactly one place |
| Closed / open issues | O3, O6, O14-O19, O23, O24-O27, O29-O36 closed, plus O4 and O8; O20 mitigated; O21, O22, O28 open |
| Developer mode | **Works, and is test-first.** First patch it ever produced was a no-op that passed every gate; that is what motivated the red phase. See BACKLOG O18. |
| First real loop run since F1-F6 | O1: 3 rounds, **did not converge**, `ARBITER_NEVER_RAN`. The gate ladder refused all three patches — one of which would have corrupted files with conflict markers. Round 3's architecture was right and needed one flag removed, done by hand. See BACKLOG O13. |
| `python -m agent_loop.selftest` | **12/12** (offline, ~40s, free) |
| Arbiter | `mistral-large-3:675b-cloud`, **chosen by measurement** over 18 configurations / 14 models / 4 services. Best available and still only 3/5 — see O20, O28 |
| Providers | ollama, anthropic, openai, **gemini**, **github** (retired), **agy** (CLI). The three Gemini paths are NOT interchangeable — see §10 |
| `tvDownloadOHLC` branch | `harden/riskguard-p0-51`, HEAD `88c3a723` (**unpushed**) — pins + installs agent-loop `v0.3.0` |
| Consumer profiles | present in tvDownloadOHLC HEAD and clean; `git log -S` attributes them to `fb682a93`, which was already in the log when this session began — the other session has been committing and possibly amending there, so **trust file contents over commit attribution in that repo** |

### ~~Two things that are broken right now~~ — RESOLVED 2026-08-10 (later session)

Both consumer blockers are closed. `agent_loop` **is** installed in the
tvDownloadOHLC venv, and `requirements.txt` pins **`v0.3.0`**. Fixing them
exposed two defects in the package; see BACKLOG **O9-O11**. Two things to carry
forward:

- **`v0.2.0` is a poisoned tag.** It carries O9 (`Path.read_text(newline=)`,
  Python 3.13+ only) and therefore cannot run *at all* on Python < 3.13. The
  consumer venv is 3.12. Pin `v0.3.0` or later; never `v0.2.0` or `v0.1.0`.
- **Run the suite on 3.12, not just the dev 3.14.** O9 was invisible on the dev
  interpreter and bricked every ticket on the consumer's:
  `C:/Users/vinay/tvDownloadOHLC/.venv/Scripts/python.exe -m pytest tests/ -q`.
  217/217 pass on both today.

And one structural lesson, because it produced *both* new defects: **the test
suite calls library functions directly with correct arguments, so nothing
exercised the CLI wiring users actually invoke.** Docs mode had never run once —
`cli._docs()` passed `run_docs()` positional args against a mismatched
signature — while the docs tests passed the whole time. New CLI tests drive
`main(argv)` so argparse is in the loop. Prefer that shape for anything
user-facing.

---

## §1 Where everything is

```
C:/Users/vinay/agent-loop/                 the package (this repo)
  src/agent_loop/                          loop, gates, arbiter, regions, providers, ...
  profiles/self.py                          the self-hosting profile (agent-loop-self)
  tickets/review_followups.json             the F1-F6 tickets
  tests/acceptance/                         the suite (count in START HERE, not here)
  src/agent_loop/config.py                  EVERY tunable, with the reason for each
  agent_loop.config.example.json            copy to agent_loop.config.json to override
  logs/agent_loop/F1..F6/                   patches + artifacts from the self-hosted run
  logs/agent_loop/loop_run_F1-F6.log        the full run log
  docs/architecture/BACKLOG.md              OPEN ISSUES (O1-O36) ← authoritative
  tests/fixtures/arbiter_bench/             LABELLED arbiter corpus + run_bench.py
  tests/fixtures/compactor_bench/           LABELLED compactor corpus + run_bench.py
  docs/architecture/ROADMAP.md              what to build next (written by the other session)
  docs/architecture/IMPLEMENTATION_DECISIONS.md   why each non-obvious choice was made

C:/Users/vinay/tvDownloadOHLC/             the main consumer
  scripts/agent_loop_config/*.py            nt8-riskguard + python-tvdownloadohlc profiles
  scripts/agent_loop/                       PREDECESSOR copy, drifted; being cleaned up
                                            by the other session — do not edit
```

---

## §2 What this session did

**Phase 1 — full review of the package (6,001 lines).** Found 22 defects, all
fixed, 52 regression tests added. The five blocking ones: `--mode test` stashed
the live working tree and never restored it; the pytest parser read an ordinary
green run (`17 passed, 1 warning`) as "runner never finished", which made
`capture_baseline` refuse and every Python ticket ERROR; the
`python-tvdownloadohlc` profile could not run one ticket (broken `test_cmd`, and
a `build_cmd` naming a file that does not exist); `--review-verify` crashed on a
nonexistent attribute; and developer mode had no protected-path gate and edited
the live tree against an empty baseline. Full table in BACKLOG.md.

**Phase 2 — reviewed the other session's `f2103cd`** (report, replay, caching,
lint gate, signature change) and found that both new features had a defect
defeating their own purpose.

**Phase 3 — ticketed six of those findings and had the loop fix them.**
`tickets/review_followups.json` F1-F6, each with a red acceptance test.
Result: **6/6 unanimous panel APPROVE in round 1**, all gates green, nine red
tests turned green. Patches landed in `41e5fd0`.

**Phase 4 — three loop defects the run exposed**, fixed by hand (same commit):
the region locator collapsed multi-line signatures to one line while `--list`
said OK; `export_patch` mangled line endings so patches did not apply to LF
sources; and `loop.py` never passed `files=` to the lint gate.

**Phase 5 — documented O1-O8** (`0812578`) and **fixed the caching defects**
(`643b93f`).

---

## §3 What the self-hosted run actually proved, and did not

Proved: the gate ladder works end to end on a real codebase — static, compile
with `{files}`, test against a frozen baseline with `expect_green`, lock-scope.
The test-first refusal works. The region model works for single-file,
single-function tickets.

**Did not prove:** the arbiter never ran (nothing was contested), compaction
never triggered (everything converged in round 1), nothing reached the
settled-decisions store, and `APPROVE_PARTIAL` / `PANEL_UNREACHABLE` /
`NOT_CONVERGING` were never reached. Six modes had never been run at all:
`plan`, `test`, ~~`developer`~~, `brainstorm`, ~~`docs`~~, `review`.

**Session 3 update.** `developer` has now been run, and the pattern held for a
third time: it had never produced a patch, and the reason was a total failure
(the provider discarded the model's tool calls), not a latent risk. Three modes
remain unrun — `plan`, `test`, `brainstorm` — plus `review`. Assume the same of
them, and smoke-run each through `main(argv)` rather than by calling the library
function, which is what caught O10 and O15.

The arbiter has now run for real, twice, and **got it wrong both times** — once
by inventing evidence to reject the only correct finding, once by ruling the
opposite way on the same substantive issue. That is BACKLOG O20 and it upgrades
§3's warning: `APPROVE` was never "reviewed", and `ARBITER_SHIP` is not either.

`docs` has since been run (changelog + handover sub-modes, live model, end to
end) — and running it is how O10 was found: **it could never have worked**, so
"never been run" was hiding a total failure, not a risk of one. Assume the same
of the five that remain.

**And a result worth taking seriously:** two adversarial reviewers from
different families produced **zero findings across six patches**, two of which
had defects visible in what the reviewers were shown. Every correctness outcome
came from the gates. See BACKLOG O6 — do not read `APPROVE` as "reviewed".

---

## §4 Open issues

In BACKLOG.md, with mechanisms. Summary of the ones that block real use:

- **O1 (HIGH)** `promote()` is a file copy, so two tickets touching one file
  cannot both be promoted — the second silently reverts the first. F4 and F5
  both touched `report.py`; they were landed with `git apply` by hand.
- **O2 (HIGH)** `replay` does not hold the prompt constant, so a verdict flip
  measures nothing, and it writes reviewer artifacts into the corpus it is
  replaying. Decorative until fixed.
- ~~**O3**~~ **CLOSED** (`cf88846`) — the gate-failure distribution is measured
  from a structural field now, not inferred from prose.
- **O4 (MED)** the report's arbiter calibration correlates mechanically coupled
  variables. Note it is now measuring an arbiter with two known bad rulings
  (O20), so fix O20's diagnosis before trusting O4's output.
- **O5 (MED)** `signature` no longer breaks on line numbers but still breaks on
  suffix changes; arbiter-assisted dedup is the durable fix. **Reconsider the
  premise:** O5's plan is to delegate dedup to the arbiter, and O20 is evidence
  the arbiter is not yet trustworthy for that.
- **O7 (GAP)** the untested rungs and modes listed in §3, minus `developer`.
- **O20 (HIGH)** the arbiter fabricated a warrant to reject the one correct
  finding, and ruled opposite ways on the same issue across two runs.
- **O21 (MED)** a self-authored acceptance test can cover half a fix; the O3
  tests left the writer side entirely unverified.
- **O22 (MED)** the default panel has one member — the schema cannot express
  more than one reviewer.

---

## §5 Commands

```powershell
# Offline, free, ~40s — run this first after any change to the loop
python -m agent_loop.selftest

python -m pytest tests/ -q

# Validate a ticket file without spending a model call. CHECK THE LINE RANGES,
# not just the OK: a degenerate one-line region also prints OK.
python -m agent_loop --profile agent-loop-self --profile-module profiles.self `
    --tickets tickets/review_followups.json --list

# Run tickets against the loop's own source (no --apply: exports patches only)
python -m agent_loop --profile agent-loop-self --profile-module profiles.self `
    --tickets tickets/review_followups.json --ticket F1 `
    --reviewers glm-5.2:cloud,minimax-m3:cloud --arbiter deepseek-v4-pro:cloud `
    --max-rounds 3 --panel-deadline 900

python -m agent_loop --mode report --profile agent-loop-self --profile-module profiles.self
python -m agent_loop --prune          # remove worktrees left by a crashed run
```

Models confirmed available in the local ollama at handover time:
`kimi-k2.7-code:cloud` (implementer), `glm-5.2:cloud`, `minimax-m3:cloud`,
`deepseek-v4-pro:cloud` (arbiter), plus `qwen3.5:cloud`, `kimi-k3:cloud`,
`mistral-large-3:675b-cloud`, `gemma4:*`, `deepseek-v4-flash:*`.

---

## §6 Traps that cost real time in this session

1. **The worktree is created from `HEAD`.** Acceptance tests must be
   **committed** before running the loop, or the worktree will not contain them
   and the test-first check refuses the ticket.
2. **A module-level `ImportError` in an acceptance test is a collection *error*,
   not a failure** — and `capture_baseline` refuses to establish a baseline from
   an errored suite, which makes *every* ticket on that profile unrunnable
   rather than merely red. Put imports of not-yet-existing names **inside test
   bodies**.
3. **`--list` prints `OK` for a useless region.** The locator bug is fixed, but
   the habit stands: read the line ranges. A function region that spans one line
   is wrong.
4. **The region model cannot add a module-level function or update a caller
   outside the region.** Both bit F1-F6: F5 emitted a mid-file `import` because
   its region began at a `def` (it had no legal alternative), and F2 added a
   parameter no caller passed. If a ticket needs a new module-level name, give
   it a region whose first line is at column 0 and say so in the ticket
   `context`; if it changes a signature, include the call site as a region.
5. **Patches generated before `41e5fd0` are CRLF** regardless of their source
   file and will not apply to LF files. The F2/F4/F5 patches on disk were
   normalised in place. New patches are correct.
6. **Anthropic's minimum cacheable prefix is model-dependent and not
   monotonic** — 512 tokens on Opus 5, 1024 on Opus 4.8/Sonnet 5, 2048 on Opus
   4.7, 4096 on Opus 4.6/Haiku 4.5. Under it, a breakpoint silently does
   nothing. Caching is now opt-in per call (`chat(..., cache=True)`) and only
   the two multi-turn callers use it; single-shot callers must stay off or they
   pay a 1.25× write premium for an entry nothing can read.
7. **Worktrees accumulate on a crash.** `--prune` cleans them. **Corrected twice
   — session 4's version is the accurate one.** Worktrees are **siblings of the
   repo** (`../agentloop-<TICKET>-<pid>`) for **every** mode, not just developer
   mode, and never under `logs/`. `--prune` finds them: `list_stale` matches the
   `agentloop-` name through `git worktree list`, and since session 4 also scans
   the filesystem for orphans git has forgotten. That scan exists because of a
   real one: `git worktree remove --force` can delete a worktree's CONTENTS and
   fail to remove the directory (Windows holds a handle), after which
   `git worktree prune` drops the registration and the empty directory becomes
   invisible to git forever — while `open_workspace` still refuses to start on a
   path that exists. `--keep-worktree` reaches developer mode now; before session
   4 `run_developer` had no such parameter and dropped it under every outcome.
   BACKLOG O23.
8. **`--mode developer` is test-first now** (session 3). The model must write a
   test that FAILS before it may edit source. Consequences worth knowing before
   you run it:
   * budget more turns. `max_turns = --max-rounds * 5`, and the red phase spends
     turns before any fix begins. `--max-rounds 14` (70 turns) was enough for a
     two-file change; the documented `--max-rounds 3` gives 15 and is not.
   * the profile must declare `test_sources`, or `write_test` has nowhere legal
     to write and the run cannot leave the red phase.
   * `require_failing_test: false` under `modes.developer` turns it off, and a
     profile with no `test_cmd` skips it automatically.
9. **A test can be red for the wrong reason, and that costs a whole run.** The
   first TDD run wrote tests that shelled out to a CLI flag which does not
   exist; argparse abbreviation-matched `--repo` to `--report-last` (an int
   flag), so they could never pass. Sixty turns went into chasing them. Read the
   `[red]` block in the log and satisfy yourself the failure is the DEFECT
   before letting a long run proceed. BACKLOG O19.

---

## §7 Suggested next steps

1. ~~**O1 `promote()`**~~ — DONE (`a4052e6`, released as `v0.2.3`). Applies a
   patch instead of copying files. Read BACKLOG O1 before touching it: the loop's
   own patch used `git apply --3way`, which on conflict **writes conflict markers
   into the live file and only then returns non-zero** — it raised while having
   already corrupted the target. Plain `git apply` is all-or-nothing.
1b. ~~**O2 `replay`**~~ — DONE (`973f370`+`f3fda21`). The loop records the rendered
   review AND arbiter prompts; replay re-sends them verbatim and REFUSES a corpus
   that has none (F1-F6 predate recording, so they refuse — that is correct).
   Replay exit codes are three-valued now: 2 = could not measure, 1 = flipped,
   0 = stable. Do not "fix" a 2 by making it a 0.
1c. **Config is central now** (`config.py`, v0.3.0). Every tunable has one
   definition with its rationale; override via `agent_loop.config.json`,
   `--config`, or `$AGENT_LOOP_CONFIG`. Two static guards fail the build if a
   literal creeps back. **Read the thinking/budget note before changing any
   max_tokens** — on a reasoning model, reasoning is spent from the answer's
   budget, which is what killed O1's first run.
2. ~~**Exercise developer mode**~~ — DONE (session 3). It was completely
   non-functional and is now test-first; see BACKLOG O15-O19. Doing it exercised
   the arbiter for the first time, which is how O20 was found. Compaction and
   `NOT_CONVERGING` remain unreached.
3. ~~**Install + retag**~~ — DONE, see §0. Each remaining untested mode should
   be smoke-run through `main(argv)` the way docs mode now is; that is what
   caught O10.
4. **O2 `replay`** — record the rendered review prompt in `run_ticket` so a flip
   means something. Prerequisite for measuring any prompt change.
5. **Answer the panel question with data** (O6) once O3/O4 are fixed.

---

## §8 APPEND BELOW — other session

The other session was asked to append its own state here. Suggested headings so
the two halves compose: what you changed, what is verified green, what you left
half-done, and any trap you hit that is not in §6.

<!-- other session: append below this line -->

### The parallel-session hazard recurred (2026-08-10, later)

§0 warns not to use `git add -A` because two sessions were editing this repo.
It happened again, in the other direction: commit
`973f370 "fix: replay fidelity — record and re-send the exact prompt (O2)"` was
made by the other session and swept up THIS session's mid-edit changes to
`arbiter.py`, `loop.py` and most of `replay.py`. No duplication resulted and the
final state is correct and verified, but the attribution is wrong: that commit
message describes work it only partly contains, and the remainder landed in
`f3fda21`.

Same lesson, stated more sharply: **in this repo, `git log` is not a reliable
record of who changed what or why.** BACKLOG.md and these handover notes are.
Stage explicit paths, and re-read `git status` immediately before committing --
if a file you did not touch is staged, another session is mid-edit in it.

---

## §9 Session 3 (2026-08-10, later) — developer mode, TDD, O3

**What changed.** Twenty-one commits on `main`, `f739b7d..HEAD`, all **unpushed**:

| | |
|---|---|
| `5c6b091` | providers surface native `tool_calls`; budget guard stops misdiagnosing |
| `bd3c296` | `MAX_TURNS_EXHAUSTED` instead of a blank verdict; `turns` populated |
| `08ad362` | **developer mode is test-first** — the red phase |
| `679962c` | the model can see WHY a test failed |
| `cf88846` | **O3 closed** |
| `2c326ca` | three reviewer findings the arbiter wrongly rejected |
| `f97492f` | **arbiter chosen by measurement**; `think` read from config |
| `4546db7` | the compactor read an eighth of what it summarised |
| `89c7c22` | **MODEL_CATALOG**; a mode can name its own model |
| `82cab53` | compactor benchmark — the cheapest model is enough |
| `370f612` | `gemini:` and `github:` backends |
| `20772a8` | GitHub Models is retired (verified with a valid token) |
| `266fcd1` | Gemini via direct API: Flash 0/5, Pro blocked by quota |
| `a540501` | **`agy:` backend** — the only path to Gemini Pro high + Claude models |
| `dad3301` | agy arms benchmarked; **stop shopping for models** (O28) |
| plus | docs commits `f946744`, `6ace224` and the O27/O28 entries |

**Verified green:** 302 tests on 3.14, selftest 12/12, and every load-bearing
guard mutation-checked. **Not verified:** the suite has not been re-run on
Python 3.12 this session. O9 was invisible on 3.14 and bricked every ticket on
the consumer's 3.12 — do that before tagging.

**What I left half-done.** Nothing is half-applied. Recorded and not fixed:
O4 and O5 (the items this session was originally asked to sequence — see below),
O20 (arbiter still 3/5), O21 (half-covered acceptance tests), O22 (one-member
default panel), O23 (four small ones), O28 (the corpus, not the pool). O22 needs
a schema decision, not a patch.

**The original task, still open.** This session began by asking whether to fix
O3/O4/O5 by hand or through the loop. O3 is closed. **O4 and O5 were never
started**, and both need re-reading before they are picked up:

* **O4** (report's arbiter calibration correlates coupled variables) now has
  real arbiter data to calibrate against, where before it had none. It also now
  measures an arbiter known to be weak (O20), so fix the diagnosis before
  trusting the metric.
* **O5** (Finding.signature breaks on suffix changes) was planned to be solved
  by delegating dedup to the arbiter. **Reconsider that premise**: eighteen
  configurations later, the best arbiter available upholds 3 of 5 correct
  findings. Building signature dedup on top of that is building on sand.

**The model/role audit**, now measured rather than argued:

| role | model | basis |
|---|---|---|
| implementer | `kimi-k2.7-code:cloud`, 96000, think=True | **measured.** Localised O3 unaided across 34 turns, correct file and field |
| reviewer | `glm-5.2:cloud`, 24000, think=False | **measured.** Five correct findings on the O3 patch. Model right, **count wrong** — one member, O22 |
| arbiter | `mistral-large-3:675b-cloud`, 24000, think=False | **measured.** Best of ten arms, 3/5 on all four runs. Was deepseek-v4-pro, which scored 0/5 twice |
| compactor | `glm-5.2:cloud`, 8000, think=False | shape is right (bounded extraction); see O24 for the benchmark |

No `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is set, so ollama is the only live
backend. `MODEL_CATALOG` in config.py now records what every model IS —
parameters, context, modalities, thinking/tools support, cost — harvested from
`ollama show`, with guards that fail the build when config and catalogue
disagree.

**Three things to carry into any future model decision**, all from BACKLOG O20:

1. **Size does not predict quality for adjudication.** A 32B model beat a 1.6T
   one and both Qwens on the arbiter benchmark. Measure; do not reason from
   parameter counts.
2. **Role competence does not transfer.** glm-5.2 is excellent at generating
   findings and poor at adjudicating the same ones. "Strongest model" is not a
   single axis.
3. **`think=True` is not a free upgrade.** It did not help the arbiter and made
   glm strictly worse. And `mistral-large-3` has no thinking capability at all,
   so setting it there is a no-op that reads like a decision.

To re-run either benchmark (both are frozen corpora, no live run needed):

```powershell
python tests/fixtures/arbiter_bench/run_bench.py            # defaults
python tests/fixtures/arbiter_bench/run_bench.py qwen3.5:cloud:false:24000
python tests/fixtures/compactor_bench/run_bench.py          # rejection recall
```

**Traps not already in §6.**

* **The gates cannot refuse what the suite cannot observe.** This is the whole
  lesson of the session. A patch that compiled and passed 232 tests fixed
  nothing, because no test covered the behaviour. If a defect's symptom is "this
  output is wrong", no gate ladder will catch a bad fix until a test asserts on
  that output. That is why the red phase exists.
* **Mutation-test your own tests, not just the model's.** Two of my acceptance
  tests passed against a deliberately broken implementation. One asserted
  `acceptance == []`, which is true whether the bad test was rejected or
  accepted with zero criteria — it looked like evidence and was not. Both were
  found by mutating the fix, never by reading.
* **A green suite says nothing about the wiring you did not exercise.** 217
  tests were green while developer mode was completely dead, because every test
  built `Completion` objects by hand and none had ever seen a real provider
  response. Same structural cause as O9 and O10. Prefer tests that drive
  `main(argv)` or a real response shape.
* **`main()` reloads config from disk unconditionally.** A test that calls
  `config.set_active()` before `main()` proves nothing — it is discarded. The
  first CLI test for per-mode models did exactly that and PASSED while
  measuring a stale default. Drive it through a real `--config` file.
* **Ask the tool, do not estimate.** `ollama show <model>` reports parameters,
  context length and capabilities. It settled in one command that
  `mistral-large-3` cannot think at all — something two sessions of reasoning
  about `think` flags had not surfaced.
* **A defect that only appears at scale needs a test at scale.** The compactor
  read an eighth of its input, and every existing test used toy histories where
  that is invisible. The new ones build 160000 chars because that is where
  Phase 4b actually runs.
* **A benchmark is a measuring instrument and needs its own validity check.**
  The first compactor benchmark scored literal tag presence and gave gemma4:31b
  0/8 — while its summary correctly said "Widening the protected-path glob:
  Rejected because it would allow the patch to edit its own tests". It was
  measuring tag-COPYING, not faithfulness, and it produced a confident,
  published, wrong ranking. **Read the raw output of the worst-scoring arm
  before believing any ranking.** Corrected, all four models score 7/8.
* **Not all benchmarks are equally trustworthy.** The arbiter one grades
  structured `UPHELD #N` rulings the model must emit; the compactor one grades
  free prose. The first is much harder to fool yourself with than the second.

---

## §10 Providers: three paths to Gemini, and they are not interchangeable

Measured 2026-08-10. This is the section to read before anyone "simplifies" the
provider list.

| path | auth | models | per-call overhead |
|---|---|---|---|
| `gemini:` direct HTTP | `GEMINI_API_KEY` (AI Studio) | API ids only (`gemini-3.6-flash`, `gemini-3.1-pro-preview`) | none |
| `google.antigravity` SDK | **also `GEMINI_API_KEY`** | API ids only — **404s on agy's `-high` ids** | **~13,600 prompt tokens** of agent scaffold |
| `agy:` CLI | **Antigravity subscription**, no key | `gemini-3.1-pro-high`, `gemini-3.6-flash-{high,medium,low}`, `claude-opus-4-6-thinking`, `claude-sonnet-4-6`, `gpt-oss-120b-medium` | n/a |

Consequences that cost time to establish:

* **The SDK does not bypass the AI Studio quota** — it uses the same key, and
  its ~13.6k-token scaffold burns that quota *faster*. Measured by asking it to
  reply "PONG": `prompt_token_count=13606, candidates_token_count=2`. If the
  goal is a single stateless ruling, the SDK is the wrong tool; it is the right
  tool for an agentic workload with vision and tools (which is why the chart
  agent uses it).
* **`agy` is the only path to Gemini Pro at high effort and to the Claude
  models.** `gemini-3.1-pro-preview` over the direct API returns HTTP 429 on the
  free tier (`generate_content_free_tier_input_token_count`) because the arbiter
  prompt is large.
* **`agy models`** lists the ids that backend accepts. They are agy's names, not
  Google's: `-high`/`-low` is a reasoning-effort setting baked into the name,
  and the SDK rejects those ids with a 404.
* **GitHub Models is retired.** Verified with a valid PAT that authenticates
  fine against `api.github.com`: `models.github.ai/inference` returns HTTP 410
  `github_models_retirement_brownout`, and `models.inference.ai.azure.com`
  returns 404 on every path including `/` and `/models`. Tested with urllib,
  curl and requests. A Copilot subscription is not a substitute — it is licensed
  for use through Copilot clients, not as a chat-completions endpoint.

Three hazards specific to the `agy:` backend, all encoded in its tests:

1. **The prompt is a command-line ARGUMENT**, capped by CreateProcess at 32767
   chars. Over 30000 the backend REFUSES rather than truncating — a shortened
   prompt would silently drop the end of the diff and the arbiter would rule on
   a patch it was never shown. The current corpus is 16,943.
2. **agy is an AGENT with file and terminal tools.** It runs `--sandbox` in a
   scratch temp dir, never the caller's cwd; pointed at the repo it could edit
   the code under review. `--dangerously-skip-permissions` is required for
   non-interactive use and is only acceptable because of that.
3. **Cleanup must not destroy an answer.** agy holds a file open in its working
   directory, so `TemporaryDirectory` cleanup raised `WinError 32` *after* a
   successful call and the exception discarded the completion. Cleanup is
   best-effort and fully guarded now.

`agy` print mode reports no usage, so token counts on an agy arm are **unknown,
not zero** — its usage lines are not comparable with an HTTP arm's.

---

## §11 Session 4 (2026-08-10) — the 3.12 gate

Session 3 left exactly one precondition for tagging: run the suite on the
consumer's Python 3.12. It came back **9 failed, 293 passed**, and the two
causes are BACKLOG **O29** and **O14**. A third, **O30**, came from running the
command §5 tells you to run first, in the environment a consumer runs it in.

All three are the same shape as O9 and O10, and that is now five instances:
**a check that only ever ran on the machine it was written on is not a check.**

* **O29** — the tests shelled out to `python`, and PATH decided which. Every
  shelled-out interpreter now goes through `tests/_interp.py`'s `PY_EXE`.
  If you add a `build_cmd`/`test_cmd`/`python -c` to a test, use it.
* **O14** — the freshness flake was real and fired here. Closed.
* **O30** — `python -m agent_loop.selftest` under an installed package resolved
  `REPO` to `<venv>/Lib` and died with a bare `FileNotFoundError`. It is a
  checkout-only check by construction (it runs the loop against this repo's own
  source), so it now says so and returns 2. **The consumer venv still has v0.3.0,
  so it still throws there** until the next tag is pinned.

**Verified green:** 302/302 on 3.12 and on 3.14; selftest 12/12 on 3.12 from the
checkout; the O30 guard mutation-checked (`return 2` → `return 0` kills it).

**Tagging is now unblocked** — the precondition §0 named is met. `main` is still
unpushed.

**A trap for §6.** A rename into a test module can collide with a name already
there: `test_defect_regressions.py` has a module-level `PY` that is a `Profile`,
and the first version of the O29 fix shadowed it —
`TypeError: unsupported operand type(s) for +: 'Profile' and 'str'`. The full
suite caught it; a single-file run would not have.

### §11a Tagged and pushed — `v0.4.0`

`main` and `v0.4.0` are on origin (`e780e29`). The 23-commit unpushed backlog
that §0 and §9 describe is cleared; read those two sections as history now, not
as state.

**The consumer has not been re-pinned.** `tvDownloadOHLC/requirements.txt` still
says `@v0.3.0` and its venv still has v0.3.0 installed, so until someone re-pins:

* O30 still throws its bare `FileNotFoundError` there rather than the new message;
* O29's `PY_EXE` fix is test-only, so it changes nothing for the consumer either way;
* every other v0.4.0 fix (developer mode's tool calls, the compactor, the report's
  gate distribution, the measured arbiter default) is absent from the consumer.

Re-pin with `@v0.4.0` and reinstall, then re-run `pytest tests/ -q` from the
agent-loop checkout with the consumer interpreter — that is the pair of checks
that caught O9 and O29.

---

## §12 Session 4 (later) — O7: the four unrun modes have been run

`plan`, `test`, `brainstorm`, `review`, each through `main(argv)` against this
repo with a live panel. Details and mechanisms: BACKLOG **O31-O35**.

**The base rate broke.** Session 3 said "never been run" had meant "completely
broken" three times out of three. It does not here: **all four run.** What three
of them do instead is produce confident output that is not what it appears to be
— which is worse, because a crash announces itself.

| mode | verdict |
|---|---|
| `brainstorm` | runs. **O31** — recommends an approach having never read a line of the code (`in=264` tokens; no graph slice, no source) |
| `review` | runs. **O32** — printed `findings -> review_prompt.txt`, i.e. the reader's own diff. **FIXED**, 3 tests, both mutations killed |
| `plan` | runs, and produced a correct, well-anchored ticket. **O33** — but it writes a bare ticket object and both consumers expect `{"tickets": [...]}`, so plan→test and plan→loop each die on `KeyError: 'tickets'` |
| `test` | runs. **O34** — printed `1 test(s) failing at baseline (correct)` for a test that died in its own stub before reaching any assertion |

**O33 is the one to fix first.** Plan mode's only purpose is to feed the loop,
and it has never once been able to.

**O34 is the one to think about.** "Failing at baseline (correct)" counts
failures; it cannot tell a defect-driven failure from a broken test, and prints
`(correct)` either way. That is O19 and O21 in a third location.

### Traps for §6

10. **Do the static pass first — it is free.** All four CLI wrappers were checked
    against their `run_*` signatures before spending a token. That is the O10
    defect class, and ruling it out in one read meant every later finding was
    known to be behavioural.
11. **Your own test doubles are not evidence either.** The hand-written
    replacement for the model's junk test was red for the WRONG reason twice
    before it was right: `Finding(author=...)` and `Vote(counted=...)` do not
    exist (`counted` is a derived property), and then `assert "1" in line` passed
    against the unfixed code because the pytest temp path contains a `1`. Read
    the failure; then mutate the fix.
12. **Watch for a reviewer talking itself out of a correct finding.** On the O29
    review, minimax raised in `<thinking>` that production profiles also hardcode
    bare `python`, reasoned it out of scope, and emitted `NONE`. It was right
    (O35). The thinking block is worth reading when the verdict is APPROVE.

### §12a O33 closed — the plan→test→loop seam

`cli.load_tickets()` is now the one loader both call sites use. It takes the
wrapper `{"tickets": [...]}`, a bare list, or a single bare ticket object (every
`plan.json` already on disk), and raises `TicketFileError` naming the file and
the expected shapes rather than `KeyError` from inside a subscript. Plan mode
writes the canonical wrapper. 12 tests; verified on the real O7 artifact.

Two things this turned up:

* **A mutation survived the first pass.** Deleting the per-ticket `id` check left
  all ten tests green — the "no id" test is caught by the SHAPE branch and never
  reaches the per-ticket one. O21 in miniature, found by mutating, not reading.
* **`plan_mode.py:23` imports `build_context_slice` and never calls it.** So
  plan mode has no codebase context either, which corrects what §12 said about
  O31: brainstorm is not alone, and only docs mode actually injects context. The
  mode whose whole job is to LOCALISE a defect has never been shown the code.
  It still produced a correctly-anchored ticket — which says more about the
  defect description it was handed than about the mode.

### §12b O34 closed — the red phase can tell you WHY it is red

`gates.failure_kinds()` + `gates.reached_an_assertion()`, three-valued
(True / False / None-for-unknown). Wired into both places the blind spot
existed, and deliberately differently: **test mode refuses** (one-shot, reports
to a human, file still on disk) while **developer mode warns** (iterative, and
the model cannot override a gate — a crash-defect test legitimately fails with
the exception the defect raises, and refusing it would strand the run burning
turns). `(correct)` is gone from both.

Two things to carry forward:

* **A bare `assert x == y` produces NO exception name anywhere in pytest's
  output** — not in the `E` gutter, not in the summary line. It is the common
  shape for an acceptance test, and a classifier that only looks for
  `AssertionError:` calls every one of them "never reached an assertion". This
  was nearly shipped that way.
* **The classifier is validated against a real runner**, 16 tests across
  `--tb=short/long/line/no`, not against a fixture of what pytest is assumed to
  print. That is O24's lesson applied before the fact rather than after.

### §12c O23 closed, and O36 opened by request

**O23** — all four. Two real (`--keep-worktree` never reached developer mode;
the developer exit code disagreed with the driver's own `apply` predicate, so a
run could apply its patch and report failure to CI in the same breath — and a
unanimous `APPROVE` was affected, not just `ARBITER_SHIP`). One was half wrong in
a way that mattered: worktrees are siblings of the repo for **every** mode, and
`--prune` does find registered ones — but the "may not find it" worry was true
via a mechanism nobody had guessed. See BACKLOG O23; §6 trap 7 is rewritten.

**Two mutations survived here**, in different ways, and both are the same lesson
at different depths:

* forwarding a flag is half a fix — every test stubbed `run_developer` and proved
  only that the flag ARRIVED, so deleting `keep=` from the driver's own call
  stayed green;
* and `Path.rmdir()` refuses a non-empty directory by itself, so deleting the
  explicit emptiness guard ALSO stayed green. **rmdir is the safety; the guard
  only makes the operator's message true.** The test asserts the message now,
  which is the honest statement of what that code is for.

**O36 (new, requested)** — plan mode can only plan a defect fix. Its signature,
system prompt, ticket schema and CLI flag all presume a defect that already
exists, and the region check refuses anything whose anchors do not resolve
against the current tree — so a feature, whose code does not exist yet, is
rejected every round until the rounds run out. Four design questions are written
down in BACKLOG O36; question 3 (how a region-based loop implements a ticket
whose files do not exist) decides the shape of the rest.

**Do O31 before O36.** Plan mode has never been shown the codebase
(`build_context_slice` imported, never called), so it is a poor foundation for
the larger job, and the context injection wants designing once for both.

### §12d O31 closed — the modes can see the codebase now

`context.build_intent_context(repo, profile, intent)`: symbols and paths are
extracted from the REQUEST, then located in the tree. Wired into plan and
brainstorm; docs mode's private duplicate delegates to it.

**The design decision worth keeping:** the mechanism is the **filesystem**, not
the graph. Docs mode's builder returns "" unless `codebase-memory-mcp` is live, so
reusing its shape would have fixed O31 only on machines running the graph server.
The graph is enrichment.

**And the counter-intuitive one:** recall beats precision in symbol extraction,
for a structural reason — a candidate that is not real finds no definition and is
dropped, so *the filesystem is the filter*. The first version was strict
(underscore or lower-to-upper transition required), which silently rejects every
single-word class name: `Config`, `Vote`, `Finding`. On a live run about
`Config.roles` it found nothing and added 25 tokens to the prompt.

Measured, same defect text, live model: **in=264 → 367 tokens**, and the output
went from inferring `Config.roles` out of the prompt text to citing
`Mapping[str, RoleSettings]` and `_DEFAULT_ROLES` — neither of which was in the
request. That is the difference between advice and grounded advice.

**Three traps the live runs caught and review would not have:**

1. **The graph injected its own failures as findings.** `trace_call_path` answers
   `{"error":"function not found"}` — a 200-OK JSON body, not a string starting
   with `ERROR`, which was all the code tested. Three of them went into the
   prompt under the heading "Call paths". That does not just waste tokens: it
   tells the model those symbols do not exist.
2. **Test files outranked production code**, because `rglob` is alphabetical and
   `roles = dict(base.roles)` in two test files pushed the real declaration out of
   the two-hit budget. `_iter_sources` yields production first now.
3. **A column-0 assignment pattern missed a dataclass field** — indented inside
   the class body, and the exact name the request pointed at.

One test in this file was written so it could not fail (it used `src/` and
`tests/`, where `src` already sorts first) and passed against the unranked
implementation. It now uses a directory that genuinely sorts before the source.

### §12e O36: the change model can express a feature; the entry point cannot ask for one

**Question 3 answered, and the answer was neither option as written.** The
requirement decided it — a feature is new files *and* additions to existing ones,
both. Routing to developer mode cannot do that: `_edit_file` returns
`ERROR: file not found`, so **developer mode cannot create a source file either**.
Only `write_test` creates files, and only under `test_sources`. Neither option was
free, so the cheaper one was to grow the region model.

Regions now carry an `op`: `replace` (default, unchanged), `create`, `insert`.
Per-region, so **one ticket mixes all three** — new module, hook into an existing
file, signature change at the call site. `op` is deliberately NOT folded into
`kind`, which is the locator strategy; one field with two meanings is the
ambiguous helper this repo avoids.

`insert` also fixes **§6 trap 4** — "the region model cannot add a module-level
function" — a limitation that already bit F1-F6 on *defect* work.

**Three things that had to ship with it**, each a defect if omitted:
`Workspace.stage_new_files()` (`git diff` ignores untracked files, so a created
file would be missing from its own patch — the red phase learned this once with a
new test file); per-op prompt text (left implicit, `create` gets a fragment and
`insert` re-emits the block it anchored to, duplicating it); and `lines_1based`,
which said `1-0` for a create region.

**The interaction that would have shipped this broken.** A feature's first red test
imports something not yet written, so it fails with `ImportError`. O34 — added
three commits earlier — reads that as "died in its own scaffolding" and REFUSES in
test mode. So the gate would have rejected every feature on arrival.
`reached_an_assertion(kinds, feature=True)` now accepts the "name is not there"
family, and `gates.is_feature_ticket()` derives the flag from `op: create`.
Narrow on purpose: a `TypeError` in a stub is a broken test either way — and
widening it to `TypeError` is one of the six mutations, because that is the change
that would quietly make the whole exception meaningless.

**What is NOT done: the entry point.** `run_plan` still takes
`defect_description`, the flag is `--defect`, and `PLAN_SYSTEM` says "analyze the
defect". Question 4 — the acceptance criterion for a feature — is unanswered, and
the plan→test→loop path has never been run for a feature end to end. Do not assume
it works; every mode that had "never been run" this session turned out to be
broken in some way.

### §12f O36 closed — `--feature`, decomposed, each part test-first

**Questions 3 and 4 answered by the user, not inferred:** a feature is broken into
smaller parts, and each goes through the same TDD cycle. Q4 therefore needed no new
answer — the acceptance criterion for a feature is the SAME one. `--feature` emits
an ordered list of parts, each with its own `expect_green`, and a part without
acceptance tests is **refused**; otherwise feature mode would be the one path into
the loop that skips the check the loop exists to apply.

`--feature` and `--defect` are mutually exclusive — different prompts, different
output shapes, so silently preferring one makes the other an invisible no-op.
A defect plan still returns one ticket; a one-part feature still returns a list.

**The live run is the lesson, and it is O31's lesson with the sign flipped.** The
first `--feature` run produced four well-formed ordered parts with correct ops and
tests — every file under a **`patchgate/` package that does not exist**. O31's
context is keyed on SYMBOLS, and a feature request names none, so
`extract_intent_symbols` returned `[]`, the context was `""`, and nothing had told
the model the code lives in `src/agent_loop/`. Structural, not tuning: for a defect
the thing complained about is already there to find; for a feature the question is
*where does new code go*, and only the layout answers that.

`context.build_layout_context()` now supplies it in feature mode — directories with
counts, real paths, where tests must live, and the `file_scope_whitelist` when set.
Re-run: zero `patchgate`, plan targets the real `cli.py` plus three new modules in
the real package. It then failed on one wrong anchor (`parser =` where the code says
`ap = argparse.ArgumentParser(`) and exhausted `--max-rounds 2` — the validator
working, and O13 again: **two rounds is not enough for a four-part plan.** Budget
rounds by parts, not by habit.

**Not yet run:** the full `--feature` → `--mode test` → loop chain. The plan is
produced and validated; nothing has yet implemented one of these parts end to end.

### §12g O4, O8, O35 — the small ones, and two BACKLOG entries that were stale

**The real one in O8** was `check_lint` reusing the compile gate's digest, whose
regex is MSBuild's `error CS1234`. On ruff or eslint output nothing matched and the
model was handed `output[-4000:]` — a raw tail to find its own errors in. Feedback
the model cannot act on is a gate that only looks like one. New
`gates.lint_digest()` is deliberately SEPARATE from `_digest`: the compile gate
wants MSBuild's shape specifically, and widening that regex would make the C#
digest start matching prose.

**`replay` had drifted back into O2.** It adjudicated without
`rules=profile.arbiter_rules` and read `profile.settled` instead of
`inject_settled(...)`, so it judged under a different contract than the run it was
replaying — which makes every flip it reports meaningless. O2's defect,
reintroduced one argument at a time.

**Two O8 entries were STALE** and this is the reusable lesson: `--replay-dir` does
exist, and the unused imports were already gone. **My test asserted the flag's
ABSENCE and would have had me "fix" correct code.** Check the claim against the
tree before implementing an old backlog entry — BACKLOG is authoritative for what
is open, not for what is still true.

**Two mutations survived, both my tests' fault, both the same shape as before:**
the lint test asserted `"F401" in feedback`, which the `output[-4000:]` fallback
also satisfies on a short fixture, so reverting the fix stayed green; and a mock
returned a dict where `run_report` is annotated `-> int`, so `main()` returned a
dict and the test blamed the code. **Assertions satisfied by the fallback path are
now the single most common way I have written a useless test in this session.**

**O35 is closed for this repo only.** `profiles/self.py` uses `sys.executable`;
the consumer's profile still uses bare `python` and was left alone on purpose — it
lives in tvDownloadOHLC where another session is committing, and it is verified
working today. Fix it there.

**Left open on purpose:** the OpenAI cached-token field. No `OPENAI_API_KEY` is
set, so the fix cannot be checked against a real response — and guessing a response
format is precisely what produced O24's and O34's confident wrong readings.

### §12h O22 closed — the panel is two members from two families, by policy

The requirement: *"we should always have at least two doing the review preferably
from different view points."* That answered the schema question by overtaking it —
it says what the DEFAULT must be, not just what the schema must express. Encoded
three ways, because one would decay:

1. `RoleSettings.extra_members` + `registry_from_config` registering every member.
   `ModelRegistry` already appended per role; that loop registering one config was
   what made the multi-family panel unreachable from config.
2. The shipped default is `glm-5.2:cloud` + `minimax-m3:cloud` — both measured. glm
   produced five correct findings on the O3 patch; minimax raised the point on the
   O29 review that glm missed and that became O35. Marginal value observed, not
   assumed.
3. `config.check_panel_policy()` runs **at import** and fails the build if the
   default drops below two members or two families. Two of the five mutations
   against this produce a COLLECTION ERROR rather than a test failure — the guard
   refusing to let the package load, which is the strongest form this can take.

**Why the guard and not a comment:** O22 survived precisely because every
documented command passes `--reviewers` explicitly, so nobody ever ran the
one-member default and nothing complained.

`models.model_family()` defines "viewpoint" as the vendor stem, lowercased, with
backend prefixes stripped — `agy:` is a transport, and two agy-routed Claudes are
one family. Deliberately crude, and it fails SAFE: two names it cannot tell apart
read as one family, which warns rather than staying silent.

**One trap worth carrying.** The first attempt made `members` the FULL set, which
shadowed `model` — so overriding `roles.reviewer.model` in a config file became a
silent no-op, the exact failure `config.py`'s docstring warns about. An existing
test caught it. `extra_members` keeps `model` as the single primary truth.

### §12i NEXT: run a real feature through the whole flow

Agreed with the user: the next piece of work is a **real feature they want built**,
used as the end-to-end exercise of `--feature` → `--mode test` → the loop. This is
the highest-value remaining item, and the reason is the session's own record: every
path that had "never been run" turned out to be broken somewhere. Five for five.

**What is already verified, so do not re-verify it:**

* `--feature` produces an ordered, decomposed plan with `op` per region and
  `expect_green` per part, validated in order against the tree plus what earlier
  parts create. Run live twice.
* The region ops apply: `create` writes a new file and reaches the patch via
  `stage_new_files()`; `insert` adds without replacing; one ticket mixes all three.
* O34's exception lets a feature's red test fail on a missing NAME.

**What has NEVER run, and is therefore the actual test:**

1. `--mode test` generating an acceptance test for a part whose code does not exist.
   The O34 exception says it should be accepted as red-for-the-right-reason; that
   path has not executed once.
2. The LOOP implementing a `create` part — the implementer emitting a whole file
   into a `create` region, through the gate ladder, with the compile gate seeing a
   file that did not exist at baseline.
3. `promote()` landing a part that adds files, then the NEXT part resolving anchors
   inside a file the previous part created. This is where a decomposed plan either
   composes or does not, and nothing has exercised it.

**Set up to avoid the two traps that will otherwise cost the run:**

* **Budget rounds by parts, not by habit.** `--max-rounds 2` exhausted itself on a
  four-part plan (O13). For a real feature start at `--max-rounds 6` or more.
* **Acceptance tests must be COMMITTED before the loop runs** — the worktree is
  made from `HEAD` (§6 trap 1). For a feature this bites harder, because the tests
  are generated per part.
* Read the `[red]` block before letting a long run proceed (§6 trap 9), and now
  also read the `[test-first] ... failing at baseline: <kinds>` line — it names the
  exception types, so a feature's `ImportError` is distinguishable from a broken
  stub at a glance.

**Do this run BEFORE cutting `v0.5.0`.** If the flow has a defect, the tag should
contain the fix, and this is the last untried path of any size.
