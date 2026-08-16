# Agent Loop — Fifth Review

> **Date:** 2026-08-16
> **Reviewer:** glm-5.2 (self-review, code-level)
> **Scope:** Full codebase audit after Waves 0–4. 676 tests pass, 34 skipped.
> **Method:** Read every source file, every test file, and the architecture
> documentation. Traced control flow through `run_ticket`, `review_panel`,
> `run_plan`, `parse_review`, `_commit_to_branch`, `terminal_ledger_record`.

---

## Summary

The loop's core (patch mode: implement → gates → panel → arbiter → apply) is
solid. The mechanical reliability fixes (Waves 0–3) are well-tested and the
invariant enforcement (Wave 1) is correct. The architecture documentation is
thorough and accurate.

**The problem is Wave 4 and Wave 0.5.** Four items were shipped with either
unfinished code, untested critical paths, or shallow implementations that
look right in a test but do not survive contact with the real system. The
plan runner (Wave 0.5) is the most serious: its central guarantee is not
implemented, and the one test that exists only covers the dry-run path.

Findings are labelled R5-1 through R5-7. Severity is BLOCKER (ships wrong
code or silently fails), MAJOR (correct but misleading, or untested critical
path), MINOR (cosmetic or edge case).

---

## R5-1 [BLOCKER] Plan runner: commit-per-part is unfinished and commits to the user's branch

**File:** `src/agent_loop/run_plan_mode.py:137-176`

`_commit_to_branch` has a comment at line 169-174 that literally says:

```python
    # Reset the live repo's HEAD back to where it was (the plan branch has
    # the commit; the user's working tree should not have commits from the
    # plan). Actually -- run_ticket with apply=True promotes to the working
    # tree, and we just committed that. We need to reset the user's branch
    # back so the commit is ONLY on the plan branch.
    # ...this is the tricky part. Let me think about this differently.
```

This is unfinished code that was shipped. The function:
1. Stages files on the user's current branch
2. Commits them to the user's current branch
3. Moves the plan branch ref to that commit
4. **Never resets the user's branch back**

So every part promoted by the plan runner leaves a commit on the user's
working branch. The plan branch and the user's branch both point at the
same commit. The "scratch branch" is not scratch — it's the user's branch
with a different label.

**Impact:** Running `--mode run-plan --apply` pollutes the user's git
history with plan-part commits. The plan branch is not isolated. The
manifest reports `commit` hashes that are on the user's branch, not a
scratch branch.

**Test gap:** `test_run_plan_mode.py` has 5 tests, all covering
`_topological_sort` (pure function, no I/O) and one covering `run_plan`
with `apply=False` (dry run). The `apply=True` path — the entire point of
the plan runner — has **zero tests**.

**Fix:** Either (a) use `git stash` + checkout plan branch + commit + checkout
back + stash pop, or (b) create a worktree at the plan branch, commit there,
and never touch the user's branch. Option (b) is what `workspace.py` is for.

---

## R5-2 [BLOCKER] Plan runner: part 2's worktree does NOT see part 1's work

**File:** `src/agent_loop/run_plan_mode.py:270-277`

The plan runner's docstring (line 8-11) states:

> The planner decomposes, orders parts, and validates the chain. The runner
> executes the chain with the guarantee that part 2's worktree can see part 1's
> work (via commits to a scratch branch)

The code computes `part_base` at line 275:

```python
if result.parts and result.parts[-1].applied:
    part_base = branch
else:
    part_base = "HEAD"
```

But `part_base` is **never passed to `run_ticket`**. `run_ticket` calls
`workspace.open_workspace(repo, tid, keep=keep_worktree)` which hardcodes
`base="HEAD"`. There is no `base_ref` parameter on `run_ticket`'s signature.

**Impact:** Every part's worktree is created at `HEAD`, not at the plan
branch's HEAD. Part 2 cannot see part 1's promoted code. A multi-part
feature where part 2 depends on part 1's new file will fail at region
extraction — the file doesn't exist in part 2's worktree.

The plan runner's central guarantee is not implemented. `part_base` is dead
code.

**Test gap:** No test runs two parts with `apply=True` and checks that part
2's worktree contains part 1's changes.

**Fix:** Add a `base_ref` parameter to `run_ticket`, pass it to
`open_workspace`, and have `run_plan_mode` pass `branch` when the previous
part was promoted.

---

## R5-3 [MAJOR] JSON fallback: bare-object regex cannot match nested JSON

**File:** `src/agent_loop/loop.py:198`

```python
for m in _re.finditer(r"(\{[^{}]*\})", text, re.DOTALL):
    results.append(m.group(1))
```

`[^{}]*` matches any character except braces. A JSON object with nested
objects or arrays-of-objects (the common case for a review with findings)
contains inner braces, so this regex matches only the **innermost** flat
object, not the outer one.

**Verified:** for `{"verdict": "REVISE", "findings": [{"severity": "BLOCKER", "text": "bug"}]}`,
this regex extracts `{"severity": "BLOCKER", "text": "bug"}` — the inner
finding, not the review. The inner object has no `verdict` key, so it is
skipped, and the review is lost as UNPARSEABLE.

The tests pass only because the test cases start with `{` and end with `}`,
triggering the first branch (line 190: `if stripped.startswith("{") and
stripped.endswith("}"): return [stripped]`). But a JSON review embedded in
prose — the exact case the bare-regex branch is for — fails silently.

**Impact:** A model that emits `Here is my review: {"verdict": "APPROVE",
"findings": [...]}` in prose is not parsed. The fallback only works for
bare flat JSON or whole-text JSON, not the prose-embedded case.

**Fix:** Use a brace-matching extractor instead of a regex. Iterate the
string, track depth, and extract balanced `{...}` blocks.

---

## R5-4 [MAJOR] Evidence ledger records the review stage, not the gate ladder

**File:** `src/agent_loop/loop.py:674-679`

```python
green_rounds = [r for r in result["rounds"] if r.get("ok")]
if green_rounds:
    last_green = green_rounds[-1]
    evidence["final_gate"] = last_green.get("stage", "")
    evidence["gate_summary"] = last_green.get("summary", "")
```

For a promotable ticket (unanimous APPROVE), the last green round's stage
is `"review"`, not a gate. The `RoundRecord` for a green review round has
`stage="review"` and `summary="APPROVE [glm=APPROVE(0), ds=APPROVE(0)]"`.
So the evidence says:

```json
"evidence": {"final_gate": "review", "gate_summary": "APPROVE [glm=APPROVE(0)...]"}
```

This is not evidence that the patch compiles or passes tests. It is evidence
that the panel approved it. The actual gate evidence (static, compile, test
summaries) is in the round records but is not surfaced.

**Impact:** The evidence ledger — whose stated purpose is "the run proved
the patch closes the defect, by this evidence" — does not record the
mechanical gates that passed. A reader checking the ledger after the fact
sees the panel verdict, not the compiler and test runner.

**Fix:** Record the gate ladder from the promotable round. The round that
cleared every gate has `stage="review"` but the gates ran before it. Either
record all gate summaries from that round's gate results, or record the
test gate's summary specifically (the most meaningful evidence).

---

## R5-5 [MAJOR] Reasoning budget warning is not thread-safe and is ephemeral

**File:** `src/agent_loop/providers.py:681-688`

The warning is printed via `print()` to stdout. Two issues:

1. **Thread interleaving:** `review_panel` runs reviewers concurrently via
   `ThreadPoolExecutor`. Two reviewers with `think=True` and low budgets
   will interleave their warnings on stdout, producing gibberish like:
   ```
     WARNING: model-a think=True with max_tokens=16000   WARNING: model-b think=True with max_tokens=8000
   ```

2. **Ephemerality:** The warning is not recorded in any artifact or ledger.
   A run that hit the warning and then returned empty content (the exact
   failure mode it warns about) has no trace of the warning in
   `logs/agent_loop/`. The operator sees `IMPLEMENTER_UNREACHABLE` and no
   hint about the budget.

**Impact:** The warning is unreadable in concurrent panels and invisible
after the run. The hazard it warns about — empty content from reasoning
consuming the whole budget — is the one that killed O1, and the warning
would not help diagnose it.

**Fix:** Use `logging.warning()` (thread-safe) or write to the ticket's
artifact directory. Or: record the warning in the `Completion` or `Vote`
so it lands in the ledger.

---

## R5-6 [MAJOR] `append_ledger` has no lock — concurrent writes can interleave

**File:** `src/agent_loop/loop.py:703-710`

```python
def append_ledger(repo: Path, record: Dict[str, Any]) -> None:
    p = repo / "logs" / "agent_loop" / "ledger.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
```

`save_settled` has a per-file `threading.Lock` (N2, Wave 0) because "on
Windows, concurrent line-buffered appends to the same file from multiple
threads/processes are NOT atomic." `append_ledger` does the same kind of
append and has no lock.

The plan runner calls `run_ticket` for each part, and each `run_ticket`
calls `append_ledger` at the end. If two plan runners run concurrently
(or a plan runner and a manual ticket), their ledger writes can interleave
on Windows, corrupting the JSONL.

**Impact:** Corrupted ledger lines on Windows under concurrent runs. The
same hazard N2 fixed for `save_settled` is unfixed for the ledger.

**Fix:** Apply the same `threading.Lock` pattern from `save_settled` to
`append_ledger`.

---

## R5-7 [MINOR] ARCHITECTURE.md §18 says Wave 4 is "Needs measurement" but Wave 4 is done

**File:** `docs/architecture/ARCHITECTURE.md:774`

```markdown
| 4 | TDD proxy, evidence ledger, JSON output, reasoning budget | **Needs measurement** |
```

Wave 4 was implemented and committed (`e091196`). The doc still says "Needs
measurement." The §18 table also says Wave 0.5 is "Implementing" but does
not reflect that it shipped with the two BLOCKERs above.

---

## What's good

- **Core loop (patch mode):** the round driver, gate ladder, panel, arbiter,
  and workspace isolation are well-designed and well-tested. The
  separation of detection from adjudication is real and enforced.
- **Mechanical reliability (Waves 0–3):** every fix has a test that would
  fail without it. The encoding gate, panel deadline, quorum rescue, and
  parser tolerance are all correct.
- **Architecture documentation:** ARCHITECTURE.md is thorough, accurate,
  and carries the reason for every design decision. It is the reference
  an operator needs.
- **Config centralization:** the rule that a tunable appears as a literal
  exactly once, with a test enforcing it, is the right discipline and it
  is held.
- **Model catalogue:** the "MEASURED" annotations are honest — they record
  what was observed, including "MEASURED BAD," and do not guess.

---

## Consolidated priority

| ID | Severity | What | Effort |
|---|---|---|---|
| R5-1 | BLOCKER | Plan runner commits to user's branch | Medium |
| R5-2 | BLOCKER | Plan runner: part 2 doesn't see part 1's work | Medium |
| R5-3 | MAJOR | JSON fallback regex can't match nested objects | Small |
| R5-4 | MAJOR | Evidence ledger records review, not gates | Small |
| R5-5 | MAJOR | Reasoning budget warning not thread-safe / ephemeral | Small |
| R5-6 | MAJOR | `append_ledger` has no lock | Small |
| R5-7 | MINOR | ARCHITECTURE.md §18 stale | Trivial |

**Recommendation:** Fix R5-1 and R5-2 before any plan runner use. They are
the difference between "the plan runner works" and "the plan runner
corrupts your git history and cannot compose parts." The MAJORs are
correctness issues in features that were shipped this session and should
be fixed before tagging.