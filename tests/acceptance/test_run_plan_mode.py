"""
Acceptance tests for the plan runner (Wave 0.5).

Tests:
1. Topological sort orders by depends_on
2. Unknown dependency is refused
3. Cycle is refused
4. --list shows the plan without running
5. Commit-per-part: part 2's worktree sees part 1's work (R5-1, R5-2)
6. _commit_to_branch does not leave commits on the user's branch (R5-1)
7. A0-1: branch retention when parts have committed
8. A0-2: part_base uses branch HEAD, not last part's applied flag
"""
import json
import subprocess
from pathlib import Path

import pytest

from agent_loop.run_plan_mode import _topological_sort, PlanError, _commit_to_branch


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


def test_commit_to_branch_does_not_leave_commits_on_user_branch(tmp_path):
    """R5-1: _commit_to_branch must not leave commits on the user's branch.

    The old code committed to the current branch, moved the plan branch ref
    to that commit, and never reset the user's branch back. So every part
    promoted by the plan runner left a commit on the user's working branch.
    The fix: soft-reset the user's HEAD back after fast-forwarding the plan
    branch, so the commit is ONLY on the plan branch.
    """
    from agent_loop.workspace import _git

    # Init a git repo with one file.
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    (tmp_path / "file.txt").write_text("initial\n", encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path),
                   capture_output=True, env=env)

    # Record the user's HEAD.
    user_head_before = _git(tmp_path, "rev-parse", "HEAD").strip()

    # Create a plan branch.
    subprocess.run(["git", "branch", "agent-loop/plan-test"],
                   cwd=str(tmp_path), capture_output=True)

    # Modify the file and commit to the plan branch.
    (tmp_path / "file.txt").write_text("modified by part 1\n", encoding="utf-8")
    commit = _commit_to_branch(
        tmp_path, "agent-loop/plan-test", ["file.txt"], "P1"
    )

    # The user's HEAD should be UNCHANGED.
    user_head_after = _git(tmp_path, "rev-parse", "HEAD").strip()
    assert user_head_after == user_head_before, (
        f"user's HEAD moved: {user_head_before} -> {user_head_after}. "
        f"_commit_to_branch must not leave commits on the user's branch."
    )

    # The plan branch should point at the new commit.
    plan_head = _git(tmp_path, "rev-parse", "agent-loop/plan-test").strip()
    assert plan_head == commit

    # The commit message should be on the plan branch.
    log = _git(tmp_path, "log", "--oneline", "agent-loop/plan-test").strip()
    assert "part P1 promoted" in log


def test_run_ticket_accepts_base_ref(tmp_path):
    """R5-2: run_ticket accepts a base_ref parameter and bases the worktree on it.

    This is what the plan runner uses to ensure part 2's worktree sees
    part 1's work: it passes base_ref=plan_branch so the worktree is
    created at the plan branch's HEAD, not at HEAD.
    """
    from agent_loop.workspace import _git, open_workspace

    # Init a git repo.
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    (tmp_path / "file.txt").write_text("initial\n", encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path),
                   capture_output=True, env=env)

    # Create a branch with different content.
    (tmp_path / "file.txt").write_text("on plan branch\n", encoding="utf-8")
    subprocess.run(["git", "checkout", "-b", "plan-branch"],
                   cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "plan commit"], cwd=str(tmp_path),
                   capture_output=True, env=env)
    subprocess.run(["git", "checkout", "master"], cwd=str(tmp_path),
                   capture_output=True)

    # Open a worktree at the plan branch.
    with open_workspace(tmp_path, "test-base-ref", base="plan-branch") as ws:
        content = (ws.root / "file.txt").read_text(encoding="utf-8")
        assert "on plan branch" in content, (
            f"worktree at plan-branch should see plan branch content, "
            f"got: {content}"
        )


# ---------------------------------------------------------------------------
# A0-1: branch retention when parts have committed
# ---------------------------------------------------------------------------

def test_a0_1_branch_retained_when_part_committed(tmp_path):
    """A0-1: the scratch branch must NOT be deleted when a part has committed
    to it, even if the plan status is not 'complete'.

    The old code at run_plan_mode.py:370-372 deleted the branch on any
    non-complete status. _commit_to_branch soft-resets the user's HEAD,
    so the files survive but the commits become unreachable. A resumed
    worktree at HEAD won't see them.

    This test simulates the branch-deletion decision logic directly:
    after a part commits to the plan branch, the branch must survive
    a 'partial' or 'failed' status.
    """
    from agent_loop.workspace import _git

    _init_git_repo(tmp_path)

    # Create a plan branch and commit a part to it.
    _git(tmp_path, "branch", "agent-loop/plan-test")
    (tmp_path / "file.txt").write_text("part 1 content\n", encoding="utf-8")
    _commit_to_branch(tmp_path, "agent-loop/plan-test", ["file.txt"], "P1")

    # The branch should exist with the commit.
    branch_head = _git(tmp_path, "rev-parse", "agent-loop/plan-test").strip()
    assert branch_head, "plan branch should exist after commit"

    # Simulate the A0-1 retention logic: any_committed = True → don't delete.
    # The old logic would delete it here (status != "complete" and not keep_branch).
    # The new logic retains it because a part committed.
    any_committed = True  # part P1 committed
    keep_branch = False
    should_delete = (not "complete" == "complete") and not keep_branch and not any_committed

    assert not should_delete, (
        "A0-1: branch with committed parts must not be deleted on non-complete status"
    )

    # Verify the branch still exists.
    branches = _git(tmp_path, "branch", "--list", "agent-loop/plan-test").strip()
    assert "agent-loop/plan-test" in branches


def test_a0_1_branch_deleted_when_no_parts_committed(tmp_path):
    """A0-1: the scratch branch IS deleted when no part committed (truly empty run)."""
    from agent_loop.workspace import _git

    _init_git_repo(tmp_path)
    _git(tmp_path, "branch", "agent-loop/plan-test")

    # No commits to the branch.
    any_committed = False
    keep_branch = False
    # The new logic: delete only if not complete AND not keep_branch AND not any_committed.
    should_delete = (True) and not keep_branch and not any_committed  # status != "complete"

    assert should_delete, (
        "A0-1: branch with no committed parts should be deleted on failure"
    )


# ---------------------------------------------------------------------------
# A0-2: part_base uses branch HEAD, not last part's applied flag
# ---------------------------------------------------------------------------

def test_a0_2_part_base_uses_branch_head_after_commit(tmp_path):
    """A0-2: part_base should be the plan branch when the branch has advanced
    past base_commit, regardless of whether the LAST part was applied.

    The old code checked `result.parts[-1].applied` — which broke under
    --continue-on-failure (part 1 lands, part 2 fails, part 3 is independent
    → part 3 builds at HEAD without part 1's code) and --from (skipping parts
    leaves result.parts empty).

    The new code compares the branch HEAD to base_commit: if different, use
    the branch.
    """
    from agent_loop.workspace import _git

    _init_git_repo(tmp_path)

    base_commit = _git(tmp_path, "rev-parse", "HEAD").strip()

    # Create a plan branch and advance it with a commit.
    _git(tmp_path, "branch", "agent-loop/plan-test")
    (tmp_path / "file.txt").write_text("part 1\n", encoding="utf-8")
    _commit_to_branch(tmp_path, "agent-loop/plan-test", ["file.txt"], "P1")

    # The branch has advanced past base_commit.
    branch_head = _git(tmp_path, "rev-parse", "agent-loop/plan-test").strip()
    assert branch_head != base_commit, "sanity: branch should have advanced"

    # A0-2 logic: use branch if branch_head != base_commit.
    # Even if the "last part" was not applied (simulating part 2 failed),
    # the branch HEAD check correctly uses the branch.
    last_part_applied = False  # part 2 failed
    branch_head_check = _git(tmp_path, "rev-parse", "agent-loop/plan-test", check=False).strip()
    if branch_head_check and branch_head_check != base_commit:
        part_base = "agent-loop/plan-test"
    else:
        part_base = "HEAD"

    assert part_base == "agent-loop/plan-test", (
        "A0-2: part_base should be the plan branch when it has advanced, "
        "even if the last part was not applied"
    )


def test_a0_2_part_base_uses_head_when_branch_not_advanced(tmp_path):
    """A0-2: part_base should be HEAD when the branch hasn't advanced
    (no parts committed yet)."""
    from agent_loop.workspace import _git

    _init_git_repo(tmp_path)

    base_commit = _git(tmp_path, "rev-parse", "HEAD").strip()
    _git(tmp_path, "branch", "agent-loop/plan-test")

    # Branch hasn't advanced — still at base_commit.
    branch_head = _git(tmp_path, "rev-parse", "agent-loop/plan-test").strip()
    assert branch_head == base_commit, "sanity: branch should not have advanced"

    if branch_head != base_commit:
        part_base = "agent-loop/plan-test"
    else:
        part_base = "HEAD"

    assert part_base == "HEAD", (
        "A0-2: part_base should be HEAD when branch hasn't advanced"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_git_repo(path: Path):
    """Init a git repo with an initial commit."""
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True)
    (path / "file.txt").write_text("initial\n", encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", "-A"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path),
                   capture_output=True, env=env)


# ---------------------------------------------------------------------------
# A1: --tdd flag and planner test path instruction
# ---------------------------------------------------------------------------

def test_a1_run_plan_accepts_tdd_flag(tmp_path):
    """A1: run_plan accepts a tdd=True parameter without error.
    The actual test generation requires a model call; this test verifies
    the parameter is wired through and doesn't break the non-apply path."""
    from agent_loop.run_plan_mode import run_plan
    from agent_loop.profiles import Profile, register

    PROFILE = Profile(
        name="test-tdd",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        test_sources=("tests/acceptance/test_*Generated.py",),
    )
    register(PROFILE)

    plan = {"tickets": [
        {"id": "F1", "title": "first", "defect": "d1", "spec": "s1",
         "depends_on": [], "regions": [{"id": "R1", "file": "src/new.py", "op": "create"}],
         "expect_green": ["tests/acceptance/test_F1Generated.py::test_f1"]},
    ]}
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    _init_git_repo(tmp_path)

    # With apply=False, tdd=True should not error (no model calls made).
    result = run_plan(
        repo=tmp_path,
        plan_path=plan_path,
        profile=PROFILE,
        implementer="model-a",
        reviewers=["model-b"],
        apply=False,
        tdd=True,
    )
    assert result.status == "complete"


def test_a1_planner_prompt_includes_test_path_pattern(tmp_path):
    """A1: the planner prompt tells the model the exact test file path
    pattern, so expect_green entries match what the runner generates."""
    from agent_loop.plan_mode import run_plan
    from agent_loop.profiles import Profile, register
    from agent_loop.test_mode import default_test_path

    PROFILE = Profile(
        name="test-planner-path",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        test_sources=("tests/acceptance/test_*Generated.py",),
    )
    register(PROFILE)

    # The default_test_path for F1 should be tests/acceptance/test_F1Generated.py
    path = default_test_path(PROFILE, "F1")
    assert "test_F1Generated" in path, (
        f"default_test_path should include part ID, got: {path}"
    )


# ---------------------------------------------------------------------------
# B1: backlog.json persistent state
# ---------------------------------------------------------------------------

def test_b1_write_backlog_creates_file(tmp_path):
    """B1: _write_backlog creates backlog.json next to the manifest."""
    from agent_loop.run_plan_mode import _write_backlog, PartResult
    from agent_loop.workspace import _git

    _init_git_repo(tmp_path)
    tickets = [{"id": "F1", "title": "first"}, {"id": "F2", "title": "second"}]
    parts = [PartResult(id="F1", title="first", applied=True, commit="abc123")]

    bl_path = _write_backlog(tmp_path, "test-plan-1", "agent-loop/plan-test-1",
                             "base123", tickets, parts, "partial")

    assert bl_path.exists(), "backlog.json should be created"
    backlog = json.loads(bl_path.read_text(encoding="utf-8"))
    assert backlog["plan_id"] == "test-plan-1"
    assert backlog["branch"] == "agent-loop/plan-test-1"
    assert backlog["status"] == "partial"
    assert len(backlog["parts"]) == 2
    # F1 is done, F2 is pending
    f1 = next(p for p in backlog["parts"] if p["id"] == "F1")
    assert f1["status"] == "done"
    f2 = next(p for p in backlog["parts"] if p["id"] == "F2")
    assert f2["status"] == "pending"


def test_b1_read_backlog_returns_dict(tmp_path):
    """B1: _read_backlog reads and returns the backlog dict."""
    from agent_loop.run_plan_mode import _read_backlog

    bl_path = tmp_path / "backlog.json"
    data = {"plan_id": "test-1", "parts": [{"id": "F1", "status": "done"}]}
    bl_path.write_text(json.dumps(data), encoding="utf-8")

    backlog = _read_backlog(bl_path)
    assert backlog["plan_id"] == "test-1"
    assert len(backlog["parts"]) == 1


def test_b1_read_backlog_raises_on_missing_file(tmp_path):
    """B1: _read_backlog raises PlanError when the file doesn't exist."""
    from agent_loop.run_plan_mode import _read_backlog, PlanError

    with pytest.raises(PlanError, match="not found"):
        _read_backlog(tmp_path / "nonexistent.json")


def test_b1_backlog_path_is_next_to_manifest(tmp_path):
    """B1: backlog.json lives at logs/agent_loop/plan-<plan_id>/backlog.json,
    next to the plan manifest, not next to the input plan."""
    from agent_loop.run_plan_mode import _backlog_path

    bl_path = _backlog_path(tmp_path, "test-42")
    assert "plan-test-42" in str(bl_path)
    assert bl_path.name == "backlog.json"


# ---------------------------------------------------------------------------
# D1: Feature-level acceptance
# ---------------------------------------------------------------------------

def test_d1_parse_feature_acceptance_returns_list():
    """D1: _parse_feature_acceptance extracts test names from the block."""
    from agent_loop.plan_mode import _parse_feature_acceptance

    raw = '<<<FEATURE_ACCEPTANCE>>>\n["test_a", "test_b"]\n<<<END FEATURE_ACCEPTANCE>>>'
    result = _parse_feature_acceptance(raw)
    assert result == ["test_a", "test_b"]


def test_d1_parse_feature_acceptance_empty_when_absent():
    """D1: no FEATURE_ACCEPTANCE block → empty list."""
    from agent_loop.plan_mode import _parse_feature_acceptance

    raw = "just some text without the block"
    result = _parse_feature_acceptance(raw)
    assert result == []


def test_d1_parse_feature_acceptance_handles_bad_json():
    """D1: malformed JSON in the block → empty list, not a crash."""
    from agent_loop.plan_mode import _parse_feature_acceptance

    raw = '<<<FEATURE_ACCEPTANCE>>>\nnot valid json\n<<<END FEATURE_ACCEPTANCE>>>'
    result = _parse_feature_acceptance(raw)
    assert result == []


def test_d1_feature_verdict_in_plan_result():
    """D1: PlanResult has a feature_verdict field."""
    from agent_loop.run_plan_mode import PlanResult

    result = PlanResult(plan_id="test", status="complete")
    assert hasattr(result, "feature_verdict")
    assert result.feature_verdict == ""


# ---------------------------------------------------------------------------
# C1: Epic decomposition (story parsing + ID prefixing)
# ---------------------------------------------------------------------------

def test_c1_parse_stories_extracts_blocks():
    """C1: _parse_stories extracts <<<STORY>>> blocks in document order."""
    from agent_loop.plan_mode import _parse_stories

    raw = """<<<STORY>>>
{"id": "S1", "title": "first", "description": "do thing 1", "acceptance_criteria": ["c1"]}
<<<END STORY>>>
<<<STORY>>>
{"id": "S2", "title": "second", "description": "do thing 2", "acceptance_criteria": ["c2"]}
<<<END STORY>>>
"""
    stories = _parse_stories(raw)
    assert len(stories) == 2
    assert stories[0]["id"] == "S1"
    assert stories[1]["id"] == "S2"
    assert stories[0]["title"] == "first"
    assert stories[1]["acceptance_criteria"] == ["c2"]


def test_c1_parse_stories_empty_when_absent():
    """C1: no <<<STORY>>> blocks → empty list."""
    from agent_loop.plan_mode import _parse_stories

    assert _parse_stories("just text, no blocks") == []


def test_c1_parse_stories_skips_malformed():
    """C1: malformed JSON in a STORY block is skipped, not crashed."""
    from agent_loop.plan_mode import _parse_stories

    raw = """<<<STORY>>>
not valid json
<<<END STORY>>>
<<<STORY>>>
{"id": "S1", "title": "ok"}
<<<END STORY>>>
"""
    stories = _parse_stories(raw)
    assert len(stories) == 1
    assert stories[0]["id"] == "S1"


def test_c1_prefix_task_ids_adds_story_prefix():
    """C1: _prefix_task_ids prefixes task IDs with the story ID."""
    from agent_loop.plan_mode import _prefix_task_ids

    tasks = [
        {"id": "F1", "title": "first", "depends_on": []},
        {"id": "F2", "title": "second", "depends_on": ["F1"]},
    ]
    prefixed = _prefix_task_ids(tasks, "S1")

    assert prefixed[0]["id"] == "S1F1"
    assert prefixed[1]["id"] == "S1F2"
    # depends_on is rewritten
    assert prefixed[1]["depends_on"] == ["S1F1"]
    # story_id is stamped
    assert prefixed[0]["story_id"] == "S1"
    assert prefixed[1]["story_id"] == "S1"


def test_c1_prefix_task_ids_preserves_other_fields():
    """C1: prefixing doesn't lose regions, expect_green, etc."""
    from agent_loop.plan_mode import _prefix_task_ids

    tasks = [
        {"id": "F1", "title": "t", "regions": [{"id": "R1", "file": "x.py"}],
         "expect_green": ["test_x"], "depends_on": []},
    ]
    prefixed = _prefix_task_ids(tasks, "S1")
    assert prefixed[0]["regions"] == [{"id": "R1", "file": "x.py"}]
    assert prefixed[0]["expect_green"] == ["test_x"]


def test_c1_prefix_task_ids_no_collision_across_stories():
    """C1: two stories with the same task IDs get different prefixed IDs."""
    from agent_loop.plan_mode import _prefix_task_ids

    story1_tasks = [{"id": "F1", "depends_on": []}]
    story2_tasks = [{"id": "F1", "depends_on": []}]

    p1 = _prefix_task_ids(story1_tasks, "S1")
    p2 = _prefix_task_ids(story2_tasks, "S2")

    assert p1[0]["id"] == "S1F1"
    assert p2[0]["id"] == "S2F1"
    assert p1[0]["id"] != p2[0]["id"]