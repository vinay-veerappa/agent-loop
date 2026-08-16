# Consumer findings — issues hit while USING the loop on a real repo

**Purpose**: a place for defects and rough edges found by *running* the loop against a consumer
repo, as opposed to by reviewing it or by its own suite. Kept separate from
[`BACKLOG.md`](BACKLOG.md) because the provenance is the point: everything here was hit by
somebody trying to get a ticket landed, and each entry records what the operator *saw* before it
records what the code does.

Each finding: what was observed, what it cost, why the current behaviour is defensible (where it
is), and the narrowest fix.

---

## Session 2026-08-16 — `nt8-riskguard`, ticket `P3-128`, run at HEAD (`5dfc303`, past `v0.6.7`)

**Context**: a one-region C# ticket. Six acceptance tests written first and verified red, then the
loop asked for the implementation. **Outcome: APPROVE in round 1** — `kimi-k2.7-code` produced the
right rung in the right slot in 11.0s, both reviewers approved, no regressions (2012 → 2018
passed, 0 failed), and the patch was applied unchanged. The findings below are all about the
*path* to that run, not the run.

---

### CF-1. The unresolved-identifier warning fires on ordinary English words, so it says nothing — HIGH, cheap

`--list` on a ticket emitted **~20 warnings**, of which **zero** named an identifier:

```
WARN 'CSS' named in spec but not found in addons/CopierStatusView.cs -- model will guess
WARN 'DETAIL' named in spec but not found ...
WARN 'Do' named in spec but not found ...
WARN 'Five' named in spec but not found ...
WARN 'GOES' named in spec but not found ...
WARN 'LOAD' named in spec but not found ...
WARN 'NOW' / 'PART' / 'RUNG' / 'SCOPE' / 'STAY' / 'SUITE' / 'WHERE' ...
```

The heuristic appears to treat any capitalised token in the spec as a symbol. House style in this
consumer writes emphasis in caps (`THE LOAD-BEARING PART`, `DO NOT CHANGE`, `SCOPE`), so a
well-written ticket produces the *most* noise.

**What it cost**: the real output — one line, `OK TheHeadlineLadder addons/CopierStatusView.cs
336-417` — was pushed off the top of the screen by warnings, and had to be recovered with
`grep -v`. The `--list` docs say to READ THE LINE RANGES, and the warnings are what stops you.

**Why it matters beyond tidiness**: this is *an alarm that is always on is off*. The warning exists
to catch a spec naming `HasEquityReading` when the region has no such symbol — a genuinely useful
signal that is now indistinguishable from the word `Five`.

**Narrowest fix**: only warn for tokens that look like code — contains `_`, or is `camelCase` /
`PascalCase` with an interior lowercase→uppercase transition, or is followed by `(` / `.` in the
spec text, or appears inside backticks. **A single-word ALL-CAPS token should never qualify**;
`SCOPE` and `TEXT` are prose in every house style. Print the count of tokens inspected alongside
the count warned, so a heuristic that starts matching everything is visible in its own output.

---

### CF-2. The test-first gate cannot tell "assertion passes" from "assertion does not exist" — HIGH

**Observed**, on the first run:

```
[worktree] agentloop-T1-36628 @ 604022c8
[baseline] 2006 passed, 0 failed at 604022c8; 0 expected failure(s)
REFUSED: expect_green test(s) not failing at baseline: [ ...all six... ]
```

The six assertions were red — in my **working tree**. The loop builds its worktree from **HEAD**,
and the tests were not committed yet, so in the baseline it measured they did not exist at all.

**The message describes the wrong problem.** "Not failing at baseline" reads as *your tests are
wrong, or your defect does not reproduce* — which sent me to re-read the assertions. The actual
cause was one `git commit` away, and nothing in the output pointed at it: the refusal is identical
whether the assertion is present-and-green (the real hazard the gate exists for — a vacuous
`expect_green`) or **absent entirely** (an operator workflow slip).

**These two states deserve different messages, because they have different fixes.** Present and
passing → your ticket is vacuous, fix the ticket. Absent → your tests are not in HEAD, commit
them.

**Narrowest fix**, in the gate that reads the baseline:

1. Classify each `expect_green` string three ways against the baseline run: **failing** (good),
   **found but passing**, **not found in the output at all**.
2. Refuse with the classification, not one generic list.
3. When any string is *not found* **and** `git status --porcelain` is dirty in a path the profile
   treats as a test path, add one line: *"N acceptance test(s) were not found at baseline. The
   worktree is built from HEAD (`<sha>`) and your test file has uncommitted changes — commit them
   before running."* That sentence would have saved the whole first run.

⚠️ Related, and worth stating in the docs even if the code does not change: **the loop measures
HEAD, not your working tree.** That is the right design — it is what makes the baseline
reproducible — but it is not written anywhere the operator meets it, and the "write the test
first" workflow makes an uncommitted test the *expected* state at exactly the moment you run.

---

### CF-3. `--list` prints warnings before the answer — LOW

Region resolution (the thing being asked for) prints once, above ~20 lines of CF-1 noise per
ticket. Put the region table last, or send warnings to stderr so `--list` can be read on its own.
Cheap, and it stops mattering entirely if CF-1 lands.

---

### CF-4. `--selftest` and `--version` do not exist, and the consumer's docs say they do — LOW, but it is a stale-docs trap

`tvDownloadOHLC/CLAUDE.md` records *"636 tests pass (34 skipped), `selftest` 13/13"* as the way to
check the install. At HEAD:

```
python -m agent_loop --selftest   -> error: unrecognized arguments: --selftest
python -m agent_loop --version    -> error: unrecognized arguments: --version
```

`--mode` accepts `patch, review, plan, test, developer, brainstorm, docs, report, replay,
run-plan` — no `selftest`. Either the flag was renamed/removed and the consumer doc is stale, or it
was never a flag. **A documented way to verify an install should exist**, because the alternative
is what happened here: the only proof the right version is loaded was
`python -c "import agent_loop, os; print(os.path.dirname(agent_loop.__file__))"`.

Suggest: `--version` printing the package version **and** the resolved package path (editable
installs are exactly when you doubt which copy is running).

---

### CF-5. Nothing in the run output states which agent-loop version produced the patch — MEDIUM

The consumer records *"implemented by agent-loop"* in commit messages and handovers, and a run at
`v0.6.7` and a run at HEAD (5,349 insertions later, with the evidence ledger, path isolation and
reasoning budget) are not the same tool. `logs/agent_loop/T1/` and the summary block should carry
the package version and git sha, so a green run is attributable months later. Same argument as the
consumer's own rule that a deployment is verified by content, not by the path the tool believes in.

### CF-6. An omitted `expect_green` disables the test-first gate SILENTLY — MEDIUM

`loop.py` reads `ticket.get("expect_green", ())`, and when the list is empty the whole
red-at-baseline check is skipped. The refusal path (CF-2) is loud; **the skip path prints
nothing at all**, so the run output for a ticket with no acceptance gate is indistinguishable from
one whose gate passed.

This came up while scoping a **pure refactor** for this consumer (`P3-124`: one symbol table
defined in four places). A refactor has no behaviour change, therefore no test that can be red
first, therefore no `expect_green` — so the strongest gate the loop has simply does not apply, and
nothing says so. The run is not *unguarded* (the no-regressions check still runs, and for a
refactor that is genuinely the right gate), but the operator cannot tell which of the two
situations they are in, and neither can anyone reading the log afterwards.

**Narrowest fix**: print one line either way, the way the gate already does when it fires —
`[test-first] 6 acceptance test(s) red at baseline` has a natural counterpart in
`[test-first] SKIPPED - ticket declares no expect_green; only the no-regression gate applies`.

**Better, if it is cheap**: let a ticket declare `"refactor": true` and then *require* the absent
`expect_green`, so "no behaviour change intended" is an assertion the ticket makes rather than an
absence anyone can create by deleting a line. That also gives the reviewers a fact worth having:
under a refactor ticket, any behaviour change visible in the diff is itself a finding.

This is the same shape as CF-1 and CF-2 and as three gates in the consumer repo: **state what the
gate inspected, including when the answer is "nothing".**

---

## What worked, recorded because it is evidence too

* **The refusal in CF-2 was CORRECT.** It refused to run a ticket whose `expect_green` was not red
  at baseline. That is the gate doing exactly its job — the complaint is only about the message.
* **`--list` resolved the region to 336-417**, the whole `Headline` method, and the docs' warning
  about degenerate one-line regions is well placed: it was checked precisely because the doc says
  to.
* **Round 1, both reviewers APPROVE, 11.0s of model time, and the patch was correct**, including
  the ordering constraint that the new rung must sit *below* the quarantine rungs — which was
  stated in the spec and is the part a careless fix gets wrong.
* **`[protected] 1 region file(s) clear of verifier`** and **`[lock-scope] ok`** both ran without
  being asked for.
* The consumer's `[memory] 48 settled decisions (20 from prior runs)` continues to load.
