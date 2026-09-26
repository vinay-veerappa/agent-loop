"""
CF-34: a FAILED `git submodule update` must leave the worktree as clean as
`git worktree add` made it.

Measured in tvDownloadOHLC 2026-09-26: `third_party/PineTS` pins a commit its
remote no longer serves, so `submodule update --init --recursive` aborts
partway. Every submodule it had already cloned is left at a commit other than
the recorded gitlink, `git status --porcelain` lists each as ` M`, and
`capture_baseline` then refuses with "refusing to capture a test baseline from
a dirty worktree" -- for EVERY ticket in that repo, while the only thing
printed was "WARNING: submodule update failed; submodule-dependent tests may
be dark". The warning described a recoverable state; the tree was not in it.
"""
import os
import subprocess
from pathlib import Path

from agent_loop.workspace import open_workspace

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    # Local-path submodules are refused by default since git 2.38.1.
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "protocol.file.allow",
    "GIT_CONFIG_VALUE_0": "always",
}


def _git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=_ENV)
    assert p.returncode == 0, f"git {' '.join(args)}: {p.stderr}"
    return p.stdout


def _repo_with_unfetchable_submodule(tmp_path: Path) -> Path:
    sub = tmp_path / "sub"
    sub.mkdir()
    _git(sub, "init", "-q")
    (sub / "a.txt").write_text("a\n", encoding="utf-8")
    _git(sub, "add", "-A")
    _git(sub, "commit", "-q", "-m", "sub")
    old = _git(sub, "rev-parse", "HEAD").strip()
    (sub / "a.txt").write_text("b\n", encoding="utf-8")
    _git(sub, "commit", "-q", "-am", "sub2")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "x.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "x.py")
    _git(repo, "submodule", "add", "-q", str(sub), "good")
    _git(repo, "submodule", "add", "-q", str(sub), "zbad")
    # Cloned before the failure, never checked out after it.
    _git(repo, "submodule", "add", "-q", str(sub), "zlater")
    # Pin `zbad` at a commit its remote does not have -- the PineTS shape.
    _git(repo, "update-index", "--cacheinfo", "160000,1111111111111111111111111111111111111111,zbad")
    # Pinned BEHIND its remote's HEAD, as every real pin eventually is: a clone
    # left unchecked-out sits at HEAD, which is not the gitlink.
    _git(repo, "update-index", "--cacheinfo", f"160000,{old},zlater")
    _git(repo, "update-index", "--cacheinfo", f"160000,{old},good")
    # No `add -A` here: it would re-stage each submodule's checked-out HEAD
    # over the gitlinks just written.
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def test_failed_submodule_update_leaves_a_clean_worktree(tmp_path, monkeypatch):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    repo = _repo_with_unfetchable_submodule(tmp_path)
    with open_workspace(repo, "CF34", workdir=tmp_path) as ws:
        assert ws.dirty_files() == [], ws.dirty_files()
        assert (ws.root / "x.py").exists()
