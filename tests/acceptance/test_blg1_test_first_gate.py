"""
Acceptance test for T-BLG1 (O64): test-first gate distinguishes causes.

TICKET_REJECTED had three causes and one message. Now:
- A feature ticket (op=create) where the suite doesn't build gets a message
  explaining the scaffold-first workaround.
- A defect ticket where the suite doesn't build gets a message saying the
  suite is broken, not "the test passes without the fix."
"""
import subprocess
from pathlib import Path

from agent_loop.loop import run_ticket
from agent_loop.profiles import Profile, register


PROFILE = Profile(
    name="test-blg1",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    test_cmd="python -m pytest tests/ -q",
    implementer_rules="test", reviewer_priorities="test",
)
register(PROFILE)


def _init_git_repo(tmp_path):
    """Create a minimal git repo with a broken test suite."""
    repo = tmp_path
    (repo / "tests").mkdir()
    # A test file with a syntax error so the suite doesn't compile.
    (repo / "tests" / "test_broken.py").write_text("def test_broken(:\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, env=env)
    return repo


def test_blg1_feature_ticket_gets_scaffold_message(tmp_path):
    """A feature ticket where the suite doesn't build gets the scaffold message."""
    repo = _init_git_repo(tmp_path)
    ticket = {
        "id": "T-FEATURE",
        "title": "new feature",
        "defect": "missing feature",
        "spec": "add a new function",
        "kind": "feature",
        "regions": [
            {"id": "R1", "file": "src/new_module.py", "op": "create"}
        ],
        "expect_green": ["test_new_feature"],
    }
    result = run_ticket(
        repo=repo, ticket=ticket, profile=PROFILE,
        implementer="model-a", reviewers=["model-b"], arbiter_model="model-c",
        max_rounds=1,
    )
    assert result["final_verdict"] == "TICKET_REJECTED"
    assert "scaffold" in result["detail"].lower() or "stub" in result["detail"].lower()
    assert "op=create" in result["detail"] or "feature" in result["detail"].lower()


def test_blg1_defect_ticket_gets_broken_suite_message(tmp_path):
    """A defect ticket where the suite doesn't build gets the broken-suite message."""
    repo = _init_git_repo(tmp_path)
    ticket = {
        "id": "T-DEFECT",
        "title": "fix bug",
        "defect": "a bug",
        "spec": "fix it",
        "regions": [
            {"id": "R1", "file": "src/existing.py", "anchor": "def existing"}
        ],
        "expect_green": ["test_existing"],
    }
    result = run_ticket(
        repo=repo, ticket=ticket, profile=PROFILE,
        implementer="model-a", reviewers=["model-b"], arbiter_model="model-c",
        max_rounds=1,
    )
    assert result["final_verdict"] == "TICKET_REJECTED"
    # Should NOT mention scaffold/stub -- this is a defect, not a feature.
    assert "scaffold" not in result["detail"].lower()
    assert "stub" not in result["detail"].lower()
    assert "broken" in result["detail"].lower() or "does not" in result["detail"].lower()