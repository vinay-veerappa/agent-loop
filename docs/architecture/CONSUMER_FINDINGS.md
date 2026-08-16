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

### CF-7. The arbiter SETTLED the opposite of a finding it upheld in the same ruling — and settled decisions persist — **HIGHEST severity here**

Observed on a second ticket the same day (`P1-130`, a live `P1` in the consumer). One arbiter
output, verbatim:

```
[UPHELD] #1: The patch fails to increment `bracket.StopModifyAttempts` when the stop order is
             absent from `account.Orders`, causing an unbounded retry loop ...
<<<SETTLED>>>
- The failure counter may increment only when the stop order is still present in account.Orders
  but no longer occupies a live slot (P1-130, this ticket).
```

**#1 says it must count when the order is ABSENT. The settled decision says it may count ONLY when
the order is PRESENT.** They are direct contradictions, produced in one call, and the rationale
underneath even restates #1 correctly (*"the implementer must fix the counter increment to cover
the not-found case"*).

**Why this is the worst one in this document**: the run's rulings die with the run, but
`[memory] saved 2 settled decision(s) to store` wrote that sentence to
`logs/agent_loop/settled_decisions.jsonl`, where it is loaded into **later** runs as an established
constraint (`[memory] 48 settled decisions (20 from prior runs)`). A wrong ruling costs one round.
**A wrong SETTLED decision teaches every future run in that repo to re-introduce the defect** — and
it arrives labelled as something already decided, which is precisely the label that stops the next
reviewer arguing with it. It had to be deleted by hand from the consumer's store.

**Narrowest fix, and it is mechanical**: before persisting, check each nominated SETTLED decision
against the rulings in the same output. A settled decision that contradicts an UPHELD finding must
be dropped and the run flagged — the model has just written both sides of one question, so neither
is safe to keep. Even a crude check (does the settled text negate a term the upheld finding
requires?) would have caught this one, because the two sentences share their subject and differ by
the word *only*.

**Second, cheaper fix**: record the provenance of every settled decision — ticket, run id, and the
finding it derives from — and print settled decisions when they are LOADED, not only when saved.
Neither the save line nor the load line names what was learned, so a poisoned entry is invisible
until a later run behaves strangely for reasons nobody can trace.

⚠️ **Corroborating detail worth keeping**: the panel split, `glm-5.2=APPROVE(0)` and
`deepseek-v4-flash=REVISE(2)`, and **the minority reviewer was right on the substance**. The
consumer's own note that the second reviewer "is where blocking verdicts come from" is holding up —
but the arbiter mishandled the very finding it agreed with.

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

---

## Verification pass — `e2ed6bd` "fix: address 7 consumer findings", measured 2026-08-16

Every fix was **driven**, not read. The consumer repo is `nt8-riskguard` at `ce5fdc17`
(2034 tests green), the same repo and the same tickets the findings were filed from. Suite at
`e2ed6bd`: **714 passed, 34 skipped, 0 failed**.

| | verdict | how it was measured |
|---|---|---|
| CF-1 | ⚠️ **partly** — 20 warnings → **5**, all still prose | `--list` on the same ticket that filed it |
| CF-2 | ✅ **verified** | one run driving **both** states at once |
| CF-3 | ✅ **verified** | stdout 2 lines / stderr 11, split |
| CF-4 | ✅ **verified** (`--version`); `--selftest` still absent | run |
| CF-5 | ❌ **does not do what it was filed for** | computed both recorded fields |
| CF-6 | ✅ **verified** | run, with an unresolvable region so it cost no model call |
| CF-7 | ✅ **verified on the measured case**, with a good negative control | drove `validate_settled` |

### CF-2 — verified, and by the right test

The discriminator is not "does it print a better message", it is **do the two states produce
DIFFERENT messages** — a single reworded string passes the first and fails the second. One run
with one name of each kind:

```
[baseline] 2034 passed, 0 failed at ce5fdc17; 0 expected failure(s)
REFUSED: expect_green test(s) not failing at baseline:
  - present but passing (vacuous gate ... the test does not test the defect): ['P3-128: and it WARNS']
  - not found in the baseline output at all (typo or uncommitted ... the worktree is built from
    HEAD ce5fdc17 and your test file may have uncommitted changes): ['ZZZ-999: a test name that
    has never existed in this suite']
```

Classified correctly both ways, and the not-found branch names the HEAD sha. This is closed.

### CF-6 — verified

`[test-first] SKIPPED - ticket declares no expect_green; only the no-regression gate applies`.
Worth recording *how*, because it generalises: the probe ticket was given a deliberately
unresolvable anchor, so the run reached the gate, printed the line, and died at region extraction
**without spending a single model call**. That is a cheap way to test anything upstream of the
first model call.

### CF-7 — verified where it matters, and it is honest about being crude

`validate_settled` drops the **verbatim** poisoned pair from the P1-130 run (`safe=0 dropped=1`),
and — the part that matters more — leaves a *legitimate* settled decision containing the word
"only" alone (`safe=1 dropped=0`). It is not an alarm that is always on.

Three gaps, all consequences of keying on a literal word. Measured, not guessed:

1. **The same contradiction with the word "only" removed passes** (`safe=1`). The check's power
   comes from one token that a paraphrase deletes.
2. **The negation pairs are a fixed table** of eight. A contradiction over any other axis passes.
3. **`if not settled or not upheld_findings: return` — a ruling that upholds nothing can settle
   anything.** Defensible (with no upheld finding there is no in-ruling contradiction to detect),
   but it means the check's coverage depends on the arbiter having upheld something.

None of these are worth chasing with a bigger word list. The useful move is the one this consumer
has now learned four separate times about its own gates: **state the region the check inspected.**
`validate_settled` should say what it examined — *"checked 2 settled decision(s) against 1 upheld
finding(s); dropped 1"* — so a run where it inspected nothing is distinguishable from one where it
found nothing. Right now both print nothing at all.

### CF-1 — 75% of the way, and the residual has an exact cause

Same ticket, same command: **~20 warnings → 5**. But all five are still prose, and two of them are
`SCOPE` and `NOW` — **ALL-CAPS words, which the fix's own docstring names as the exemplar it
eliminates** (*"ALL-CAPS with no lowercase = prose (CSS, DETAIL, SCOPE)"*). `SCOPE` still warns.

The prose filter is correct in isolation; it is **ORed with two broader rules that overrule it**:

```
SCOPE      _looks_like_code=False  in_call_tokens=True   -> WARNS
NOW        _looks_like_code=False  in_call_tokens=True   -> WARNS
Do         _looks_like_code=True   in_call_tokens=False  -> WARNS
Five       _looks_like_code=True   in_call_tokens=False  -> WARNS
Reporting  _looks_like_code=True   in_call_tokens=False  -> WARNS
```

* `call_tokens = re.findall(r'\b([A-Z][a-zA-Z0-9_]+)\s*[.(]', spec_text)` is meant to catch
  `Foo.Bar` and `Foo(`. It also catches **a capitalised word that ends a sentence** — and the
  house style CF-1 was filed about writes headings exactly that way: the measured text is
  `'m. SCOPE. Thi'` and `'EN NOW. It mu'`.
* `_looks_like_code` returns True for **any** mixed-case token, so every sentence-initial English
  word qualifies (`Do`, `Five`, `Reporting`). ⚠️ **The comment at the call site describes the
  right rule and the function implements a broader one**: the comment says *"it has an interior
  lowercase->uppercase transition (camelCase)"*, which `Do` does not have — but the function only
  checks `has_upper and has_lower`. Comment and code disagree, and the code is what runs.

**Narrowest fix**, both one-liners: require the `.`/`(` to be followed by an identifier character
(`Foo.Bar`, `Foo(` — never `. ` + capital, which is a sentence boundary), and implement the
interior-transition rule the comment already claims. Adding `Do`/`Five`/`Reporting` to the
stop-word list would be the wrong fix — the list is already 19 words long and English is not.

### CF-5 — the fields were added; neither one identifies the tool ⚠️ NOT FIXED

This is the finding to re-open. Both fields are set unconditionally at the top of `run_ticket`,
and here is what they evaluate to on this box:

```
agent_loop_version = 0.6.7            <- importlib.metadata, the packaging constant
agent_loop_sha     = ce5fdc1          <- git rev-parse --short HEAD, cwd=str(repo)
                                         ...which is nt8-riskguard, the CONSUMER
agent-loop's own HEAD = e2ed6bd
```

Two separate problems, and together they leave CF-5's stated purpose unmet:

1. **`cwd=str(repo)` is the consumer repo, not the package.** The commit message says so out loud
   (*"git rev-parse --short HEAD of the consumer repo"*), so this is a spec slip rather than a
   typo. That sha is already printed on the `[worktree] agentloop-T1-36912 @ ce5fdc17` line and in
   `[baseline] ... at ce5fdc17` — the run now records it a third time, and records the tool zero
   times. Fix: `cwd=Path(agent_loop.__file__).resolve().parent`.
2. **`agent_loop_version` is frozen at the tag.** CF-5's whole premise was that *"a run at v0.6.7
   and a run at HEAD are not the same tool"* — and both runs record `0.6.7`, because the constant
   has not moved since the tag. This is the consumer's own `check_version_matches_tag.py` lesson
   arriving here: **that gate catches constant-behind-tag and is blind to code-ahead-of-tag.**

The divergence is not hypothetical, and it is bigger than it looks:

```
requirements.txt:  git+https://github.com/vinay-veerappa/agent-loop.git@v0.6.7
pip show:          Version: 0.6.7
                   Editable project location: C:\Users\vinay\agent-loop   <- HEAD, e2ed6bd
```

What is installed is an **editable pointer to a working checkout**, thousands of insertions past
the tag that names it. A colleague running `pip install -r requirements.txt` gets materially
different code and **every version surface agrees with them**. The one number that would have
revealed it is the one that is frozen. ⚠️ Note this also silently changed what CF-4 buys: it
prints the resolved *path*, which is the only surface on the box that was telling the truth.

### CF-8 (new, LOW) — the new messages are written to a Windows console as UTF-8

The CF-2 message renders as `vacuous gate <?> the test does not test the defect` on a cp1252
console — the em dash does not survive. Cosmetic, but it is the **output** half of exactly the
encoding class `v0.6.7` fixed for subprocess *capture*, and it appeared in brand-new code. Either
reconfigure stdout once at startup (`sys.stdout.reconfigure(encoding="utf-8", errors="replace")`)
or keep the loop's own operator-facing strings ASCII. The house prose in this document is full of
em dashes; the *program's* prose does not need them.

### CF-9 (new, LOW) — a refused or errored run leaves the previous run's `result.json` in place

Both probe runs above ended without writing `logs/agent_loop/T1/result.json`, so it still reports
`final_verdict: MAX_ROUNDS_EXHAUSTED` from a run hours earlier. Nothing in the directory records
that two later runs were refused. The early returns in the gate block build a `result` dict and
return it without persisting; the last *completed* run is therefore indistinguishable from the
current state of the ticket. Same family as CF-6: **an absent record and a stale record read
identically.**

### Still true, still worth stating in the docs

The loop measures **HEAD, not your working tree**. CF-2's message now says so at the moment it
bites, which is most of the value — but the "write the test first" workflow makes an uncommitted
test the *expected* state at exactly the moment you run, and that is worth one line in the README
rather than only in an error path.
