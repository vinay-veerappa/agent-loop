"""
Acceptance test for Phase 2: graph freshness check.

The loop must check graph freshness at startup and report the status.
When the profile has a graph_project set, the loop should print a
[graph] status line. When graph_project is empty, it should skip
silently (status "no-project").
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop.profiles import Profile, register
from agent_loop import loop
from agent_loop.context import check_graph_freshness
from agent_loop.loop import PanelResult, Vote


# Profile with a graph_project set
GRAPH_PROFILE = Profile(
    name="test-graph",
    language="python",
    file_suffixes=(".py",),
    line_comment="#",
    block_comment=(),
    block_kind="indent",
    graph_project="test-graph-project",
    implementer_rules="test",
    reviewer_priorities="test",
)
register(GRAPH_PROFILE)

# Profile without a graph_project
NO_GRAPH_PROFILE = Profile(
    name="test-no-graph",
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


def test_phase2_graph_freshness_returns_status():
    """check_graph_freshness returns a status string, not an exception."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        (repo / "src").mkdir()
        (repo / "src" / "target.py").write_text("x = 1\n", encoding="utf-8")

        status = check_graph_freshness(repo, GRAPH_PROFILE)
        assert isinstance(status, str)
        assert status != "no-project"  # GRAPH_PROFILE has graph_project set


def test_phase2_no_graph_project_returns_no_project():
    """When profile has no graph_project, check_graph_freshness returns 'no-project'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        status = check_graph_freshness(repo, NO_GRAPH_PROFILE)
        assert status == "no-project"


def test_phase2_graph_freshness_runs_in_loop(capsys, tmp_path):
    """The loop should print a [graph] status line when graph_project is set."""
    import os
    from contextlib import contextmanager
    from unittest.mock import patch
    from agent_loop.providers import Completion
    from agent_loop import regions

    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    (src / "target.py").write_text("class TargetClass:\n    def method(self):\n        return 42\n", encoding="utf-8")
    os.system(f'cd /d "{repo}" && git init && git add -A && git commit -m init')

    ticket = {"id": "P2-1", "title": "Graph freshness", "defect": "d", "spec": "s",
              "regions": [{"id": "R1", "file": "src/target.py", "anchor": "class TargetClass"}]}

    path = repo / "src" / "target.py"
    region = regions.Region(id="R1", file="src/target.py", path=path, anchor="class TargetClass",
                            kind="decl", start_line=0, end_line=2, text=path.read_text())

    def impl_ok(model, messages, **kw):
        for msg in messages:
            if msg.get("role") == "user":
                for line in msg["content"].split("\n"):
                    if line.startswith('### REGION id='):
                        rid = line.split('id="')[1].split('"')[0]
                        return Completion(
                            text=f'<<<BLOCK id="{rid}">>>\nclass TargetClass:\n    def method(self):\n        return 42\n<<<END id="{rid}">>>\n<<<NOTES>>>\n- fixed\n<<<END NOTES>>>',
                            model=model, input_tokens=100, output_tokens=50)
        return Completion(text='<<<BLOCK id="R1">>>\nclass TargetClass:\n    def method(self):\n        return 42\n<<<END id="R1">>>', model=model)

    panel = PanelResult(votes=[Vote("r1", "APPROVE")], verdict="APPROVE", valid=True)

    with patch("agent_loop.loop.chat", side_effect=impl_ok):
        with patch("agent_loop.loop.regions.extract", return_value=[region]):
            with patch("agent_loop.loop.review_panel", return_value=panel):
                loop.run_ticket(repo, ticket, GRAPH_PROFILE, "test-impl", ["r1"],
                               max_rounds=1, apply=False, arbiter_model="")

    captured = capsys.readouterr()
    assert "[graph]" in captured.out, \
        "loop should print [graph] status line when graph_project is set"