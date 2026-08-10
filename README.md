# agent-loop

**Language-agnostic AI agent loop for software engineering.**

Implement -> gate -> review -> arbitrate -> apply.

- **Multi-model adversarial panel** — different model families review concurrently; the worst verdict wins.
- **Adjudicating arbiter** — rules on each reviewer finding; only upheld findings go back to the implementer. No other harness separates detection from adjudication.
- **Settled-decisions cache** — adjudication precedents persist across tickets, preventing reviewers from re-litigating known false positives.
- **Language-agnostic** — the loop driver, gates, and region extractor contain zero language-specific strings. Everything lives in a `Profile`. Adding Python or TypeScript support is a new profile, not a fork.
- **Model-by-capability registry** — declarative mapping from role to model. The arbiter must not be the same model as any reviewer.
- **Token efficiency** — per-round input budget, per-role output caps, graph context capped.

## Status

All 8 phases complete, all 17 backlog items addressed, 3-model cross-review
done with fixes applied. Tagged `v0.1.0`. 77/77 tests pass.

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
| `docs` | git diff → documentation updates | `--mode docs --review-base HEAD~1` |

See [AGENT_LOOP_V2_PLAN.md](docs/architecture/AGENT_LOOP_V2_PLAN.md) for the full execution plan,
[IMPLEMENTATION_DECISIONS.md](docs/architecture/IMPLEMENTATION_DECISIONS.md) for the decision log,
and [BACKLOG.md](docs/architecture/BACKLOG.md) for the status of all items.

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
    block_comment=("#",),
    preprocessor_directives=(),
    build_cmd="python -m compileall src/",
    test_cmd="python -m pytest tests/ -v",
    protected=("test_*.py", "conftest.py", "agent_loop/*"),
    implementer_rules="You are a senior Python engineer...",
    reviewer_priorities="You are an adversarial code reviewer...",
)

register(MY_PROFILE)
```

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