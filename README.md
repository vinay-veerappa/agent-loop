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

See [AGENT_LOOP_V2_PLAN.md](docs/architecture/AGENT_LOOP_V2_PLAN.md) for the full execution plan,
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

**Reference**: the documentation architect skill (`.agents/skills/doc-architect`
or equivalent) defines the conventions for documentation structure — section
headers, ADR format, handover format. The docs mode follows these conventions
in its system prompts. When the skill is available, its conventions should be
injected into the docs mode's system prompt to ensure generated docs match
the project's established format.

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