# HANDOVER — agent-loop

Live state for a fresh session. Written 2026-08-10 at the end of the review +
self-hosted-run session. **Read §0 before touching anything.**

Single source of truth for *open issues* is [BACKLOG.md](BACKLOG.md) §"2026-08-10
— open issues after the F1-F6 self-hosted run" (items O1-O8). This file is the
orientation layer: state, hazards, commands, and the traps that cost real time.

---

## §0 READ FIRST

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
| `agent-loop` HEAD | `afff8e0 fix: docs mode has never been able to run (v0.2.2)` — tags `v0.2.0`, `v0.2.1`, `v0.2.2` all pushed |
| Pushed? | **Yes** — `origin/main..HEAD` is empty, nothing outstanding |
| Tests | **173 passed**, 0 failed — on Python 3.12 **and** 3.14 |
| `python -m agent_loop.selftest` | **12/12** (offline, ~40s, free) |
| `tvDownloadOHLC` branch | `harden/riskguard-p0-51`, HEAD `9be1b779` (unpushed) — pins + installs agent-loop `v0.2.2` |
| Consumer profiles | present in tvDownloadOHLC HEAD and clean; `git log -S` attributes them to `fb682a93`, which was already in the log when this session began — the other session has been committing and possibly amending there, so **trust file contents over commit attribution in that repo** |

### ~~Two things that are broken right now~~ — RESOLVED 2026-08-10 (later session)

Both consumer blockers are closed. `agent_loop` **is** installed in the
tvDownloadOHLC venv, and `requirements.txt` pins **`v0.2.2`**. Fixing them
exposed two defects in the package; see BACKLOG **O9-O11**. Two things to carry
forward:

- **`v0.2.0` is a poisoned tag.** It carries O9 (`Path.read_text(newline=)`,
  Python 3.13+ only) and therefore cannot run *at all* on Python < 3.13. The
  consumer venv is 3.12. Pin `v0.2.2` or later; never `v0.2.0` or `v0.1.0`.
- **Run the suite on 3.12, not just the dev 3.14.** O9 was invisible on the dev
  interpreter and bricked every ticket on the consumer's:
  `C:/Users/vinay/tvDownloadOHLC/.venv/Scripts/python.exe -m pytest tests/ -q`.
  173/173 pass on both today.

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
  tests/acceptance/                         148 tests
  logs/agent_loop/F1..F6/                   patches + artifacts from the self-hosted run
  logs/agent_loop/loop_run_F1-F6.log        the full run log
  docs/architecture/BACKLOG.md              OPEN ISSUES (O1-O8) ← authoritative
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
`plan`, `test`, `developer`, `brainstorm`, ~~`docs`~~, `review`.

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
- **O3/O4 (MED)** the report's gate-failure distribution reads a `detail` field
  the ledger only writes on protected-path rejections (observed double-counting),
  and its arbiter calibration correlates mechanically coupled variables.
- **O5 (MED)** `signature` no longer breaks on line numbers but still breaks on
  suffix changes; arbiter-assisted dedup is the durable fix.
- **O7 (GAP)** the untested rungs and modes listed in §3.

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
7. **`logs/` accumulates worktrees on a crash.** `--prune` cleans them.

---

## §7 Suggested next steps

1. **O1 `promote()`** — smallest fix with the largest correctness payoff. Either
   apply `final.patch` with `git apply` or detect the collision and refuse.
2. **Exercise developer mode** (O7). It received the largest changes — worktree,
   frozen baseline, protected-path gate in `_edit_file` — and has the least
   coverage. A deliberately hard ticket also exercises the arbiter, compaction
   and `NOT_CONVERGING`, none of which the F1-F6 run reached.
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
