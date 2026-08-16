"""
Acceptance test for T-OPS3: revert() prunes untracked files.

When a candidate creates a new file (op=create) and then fails a gate, the
revert must remove the orphaned file. The old revert only did `git checkout
--`, which restores tracked files but leaves untracked files in place.
"""
import subprocess
from pathlib import Path

from agent_loop.workspace import open_workspace, Workspace


def _init_repo(tmp_path):
    """Create a minimal git repo."""
    repo = tmp_path
    (repo / "existing.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, env=env)
    return repo


def test_revert_removes_untracked_files(tmp_path):
    """revert() removes untracked files created during the run."""
    repo = _init_repo(tmp_path)
    with open_workspace(repo, "T-OPS3") as ws:
        # Create a new file in the worktree (simulates op=create).
        new_file = ws.root / "new_module.py"
        new_file.write_text("def new():\n    pass\n", encoding="utf-8")
        ws.stage_new_files(["new_module.py"])
        assert new_file.exists(), "file should exist after create"

        # Revert -- should remove the untracked file.
        ws.revert(["new_module.py"])
        assert not new_file.exists(), "untracked file should be removed by revert"


def test_revert_restores_tracked_files(tmp_path):
    """revert() still restores tracked files to their HEAD version."""
    repo = _init_repo(tmp_path)
    with open_workspace(repo, "T-OPS3b") as ws:
        # Modify an existing tracked file.
        existing = ws.root / "existing.py"
        original = existing.read_text(encoding="utf-8")
        existing.write_text("x = 2\n", encoding="utf-8")
        assert existing.read_text(encoding="utf-8") != original

        # Revert -- should restore the original.
        ws.revert(["existing.py"])
        assert existing.read_text(encoding="utf-8") == original, "tracked file should be restored"


def test_revert_handles_both_tracked_and_untracked(tmp_path):
    """revert() handles a mix of tracked and untracked files."""
    repo = _init_repo(tmp_path)
    with open_workspace(repo, "T-OPS3c") as ws:
        # Modify existing + create new.
        existing = ws.root / "existing.py"
        original = existing.read_text(encoding="utf-8")
        existing.write_text("x = 2\n", encoding="utf-8")
        new_file = ws.root / "new_module.py"
        new_file.write_text("pass\n", encoding="utf-8")
        ws.stage_new_files(["new_module.py"])

        # Revert both.
        ws.revert(["existing.py", "new_module.py"])
        assert existing.read_text(encoding="utf-8") == original
        assert not new_file.exists()