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

### Implementation (CF-25 fix)

- **Prompt fidelity rule** added to `build_implement_prompt` (`loop.py`): tells the model
  to reproduce untouched lines byte-for-byte, including non-ASCII glyphs and comment syntax.
- **`check_comment_drift` gate** (`gates.py`): a **BLOCKING** gate (ok=False) that uses
  `difflib.SequenceMatcher` to align original vs replacement lines, then compares
  code-stripped forms. When every changed line is code-identical but raw-different, the
  only thing that changed is the comment. Handles line-count changes (reflow, insertion,
  deletion) correctly via difflib opcodes — no false positives from shifted diffs.
- **Doc-ticket exemption**: a ticket whose spec asks for documentation changes
  ("update documentation", "rewrite comments") is exempt — the model is supposed to
  rewrite comments there. Uses intent-phrase matching, not bare word matching, so
  "delete the comment on line 5" does NOT exempt the ticket.
- Gate also runs at **arbitration** — a candidate with comment drift is never promoted
  even if the panel approved.
- Acceptance tests in `tests/acceptance/test_cf25_comment_only_hunks.py` (13 tests):
  blocks comment-only rewrites, blocks box-drawing reflow, blocks comment insertion,
  allows doc tickets, blocks incidental "comment" mentions, clean on code changes,
  clean on mixed blocks, clean on identical blocks, clean on create regions.

### CF-26 — `--mode plan` runs ZERO rounds on the documented invocation, and calls it MAX_ROUNDS_EXHAUSTED

**Measured on `nt8-riskguard` 2026-08-19, first attempt to use plan mode this session.** The exact
invocation the consumer's own `CLAUDE.md` documents:

```
python -m agent_loop --profile nt8-riskguard --profile-module agent.nt8_riskguard \
    --mode plan --defect "<description>"
```

returned in under two seconds, having made **no model call at all**:

```json
{ "ticket": "PLAN", "rounds": [], "plan": null, "verdict": "MAX_ROUNDS_EXHAUSTED" }
```

**Cause.** `cli.py:740` declares `ap.add_argument("--max-rounds", type=int, default=0)`, and
`_plan` passes `max_rounds=args.max_rounds` straight through. `plan_mode.py` then does
`for rnd in range(1, max_rounds + 1)`, which for `0` is `range(1, 1)` — **empty**. `final` keeps its
initial value of `"MAX_ROUNDS_EXHAUSTED"` and is returned as the verdict.

`plan_mode.py`'s own signature says `max_rounds: int = 4`. That default is unreachable from the CLI,
because the CLI always supplies a value and the value it supplies is `0`.

**It is a per-mode gap, not a global one.** Two modes already have the fallback:

| site | line | has fallback |
|---|---|---|
| `loop.py` (patch) | `max_rounds = max_rounds or _loop_cfg.max_rounds` | ✅ |
| `replay.py` | `max_rounds = max_rounds or config.get().loop.max_rounds` | ✅ |
| `plan_mode.py` — BOTH round loops (defect plan and feature plan) | — | ❌ |

So patch mode has worked all along on `--max-rounds 0` and plan mode never has. Passing
`--max-rounds 4` explicitly makes plan mode work immediately, which is how this was confirmed.

⚠️ **Three separate things make this silent rather than loud**, and the verdict is the least of them:

1. **The verdict misattributes the cause.** `MAX_ROUNDS_EXHAUSTED` with `"rounds": []` is
   self-contradicting: you cannot exhaust rounds you never entered. Any verdict that a zero-iteration
   loop can produce is a verdict that says nothing about why.
2. **No error is surfaced anywhere** — no `error` key, no stderr, and `logs/agent_loop/PLAN/`
   contains only `result.json`. Patch mode writes `00_implement_prompt.md`; a plan run that made no
   call writes no prompt, so there is nothing to distinguish "the model refused" from "we never
   asked".

   ⚠️ **CORRECTION, from a later run in the same session:** `"rounds": []` is **not** a symptom of the
   zero-round bug, because it is *always* empty. `plan_mode.py` appends to `result["rounds"]` on
   exactly one path — `except ProviderError` — so a run that completes four successful rounds writes
   the identical `{"rounds": [], "plan": null, "verdict": "MAX_ROUNDS_EXHAUSTED"}`. A four-round
   rejection and a zero-round no-op are **byte-identical in `result.json`**. That is the more serious
   half of this finding: the artifact cannot distinguish "never asked" from "asked four times and was
   turned down", and the only way to tell them apart is to count `rN_plan_raw.txt` files by hand.
3. **It exits 0.** A caller scripting the documented `plan → run-plan` chain sees success and an
   absent plan.

**Suggested fix**, in the order that matters: give `plan_mode` the same `max_rounds or config`
fallback both other call sites have — better still, resolve it once in `cli.py` so a third mode
cannot be added without it. Then make a zero-iteration round loop **impossible to mistake for
exhaustion**: initialise `final` to something like `"NO_ROUNDS_RAN"`, or assert `max_rounds >= 1`
at entry. And return non-zero for any verdict that produced no plan.

⚠️ A test for this cannot be "plan mode returns a plan" — that needs a live model. It can be
`max_rounds=0` raises, or `range` is never empty. **The property is that the loop body runs at least
once**, which is assertable without a provider.

### CF-27 — `--help` cannot be printed on a Windows console, because of one arrow

**Measured on `nt8-riskguard` 2026-08-19, same session, before anything else was attempted.**

```
python -m agent_loop --help
...
  File "...\encodings\cp1252.py", line 19, in encode
UnicodeEncodeError: 'charmap' codec can't encode character '\u2192' in position 4710
```

`--help` is the tool's front door and it raises a traceback instead of printing. The character is
`\u2192` (`→`), and it is inside an argparse `help=` string — `cli.py:782`:

```python
help="run-plan mode: chain plan → run-plan --tdd --apply in one invocation",
```

`PYTHONIOENCODING=utf-8` works around it, which is how the help was eventually read.

⚠️ **This is the same class the project already gates against, one layer out.**
`tools/check_batteries_pin_encoding.py` and the consumer's `check_tools_pin_stdout.py` both exist
because *"a gate dies exactly when it has something to say"*. Here it is not a gate but argparse,
and it dies whenever the user asks what the flags are. The existing gates cannot see it because they
inspect scripts for a stdout pin; argparse does the writing.

⚠️ **The arrows are load-bearing prose in this codebase** (`plan → run-plan`, `epic → stories →
tasks`) and appear in comments and docstrings too, where they are harmless. Only strings that reach
a console need to be ASCII. Suggested fix: ASCII (`->`) in `help=` and `description=` strings
specifically, plus a `sys.stdout.reconfigure(encoding='utf-8', errors='replace')` in `main()` before
argparse can print — the pin the project already requires of every other printing script. A gate
would be: import the parser, format its help, and encode it as cp1252.

### CF-28 — plan mode invented a file tree, spent 4 rounds failing to anchor it, and discarded the good half

**Measured on `nt8-riskguard` 2026-08-19, `--mode plan --max-rounds 4` against a real defect
(`P0-171`).** Four real rounds, ~190s, `kimi-k2.7-code:cloud`, ending `MAX_ROUNDS_EXHAUSTED` with the
ticket written to `plan_rejected.json`. Every region it proposed names a file and a symbol that **do
not exist anywhere in the repo**:

| the plan's region | what the repo actually has |
|---|---|
| `src/Overtrading/DuplicateEntryRule.cs` | `addons/RiskGuardAddOn.cs` (the rule is inline in a 6400-line file) |
| anchor `Evaluate(` | `ExecuteOrderUpdate` |
| anchor `class Anchor` | `RecentEntryAnchor`, in `RiskGuardModels.cs` |
| anchor `IsReducing` | `IsPositionReducingOrder` |
| anchor `DateTime.UtcNow` | real, but in the wrong file |

There is no `src/` directory in this repo at all. Region validation caught all five and was right to,
but the run had already spent its budget: round 1 was **148.8s / 18154 out / 77312 thinking chars**,
and rounds 2-4 were 1008, 1270 and 1935 out — the shape of a model re-emitting the same invented
structure with cosmetic changes, because nothing in the feedback told it the *architecture* was wrong
rather than the *format*.

⚠️ **The defect description was behavioural on purpose.** It described observable behaviour, logs and
required properties, and deliberately named no files, because the point of the exercise was to test
`--mode test --path-isolated` — tests generated from a spec rather than from an implementation. Plan
mode responded by inventing an implementation to match the prose. **A plan step that has no grounding
in the actual tree will confabulate one**, confidently and consistently across four rounds.

⚠️ **THE SPEC IT WROTE IS GOOD, AND IT IS THROWN AWAY.** The rejected ticket's `spec` field
independently arrived at the correct fix — use the order's placement time rather than the guard's
observation time; add a staleness horizon so a replayed fill cannot be a duplicate; keep a set of
already-evaluated order ids so a replay cannot refresh an anchor; run the reducing-position and
copier exclusions first and unconditionally; prune anchors relative to the candidate. That last-but-one
item also solves a *different* open defect in the consumer repo (`P1-167`, one refusal per order per
rule) which the prose never mentioned. **The reasoning was reusable and only the coordinates were
wrong**, yet both are discarded together into a file named `plan_rejected.json`.

**Suggested fixes, roughly in value order:**

1. **Ground the planner in the tree.** Give it the repo's file list (or the profile's
   `source_globs` expansion), or let it call a search tool, before it proposes regions. A planner that
   cannot see the tree cannot anchor to it.
2. **Tell it WHICH failure it hit.** "Region R1 did not resolve: no such file `src/...`" is a
   different instruction from "your format was wrong", and the current feedback did not distinguish
   them — four rounds of the same answer is the evidence. Feed back the *nearest* real paths.
3. **Separate the spec verdict from the region verdict.** A ticket whose prose is sound and whose
   anchors are wrong is one search away from usable. Emitting `plan_partial.json` with the spec
   intact, or failing fast after round 1 on an unresolvable *file* (as opposed to an unresolvable
   anchor within a real file), would both have saved most of the run.
4. Consider failing fast: an anchor inside a real file may legitimately need a retry, but a **file
   that does not exist** will not start existing on round 2.

⚠️ **Consumer-side workaround**, recorded because it is not obvious: name the real files and symbols
in the `--defect` text. That reintroduces exactly the implementation coupling that
`--path-isolated` exists to avoid, so the two features are in tension — grounding the *planner*
without grounding the *test writer* is what resolves it.

### Implementation (CF-28 fix)

- **Layout grounding** (`plan_mode.py`): the defect path now gets `build_layout_context`
  (the real file tree listing), same as the feature path already had. The planner sees
  real paths before it proposes regions, stopping confabulation of non-existent directories.
- **`plan_partial.json`** (`plan_mode.py`): when regions fail extraction but the spec is
  sound, the ticket is saved to `plan_partial.json` with ALL region errors and an
  actionable note: "fix the anchors and re-run." The sound engineering is no longer
  thrown away into `plan_rejected.json`. Each region is extracted individually so all
  errors are collected, not just the first.
- **Fail fast on non-existent files**: uses `FileNotFoundRegionError` (a structured
  exception subclass in `regions.py`), not a substring match on the error message. A
  file that does not exist will not appear on round 2; the loop breaks after round 1
  with `PLAN_REJECTED_FILE_NOT_FOUND`. Bad anchors in real files still retry.
- Acceptance tests in `tests/acceptance/test_cf28_plan_mode_grounding.py` (6 tests):
  layout context in defect path, fail fast on non-existent file, retry on bad anchor
  in real file, plan_partial.json saved with spec and errors, all region errors
  collected, actionable note present.

### CF-29 — the plan prompt omits the `kind` field, so the planner cannot obey the extractor's own advice

**Measured on `nt8-riskguard` 2026-08-19, `--mode plan --max-rounds 4`, second attempt — this time
with the defect text naming every real file and symbol** (the CF-28 workaround; see CF-28 below). The plan came back
`MAX_ROUNDS_EXHAUSTED` again after 4 rounds, ~190s and ~19k output tokens.

This time the regions were **right**. Running the extractor by hand on the rejected ticket gives one
error, and it is entirely specific:

```
NoBlockError: line 2232 declares no brace-delimited body:
  'DateTime dupNow = stateModel.UtcNow();'.
  kind=decl expands from an opening brace to its match, and there is none here, so it
  would run on to the end of the enclosing block. Use kind=line to target this single line.
```

**Adding `"kind": "line"` to that one region — and changing nothing else — makes all three regions
extract cleanly:**

```
R1   addons/RiskGuardAddOn.cs    lines 1451-1460
R2   addons/RiskGuardAddOn.cs    lines 2232-2232
R3   addons/RiskGuardModels.cs   lines 143-143
```

So the plan was **one undocumented field away from usable**, and the run was discarded.

⚠️ **The planner could not have known.** `PLAN_SYSTEM` is 627 characters and states the region schema
as exactly this:

```
{"id": "REGION_ID", "file": "path/to/file.py", "anchor": "unique anchor string in the file"}
```

`kind` is never mentioned — not in the schema, not in prose. The extractor's remediation advice
(*"Use kind=line"*) **is** fed back verbatim (`f"Region extraction failed: {exc}. Fix the anchors and
re-emit."`), so the model was told the answer three times and could not act on it, because as far as
its instructions go `kind` is not a legal key. It did the only thing left: swapped anchors. Rounds 2,
3 and 4 returned 1180, 1583 and 1309 tokens, oscillating between `dupNow = stateModel.UtcNow()` and
the ambiguous `dupWindowMs`, never once emitting `kind`.

**This also explains the silence.** Neither the `regions: N resolved OK` line nor the `[panel] ...`
line ever printed, because the `RegionError` branch does `continue` with **no print at all**. From the
console, four rounds of region failure and four rounds of panel rejection look the same: a list of
round timings followed by `MAX_ROUNDS_EXHAUSTED`. There is no `r*_arbiter.txt` and no reviewer
artifact either, so nothing on disk says the panel was never reached.

**Suggested fixes, in value order:**

1. **Document `kind` in `PLAN_SYSTEM`**, with the one-line rule: `decl` for anything with a
   brace-delimited body, `line` for a bare statement. One sentence recovers this entire class of run.
2. **Print the region failure.** `print(f"           plan rejected: {exc}")` in the `RegionError`
   branch, matching what the feature path already does. Right now the single most common rejection
   reason is invisible.
3. ⚠️ **Warn on a degenerate region.** With `kind=line` added, `R2` resolves to `2232-2232` — ONE
   line. That satisfies the extractor and hands the implementer a single statement to edit when the
   change needs the surrounding block, and a one-line region "prints OK" exactly like a good one.
   The consumer's own `CLAUDE.md` already carries a warning to read the line ranges by hand for this
   reason. A span of 1 (or below some floor) deserves a printed caution, not silent success.
4. Consider **suggesting the fix mechanically**: the extractor already knows the anchor matched a
   single non-brace line, so it can emit the corrected region rather than describing it.

⚠️ **What was lost is the expensive part.** Both rejected plans contained sound engineering: the
second independently proposed a bounded `ReplaySuppressionUntilUtc` set in `OnConnectionStatusUpdate`,
a `DuplicateEntryEvaluatedOrderIds` set to make refusals once-per-order, keeping both fields OUT of
the persisted shape so a recompile resets them, and a fail-loud assertion that the suppression stamp
can never sit further ahead than the window. It respected all four constraints the defect text
imposed, including "do not key on a broker-owned value". **The design was right and the coordinates
were one field short**, and `plan_rejected.json` is where both went.

### CF-30 — `--mode test --path-isolated` picked the right SCENARIOS and reinvented every SEAM, because isolation hid the test harness too

**Measured on `nt8-riskguard` 2026-08-19.** First real use of test mode. 321s, 37932 output tokens,
152846 thinking characters, 16467 chars written to `tests/P0171GeneratedTests.cs`. Then:

```
[test-first] WARNING: cannot verify the generated tests: the runner produced no
             parseable result summary at baseline
```

**The generated tests do not compile.** 12 errors, and they fall into two groups.

**Group 1 — it wrote a standalone program into a suite that is one program.**

```
RiskGuardAddOnTests.cs(24,18):  CS0101 namespace already contains a definition for 'Program'
RiskGuardAddOnTests.cs(68,29):  CS0111 'Program' already defines a member called 'Run'
RiskGuardAddOnTests.cs(73,29):  CS0111 ... 'RunNamed'
RiskGuardAddOnTests.cs(14715,29): CS0111 ... 'Assert'
RiskGuardAddOnTests.cs(117,28): CS0111 ... 'Main'
```

The generated file declares its own `class Program` with `Main`, `Run`, `RunNamed` and `Assert`.
That is correct for a language where each test file is independently runnable, and wrong here: this
consumer's suite is a single `Program` with one `Main` and a hand-maintained `Run(TestName)` registry.
The profile declares `test_sources=("tests/*Tests.cs",)`, which says WHERE tests live and nothing
about their SHAPE.

**Group 2 — it invented a seam that does not exist.**

```
P0171GeneratedTests.cs(279,37): CS0115 'TestableAddOn.LogEvent(string,string,string)':
                                no suitable method found to override
```

It subclassed the addon to intercept logging, and `LogEvent` is not virtual. It also reached for
reflection to reach private state:

```csharp
SetField(this, "_config", config);
SetField(this, "_accountStates", states);
```

⚠️ **Every one of those seams already exists and is public to tests**: `SetConfigForTest`,
`SetAccountStateForTest`, and a static `LogEventMessageObserver` hook that exists precisely so a test
can capture log output. The repo also has a purpose-built harness for this very method
(`P1160Setup` / `P1160Order` / `P1160Send`) that drives the real `ExecuteOrderUpdate`. The model used
none of them, because `--path-isolated` withheld the implementation **and, incidentally, the test
harness with it**.

⚠️ **THE SCENARIOS, THOUGH, ARE RIGHT — and that is the part worth paying for.** All six map exactly
onto the spec's requirements, with nothing missing and nothing invented:

| generated test | spec requirement |
|---|---|
| `TestReplayedOrderNotRefusedAsDuplicate` | 1 |
| `TestOneOrderDrawsExactlyOneRefusal` | 2 |
| `TestGenuineDuplicateStillRefused` | 3 |
| `TestOrderSeenOnlyAsFilledIsStillEvaluated` | 4 |
| `TestReducingOrderNeverRefused` | 5 |
| `TestReplaySuppressionExpiresOnItsOwn` | the bounded-suppression constraint |

So path isolation **worked for its stated purpose**: the tests are derived from the spec, they are not
tautological against an implementation, and they include the two negative controls that stop the fix
over-reaching. The scenarios were kept and ported by hand into the existing harness; only the
scaffolding was thrown away.

**The finding is therefore narrow and fixable: isolate the test writer from the CODE UNDER TEST, not
from the TEST HARNESS.** Suggested, in value order:

1. Give test mode the existing test file's **preamble and one representative test** (or a profile
   field like `test_style_exemplar`) as context that is always supplied, even under
   `--path-isolated`. Idioms are not the implementation.
2. Add a profile notion of test-file **shape** — standalone vs. append-to-existing-class. For an
   append-style suite, generating into a new file cannot work without also emitting the registration,
   and this profile's suite needs a `Run(TestName);` line added to `Main`.
3. **Compile the generated tests before declaring them written.** The run reports
   `tests written to: ...` and exits **0**, and the only signal that they are unusable is a WARNING
   line about an unparseable baseline. The tests exist and are not evidence; that should be a
   non-zero exit.
4. ⚠️ The baseline message *"the runner produced no parseable result summary"* blames the runner for
   what is a compile failure in the just-generated file. Surface the `error CS...` lines — the same
   complaint as `CF-22`'s *"baseline test run produced no parseable result summary"*, which also read
   as the consumer's fault.


### CF-31 — `--list` correctly warns that the model will GUESS a symbol, and then the run lets it guess: six rounds burned on a property path

**Measured on `nt8-riskguard` 2026-08-19**, patch mode on ticket `P0171`, immediately after `CF-30`.
Same ticket, same three regions, six acceptance tests red at baseline. The run is the good case in
every other respect — test-first gate satisfied, baseline `3341 passed, 6 failed`, `6 expected
failure(s)`, static and compile gates working — and it still ends `ARBITER_NEVER_RAN` with
`applied=False`.

**Validation said exactly what would go wrong.** `--list`, before a single model call:

```
WARN 'DuplicateEntryWindowMs' named in spec but not found in <region file>
     -- model will guess; add its declaration to a read-only region
```

It guessed. The spec asked for a suppression stamp of *"that account's `UtcNow()` plus
`DuplicateEntryWindowMs` milliseconds"*. The value lives at `_config.Overtrading.DuplicateEntryWindowMs`
and is read in plain sight **100 lines below the edited region, in the same file** — but that read is
outside every region the implementer was shown, so the model could see the *name* and not the *path*.

What it wrote instead of `_config.Overtrading.DuplicateEntryWindowMs`:

```csharp
int ResolveDuplicateEntryWindowMs()          // + ReadMs + FindAnyDuplicateWindowMs + CoerceToMilliseconds
{
    ...
    foreach (PropertyInfo prop in type.GetProperties(BindingFlags.Public | BindingFlags.NonPublic
                                                     | BindingFlags.Instance))
        if (prop.Name.IndexOf("Duplicate", StringComparison.OrdinalIgnoreCase) >= 0
            && prop.Name.IndexOf("Window",    StringComparison.OrdinalIgnoreCase) >= 0)
            ...
}
```

**90 lines of reflection**, four nested local functions, fuzzy name matching on `"Duplicate"` +
`"Window"`, and a `CoerceToMilliseconds` handling `TimeSpan`, `int`, `double` and `string` — a
config-discovery layer, invented, in a file whose config object is a plain typed POCO.

**And that is why the run stalled.** The scan looks for the property on `RiskConfig` and does not
recurse into `RiskConfig.Overtrading`, where it lives. It returned `0`, the arming guard
`if (dupWindowMs > 0)` never passed, and **the suppression was never armed on any path**. Rounds 5
and 6 both compiled and both reported the identical `4 acceptance test(s) still failing; 3343 passed,
4 failed, 2 expected failure(s) now green` — the two that went green are precisely the two that do
not need the suppression. Four rounds before that were spent on compile errors in the reflection.

The loop had already computed the diagnosis and printed it. Nothing consumed it — the same shape as
`CF-18`, one stage earlier.

**Suggested fixes, cheapest first.**

1. ✅ **Make the `--list` warning binding by default.** A ticket whose spec names a symbol that
   appears in no region is not ready to run; refusing costs nothing and the wording is already
   right. `--allow-unresolved-symbols` for the deliberate case.
2. ✅ **Resolve it instead of refusing.** The loop can already find the symbol — that is how it
   knows to warn. Attach the declaration site, or the single line that reads it, as read-only
   context. One line of `_config.Overtrading.DuplicateEntryWindowMs` would have replaced 90 lines of
   reflection.
3. **Have the static gate reject reflection in an implementation patch** unless the ticket asks for
   it. `[static] ok - 3 block(s) well-formed` passed all six rounds; well-formed is a much weaker
   claim than the label suggests, and `System.Reflection` appearing in a patch for a typed-field read
   is a strong smell that the model is guessing at a shape it cannot see.
4. **Report which acceptance tests are still red, not just how many.** Six rounds said
   `4 acceptance test(s) still failing` and never once named them. The four share one cause and the
   two that pass share the complement — that is visible at a glance from the names and invisible
   from the count, and the implementer is handed the same undifferentiated number every round.

### Implementation (CF-31 fix)

- Added `readonly` as a region `op` in `src/agent_loop/regions.py`. Readonly regions resolve like
  replace regions but are skipped by `apply()` and are allowed to overlap editable regions.
- `cli.py` `_list()` now:
  - refuses tickets that name a symbol not found in any region's file (unless
    `--allow-unresolved-symbols` is passed);
  - auto-attaches a `readonly` region for symbols that exist in the file but outside every editable
    region, so the implementer can see the type/path without being able to edit it.
- Added `--allow-unresolved-symbols` CLI flag for the deliberate opt-out case.
- Patch mode runs the same validation before spending model calls, so a ticket that would have
  burned rounds is now rejected at the gate.
- Acceptance tests in `tests/acceptance/test_list_symbol_warning_reads_code_not_prose.py` cover:
  auto-attach when the symbol exists; refusal when it does not; and opt-out with
  `--allow-unresolved-symbols`.

**What the run got right, since this reads as a bad outcome and mostly is not.** The two-mechanism
structure was correct and matched the spec, the placement of the evaluated-order record was correct,
the model fields were right and correctly marked runtime-only. Hand-arbitrating meant deleting the
reflection, substituting one property path, and re-expressing the region-2 change as three added
lines rather than the loop's full re-indent. Suite went `3352 / 0`, and 15 mutants against the
result all died. **The reasoning was sound; one unresolved symbol wasted six rounds.**

⚠️ **Unrelated but from the same patch: the implementer rewrote comments it was not asked to touch.**
Three `⚠️` markers became `WARNING:` and an unrelated comment was reflowed. In this repo mutation
batteries anchor on exact source strings, several of them comments, so a gratuitous comment edit can
silently turn a battery into `[SKIP]` — which scores as a **survivor**, not a pass. None of these
three matched an anchor, checked. Worth a static rule: an implementation patch that changes only
comment text outside the described change is noise at best.

### CF-32 — an unresolved-symbol guess produced a patch that passed EVERY gate green; only hand-verification against the real source caught it

**Measured on `nt8-riskguard` 2026-08-21**, patch mode, ticket `P2-132` (report a sizing rule's
`currentValue` from the account's max position). Same family as `CF-31` and it reproduced `CF-31`'s
two behaviours exactly — the model GUESSED an unresolved symbol, and it gratuitously rewrote a `⚠️`
comment to `WARNING:` in unrelated code (a second independent instance of the comment-edit noise
`CF-31` already flags). **But the new and more dangerous part is the opposite of `CF-31`'s outcome.**

In `CF-31` the guess stalled LOUDLY: the invented code returned `0`, the arming guard never passed,
and the acceptance tests **stayed red** for six rounds — the failure was visible. Here the guess
shipped a **GREEN** candidate: `[static] ok`, `[compile] ok`, `[test] ok — 3513 passed, 0 failed, all
2 acceptance test(s) green`, `[lock-scope] ok`, every round. The spec said derive the value from
`state.Positions` (the cached collection the `MAX_SIZE_BREACH` enforcer iterates); with
`--allow-unresolved-symbols` passed, the model instead read the LIVE `account.Positions` — a
different source the enforcer does not use — with a plausible comment ("the cached state can lag").
It was **functionally wrong** (the report would diverge from the enforcer, which is the whole defect)
and **no gate could tell**, because the two acceptance tests set the snapshot field directly and
never exercised the population path. Only reading the enforcer by hand (it iterates
`stateModel.Positions`, not `account.Positions`) caught it.

**Why this matters beyond `CF-31`.** `CF-31`'s fixes assume the guess fails in a way a gate catches —
it recommends `--allow-unresolved-symbols` "for the deliberate case", implying the deliberate case is
safe. It is not always: when the guessed symbol decides a **data source** and the acceptance tests do
not discriminate between sources, the escape hatch converts a safe REFUSE into a **silent, green,
wrong** patch. The run's `NOT_CONVERGING` verdict (panel churned 1→5→2 non-overlapping blocking
findings, never auto-applied) is what forced a human read — but that was luck, not a safeguard: had
the panel converged, the wrong-source patch would have auto-applied, green.

**Suggested fixes, cheapest first.**

1. **When `--allow-unresolved-symbols` is used, name the guessed symbols in the run output AND in the
   final report**, so the reviewer knows exactly which facts the model invented. The `--list` warning
   is pre-run and easy to forget by the time a green patch appears; the guess should be surfaced
   AGAIN on the candidate.
2. **Warn when an editable region carries no acceptance-test coverage.** The population region
   (`GetAccountSnapshots`) was edited by the patch but hit by no acceptance test — so the gate that
   was supposed to hold it (test-first) was structurally blind to it. A region the tests never enter
   is a region the loop cannot verify, and it should say so.
3. **Do not describe the deliberate opt-out as safe.** `CF-31`'s framing ("refusing costs nothing;
   `--allow-unresolved-symbols` for the deliberate case") is right that refusing is cheap, but the
   opt-out should carry the warning that a guessed symbol the tests do not discriminate ships
   unverified.

**What the loop got right.** The implementation logic was otherwise correct and minimal, the
test-first/compile/lock-scope gates all functioned, and `NOT_CONVERGING` correctly withheld
auto-apply so a human saw the patch. Hand-arbitration kept the correct hunk (the evaluator change),
substituted the right source (`state.Positions`), and dropped the comment-rewrite hunk; suite `3513
/ 0`. The reasoning was sound; one unresolved symbol on a data-source decision made a green patch
wrong.

### Implementation (CF-32 fix)

- **`_scan_unresolved_symbols`** (`cli.py`): extracted from `_list` into a reusable
  function that returns guessed symbols as data: ``[{symbol, file, status}]``.
- **`_has_unresolved_candidates`** (`cli.py`): quick check whether the spec contains
  code-like tokens. Gates the scan itself — most tickets have no such tokens, so the
  scan and the deepcopy it needs are skipped entirely, avoiding per-run latency.
- **Scan skipped on `--resume-raw`** to avoid duplicate readonly regions from a prior
  `--list` run.
- **Guessed symbols surfaced in `run_ticket`** (`loop.py`): stored in
  `result["guessed_symbols"]`, printed in the final report with an explicit
  "ships UNVERIFIED" warning when `--allow-unresolved-symbols` is set. The refusal
  path (when `--allow-unresolved-symbols` is NOT set) also records the guessed symbols
  and writes the ledger for audit.
- **O(regions²) inner loop replaced** with a pre-computed region-text cache: each
  region is extracted once, and all region texts for a file are joined for the
  "in any region" check. Files where all regions failed extraction are skipped.
- **Fake coverage warning removed**: the basename string match against test source text
  was not coverage analysis and would produce false positives/negatives. The
  guessed-symbol surfacing is the real fix; the operator reviews the patch knowing
  exactly which facts the model invented.
- **Help text** (`cli.py`): `--allow-unresolved-symbols` warns that a guessed symbol
  the tests do not discriminate ships unverified.
- Acceptance tests in `tests/acceptance/test_cf32_unresolved_symbols_surface.py`
  (6 tests): scan returns refused symbols, scan returns empty for clean ticket,
  has_candidates true/false, help text warns about unverified ships.

### Review verification

The fixes were reviewed by the agent loop's own review mode (`--mode review`) across
3 rounds with a two-reviewer panel (`glm-5.2:cloud`, `deepseek-v4-flash:cloud`) and
arbiter (`minimax-m3:cloud`). Round 1 found 6 upheld findings (difflib unequal-count
bug, doc-keyword over-matching, resume-raw duplication, O(n²) extraction, string
coupling for fail-fast, assertion-free test). Round 2 found 1 upheld finding
(doc exemption too permissive). Round 3 found **0 findings** — clean review.

Suite: **807 passed, 36 skipped**. Selftest: **13/13 passed**.

---

### CF-33 (new, HIGH) — `--mode review` with `--reviewers` omitted ran a ZERO-member panel and called it a valid review

**Observed**, reviewing `v2.3.0-surface-v3..HEAD` (10 files, 27,808 chars of diff) in the
tradingview-mcp consumer:

```
  reviewing 10 file(s), 27,808 chars of diff

  findings (0) -> (no reviewer output)
  prompt sent -> logs\agent_loop\review-.../review_prompt.txt
  artifacts -> logs\agent_loop\review-...
  REVIEW MODE IS ADVISORY. It changes nothing; read the findings and decide.
```

Runtime: **0.2 seconds**. `result.json`: `panel_verdict: ""`, `panel_valid: true`,
`findings_total: 0`, `arbiter: "(not run)"`. Re-running WITH `--reviewers` named explicitly
produced 19 findings and a REVISE-from-both panel in ~25 seconds. The first invocation
never reached a model.

**Root cause, one line**: `main()` resolves a default panel from the registry
(`reviewers_str = args.reviewers or ",".join(...registry.get_all("reviewer")...)`), validates
its family policy, and then hands it to nobody — `_review(args, profile)` does not take the
resolved panel (every other mode does) and re-parses the RAW `--reviewers` string.
Empty flag -> `[]` -> `review_panel([])` -> `valid = all(v.counted for v in votes) and
len(votes) == len(reviewers)` -> `all([]) is True` and `0 == 0` -> **vacuously valid**.

**What it cost**: an operator can believe their change was adversarially reviewed when zero
opinions were collected. In this session the "clean" first run was read as a pass before the
0.2s runtime raised the question; a second reviewer-free run tomorrow would pass the same way.
The failure is indistinguishable from a clean review in the artifacts — empty verdict, zero
findings, `panel_valid: true`.

**Why it happened**: review mode is the only mode that re-parses the raw flag. `_plan`,
`_developer`, and `_run_plan` all take `reviewers`/`arbiter` parameters and receive what
`main()` resolved. `_review` predates the registry-default resolution (the family-policy
warning block above it was added later and warns about the panel the review will never see —
the one-member warning even knows to print `(none)` for the empty case the review then
silently accepts).

**Fixes applied** (all three layers, so the root cause and the structural hazard close):

1. `cli._review(args, profile, reviewers, arbiter)` — review mode now receives the RESOLVED
   panel and arbiter, same as every other mode. `--reviewers` still overrides (the override
   is applied in `main()` before the hand-off, where the family checks run).
2. `review_mode.run_review` raises `ReviewError` on an empty reviewer list — protects
   programmatic callers, not just the CLI path. Fires before diff collection.
3. `loop.review_panel` raises `ValueError` on an empty reviewer list — the structural
   half. A panel of zero opinions is not a review, and the validity rule can no longer be
   vacuously true for it. One- and two-member panels remain legal (the CLI warns about the
   one-member case; it does not refuse).
4. `result.json` records `"reviewers": [...]` — the actual panel is auditable from the
   artifacts instead of being reverse-engineered from vote rows (or invisible, as before).

**Accepted tests** in `tests/acceptance/test_cf33_empty_reviewer_panel.py` (5): the registry
pre-condition (multi-member panel), the `run_review` guard, the `review_panel` structural
guard, `result.json` panel recording, and a guard-does-not-overcorrect check (1- and 2-member
panels still valid).

**Note for reviewers of this fix**: the 0.2s no-op review is the failure mode to hold onto —
not a wrong verdict, but the ABSENCE of one wearing a verdict's clothes. The lesson generalizes:
every `all()`-over-a-collection-validity rule needs an `if not collection: refuse` sibling.

**Suite: 812 passed, 36 skipped** (was 807; +5 from CF-33). **Selftest: 13/13 passed.**

---

### CF-34 (new, HIGH, FIXED) — a failed submodule update left the worktree dirty, so EVERY ticket was refused before a model call

**Observed** 2026-09-26, tvDownloadOHLC, first ticket on a new Rust profile:

```
  [worktree] WARNING: submodule update failed; submodule-dependent tests may be dark
  REFUSED: the test suite does not produce a parseable result summary at baseline. ...
```

The console line is cut at 200 chars; `result.json` carries the real cause: *"refusing to
capture a test baseline from a dirty worktree"*. `third_party/PineTS` pins a commit its
remote no longer serves, so `submodule update --init --recursive` aborts partway. Every
submodule already cloned but not yet checked out is left at its remote's HEAD, not the
gitlink, and `git status --porcelain` lists each as ` M`. The warning described a
recoverable state (submodules absent); the tree was not in that state, and the dirty-tree
guard refused it. Every ticket in the repo, on every profile, failed the same way.

**Fix** (`workspace.open_workspace`): on failure, `git submodule deinit --all --force`,
which returns the tree to exactly what `worktree add` produced. The same `except` now also
catches `subprocess.TimeoutExpired`: the 120s cap used to escape as an uncaught exception.

**Accepted test**: `tests/acceptance/test_cf34_failed_submodule_update_leaves_clean_tree.py`.
Red without the fix (`['zbad', 'zlater'] == []`), green with it. Two traps met while
writing it: a submodule pinned AT its remote's HEAD looks clean after the failure (a real
pin is always behind), and a `git add -A` after `update-index --cacheinfo` silently
re-stages the checked-out HEAD over the forged gitlink, so the update never fails.

**Suite: 813 passed, 36 skipped. Selftest: 13/13.**

### CF-35 (new, MEDIUM, OPEN) — Rust lifetimes are read as char literals, so a region's braces are miscounted

`regions.strip_code` treats `'` as a char-literal quote and scans for a closing quote
to the end of the line. `fn key(&self) -> &'static str {` therefore swallows its own `{`,
the brace count goes negative inside an `impl` block, and the region ends early. Worked
around in the consumer by writing lifetime-free signatures (`-> &str`, `Vec<u8>` instead
of a borrowed slice). Fix: for `language == "rust"`, treat `'ident` not followed by a
closing `'` as a lifetime, not a literal.

### CF-36 (new, MEDIUM, OPEN) — no cargo parser, and the unresolved-symbol check is per file

1. `parse_tests` knows only the NT8 runner and pytest, and `_DIAG` knows only MSBuild's
   `error CS1234`. On cargo output a test run reads as "did not run", and a Rust compile
   error reaches the model as the raw 4000-char tail. The consumer ships
   `scripts/agent_loop_config/cargo_test_adapter.py` to translate. The consumer also found
   that cargo needs `--no-fail-fast`: without it the first red test binary silently skips
   every later one, and the RESULTS count shrinks with no failure line to show it.
2. The pre-flight symbol check requires every symbol named in the spec to resolve in
   EVERY region file. It REFUSES `Some`, `Ok` and `Err` (the prelude), and it refuses a
   constant because it is not declared in an unrelated file. A multi-file ticket cannot
   pass it, so `--allow-unresolved-symbols` becomes mandatory, and that turns the check off
   for the symbols it could genuinely catch.


### CF-37 (new, HIGH, FIXED) — the default second reviewer was retired, and a quorum of one ships as APPROVE_PARTIAL

Measured in tvDownloadOHLC on 2026-09-26. Ollama retired `deepseek-v4-flash:0731-cloud`
on 2026-09-25 and now returns `HTTP 410 Gone`. That model is the package default
`reviewer.extra_members` (`config.py`). The bare `deepseek-v4-flash:cloud` tag now
resolves to the same retired model, so the drift-avoiding pin and its fallback died
together. Every panel since then has one voter.

The loop handles this as it should: the member is dropped as UNREACHABLE, the run ends
`APPROVE_PARTIAL`, and it awaits human sign-off. But the finding is printed once, mid-log.
Nothing tells the operator that the panel will be one model on *every* run until the
config changes. Consumer workaround: override `roles.reviewer.extra_members` with
`deepseek-v4-pro:cloud`, which is a different family from glm; its BAD profile mark is
as arbiter.

Fixed 2026-09-26: the reviewer panel is now `glm-5.3-flash:cloud` +
`deepseek-v4.1-flash:cloud`, still two families. The role runs with **think=True at
64000**, because `glm-5.3-flash` with think off leaked 349 tokens of reasoning into
`content` on a bare-JSON probe; with think on, its content is clean. The retired model
keeps its catalogue entry, marked `RETIRED`. `test_cf37_no_retired_model_is_configured.py`
fails if any role member, extras included, is retired, and it has a negative control
against a lost marker. It was red before the fix.

Still open: a startup probe that refuses a member answering 410. Retirement is still
discovered by the catalogue being edited, not by the loop asking. Both new models are
UNMEASURED on the reviewer bench.

### CF-38 (new, HIGH, FIXED) — the static gate brace-checked READONLY blocks

Measured in tvDownloadOHLC on 2026-09-26 (ticket T2 of `tickets_spine_p1`). The symbol scan
auto-attached a one-line read-only context region:
`if b.minute >= IB_START_MIN && b.minute < IB_END_MIN {`. The implementer echoed it back
verbatim, and `check_static` counted 1 open brace and 0 closes. That failed rounds 1 and 2
and would have failed every later round. The model could not clear it, and the only
console output was `[static] FAIL - 1 problem(s)`, with the detail not logged.
`apply_blocks` already skips readonly blocks (CF-31), so the gate was holding a region the
patch never writes to a shape that region never had.

Fix: `check_static` skips `op == "readonly"`. Test:
`tests/acceptance/test_cf38_static_gate_skips_readonly_context.py`. It was red before the
fix: 2 failed, and the negative control (an editable block is still checked) passed. The
suite is at 816 passed, 36 skipped, and selftest at 13/13.

Still open: the static problem detail is not printed to the console or written to the run
directory. It had to be reproduced by hand.

### CF-39 (new, HIGH, FIXED) — `think` was per role, so the CF-37 panel had one voter again

Measured in tvDownloadOHLC on 2026-09-26 (ticket T3 of `tickets_spine_p1`). CF-37 seated
glm-5.3-flash + deepseek-v4.1-flash with `think=True`, because glm-5.3-flash leaks its
reasoning into `content` when thinking is off. That setting reached both members. On its
first real review, deepseek-v4.1-flash spent the whole 64000-token budget on 227,501
chars of reasoning (`eval_count=64000`, `done_reason=length`) and returned empty content.
The panel ended `APPROVE_PARTIAL` on quorum 1/2, the outcome CF-37 existed to end. The
strict-JSON probe that CF-37 relied on had shown deepseek clean with thinking on (53 chars
of reasoning). A probe is not a review.

Fix: `RoleSettings.no_think_members`, read through `think_for(model)` by both
`review_panel` and `registry_from_config`. The shipped reviewer runs deepseek-v4.1-flash
with thinking off; glm-5.3-flash keeps it on. An explicit `think=` argument still wins for
every member. A config file that names a non-member is refused. An inherited entry follows
a consumer that reseats the panel, so swapping the second reviewer does not trip over a
name the consumer never wrote.

Test: `tests/acceptance/test_cf39_reviewer_think_is_per_member.py` (8 tests, including the
negative control that without the override every member inherits the role). Suite: 831
passed, 40 skipped. Selftest: 13/13.

Still UNMEASURED: deepseek-v4.1-flash with thinking off on a real review.

Measured since (CF-40's run, P1 T4): with thinking off, deepseek-v4.1-flash returned a
parseable REVISE with 14 findings. So the panel had two voters on a real review.

### CF-40 (new, HIGH, FIXED) — the default arbiter was retired, and CF-37's guard could not see it

Measured in tvDownloadOHLC on 2026-09-26 (P1 T4). Round 1 passed every gate, the panel
split REVISE/APPROVE, and the arbiter call failed on its first attempt:
`HTTP 410 {"error":"qwen3.5:397b was retired at 2026-09-25"}`. The run ended
`ARBITER_DEADLOCK` and applied nothing. Ollama retired it on the same day as
deepseek-v4-flash (CF-37).

`test_cf37_no_retired_model_is_configured.py` already checked every role, the arbiter
included, and stayed green. It tests for the word `RETIRED` in a catalogue note, and
nobody had written that word into qwen3.5's note. **Retirement is only as visible as the
note that records it.** When CF-37 fixed one retired model, it fixed that instance; the
class, a member that answers 410, is still found at call time, on a real run.

A live probe of every configured member (implementer, both reviewers, arbiter,
compactor) found qwen3.5 the only dead one. The arbiter bench was re-run on the live
candidates (`results_sweep_2026-09-26.json`):

| model | shipped prompt | inverted prompt | false positives |
|---|---|---|---|
| kimi-k3 | 5.0/5 (3 reps, 4-5s) | 5.0/5 (3 reps, 3-5s) | 0 |
| minimax-m3 | 1.3/5 | 4.3/5 | 0 |

Fix: the arbiter is `kimi-k3:cloud`. Its catalogue entry now lists `arbiter` as suited,
and qwen3.5's note is marked `RETIRED`. The CF-37 negative control is parametrised over
both retired models. The pinned-winner test and the example config are updated. kimi-k3
shares a family with the kimi-k2.7-code implementer; the only rule enforced is
arbiter != reviewer family.

Suite: 832 passed, 40 skipped.

**The class, closed (same day, `3f8a783`):** `probe.py`. Before any round, `cli.main()`
sends one 16-token call to every distinct model the run will call: implementer, every
reviewer, arbiter and compactor. **HTTP 410 refuses the run** (exit 2) and names each
model with its role. Any other failure only **warns**: the loop already degrades
correctly when a member cannot vote, so blocking on a flaky endpoint would make the probe
itself the outage. Retirement is read from the status code, never from the provider's
prose. `--list` and report mode make no probe calls, and `--no-probe` or
`AGENT_LOOP_NO_PROBE=1` skips it. `tests/conftest.py` sets that env var for the whole
suite, because more than a dozen tests drive `main()` and none may reach a real provider.
The budget and timeout are `provider.probe_max_tokens` and `provider.probe_timeout_secs`
in config.py (the budget-literal gate refused them inline).

Live, against Ollama: the configured set plus qwen3.5 was refused in 0.8s, naming
`qwen3.5:cloud (arbiter)`; the current set passed in 0.9s.

Test: `tests/acceptance/test_cf40_startup_probe.py` (8 tests, driving the real `main()`
with `chat` stubbed at the probe's import site):
- a retired arbiter, and a retired EXTRA reviewer, each refuse the run with no ticket run;
- the negative control: an unreachable model warns and the run proceeds;
- each distinct model is probed exactly once;
- all three opt-outs spend no call;
- a 404 whose prose says "retired" is not read as retirement.

Two mutants, both killed: skipping the refusal (2 red), and `is_retired` always false
(3 red). Suite: 840 passed, 40 skipped. Selftest: 13/13 (it calls `run_ticket`
directly, so it does not pass through the probe).

### CF-41 (new, HIGH, FIXED) — a Rust lifetime read as an unterminated char literal

Measured in tvDownloadOHLC 2026-09-26, T6 of `tickets_spine_p2`. The implementer wrote a
valid `impl RiskBudget` containing `fn reason_str(r: &DoneReason) -> &'static str {`. The
static gate refused it as unbalanced braces (50 open vs 51 close). All four rounds failed
the same way on a different body each time, and the ticket ended `ARBITER_NEVER_RAN`, so
no reviewer ever saw the code. The block was balanced; the gate was not.

Every quote reader in `regions.py` (`strip_code`, `strip_code_default` and
`_mask_block_comments`) treated `'` as opening a literal that runs to the NEXT `'`. With
no second quote on the line, it swallowed the rest, the `{` included.

- In Python and JS `'` does delimit a string.
- In Rust, C#, Go, Java and C it delimits one character, in a fixed shape.
- In Rust it is also a lifetime or a loop label.

The same reader sets region BOUNDARIES (`find_region`, `extract_named_block`) and masks
block comments. So a Rust region with a lifetime in it could end in the wrong place, and a
`/*` after a lifetime went unseen. This was not only the gate's defect.

**Fix.** All three readers now call one helper, `regions.literal_end`.
- `Profile.char_quote()` decides the rule. It is derived from `language` through
  `CHAR_QUOTE_LANGUAGES` (rust, csharp, go, java, c, cpp), and `single_quote_is_char`
  overrides it.
- For those languages, `'` opens a literal only in the shape `'x'` or `'\...'`; anything
  else is code.
- With no profile, the string reading is kept.

Test: `tests/acceptance/test_cf41_rust_lifetimes_are_not_char_literals.py` (15 tests):
- lifetimes and labels leave the brace counted;
- the negative controls: `'{'`, `'\''`, `'\u{7B}'` and `b'{'` are still blanked, C# reads
  as before, and a Python `'...'` string is still a string;
- the override wins over the language;
- the measured block passes the static gate, and a truly unbalanced one is still refused;
- a region ends where its block ends;
- a lifetime does not hide a block comment.

5 of the 15 are red on the old code, including the region-boundary test. Mutants: the
char rule disabled (5 red) and the one-char shape disabled (4 red); both killed. Suite:
855 passed, 40 skipped. Selftest: 13/13.

### CF-42 (new, HIGH, FIXED) — every ticket inherited every other ticket's settled decisions

**Measured** in tvDownloadOHLC on 2026-09-26. Ticket T7 (a Rust exit-policy state machine,
profile `rust-spine`, whose `settled` is empty) was reviewed and arbitrated under five
"already-settled decisions" about C# alias mapping and `PerTickerMatrix`. An unrelated
copier ticket, CM2, had persisted them months earlier.

`load_settled` read the whole `settled_decisions.jsonl`. `inject_settled` passed the most
recent 20 into every review and arbiter prompt in the repo, whatever the ticket. "Restates
a settled decision" is REJECT criterion #4. So a foreign settlement that happened to match
a real finding would have silenced it, and the ruling would cite a decision nobody made
for that code. On T7 none matched, which is luck, not design.

Ticket ids do not separate tickets either. They are unique per tickets FILE, and this repo
has reused `T7`.

**Fix.**
- `save_settled` records the profile.
- `load_settled(repo, ticket_id, profile)` returns only that ticket's settlements under that
  profile. An entry from before profiles were recorded matches on the ticket alone.
- `inject_settled` now requires the ticket, and the loop, plan mode and replay pass it with
  the profile. With no ticket, `load_settled` still returns the whole store, but only for
  audit.

Test: `tests/acceptance/test_cf42_settled_decisions_are_scoped_to_their_ticket.py` (6 tests):
- a foreign ticket's settlement is not injected;
- the same ticket still gets its own, as the negative control;
- a colliding id under another profile is not injected;
- a legacy entry matches on its ticket only;
- profile-curated decisions are always kept;
- the unscoped read is still the whole store.

6 of 6 are red on the old code. Mutants: the ticket filter off, the profile filter off,
legacy entries excluded, and `inject_settled` unscoped. All four were killed. Suite: 861
passed, 40 skipped. Selftest: 13/13.

### CF-43 (new, HIGH, OPEN) — a self-retracted BLOCKER still carries its label, and makes SHIP unreachable

**Measured** in tvDownloadOHLC on 2026-09-26, on ticket T9 (profile `rust-spine`), round 4.
All gates were green. `deepseek-v4.1-flash` returned 39 findings, and 34 of them retract
themselves in their own text ("NOT a blocker on this path", "Correct. NOT a defect"):

| label   | findings | self-retracted |
|---------|---------:|---------------:|
| BLOCKER |        2 |              2 |
| MAJOR   |        8 |              8 |
| MINOR   |       29 |             24 |

The reviewer uses the finding list as a scratchpad for its trace, and the severity tag is
written before the conclusion is.

The arbiter handled the rulings correctly: it rejected all 34 under criterion 5, and its
settled note says so. The guard after it did not. `_blocker_indices` reads only
`Finding.severity`, so the two retracted BLOCKERs count as "BLOCKERs the arbiter
dismissed". On this round the arbiter recommended REVISE, over one real MAJOR, so the guard
never fired. But had that finding already been fixed, SHIP would have been converted to
`ESCALATE`. A reviewer that retracts in-line therefore makes SHIP structurally unreachable
on every round where it does so. That is the O28/O20 safety rule firing on noise. The
escalation then reads as "a human must confirm a rejected blocker" when no blocker was ever
claimed.

The noise has a second cost. The one finding that held (the unspecified fallback reason
string, a MAJOR) sat among 38 that did not. The run ended `MAX_ROUNDS_EXHAUSTED`, and the
fix was applied by hand.

**Proposed fix (not built).**
- Before parsing severity, drop any finding whose own body concludes that it is not a
  defect. That decision is the reviewer's own, so dropping it costs nothing. It needs a
  negative control: a finding that says "this is NOT a defect *in X*, but Y is" must survive.
- Alternatively, have the reviewer prompt demand the conclusion before the label.
- Either way, `_blocker_indices` should count only the BLOCKERs the arbiter rejected on a
  criterion other than 5 ("the finding refutes itself"), since that criterion is the one
  case where no human judgement is being overridden.

Evidence: `logs/agent_loop/T9/r4_review_deepseek-v4.1-flash_cloud.txt` and `r4_arbiter.txt`
in tvDownloadOHLC (the `logs/agent_loop/*` entries are gitignored there; the review and
arbiter texts are local only).

### CF-44 (new, HIGH, OPEN) — CF-31's auto-attached context can anchor on a blank line and kill the ticket

**Measured** in tvDownloadOHLC on 2026-09-26, on ticket T10 (profile `rust-spine`). The
spec said that some types "implement Serialize + Deserialize + Clone + PartialEq". The
run died before round 1 with
`RegionError: anchor not unique (607 hits): ''` in `crates/spine/src/risk.rs`. `--list` on
the same ticket had printed `OK` for every region.

**Mechanism** (`cli._attach_readonly_context`):
1. In `risk.rs`, `PartialEq` appears only on `#[derive(...)]` lines. The declaration
   heuristic skips every line starting with `#`, so it finds no candidate.
2. The fallback takes the first line that mentions the symbol (line 52). That line is not
   unique, because every derive repeats it, so the anchor becomes
   `lines[start - 3].rstrip()`, which is line 49.
3. Line 49 is blank. The empty anchor matches every line in the file, and `RegionError`
   aborts the whole ticket. The failure is in a *read-only context* region, which the
   ticket never asked for.

`--list` prints the `AUTO … attaching read-only context` line, but it never resolves the
region it would attach. It is the only pre-flight, and it passed a ticket that cannot run.

**Consequences.** Any capitalised word in a spec that the file uses only in an attribute, a
derive or a comment, placed 3 lines below a blank line, is fatal. The trigger is ordinary
prose.

**Proposed fix (not built).**
- The fallback anchor must be non-empty and occur exactly once in the file. Search the window
  for such a line; if none exists, attach nothing and say so. Auto-attaching context is
  best-effort, and a failure in it must never abort the ticket.
- `--list` must resolve the regions it auto-attaches, exactly as a run does.
- Negative control: a symbol with a unique declaration line still attaches, anchored on that
  line.

**Workaround used:** the ticket's spec was reworded to drop `Clone + PartialEq`
(tvDownloadOHLC `6bdbb7c9`).

### CF-45 (new, HIGH, OPEN) — no rejection criterion covers "demands what the contract does not say", so hostile-input guards ratchet

**Measured** in tvDownloadOHLC on 2026-09-26, on ticket T11 (profile `rust-spine`). This is
the Nautilus host that executes a session's order intents. All 4 rounds were green at
193 passed / 0 failed, with all 8 acceptance tests passing. The run ended
`MAX_ROUNDS_EXHAUSTED`, and the arbiter rejected nothing:

| round | kept | rejected | fill-guard conditions in the implementation |
|------:|-----:|---------:|---------------------------------------------:|
| 1 | 31 | 0 | 1 |
| 2 | 47 | 0 | 2 |
| 3 | 27 | 0 | 4 |
| 4 | 15 | 0 | 6 |

**Mechanism.**
1. In round 1, a reviewer asked the host to *skip* any fill whose quantity is not a
   positive finite integer. That input is outside the ticket's contract; the venue does not
   produce it.
2. The arbiter kept the finding. None of its five criteria applies: the code exists, the
   mechanism "holds", and the finding contradicts no gate.
3. Rounds 2 and 3 asked for the guard to be tightened (NaN, fractional quantities,
   ordering against the cast), and the implementer complied each time.
4. In round 4, both reviewers flagged the guards themselves as defects. A legitimate fill at
   a negative price (real on spread instruments) is now dropped, so the position never
   updates. Error-swallowing added for the same reason drew five more findings.
5. The loop spent its rounds adding and then objecting to the same code. In human review,
   five defects were fixed. Three of them had been *injected* on reviewer request:
   - fills filtered on price and quantity;
   - swallowed submit/modify/cancel errors;
   - a leg mapping evicted on cancel.

**The missing criterion.** The arbiter prompt (`arbiter.py`, `_ARBITER_CONTRACT`) can
reject only on four grounds: a contradicted gate, code that doesn't exist, scope, or
mechanism. It has no ground for "the finding demands behaviour the ticket's written
contract does not specify" or "the finding contradicts the contract". When a reviewer's
hypothetical input is not one the contract admits, criterion 5 ("the mechanism doesn't
hold") does not bite, because the arbiter reasons about the *code*, not about the
admissible inputs. The round-4 rationale says so in as many words: "semantic judgments
about contract intent that I cannot demonstrably reject."

**Proposed fix (not built).**
- Add criterion 6, **OUTSIDE THE CONTRACT**: the finding's failure needs an input or a
  behaviour that the region the ticket marks as the contract does not admit or require.
  The ruling must quote the contract line it relies on.
- Negative control: a finding that the contract *does* cover (for example, "the contract
  says every fill is passed on, and this guard drops some") must be KEPT. That was round 4's
  real finding.
- Separately, when round N's kept findings object to code that round N−1's kept findings
  requested, report it as a contradiction and escalate. Do not revise again.

Evidence: `logs/agent_loop/T11/r{1..4}_review_*.txt` and `r{1..4}_arbiter.txt` in
tvDownloadOHLC, which are local only. The promoted round is `356b0602` and the review fixes
are `8f09fdca`.

### CF-46 (new, MEDIUM, OPEN) — a locked link output is scored as the implementer's compile failure

**Measured** in tvDownloadOHLC on 2026-09-27, on ticket T15 (profile `rust-spine`). Round 1
failed to compile on a real defect (a missing `chrono::Datelike` import). Rounds 2, 3 and 4 all
failed with `error: linking with link.exe failed: exit code: 1104` / `LNK1104: cannot open
file '...\agent-loop-cargo-target\debug\deps\three_way-<hash>.exe'`. That exe belongs to a
test binary the ticket never touched. The profile's `--target-dir` is a shared cache outside
the worktree, and something held the previous build's exe open. By the time anyone looked,
no process was running from that directory, so it was transient: a lingering test process, or
an antivirus scan of the freshly linked file. The run ended `ARBITER_NEVER_RAN`. Applied by
hand, the round-4 patch compiled and passed all 9 acceptance tests, and a 16-mutant battery
killed every mutant.

**Mechanism.** The compile gate scores any non-zero `cargo build` as `build FAILED` and hands
the log to the implementer as feedback. A linker that cannot open its output file says
nothing about the patch. The implementer cannot fix it, so each round it was asked to try
again was spent on a failure that was not its own.

**Proposed fix (not built).** Classify an OS-level output-lock failure as an
**infrastructure** result, not a candidate result: `LNK1104` or `os error 32` on a path under
the target dir. Retry the gate after a short backoff without spending a round. If it persists,
end the run `INFRA_ERROR`, a verdict distinct from `MAX_ROUNDS_EXHAUSTED`, so it is never
mistaken for a patch that did not converge.
- Negative control: a genuine link error, such as an unresolved external symbol (`LNK2019`),
  must still reach the implementer.

Evidence: `logs/agent_loop/T15/r{2,3,4}_build.txt` in tvDownloadOHLC.

**Second instance, same day, on T16.** Rounds 1–2 were green. The round-4 candidate never got a
test verdict, because its build hit the same `LNK1104` on `three_way-<hash>.exe`. Applied by
hand, it passed 11/11, and a 22-mutant battery killed every mutant.

**A mechanism that fits, not yet confirmed.** `Workspace.run` calls `subprocess.run(cmd,
shell=True, timeout=...)`. On Windows, a timeout kills only the `cmd.exe` it started. `cargo`
and the test executables it spawned are orphaned, keep running, and keep their exe locked, so
the next round's link fails. `three_way` is the slowest test binary in the suite. To confirm,
look for an orphaned `three_way-*.exe` while a run is in progress. If that is the cause,
the fix is to kill the process tree on timeout: create the process with a new process group
or a Job object and run `taskkill /T /F` on the tree. CF-46's classification is still needed
either way.

### CF-47 (new, HIGH, OPEN) — the arbiter recommends REVISE when nothing above MINOR survives

**Measured** in tvDownloadOHLC on 2026-09-27, on ticket T14 (profile `rust-spine`, JSON
schemas). Rounds 2–4 were green on every gate: 219 passed, 0 failed, all 7 acceptance
tests green. The round-4 arbiter rejected the only three BLOCKER/MAJOR findings, because each
self-refutes. It kept 12 findings, all MINOR, and recommended **REVISE**. Its own rationale
says: "None of the survivors allege a wrong decision, a failed gate, or a reachable panic;
they are quality/coverage notes for the implementer and human reviewer to weigh." The run
ended `MAX_ROUNDS_EXHAUSTED`. Human review applied the round-4 patch unchanged, and a
17-mutant battery killed 16; the survivor is an equivalent mutant.

**Mechanism.** Nothing ties the recommendation to the severity of what survives. A MINOR
is, by the arbiter's own description, a note for the human, yet it blocks promotion exactly
as a BLOCKER does. This differs from CF-43, where a self-retracted BLOCKER keeps its label.
Here the labels are right and the recommendation ignores them.

**Proposed fix (not built).** Derive SHIP mechanically when every gate is green and no
upheld finding is above MINOR. Carry the MINORs into the result as review notes. Do not let
the model's recommendation override that.
- Negative control: one upheld MAJOR must still yield REVISE.

Evidence: `logs/agent_loop/T14/r4_arbiter.txt` and the committed
`logs/agent_loop/T14/result.json` (tvDownloadOHLC `799e67b1`).

**Second instance** (2026-09-27, ticket T17, `rust-spine`, NT8 parquet reader). Round 4
was green on every gate, and the arbiter recommended REVISE again. This time 43 of the 49
findings self-refuted ("OK", "Matches", "Not a defect"), which the arbiter rejected under its
own criterion 5. The 5 survivors were all style notes: unreachable defensive arms and struct
placement. Human review applied the patch unchanged, and a 20-mutant battery killed 16; the 4
survivors are equivalent. Evidence: `logs/agent_loop/T17/r4_arbiter.txt` (tvDownloadOHLC
`edbd7c1c`).

### CF-48 (new, HIGH, OPEN) — the successor panel member degenerates like the one it replaced

**Measured** in tvDownloadOHLC on 2026-09-27, on ticket T18 (profile `rust-spine`).
`deepseek-v4.1-flash:cloud` replaced the retired `deepseek-v4-flash`, and it returned **1551
findings** against a cap of 60. That was correctly scored `UNPARSEABLE`, and the member was
dropped. The run then ended `APPROVE_PARTIAL` on a quorum of 1/2 and needed human sign-off.
The retired model failed the same way, measured at 373 and then 853 findings. So the
replacement brought the same failure mode with it. Nobody measured it on the reviewer bench
before it became the package default.

**Consequence.** On a two-model panel, one degenerate member means every ticket is
reviewed by one model, and every approval is partial. CF-23 made that survivable; it did not
make it rare.

**Proposed fix (not built).** Bench a candidate panel member for repetition before it can
become a default: it must stay under the cap on the bench corpus. Count `UNPARSEABLE` drops
per member across runs, and warn once a member's drop rate passes a threshold.
- Negative control: a member that never degenerates must never trip the warning.

Evidence: `logs/agent_loop/T18/result.json` (tvDownloadOHLC `4ed56039`).

### CF-49 (new, HIGH, OPEN) — the arbiter upholds every finding, including a compile error the compile gate refutes

**Measured** in tvDownloadOHLC on 2026-09-27, on ticket T19 (profile `rust-spine`, the
catalog day store). Round 4 was green on every gate: the build succeeded and all 7
acceptance tests passed. The panel raised 13 findings. The arbiter kept **all 13**, and every
ruling carries the same boilerplate, "[KEEP] #n: no rejection criterion met". Its rationale
then asks the implementer to "resolve potential borrow-checker issues around
`builder.build()` (#5, #6)". Those are two MAJOR findings claiming the patch does not
compile, in a round whose `[compile]` gate reads `ok - build succeeded`. Human review found
2 of the 13 real (a metadata lookup rule, an invented five-impl trait shim) and fixed both. A
22-mutant battery then killed 20; the 2 survivors are equivalent.

**Mechanism.** The arbiter is given the gate results but has no criterion that says "a
finding contradicted by a mechanical gate is rejected". So a claim the machine already
disproved stands on equal footing with a real one. It is the mirror of CF-47: there, the
recommendation ignores the severities; here, the rulings ignore the evidence.

**Proposed fix (not built).** Add a rejection criterion stating that a finding asserting
the patch fails to compile, or a named test fails, is rejected when that gate is green.
Also treat a ruling list that is 100% KEEP with identical text as a degenerate arbiter
response, handled like `UNPARSEABLE`.
- Negative control: a compile claim in a round whose build gate is red must still be kept.

Evidence: `logs/agent_loop/T19/r4_arbiter.txt`, `r4_arbiter_prompt.md` (tvDownloadOHLC `151acce6`).

### CF-50 (new, MEDIUM, OPEN) — a two-file ticket resolves spec symbols against one file only

**Measured** in tvDownloadOHLC on 2026-09-27, validating ticket T26 (profile
`csharp-spinehost`). Its regions span `SpineCore.cs` and `SpineNative.cs`. `--list` printed
`REFUSE 'SpineJson' named in spec but not found in scripts/ninjatrader/spine/SpineNative.cs`,
and the same for `SpineExecutor`, `SpineClock`, `SpineBackstop` and the other types. Every one
of them is declared in `SpineCore.cs`, which is the ticket's own region file. Only
`--allow-unresolved-symbols` lets the ticket run, and that flag also silences the REAL
unresolved names the check exists to catch.

**Proposed fix (not built).** Resolve each spec symbol against the union of every region
file, plus the read-only context. Name the file it was found in.
- Negative control: a name declared in no region file must still REFUSE.

### CF-51 (new, MEDIUM, OPEN) — the reasoning-exhaustion diagnosis prescribes the setting already in force

**Measured** in tvDownloadOHLC on 2026-09-27 with ticket T25 (profile `rust-spine`). The
implementer role is configured `kimi-k2.7-code:cloud`, `max_tokens 96000`, `think: false`
(in `agent_loop.config.json`, since 2026-08-11). Round 1 died `IMPLEMENTER_UNREACHABLE` with:
`343848 chars of thinking, empty content (eval_count=96000, done_reason=length) ... Set
think=False for this role before raising max_tokens above 96000`.
- `think` WAS False. `providers._call_ollama` sent `payload["think"] = False`, and the
  model reasoned anyway, into the `thinking` channel, for the whole budget.
- The message does not know what was sent, so it prescribes the setting that just failed.
  An operator following it changes nothing and reruns into the same wall.

Two defects:
1. **The advice ignores its own input.** When `think is False` and thinking came back
   anyway, the message must say so. It should say that the model does not honour
   `think=false`, and that the remedies are a different implementer (`--implementer`) or a
   smaller ticket, not a setting.
2. **The run summary names symbols "shipped with this candidate" when no candidate
   exists.** The `[unresolved] 8 guessed symbol(s)` block printed after a round that
   produced no patch. It reports the ticket's pre-flight symbol refusals as a candidate's.

**Proposed fix (not built).** Thread `think` into the ProviderError text and branch on it.
Suppress the unresolved-symbols block when `applied` is False and no patch was produced.
- Negative control: `think=None` or `think=True` with the same response still prints the
  current advice.

### CF-52 (new, HIGH, OPEN) — context is attached for names in the spec, never for names in the regions' own signatures

**Measured** in tvDownloadOHLC on 2026-09-27 with ticket T27 (profile `rust-spine`,
`spine_verify`). The ticket ended `ARBITER_NEVER_RAN` after four rounds, three of which
failed to compile. Each failed round guessed a different shape for a type the regions
return:
- r1: `Check::Fail(..)`, `Check::Pass(..)`, `Refusal { .. }` as a `Check` variant;
- r2: `Check::fail(..)`, and fields `bar_time`, `seq` and `side` on `spine::log::InputBar`;
- r4: `Check { passed, vacuous }`, `Refusal { detail }`, `BTreeMap` without an import.

Round 3 asked in prose for the file contents. The rewritable regions' signatures are
`pub fn v0_identity(..) -> Check` and `pub fn parse(..) -> Result<HostLog, Refusal>`, so
`Check` and `Refusal` are declared in the SAME files, outside every region. The AUTO
read-only context attachment (`'X' declared outside every region in F; attaching
read-only context`) fires only for symbols the SPEC names. The spec never wrote `Check`,
because the signature already says it.

It is invisible for two reasons:
- The pre-flight is silent about signature types. It cannot refuse what it never looks for.
- `--allow-unresolved-symbols` is needed anyway because of CF-50's per-file false positives.
  That also switches off the one check that would have helped.

Adding the types as `readonly` regions by hand fixed the prompt. T28 and T24 had the same
gap and were fixed before they ran: `SetupEvaluation::gate`, `Ema`, `open_envelope`;
`SessionHost::from_registry`, `day_feed`, `Day`.

**Proposed fix (not built).** Extract the type and path identifiers from every rewritable
region's signature (return type, parameter types, `impl` target). Resolve them like spec
symbols, including the AUTO attach for a declaration outside every region, and follow
cross-file imports one hop. Print what was attached.
- Negative control: a signature using only std or prelude types attaches nothing.

**Related, same session:** a `readonly` region in a test file (`tests/venue.rs`, as a
worked example of driving an engine) makes `--list` print `[REFUSED: targets the
verifier]` for the whole ticket. Refusing to REWRITE the verifier is right. Refusing to
SHOW it read-only removes the only working example of an external API when none exists
outside the tests. The workaround was to transcribe the call shapes into the spec.

### CF-53 (new, HIGH, OPEN) — one transient `git diff` failure discards a finished run, and the error keeps no evidence

Seen on tvDownloadOHLC T28 (rust-spine), 2026-09-27. Round 3 was green at every gate:
compile, tests (13/13 acceptance), lock-scope. The panel then ruled REVISE. The next
`Workspace.diff()` call returned nonzero with EMPTY stderr, and the run ended
`ERROR: WorkspaceError: git diff failed: ` with `applied=False`. No `final.patch`,
`final_blocks.json` or `result.json` was written. Re-running `git diff` by hand in the
same worktree minutes later returned 0 and the correct one-file diff.

Three defects:
- **The error keeps nothing to diagnose it with.** `workspace.py` raises with stderr
  only. The returncode, the command and stdout are dropped, so an empty-stderr failure
  prints `git diff failed: ` and the cause is unknowable after the fact.
- **One transient failure costs the whole run.** Three model rounds and a panel review
  were spent. The cause is a read-only operation on a worktree the run owns. One retry,
  or writing the round's blocks to `final_blocks.json` BEFORE diffing, would have kept the
  artifact. The code was recovered only because the worktree directory survived.
- **The cleanup message is false.** `remove_worktree` runs `git worktree remove --force`
  with `check=False`, then prints "git no longer tracks it" for ANY non-empty directory
  left behind. `git worktree list` still listed `agentloop-T28-16548`. The removal failed
  (plausibly the same transient cause), and the message says the opposite of what
  happened.

**Proposed fix (not built).**
- Include `returncode`, `cmd` and stdout in every `WorkspaceError`.
- Persist the last green round's blocks before any post-panel git operation.
- Retry `git diff` once.
- In `remove_worktree`, check the remove's returncode and report "git still tracks it"
  when it failed.
- Negative control: a genuinely failing diff (a corrupt index) still ends ERROR, with
  the returncode printed.

### CF-54 (new, MEDIUM, OPEN) — the baseline runs the test command without the build command, and the refusal is cut before its cause

Seen on tvDownloadOHLC T26 (csharp-spinehost), 2026-09-27. The run ended
`TICKET_REJECTED` before any model call: "the test suite does not produce a parseable
result summary at baseline". The profile's `test_cmd` is `dotnet run --no-build`. In the
fresh worktree nothing had been built, so dotnet printed `cannot find the file ...
SpineHostTests.exe`. The same command in the main checkout gave the correct red baseline
(`RESULTS: Passed = 0, Failed = 18`).

Two defects:
- **`capture_baseline` runs `test_cmd` alone.** Every round runs `build_cmd` before
  `test_cmd`; the baseline does not. A profile whose test command relies on its build
  command therefore passes every round and fails at baseline. Here that happens on
  the first worktree it ever gets. The consumer worked around it: the test command now
  builds what it runs (tvDownloadOHLC `9d043a17`).
- **The console refusal is cut at 200 characters**, which is exactly where the cause
  starts. `print(f"  REFUSED: {result['detail'][:200]}")` ends at `Baseline error: `.
  The dotnet message is in `result.json` only, and the console reads as though the
  error were empty.

**Proposed fix (not built).**
- Run `profile.build_cmd` in `capture_baseline` before `test_cmd`. A build failure is
  its own refusal ("baseline does not build"), not "no parseable summary".
- Print the refusal's cause, i.e. the tail of the raw output, rather than a prefix of
  the prose.
- Negative control: a test command that is genuinely broken, with the build green,
  still ends `TICKET_REJECTED` and shows its output.

### CF-55 (new, HIGH, FIXED) — an auto-attached read-only context region could have a non-unique anchor, and that killed the run

Seen on tvDownloadOHLC T26 (csharp-spinehost), 2026-09-27, right after the CF-54
workaround gave a correct baseline. `_attach_readonly_context` (CF-31) anchors the
region on the declaration line, or, when that line repeats, on the first line of a
three-line window, and it never checked that fallback. In C# that line is routinely
`/// <summary>`, which has 14 hits in `SpineCore.cs`. The region then raised
`RegionError: anchor not unique` and the run ended `ERROR` before round 1, all for a
region that is only a courtesy.

**Fix.** `_unique_anchor_near` returns the declaration line if it is unique. Otherwise
it returns the nearest non-blank line that occurs once, searching within 20 lines and
trying above before below. If there is none, the context is not attached and a `SKIP`
line says so; a courtesy region no longer ends a run. Enforced by
`tests/test_readonly_context_anchor.py` (4 tests):
- the shape that failed (a repeated declaration whose window starts on `/// <summary>`);
- the declaration line is preferred when unique;
- the nearest unique line is chosen;
- the negative control: no unique line, nothing attached.

The failing-shape test and the negative control are both red on the pre-fix code. Suite:
865 passed, 40 skipped. Selftest: 13/13.

### CF-56 (new, HIGH, FIXED) — a test runner that dies before its summary hides how it died from the implementer

Seen on tvDownloadOHLC T26 (csharp-spinehost), 2026-09-27, round 2. The patch compiled.
The runner printed six `[FAIL] ...: FormatException: bad kind` lines, then a P/Invoke
call raised `System.AccessViolationException`, which .NET cannot catch, and the process
died before `RESULTS:`. `check_tests` put that output in `GateResult.detail` and set
`feedback` to one sentence: "no conclusion can be drawn about your patch". The
implementer is handed `feedback or summary` (the CF-18 shape), so the model was told its
patch had an unknown effect. The log it never saw named both of its defects and the
crashing frame.

**Fix.** On an uncounted run, `feedback` carries the last 3000 characters of the output
and says that a crash is usually the patch's doing. Enforced by
`tests/acceptance/test_uncounted_run_reaches_the_implementer.py` (3 tests):
- the T26 shape (FAIL lines, then an uncatchable crash) reaches `feedback or summary`,
  crash frame included;
- a long output hands on only its tail;
- the negative control: a counted run is unchanged.

The first two are red on the pre-fix code. Suite: 868 passed, 40 skipped. Selftest: 13/13.
