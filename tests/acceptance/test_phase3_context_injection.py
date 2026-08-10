"""
Acceptance test for Phase 3: passive graph-augmented context injection.

The loop must inject graph context (callees, callers, tests, types) into
the implementer prompt when a graph_context.json cache file exists.
When no cache exists, the loop works without context (backward compat).
"""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop.profiles import Profile, register
from agent_loop.providers import Completion
from agent_loop import loop, regions
from agent_loop.loop import PanelResult, Vote
from agent_loop.context import build_context_slice, write_context_cache


GRAPH_PROFILE = Profile(
    name="test-graph-ctx",
    language="python",
    file_suffixes=(".py",),
    line_comment="#",
    block_comment=(),
    block_kind="indent",
    graph_project="test-project",
    context_token_budget=3000,
    implementer_rules="test",
    reviewer_priorities="test",
)
register(GRAPH_PROFILE)

NO_GRAPH_PROFILE = Profile(
    name="test-no-graph-ctx",
    language="python",
    file_suffixes=(".py",),
    line_comment="#",
    block_comment=(),
    block_kind="indent",
    graph_project="",
    implementer_rules="test",
    reviewer_priorities="test",
)
register(NO_GRAPH_PROFILE)


def _make_repo(tmpdir):
    repo = tmpdir / "repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    (src / "target.py").write_text("class TargetClass:\n    def method(self):\n        return 42\n", encoding="utf-8")
    os.system(f'cd /d "{repo}" && git init && git add -A && git commit -m init')
    return repo

def _make_region(repo):
    path = repo / "src" / "target.py"
    return regions.Region(id="R1", file="src/target.py", path=path, anchor="class TargetClass",
                          kind="decl", start_line=0, end_line=2, text=path.read_text())

def _impl_ok(model, messages, **kw):
    for msg in messages:
        if msg.get("role") == "user":
            for line in msg["content"].split("\n"):
                if line.startswith('### REGION id='):
                    rid = line.split('id="')[1].split('"')[0]
                    return Completion(
                        text=f'<<<BLOCK id="{rid}">>>\nclass TargetClass:\n    def method(self):\n        return 42\n<<<END id="{rid}">>>\n<<<NOTES>>>\n- fixed\n<<<END NOTES>>>',
                        model=model, input_tokens=100, output_tokens=50)
    return Completion(text='<<<BLOCK id="R1">>>\nclass TargetClass:\n    def method(self):\n        return 42\n<<<END id="R1">>>', model=model)


def test_phase3_context_from_cache(tmp_path):
    """build_context_slice returns context when a cache file exists."""
    repo = _make_repo(tmp_path)
    region = _make_region(repo)

    write_context_cache(repo, {
        "R1": {
            "callees": ["parse_blocks", "review_panel", "check_static"],
            "callers": ["main", "cli.py"],
            "tests": ["test_loop.py::test_run_ticket"],
            "types": ["PanelResult", "Vote"],
        }
    })

    result = build_context_slice(repo, [region], GRAPH_PROFILE)
    assert "parse_blocks" in result, "context must include callees"
    assert "main" in result, "context must include callers"
    assert "test_loop" in result, "context must include tests"
    assert "PanelResult" in result, "context must include types"


def test_phase3_no_cache_returns_empty(tmp_path):
    """build_context_slice returns empty when no cache file exists."""
    repo = _make_repo(tmp_path)
    region = _make_region(repo)
    result = build_context_slice(repo, [region], GRAPH_PROFILE)
    assert result == "", "should return empty when no cache file exists"


def test_phase3_no_graph_project_returns_empty(tmp_path):
    """build_context_slice returns empty when graph_project is not set."""
    repo = _make_repo(tmp_path)
    region = _make_region(repo)
    result = build_context_slice(repo, [region], NO_GRAPH_PROFILE)
    assert result == ""


def test_phase3_token_budget_truncation(tmp_path):
    """build_context_slice respects the token budget by truncating.

    The per-region context already limits to 10 callees, 10 callers, 5 tests,
    5 types. The overall budget truncation kicks in when the total across
    all regions exceeds the budget. We test with a very small budget and
    multiple regions to trigger the overall truncation.
    """
    repo = _make_repo(tmp_path)

    # Write a cache with many regions so the total exceeds the small budget
    cache = {}
    for i in range(20):
        cache[f"R{i}"] = {
            "callees": [f"func_{j}" for j in range(10)],
            "callers": [f"caller_{j}" for j in range(10)],
            "tests": [f"test_{j}" for j in range(5)],
            "types": [f"Type_{j}" for j in range(5)],
        }
    write_context_cache(repo, cache)

    # Create 20 regions matching the cache
    from agent_loop.regions import Region
    path = repo / "src" / "target.py"
    regions_list = [Region(id=f"R{i}", file="src/target.py", path=path, anchor="x",
                    kind="line", start_line=0, end_line=0, text="x=1") for i in range(20)]

    small_budget_profile = Profile(
        name="test-small-budget",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        graph_project="test", context_token_budget=100,  # 100 tokens = ~400 chars
        implementer_rules="test", reviewer_priorities="test",
    )
    register(small_budget_profile)

    result = build_context_slice(repo, regions_list, small_budget_profile)
    assert len(result) <= 500, f"result should be truncated to ~400 chars, got {len(result)}"
    assert "truncated" in result, "truncated result should contain truncation marker"


def test_phase3_loop_injects_context(capsys, tmp_path):
    """The loop should inject graph context into the implementer prompt and print [graph]."""
    repo = _make_repo(tmp_path)
    region = _make_region(repo)

    # Write the cache to the MAIN repo (the loop reads from repo, not ws.root)
    write_context_cache(repo, {
        "R1": {
            "callees": ["parse_blocks", "review_panel"],
            "callers": ["main"],
            "tests": ["test_loop"],
            "types": ["PanelResult"],
        }
    })

    ticket = {"id": "P3-1", "title": "Context injection", "defect": "d", "spec": "s",
              "regions": [{"id": "R1", "file": "src/target.py", "anchor": "class TargetClass"}]}
    panel = PanelResult(votes=[Vote("r1", "APPROVE")], verdict="APPROVE", valid=True)

    captured_prompt = []
    original_impl = _impl_ok
    def capturing_impl(model, messages, **kw):
        for msg in messages:
            if msg.get("role") == "user":
                captured_prompt.append(msg["content"])
        return original_impl(model, messages, **kw)

    with patch("agent_loop.loop.chat", side_effect=capturing_impl):
        with patch("agent_loop.loop.regions.extract", return_value=[region]):
            with patch("agent_loop.loop.review_panel", return_value=panel):
                loop.run_ticket(repo, ticket, GRAPH_PROFILE, "test-impl", ["r1"],
                               max_rounds=1, apply=False, arbiter_model="")

    captured = capsys.readouterr()
    assert "[graph] injected" in captured.out, "loop should print [graph] injected message"
    assert len(captured_prompt) > 0, "implementer should have received a prompt"
    assert "Graph context" in captured_prompt[0], "prompt should contain graph context section"
    assert "parse_blocks" in captured_prompt[0], "prompt should contain callees from cache"