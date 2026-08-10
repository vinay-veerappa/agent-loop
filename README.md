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

Phase 1 (state machine fixes) is complete. The loop can run against its own Python source.

| Milestone | Status |
|---|---|
| Package extraction from tvDownloadOHLC | Done |
| Language-agnostic profiles + model registry | Done |
| Indent-based region finder (Python) | Done |
| Phase 1: 7 state-machine fixes (stale artifacts, arbiter deadlock, ARBITER_NEVER_RAN, applied split, quorum) | Done (17/17 tests pass) |
| Phase 2: Re-index the graph | Next |
| Phase 3: Passive graph-augmented prompts | Planned |
| Phase 4: Compaction | Planned |
| Phase 5: Persistent memory | Planned |
| Phase 6: Plan + Test modes | Planned |
| Phase 7: Active graph tools | Planned |
| Phase 8: Developer mode | Planned |

See [AGENT_LOOP_V2_PLAN.md](docs/architecture/AGENT_LOOP_V2_PLAN.md) for the full execution plan.

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