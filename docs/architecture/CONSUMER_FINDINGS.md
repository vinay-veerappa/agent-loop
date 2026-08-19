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

### CF-5 — the fields were added; neither one identifies the tool ✅ FIXED (commit `0a588ba`)

**Fixed:** `agent_loop_describe` added to `result.json` — `git describe --tags
--always --dirty` gives `v0.6.7-23-g23ba872` (23 commits past the tag), which
distinguishes a tag run from a HEAD run. The packaging constant
(`agent_loop_version`) is kept for compatibility but is no longer the only
version surface. Also printed in the terminal summary as `tool: v0.6.7-23-g23ba872`.

The original finding is preserved below for context.

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

### CF-10 (new, HIGH) — the arbiter upheld a finding the ticket had explicitly scoped OUT, and reported `out-of-scope=0` while doing it

Ticket `P2-127` in `nt8-mcp-bridge`, run at `abea3bc`. Its `context` field opens with a SCOPE
paragraph naming, in order, the things the ticket does not touch — including *"the wiring of the
`system` cell into it, are later slices of P2-127 and are deliberately not in this one"*. The
arbiter's first ruling:

```
- UPHELD #1: The system severity is never incorporated into the tree's rank computation ...
[arbiter] REVISE (upheld=2 rejected=10 out-of-scope=0)
```

**The ruling line has an `out-of-scope` category and it reported zero.** So the mechanism exists
and did not fire on the one finding the ticket had pre-emptively answered in prose. That upheld
finding is half of what drove `NOT_CONVERGING`: it cannot be closed without doing work the ticket
forbids, so each round either ignores it (and stays REVISE) or starts widening the patch.

**Narrowest fix**: the arbiter prompt already receives the ticket. Give the scope text its own
labelled block rather than leaving it inside `context` prose, and require a ruling of
`OUT_OF_SCOPE` — not `UPHELD` — for any finding whose subject the scope block names. Cheaper
alternative if that is too strong: make the arbiter quote the sentence it believes puts a finding
in scope, which is the same discipline the reviewers are already held to.

⚠️ **Worth recording alongside it: the arbiter also REJECTED a finding that was correct.** It
dismissed *"the unlinked children sort is stable"* as "stable and correct" — `List<T>.Sort` is
documented **unstable**, and the consumer verified the defect by mutation afterwards. So in one
ruling it upheld something out of scope and rejected something true. **The rulings are not a
filter you can lean on in either direction**, which is the same conclusion the consumer's own
memory reached from four earlier runs; this is the first time both errors appeared in one output.

### CF-11 (new, MEDIUM) — the worktree does not populate submodules, so submodule-dependent tests are dark for the whole run

Measured, same run. `nt8-mcp-bridge` vendors its core as a git submodule and has two tests that
assert on it. In the loop's worktree they cannot pass:

```
[baseline] 439 passed, 17 failed at e18b09a4; 17 expected failure(s)
```

against **444 passed, 15 failed** for the identical commit in the main checkout. The loop's
handling is *correct* — it treats them as expected failures and reports no regression — and the
run is not invalid. But **two real gates were dark for four rounds and nothing in the output says
so**, and the operator only notices by comparing two numbers that appear in different places.

Note the assertion COUNT also dropped (456 vs 459), because a failing assertion aborted the rest
of its method — so the difference is not simply "two more failures".

**Narrowest fix**: `git worktree add` does not initialise submodules; run
`git submodule update --init --recursive` in the new worktree when `.gitmodules` exists. If that is
unwanted (it is a network fetch), then say it: one line at baseline time — *"`.gitmodules` present;
submodules are NOT populated in the worktree, so N test(s) may fail for that reason alone."*
Same principle as CF-6: **state what the gate inspected, including the part it could not.**

### CF-12 (new, MEDIUM) — `--list` exists to catch a malformed ticket without spending a model call, and it crashes with a raw traceback instead of naming what is wrong

Measured 2026-08-16 writing `agent/tickets_p1133.json`. A region written with `start`/`end` line
numbers instead of an `anchor`:

```
  File "src/agent_loop/regions.py", line 646, in extract
    start, end = find_region(lines, spec["anchor"], kind, profile)
KeyError: 'anchor'
```

The ticket title had already printed, so **the crash looks like it happened while processing that
ticket's content** rather than while reading its schema. `find_region` itself is exemplary about
this — an anchor spanning two lines raises a `RegionError` that explains anchors are matched one
line at a time, *and lists the nearest real lines in the file*. The schema layer above it has none
of that care.

⚠️ **The failure mode this permits is the expensive one.** `--list` is documented in the consumer's
own `CLAUDE.md` as *"validate a ticket file without spending a model call"*. A contributor who sees
a traceback reasonably concludes the tool is broken rather than their ticket, and runs the real
thing — which is where the model call gets spent.

**Narrowest fix**: validate each region dict before extraction and raise `RegionError` naming the
region's `id`, the key that is missing, and the two shapes that are legal (`anchor` + optional
`kind`, or `op: "create"`). One `if` in `extract`.

### CF-13 (new, LOW) — the "named in spec but not found" warning cannot see a file the ticket CREATES, and repeats itself once per region

Same ticket, same command. It has one `op: "create"` region for a new class and a `spec` that names
that class's members. `--list` emitted **49 warning lines**:

```
WARN 'AtmOrderIdentity' named in spec but not found in addons/DynamicAtmManager.cs
     -- model will guess; add its declaration to a read-only region
WARN 'EntryName'  ... (x7)
WARN 'FindByName' ... (x7)
```

Two separate things:

1. **The symbols are unfindable BY CONSTRUCTION** — they belong to a file this ticket is asking the
   model to write. The advice *"add its declaration to a read-only region"* is not just unhelpful,
   it is impossible to follow. The check should skip symbols that match a `create` region's file,
   or at minimum say *"…not found; note this ticket creates `addons/AtmOrderIdentity.cs`, so this
   may be expected."*
2. **7 symbols × 7 regions = 49 lines**, because the check runs per region and each region scans the
   whole spec. The set of symbols is a property of the *ticket*, not of a region. De-duplicate.

The signal is real and worth keeping — it caught a genuine class of ticket defect before. But **a
correct ticket currently produces 49 warnings and one line of useful output**, which is the shape
that trains people to stop reading warnings. Same family as this repo's own
*an alarm that is always on is off*.

⚠️ It also fired on `'Stop_15bc730b'`, a **live order name quoted in the defect narrative as
evidence**. Anything that looks like an identifier in prose is treated as a symbol the model will
need. Restricting the scan to the `spec` field rather than `defect` would cut most of that.

### CF-14 (new, HIGH) — a source file vanished from the worktree between rounds, and round 2 died on `FileNotFoundError` instead of the run failing cleanly

Measured 2026-08-16, `nt8-riskguard`, ticket `T1` of `agent/tickets_p1133.json`. Seven regions: six
in `addons/DynamicAtmManager.cs`, one `op: "create"` for `addons/AtmOrderIdentity.cs`.

```
[baseline] 2034 passed, 4 failed at 8b4f93a7; 4 expected failure(s)
round 1: implement 282.3s   [static] ok  [compile] ok
         [test] FAIL - 21 regression(s); 2016 passed, 23 failed, 2 expected failure(s) now green
round 2: implement 472.6s in=9351 out=66076
ERROR T1: FileNotFoundError: 'C:\Users\vinay\agentloop-T1-39480\addons\DynamicAtmManager.cs'
```

Round 1 is an ordinary red round and the loop was right to retry. What is not ordinary: by the time
round 2 tried to re-read its regions, **the file those six regions live in was gone from the
worktree.** The run ends `applied=False` with a stack-trace message rather than a verdict, so a
recoverable red round is reported the same way an infrastructure failure would be.

⚠️ **The sharpest clue is that the deletions are INVERTED.** After the run the worktree contained
exactly one file:

```
agentloop-T1-39480/addons/AtmOrderIdentity.cs      <- untracked, created by the run: SURVIVED
agentloop-T1-39480/addons/DynamicAtmManager.cs     <- tracked, in HEAD:            DELETED
```

`Workspace.revert` is the only code that unlinks by path, and its contract is precisely the
opposite of this: restore what is in HEAD, remove what is not. So either `revert` ran and got both
files backwards, or it never ran and something else removed the tracked file. **I could not
establish which, and am not going to guess** — the observed state is the finding.

(The tracked files being absent *at the end* is expected and is not the bug: teardown runs
`git worktree remove --force`, which deletes tracked content and correctly leaves the untracked
file, printing *"still present and NOT empty, left alone"*. That teardown happens in a `finally`,
after the error. It is a red herring in the log ordering, and worth knowing when reading one.)

**Two fixes, and the second matters more than the first:**

1. Find the deletion. A cheap guard regardless of cause: before each round, assert every region
   file still exists and fail with *"`<file>` disappeared from the worktree between rounds"*, which
   names the problem instead of leaking a path from inside `open()`.
2. **An exception inside a round should end that round, not the run.** Rounds already have a
   failure vocabulary — `[static] FAIL`, `[compile] FAIL`, `[test] FAIL`. An unhandled exception is
   the one outcome that escapes it, and it escapes at the point where the loop has the most context
   about what it was doing and the operator has the least.

⚠️ **Cost to the consumer: this is the second round of a two-round ticket, so ~755s of model time
and 100k output tokens produced nothing promotable** — and the only artifact left on disk was the
new file, which happened to be correct. A retry that cannot retry is worse than a loop that stops
at round 1, because the budget is spent before the failure is visible.

### CF-15 (new, HIGH) — four rounds returned the IDENTICAL failing test set and the loop never said "this may be unreachable from your regions"

Measured 2026-08-16, same ticket as CF-14, on the clean re-run:

```
round 1: 219.7s  out=31411  [test] FAIL - 21 regression(s); 2016 passed, 23 failed, 2 expected now green
round 2: 130.0s  out=18870  [test] FAIL - 21 regression(s); 2016 passed, 23 failed, 2 expected now green
round 3:  64.5s  out= 9241  [test] FAIL - 21 regression(s); 2016 passed, 23 failed, 2 expected now green
round 4:  93.5s  out=12757  [test] FAIL - 21 regression(s); 2016 passed, 23 failed, 2 expected now green
NOT APPLIED: verdict=ARBITER_NEVER_RAN
```

**The cause was a ticket defect and the model's work was correct throughout.** The ticket gave
`ModifyStopPrice` as a region and the fix changes its parameter from an id to a name — but
`RequestStopMove`, the sole caller, was *not* a region, and it passes `bracket.StopOrderId`. So the
callee wanted a name, the caller kept handing it a GUID, and every stop move failed. **The model
could not have fixed it**: the one line that needed to change was outside every region it was
allowed to write.

⚠️ **The numbers are the tell, and the loop has them.** Four rounds, the same 21 regressions, the
same `23 failed`, the same `2 expected now green` — while `out=` fell 31411 → 9241, i.e. the model
was *running out of things to try*. That is a distinguishable state from "not converging yet", and
it has a specific likely cause worth naming:

> Round N produced the same failing test set as round N-1 (and N-2). The fix may be outside the
> regions this ticket grants. Failing tests reference `RequestStopMove`, which is in
> `addons/DynamicAtmManager.cs` but not in any region.

Even the first clause alone would have saved three rounds; the last clause is cheap — the loop
already parses failure output, and it already knows the region set and their files.

⚠️ **`ARBITER_NEVER_RAN` is the wrong last word for this.** It describes the machinery, not the
run: it reads as *something went wrong with the arbiter*, when what happened is *the rounds never
produced a candidate worth arbitrating*. Compare `NOT_CONVERGING`, which names the run's own
condition. A verdict named after a component that was never reached sends the operator to the
wrong place first — I went looking at the panel config before reading the patch.

**Cost**: 508s of model time and ~72k output tokens across four rounds, all of it correct work
against an impossible constraint. Combined with CF-14's failed first attempt, one ticket spent
~21 minutes of model time before the actual defect (mine) was visible — and it was visible in
`final.patch` in about thirty seconds, because `grep -c RequestStopMove` returned `0`.

**Consumer-side lesson, recorded because it is not the loop's fault**: when a fix changes a
signature, the region set must include every CALLER, not just the sites that match the pattern you
grepped for. I grepped for `OrderId` comparisons and got four sites; the fifth site *passes* the
id and compares nothing.

### Still true, still worth stating in the docs

The loop measures **HEAD, not your working tree**. CF-2's message now says so at the moment it
bites, which is most of the value — but the "write the test first" workflow makes an uncommitted
test the *expected* state at exactly the moment you run, and that is worth one line in the README
rather than only in an error path.

---

## Review of the CF-12..CF-15 fixes (ebfc757, 9f9d598)

Read before running the loop again. Six findings, all in the fixes themselves,
found by reading the two commits rather than by running anything. **Neither
commit added a test.** This repo has 70 acceptance tests, one per finding, and
that convention is the reason its fixes hold — five of the six below would have
been caught by writing one.

### CF-16 — the stuck detector recovered its failing set by scraping a rendered string

`loop.py` rebuilt the failing-test set by walking `GateResult.detail` for lines
beginning `"- "`. The regression path renders **two** bullet lists with that
exact prefix:

```
REGRESSIONS (not in baseline):
  - test_alpha
Newly passing:
  - test_gamma
```

So a round that broke 2 tests and fixed 5 was recorded as a **7-test failure
set**, and the warning's "Failing tests reference: ..." named tests that had
just started **passing** — sending the reader to look at working code. `detail`
is built for a human; the set has to travel as data. Fixed by adding
`GateResult.failing: Tuple[str, ...]`, populated at both red returns, with a
source gate pinning the count at 2 so a third red path cannot go dark.

### CF-17 — "consecutive" was not consecutive

`test_failure_history` is appended to **only when the test gate fails**. A round
that failed to compile in between left no entry, so rounds 1, 2 and 7 satisfied
"3+ consecutive rounds" and the warning said so in as many words. Fixed by
requiring the round numbers to be adjacent as well as the sets equal.

### CF-18 — the diagnosis was written to the one field the reader does not read

This is the one that matters. The stuck message was appended to `failed.summary`.
The implementer is handed:

```python
{"role": "user", "content": failed.feedback or failed.summary}
```

and `check_tests` **always** populates `feedback` on both red paths — so on the
only path that can produce a stuck round, `summary` is dead. The console print
was `[stuck] identical test failures for 3 consecutive rounds`, carrying none of
the region files, none of the failing tests and none of the advice. The whole
diagnosis was computed correctly, stored in `result.json`, and **read by nobody
until the run was already over**, which is the state CF-15 exists to fix.

⚠️ Note who can act: the advice is *add a region*, and only the **operator** can
do that. So the console is the load-bearing channel and it was the one carrying
nothing. Fixed by printing it in full and appending to `feedback` as well, so
delivery is not conditional on which field a future caller happens to prefer.

Same shape as *an alarm that is always on is off*, inverted: **an alarm wired to
an output nobody is listening on.**

### CF-19 — a comment described a narrowing that was never written

`cli.py` carried:

```python
spec_text = t.get("spec", "") + " " + t.get("context", "")
# CF-13: also restrict the scan to the spec field, not the defect narrative
spec_only = t.get("spec", "") + " " + t.get("context", "")
```

Two variables, one expression, and a comment claiming the second is narrower
than the first. `spec_text` was then never used. Nothing was restricted. Fixed
by deleting the dead variable and rewriting the comment to say what the code
does — narrowing may well be right, but it needs evidence that `context` is
where the false positives come from, and nobody has measured that.

### CF-20 — the CF-1 fix overshot and dropped every zero-arg call

Tightening the call rule to `\(\s*[\w"\']` (no whitespace before the paren)
correctly stopped reading `SCOPE (the test...)` as a call. It also stopped
matching `Flatten()`, `CanTrade()`, `Reset()` — **zero-arg calls**, because the
character class requires content inside the parens and `)` is not in it. A
predicate or a command is the shape these tickets are mostly about. Fixed by
adding `)` to the class.

⚠️ A filter tightened past its target fails **silently**: you get fewer warnings
and read it as the fix working.

### CF-21 — the encoding gate said "every text capture" and walked `src/` only

`test_subprocess_capture_encoding.py` pins `SRC = src/agent_loop` and has
already been widened once (`glob` → `rglob`, 26 → 29 files) under a comment
about gates that pass when their subject shrinks. It stops at `src/`. Meanwhile
`tests/` had **five** live captures decoding without an explicit encoding —
`git apply --check`, `git stash list`, and two `python -m pytest` runs against
generated repos — and this repo's own consumer tests emit non-ASCII assertion
text. On Windows that kills the reader thread and hands the test `stdout is
None`, which surfaces as an AttributeError blaming the assertion rather than
the capture.

Fixed: five sites pinned, and a second gate added over `tests/`. The **sixth**
unpinned capture is the existing negative control that reproduces the hazard on
purpose — it is why the suite prints one `PytestUnhandledThreadExceptionWarning`
naming a cp1252 `UnicodeDecodeError`, and **that warning is the control working,
not a defect**. It is exempted by `(file, function)` rather than line number,
and the gate asserts the exemption was **used**, so an allowlist that has rotted
fails instead of quietly permitting the control to be pinned — which would
delete the proof that the hazard is still real.

Fourth instance of *state the region a gate inspects*.

### What was verified

Suite **716 → 730 passed, 34 skipped**. The 14 new tests were run against the
pre-fix source first: **6 red**, and the widened encoding gate was driven red by
un-pinning one site and watched fail by name.

### CF-22 — the suite had never run anywhere but one machine

Adding CI (this repo had none) turned up two pre-existing defects on its first
run, neither of which is in the loop's runtime and both of which were invisible
while `pytest -q` was only ever run in one place.

**37 of 39 Windows failures were one cause: no git identity.** Dozens of tests
build a scratch repo and commit into it. A CI runner has no global
`user.email`, so every `git commit` silently failed, the repo was left with no
HEAD, and the symptom surfaced three layers away as

```
agent_loop.workspace.WorkspaceError: git rev-parse HEAD failed:
  fatal: ambiguous argument 'HEAD': unknown revision or path not in the working tree
```

which reads as a defect in `workspace.py`. ⚠️ **The suite passed locally because
the developer machine has an identity** — a green there was evidence about the
machine, not about the code. Same family as *a worktree is not a fresh checkout*.

**Linux fails for a second, separate reason: the fixtures are not portable.**
They shell out with

```python
os.system(f'cd /d "{repo}" && git init && git add -A && git commit -m init --allow-empty')
```

`cd /d` is cmd.exe. On Linux the whole command is a no-op and the tests that
depend on it fail in a heap. The linux jobs are **deliberately not in the
matrix** rather than left red — a CI that is always red is off, which this
project has already paid for once (10 consecutive red pushes read as green).
Add them back when the fixtures use `subprocess.run(cwd=...)` instead of a
shell string; that is a contained change and worth doing, but it is a test-suite
job and not a loop fix.

### CF-23 — a two-model panel made the drop-a-malfunctioning-reviewer rule unreachable

`loop.py` drops a reviewer that malfunctions -- times out, or returns many times the finding cap
-- and proceeds on the survivors. Its own comment states the intent plainly:

> one malfunctioning reviewer (returning 8x the findings cap, or timing out) ended the ticket --
> even when the other reviewer had a clear verdict [...] it should be DROPPED with a loud line,
> not allowed to end the ticket.

The quorum under it is `ceil(2 * len(reviewers) / 3)`. That was written when the panel had **three**
members, where it evaluates to 2 and leaves room for exactly one casualty. **v0.6.6 cut the panel
to two**, and `ceil(4/3)` is **2** — so the quorum became unanimity and the rule could never fire.
Nothing failed; the mechanism simply stopped having a case, disarmed by an edit in a different
file that never mentioned it.

**Measured on `nt8-riskguard`, two sessions running.** `deepseek-v4-flash` returned **373**
findings, and then **853**, against a cap of 60. Both times every mechanical gate had passed,
`glm-5.2` said APPROVE, and the run ended `PANEL_OUTAGE` with the patch arbitrated by hand:

```
[test] ok - no regressions; 2063 passed, 0 failed; all 5 acceptance test(s) green
[panel] APPROVE  [glm-5.2=APPROVE(0), deepseek-v4-flash=UNPARSEABLE(0)]
panel OUTAGE - no quorum (1/2)
NOT APPLIED: verdict=PANEL_OUTAGE
```

Fixed by capping the quorum at `len(reviewers) - 1`, so it can never *be* the whole panel. Below
that cap the 2/3 rule is unchanged — the acceptance test pins 3→2, 4→3, 5→4, 6→4 as a negative
control, so this cannot quietly loosen larger panels, and 1→1 because a one-model panel with no
answer is not a review.

⚠️ **The general shape: a rule expressed as a ratio of a population is disarmed by shrinking the
population**, and the code that shrinks it is nowhere near the code that reads it. The finding cap
itself is right and stays — 853 findings is repetition, not review. What was wrong is that one
member's malfunction was allowed to be the whole panel's verdict.

### CF-24 — a second harness nobody ran, failing on a verdict name the ticket path does not produce

`python -m agent_loop.selftest` drives the whole loop against stubbed models and asserts a verdict
per scenario. It was found at **11/13**, and both failures were the same stale expectation:

```
expect=PANEL_UNREACHABLE  got=PANEL_OUTAGE
```

**`loop.py` does not have a `PANEL_UNREACHABLE`.** The ticket path says `PANEL_OUTAGE`;
`plan_mode.py`, `replay.py` and `developer/driver.py` all say `PANEL_UNREACHABLE`. **Two names for
one condition**, split across modes, and this harness asserted the name the ticket path never
emits. A comment a few lines above the failures even records the moment a sibling expectation was
corrected for exactly this reason — *"the expectation here was left at the old name when that split
landed"* — and these two were left.

It had been red for an unknown number of sessions while `pytest -q` was **744 / 0** and CI, which
runs only pytest, reported that green as the repo's state. **A harness nobody runs is not a
harness**, so it is now a CI step.

Fixed expectations, both of which are now load-bearing for `CF-23`: one reviewer returning EMPTY on
a two-model panel yields `APPROVE_PARTIAL` (the malfunctioning member is dropped, the other's
APPROVE carries), and **both** reviewers failing still yields `PANEL_OUTAGE` — the negative control
that keeps `CF-23` from meaning "any single opinion is enough".

⚠️ **The two verdict names are NOT unified here.** Scripts, logs and the operator's own habits may
match on either, so renaming is a change with a blast radius that wants its own ticket. What is
recorded is that they exist and which mode produces which.

⚠️ Note the sequencing: `CF-23` changed a real outcome and **`pytest -q` stayed green**, because
nothing in that suite covered it. The selftest was the only thing that noticed, and it was not
running. The acceptance test added with `CF-23` covers the arithmetic; this covers the outcome.

### CF-25 — the implementer rewrote 11 unrelated comments to strip non-ASCII, inside protected regions

**Measured on `nt8-riskguard`, ticket `P1-160`, round 3 (`kimi-k2.7-code:cloud`).** The patch that
passed every mechanical gate — static, compile, test, lock-scope — contained the fix in 5 hunks and
**11 further hunks that changed nothing but comment text**, across three files:

```diff
-        // ⚠️ `!= 0.0` AND NOT `> 0`. An account whose equity has gone NEGATIVE is reporting a
+        // WARNING: `!= 0.0` AND NOT `> 0`. An account whose equity has gone NEGATIVE is reporting a

-        // ── helpers ───────────────────────────────────────────────────────────────────
+        // --- helpers -------------------------------------------------------------------
```

Every `⚠️` in a touched region became `WARNING:`, and two `//` lines inside a parameter comment
became `///`, which changes what the C# compiler treats as documentation. The consumer repo uses
`⚠️` as a deliberate convention for the paragraph that records why a line is the way it is —
several hundred instances — so this is not cosmetic drift, it is the loop rewriting the thing the
repo uses to keep its own reasoning attached to the code.

**Why the gates could not see it.** `[static]` checks that the emitted blocks are well-formed;
`[compile]` and `[test]` are indifferent to a comment; `[lock-scope]` reads for broker calls. There
is no gate that asks *"did this patch change anything the ticket did not ask for"*, and the review
panel did not raise it either — `glm-5.2` filed 5 findings, none about the rewrites.

**It is NOT the prompt builder, and that was the first thing to rule out.** The saved
`00_implement_prompt.md` for this run carries **10** warning glyphs, **69** box-drawing characters
and **zero** replacement characters — the region text reaches the model with its bytes intact.
So this is not the cp1252 round-trip hazard `CF-22` pins against arriving by another door: the
model is handed the glyphs and emits ASCII in their place. That narrows the fix to the emit side,
and it means no amount of hardening the extraction path will help.

⚠️ **The consequence is worse than noise, because the obvious response is to accept the patch.**
It applies cleanly, the suite is green, and a reviewer skimming a 271-line diff for the logic will
not read 11 comment hunks. Applying it would have silently degraded three files, and the next
patch would have carried the degradation forward as context. I filtered the diff down to the 5
intended hunks by hand.

**Suggested fix, in order of value:**

1. **Say it in the implement prompt.** The prompt tells the model what to change; it does not tell
   it to reproduce untouched lines byte-for-byte. One sentence — a line you are not changing must
   come back exactly as given, including non-ASCII — is the cheapest thing to try, and it is
   testable against this exact run.
2. **A gate that fails a patch touching a line the ticket's regions do not cover** would be too
   strict — regions are coarse. But a gate that fails a hunk whose only change is inside a comment,
   unless the ticket asks for documentation, is cheap and would have caught all 11.
3. At minimum, **print a count of comment-only hunks** in the round summary, so a human filtering
   the patch knows how many to look for rather than discovering them by reading.

**Not a blocker for the loop's usefulness.** The logic in that same patch was correct and the run
was still worth its cost — this is about the diff carrying passengers.
