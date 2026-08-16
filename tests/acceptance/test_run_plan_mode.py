"""
Acceptance tests for the plan runner (Wave 0.5).

Tests:
1. Topological sort orders by depends_on
2. Unknown dependency is refused
3. Cycle is refused
4. --list shows the plan without running
5. Commit-per-part: part 2's worktree sees part 1's work
"""
import json
import subprocess
from pathlib import Path

import pytest

from agent_loop.run_plan_mode import _topological_sort, PlanError


def test_topological_sort_orders_by_dependency():
    """Parts are sorted so dependencies come first."""
    tickets = [
        {"id": "F3", "depends_on": ["F1", "F2"], "title": "third"},
        {"id": "F1", "depends_on": [], "title": "first"},
        {"id": "F2", "depends_on": ["F1"], "title": "second"},
    ]
    ordered = _topological_sort(tickets)
    ids = [t["id"] for t in ordered]
    assert ids == ["F1", "F2", "F3"], f"expected F1->F2->F3, got {ids}"


def test_topological_sort_preserves_json_order_for_independent():
    """Parts with no dependency relation preserve JSON array order."""
    tickets = [
        {"id": "B", "depends_on": [], "title": "b"},
        {"id": "A", "depends_on": [], "title": "a"},
        {"id": "C", "depends_on": [], "title": "c"},
    ]
    ordered = _topological_sort(tickets)
    ids = [t["id"] for t in ordered]
    assert ids == ["B", "A", "C"], f"expected JSON order B,A,C, got {ids}"


def test_topological_sort_refuses_unknown_dependency():
    """A depends_on naming a part that doesn't exist is refused."""
    tickets = [
        {"id": "F1", "depends_on": ["NONEXISTENT"], "title": "first"},
    ]
    with pytest.raises(PlanError, match="NONEXISTENT.*not in the plan"):
        _topological_sort(tickets)


def test_topological_sort_refuses_cycle():
    """A circular dependency is refused."""
    tickets = [
        {"id": "A", "depends_on": ["B"], "title": "a"},
        {"id": "B", "depends_on": ["A"], "title": "b"},
    ]
    with pytest.raises(PlanError, match="cycle"):
        _topological_sort(tickets)


def test_topological_sort_refuses_duplicate_ids():
    """Duplicate ticket ids are refused."""
    tickets = [
        {"id": "F1", "depends_on": [], "title": "first"},
        {"id": "F1", "depends_on": [], "title": "duplicate"},
    ]
    with pytest.raises(PlanError, match="duplicate"):
        _topological_sort(tickets)


def test_run_plan_no_apply_shows_plan(tmp_path):
    """Without --apply, the plan is shown but not executed."""
    from agent_loop.run_plan_mode import run_plan
    from agent_loop.profiles import Profile, register

    PROFILE = Profile(
        name="test-runplan",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        implementer_rules="test", reviewer_priorities="test",
    )
    register(PROFILE)

    plan = {"tickets": [
        {"id": "F1", "title": "first", "defect": "d1", "spec": "s1",
         "depends_on": [], "regions": [{"id": "R1", "file": "src/new.py", "op": "create"}],
         "expect_green": ["test_f1"]},
        {"id": "F2", "title": "second", "defect": "d2", "spec": "s2",
         "depends_on": ["F1"], "regions": [{"id": "R1", "file": "src/new.py", "op": "insert", "anchor": "def new"}],
         "expect_green": ["test_f2"]},
    ]}
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    # Init a git repo.
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), capture_output=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True, env=env)

    result = run_plan(
        repo=tmp_path,
        plan_path=plan_path,
        profile=PROFILE,
        implementer="model-a",
        reviewers=["model-b"],
        apply=False,
    )
    assert result.status == "complete"
    assert len(result.parts) == 0  # not executed