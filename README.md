# agent-loop

**Language-agnostic AI agent loop for software engineering.**

Implement -> gate -> review -> arbitrate -> apply.

- **Multi-model adversarial panel** — different model families review concurrently; the worst verdict wins.
- **Adjudicating arbiter** — rules on each reviewer finding; only upheld findings go back to the implementer. No other harness separates detection from adjudication.
- **Settled-decisions cache** — adjudication precedents persist across tickets, preventing reviewers from re-litigating known false positives.
- **Learning feedback** — the loop records which findings the arbiter UPHELD vs REJECTED across tickets, and injects "known false positives" and "known real defects" into future reviewer prompts. The loop gets smarter with every ticket.
- **Context bloat control** — settled-decisions injection is capped at 20 most recent (~1K tokens). Learning feedback capped at 10 entries (~500 tokens). Graph context capped at 3000 tokens. Per-round input budget 40K tokens. Older data stays on disk for auditability but doesn't bloat the prompt.
- **Language-agnostic** — the loop driver, gates, region extractor, and arbiter contain zero language-specific strings. Everything lives in a `Profile`: the code-fence label, the declaration forms, whether braces delimit blocks, whether ASCII-only is enforced, and the arbiter's standard for what "blocks" in this codebase. Adding Python or TypeScript support is a new profile, not a fork.
- **Model-by-capability registry** — declarative mapping from role to model. The arbiter must not be the same model as any reviewer.
- **Token efficiency** — per-round input budget, per-role output caps, graph context capped.

## Status

All 8 phases complete, all 17 backlog items addressed, 3-model cross-review
done with fixes applied, plus a line-by-line review of the whole package
(2026-08-10) whose findings are fixed and pinned by tests. 129/129 tests pass.

`v0.1.0` is tagged but predates Phase 9 and the review fixes — install from
`main` until the next tag.

| Phase | Status |
|---|---|
| 1: State machine fixes | Done |
| 2: Graph freshness | Done |
| 3: Passive context injection (live MCP) | Done |
| 4: Compaction (mechanical + LLM) | Done |
| 5: Persistent memory | Done |
| 6: Plan + Test modes | Done |
| 7: Active graph tools (live MCP) | Done |
| 8: Developer mode | Done |
| Backlog: PANEL_REJECT, developer panel, reviewer context, token accounting | Done |
| Backlog: MCP client, brainstorm mode, docs mode | Done |
| Backlog: Consumer profiles (nt8-riskguard, python-tvdownloadohlc) | Done |
| Cross-review (glm-5.2 + deepseek-v4-pro + minimax-m3) | Done, fixes applied |
| Phase 9: Learning feedback + context bloat control | Done |
| Full-package review (2026-08-10): 22 defects fixed, 52 regression tests added | Done |

The loop bootstrapped itself: it ran a ticket against its own source,
generated a fix, passed all gates, and both reviewers unanimously approved.

### Modes

| Mode | Input → Output | Flag |
|---|---|---|
| `patch` | ticket JSON → patched code | `--mode patch` (default) |
| `review` | existing diff → panel verdict | `--mode review --review-base HEAD~1` |
| `plan` | defect → ticket JSON (panel+arbiter reviewed) | `--mode plan --defect "..."` |
| `test` | defect + ticket → failing acceptance tests | `--mode test --defect "..." --tickets plan.json` |
| `developer` | defect → patched code (autonomous localize+edit) | `--mode developer --defect "..."` |
| `brainstorm` | defect → candidate approaches + trade-offs | `--mode brainstorm --defect "..."` |
| `docs` | codebase → documentation (4 sub-modes) | `--mode docs --docs-type changelog\|handover\|design\|prd` |
| `run-plan` | plan JSON → executed chain | `--mode run-plan --plan plan.json` |

### run-plan mode flags

| Flag | Description |
|---|---|
| `--apply` | Commit each promoted part to the scratch branch |
| `--tdd` | Generate failing acceptance tests before each part (TDD) |
| `--pipeline` | Chain plan → run-plan --tdd --apply in one invocation |
| `--epic "..."` | Two-tier decomposition: epic → stories → tasks |
| `--feature "..."` | Single-tier decomposition: feature → parts |
| `--resume` | Read backlog.json, skip done parts, retry failed/blocked |
| `--backlog PATH` | Path to backlog.json (for --resume or status display) |
| `--from PART_ID` | Resume from a specific part (skip earlier parts) |
| `--keep-branch` | Do not delete the scratch branch on failure |
| `--replan` | Re-plan a failed part instead of stopping |
| `--replan-limit N` | Max re-plans per part (default 2) |
| `--continue-on-failure` | Continue to next independent part on failure |

See [ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md) for the authoritative
architecture reference, [AGENT_LOOP_V2_PLAN.md](docs/architecture/AGENT_LOOP_V2_PLAN.md) for the full execution plan,
[IMPLEMENTATION_DECISIONS.md](docs/architecture/IMPLEMENTATION_DECISIONS.md) for the decision log,
and [BACKLOG.md](docs/architecture/BACKLOG.md) for the status of all items.

## Docs mode

The docs mode generates documentation from the codebase, not just from a diff.
Four sub-modes, each with a different input and output:

| Sub-mode | Input | Output | Use case |
|---|---|---|---|
| `changelog` | git diff | changelog entry (Added/Fixed/Changed/Removed) | "What changed in this commit?" |
| `handover` | session ledger + git state | handover document (done/remaining/traps/next steps) | "What did I do, what's left?" |
| `design` | feature idea + graph context | design document (problem/approach/alternatives/impact/open questions) | "How should we build this?" |
| `prd` | defect/feature + graph context | product requirements document (background/requirements/acceptance criteria/out-of-scope/risks) | "What are we building and why?" |

All sub-modes use the graph context (callers, callees, types) when the
profile has `graph_project` set. The `design` and `prd` sub-modes use the
graph to answer "what existing code does this touch?" — the same graph the
loop uses for passive context injection.

When `profile.docs_conventions` is set, docs mode prepends those conventions
to the system prompt so generated documents match the repo's house format
(section headers, ADR format, handover format). Without it, the four
hardcoded system prompts in `docs_mode.py` are used as-is.

```bash
# Generate a changelog from the last commit
agent-loop --mode docs --docs-type changelog --review-base HEAD~1

# Generate a handover document from the session state
agent-loop --mode docs --docs-type handover

# Generate a design document for a feature
agent-loop --mode docs --docs-type design --defect "Add a trailing stop to the copier"

# Generate a PRD for a defect
agent-loop --mode docs --docs-type prd --defect "Fix the copier not copying exits"
```

Output goes to `docs/generated/<docs-type>.md` unless `--docs-out` says
otherwise. That directory is gitignored: these are model artifacts, regenerated
on demand, and should not be reviewed as if a human wrote them. `changelog` is
the only sub-mode that needs `--review-base`; `design` and `prd` require
`--defect`.

## Configuration

Every tunable number — which model does which job, token budgets, whether a role
thinks, round limits, panel deadlines, transport settings — lives in one place:
[`src/agent_loop/config.py`](src/agent_loop/config.py). That module carries the
**reason** each default has its value; read it before changing one.

Override without editing the package by copying
[`agent_loop.config.example.json`](agent_loop.config.example.json) to
`agent_loop.config.json` and deleting everything you are not changing:

```json
{
  "roles": {"reviewer": {"model": "kimi-k3:cloud"}},
  "modes": {"docs": {"max_tokens": 48000}},
  "loop":  {"max_rounds": 6}
}
```

Resolution order: `--config PATH` → `$AGENT_LOOP_CONFIG` → `./agent_loop.config.json`
→ built-in defaults. A path that does not exist is an error rather than a silent
fallback, and **unknown keys are rejected** — a typo that is quietly ignored is
worse than no config file, because you believe the setting took effect.
Underscore-prefixed keys (`"_comment"`) are treated as comments.

> **Budgets and thinking.** On a reasoning model, chain-of-thought is spent from
> the *same budget as the answer*, and `think=None` leaves the model's own
> default in force — which is ON. A budget sized for the expected output
> therefore becomes a budget shared with an unbounded reasoning prefix. This is
> not hypothetical: the implementer had 48000, and on a two-region ticket the
> model spent 125,070 characters reasoning and returned **empty content**, so the
> run died having produced nothing. Every role and mode declares `think`
> explicitly, and anything with `think: true` is budgeted for reasoning *plus*
> answer. If you turn thinking on, raise the budget in the same edit.

## Install

```bash
pip install git+https://github.com/vinay-veerappa/agent-loop.git
```

## Quick start

1. **Create a profile** for your codebase:

```python
# my_project/agent_loop_config.py
from agent_loop.profiles import Profile, register

MY_PROFILE = Profile(
    name="my-python-project",
    language="python",
    file_suffixes=(".py",),
    line_comment="#",
    # DELIMITED comments only, and Python has none. Listing "#" here refuses
    # every Python file that contains a comment: block_comment is the set of
    # tokens the region locator cannot parse safely, not the comment syntax.
    block_comment=(),
    block_kind="indent",          # "decl" for brace-delimited languages
    preprocessor_directives=(),
    # {files} is replaced with the files the patch touched. A fixed target here
    # makes the compile gate pass no matter what the patch did.
    build_cmd="python -m py_compile {files}",
    test_cmd="python -m pytest tests/ -q",
    protected=("test_*.py", "conftest.py", "agent_loop/*"),
    implementer_rules="You are a senior Python engineer...",
    reviewer_priorities="You are an adversarial code reviewer...",
    # What "blocks" means here, and what an unsound SHIP costs. Omit it and the
    # arbiter gets a generic bar; give it the wrong one and no finding can clear
    # it, so the arbiter rejects everything and recommends SHIP.
    arbiter_rules="Blocking means a wrong result reaches a caller...",
)

register(MY_PROFILE)
```

Your `test_cmd` must produce a parseable summary and must not report suite-level
errors — the loop freezes its failures as the expected-failure baseline and
refuses to run against a broken suite, because a baseline captured from one
lets a patch inherit it as its success criterion.

2. **Write a ticket**:

```json
{
  "tickets": [
    {
      "id": "T1",
      "title": "Fix the off-by-one in parse_date",
      "defect": "parse_date returns the wrong day when the input is the last day of a leap year.",
      "spec": "Fix the leap year check in parse_date to handle Feb 29 correctly.",
      "regions": [
        {"id": "PARSE_DATE", "file": "src/dates.py", "anchor": "def parse_date"}
      ],
      "expect_green": ["test_parse_date_leap_year"]
    }
  ]
}
```

3. **Run the loop**:

```bash
agent-loop --profile my-python-project --profile-module my_project.agent_loop_config --tickets tickets.json --ticket T1
```

The loop runs implement -> gate ladder -> panel -> arbiter. If the arbiter recommends SHIP, a human promotes:

```bash
agent-loop --profile my-python-project --profile-module my_project.agent_loop_config --tickets tickets.json --ticket T1 \
    --resume-raw logs/agent_loop/T1/r2_impl_raw.txt --allow-unapproved --apply
```

## Architecture

See the docs:
- [AGENT_LOOP_RESEARCH.md](docs/architecture/AGENT_LOOP_RESEARCH.md) — state of the field across 13 coding agent harnesses
- [AGENT_LOOP_V2_PLAN.md](docs/architecture/AGENT_LOOP_V2_PLAN.md) — execution plan: 8 phases, new states, Developer mode, language agnosticism, model registry, token efficiency
- [AGENT_PATCH_LOOP.md](docs/architecture/AGENT_PATCH_LOOP.md) — the current loop's proven history (NT8 RiskGuard hardening)

## License

MIT