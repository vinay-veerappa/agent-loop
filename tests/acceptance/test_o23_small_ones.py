"""
O23: four small ones, recorded in session 3 and left open.

Two were real, one was half-right, and one was a documentation error. Keeping
them together because they were filed together.

1. `--keep-worktree` never reached developer mode: `run_developer` had no such
   parameter, so the flag was accepted and silently dropped. The runs most worth
   a post-mortem are exactly the ones whose worktree was deleted.

2. `cli._developer` returned 0 only for `verdict == "DONE"`. The driver itself
   already knew better -- its `apply` check at driver.py:544 uses
   ("APPROVE", "ARBITER_SHIP", "DONE") -- so a run could apply its patch and
   report failure to CI in the same breath. The same predicate written twice,
   once wrong. O23 named only ARBITER_SHIP; a unanimous panel APPROVE was
   affected too.

3. "Developer mode's worktree is a sibling directory that `--prune` may not
   find" -- HALF WRONG, and the correction matters because it is the difference
   between a cleanup command that works and one nobody trusts. Every mode's
   worktree is a sibling of the repo (`repo.parent / f"agentloop-{ticket}-{pid}"`),
   not just developer mode's, and `list_stale` matches on the `agentloop-` name
   via `git worktree list`, so `--prune` finds all of them. HANDOVER §6 trap 7
   was the thing that was wrong.

4. Implementer ROLE budget 96000 vs developer MODE budget 48000 -- documentation,
   not code. See config.py.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop import cli, workspace
from agent_loop.profiles import Profile, register


PROFILE = Profile(
    name="test-o23",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    implementer_rules="t", reviewer_priorities="t",
)
register(PROFILE)

BASE = ["--mode", "developer", "--profile", "test-o23", "--defect", "d"]


def _run(argv, result):
    captured = {}

    def fake(repo, defect, profile, implementer, reviewers, **kwargs):
        captured.update(kwargs)
        return result

    with patch("agent_loop.developer.driver.run_developer", fake):
        code = cli.main(argv)
    return code, captured


# --------------------------------------------------------------------------
# 1. --keep-worktree reaches developer mode
# --------------------------------------------------------------------------
def test_keep_worktree_reaches_developer_mode():
    _, kwargs = _run(BASE + ["--keep-worktree"], {"verdict": "DONE"})
    assert kwargs.get("keep_worktree") is True, (
        "the flag was accepted and dropped; the runs worth a post-mortem are the "
        "ones whose worktree gets deleted"
    )


def test_keep_worktree_defaults_off():
    _, kwargs = _run(BASE, {"verdict": "DONE"})
    assert kwargs.get("keep_worktree") is False


def test_open_workspace_keeps_the_worktree_when_the_body_raises(tmp_path):
    """The post-mortem case: `keep` must survive an exception, not just a clean
    exit. (`open_workspace` already did this -- pinned so it stays true.)"""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
        cwd=repo, check=True,
    )

    root = None
    with pytest.raises(RuntimeError):
        with workspace.open_workspace(repo, "KEEPTEST", keep=True) as ws:
            root = ws.root
            raise RuntimeError("boom")
    assert root is not None and root.exists(), "the worktree was removed on the error path"
    workspace.prune(repo, str(root))


def test_prune_finds_a_sibling_worktree(tmp_path):
    """Corrects O23's own claim. Worktrees are siblings of the repo for EVERY
    mode, and `list_stale` matches the `agentloop-` name through
    `git worktree list`, so `--prune` does find them."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
        cwd=repo, check=True,
    )

    with workspace.open_workspace(repo, "DEV", keep=True) as ws:
        root = ws.root
    assert root.parent == repo.parent, "worktrees are siblings of the repo"
    # Compared as Paths: `git worktree list` reports forward slashes even on
    # Windows, so a string compare fails here while `--prune` works fine -- git
    # accepts its own format back. The path FORM is git's business; the only
    # claim under test is that the sibling worktree is visible at all.
    assert Path(root) in [Path(p) for p in workspace.list_stale(repo)], (
        "--prune could not see it"
    )
    workspace.prune(repo, str(root))
    assert not root.exists()


# --------------------------------------------------------------------------
# 2. the exit code agrees with the driver's own definition of success
# --------------------------------------------------------------------------
@pytest.mark.parametrize("verdict", ["DONE", "APPROVE", "ARBITER_SHIP"])
def test_a_promotable_developer_verdict_exits_zero(verdict):
    code, _ = _run(BASE, {"verdict": verdict, "patch": "p.diff"})
    assert code == 0, f"{verdict} produced a candidate but reported failure to CI"


@pytest.mark.parametrize(
    "verdict",
    ["MAX_TURNS_EXHAUSTED", "ESCALATED", "PANEL_UNREACHABLE", "NO_FAILING_TEST",
     "BUILD_FAILED", "TEST_FAILED", "IMPLEMENTER_UNREACHABLE"],
)
def test_a_failed_developer_verdict_exits_nonzero(verdict):
    code, _ = _run(BASE, {"verdict": verdict})
    assert code == 1, f"{verdict} is not a success"


def test_the_success_sets_are_defined_once():
    """The bug was the same predicate written twice, so the constant is the fix.
    Developer mode adds DONE (no reviewers means no panel to approve) and cannot
    produce APPROVE_PARTIAL.

    Imported in the body, not at module scope: a module-level import of a name
    that does not exist yet is a collection ERROR, which makes capture_baseline
    refuse and takes every ticket on the profile down with it (HANDOVER §6.2)."""
    from agent_loop.loop import DEVELOPER_PROMOTABLE, PROMOTABLE

    assert "DONE" in DEVELOPER_PROMOTABLE
    assert "DONE" not in PROMOTABLE
    assert set(PROMOTABLE) - {"APPROVE_PARTIAL"} <= set(DEVELOPER_PROMOTABLE)


def test_run_developer_actually_honours_keep_worktree(tmp_path):
    """Forwarding the flag is half the fix. Deleting `keep=keep_worktree` from
    the driver's own open_workspace call left every other test in this file
    green, because they all stub run_developer and only prove the flag ARRIVES.
    This one runs the real thing and looks on disk."""
    import os

    from agent_loop.developer.driver import run_developer
    from agent_loop.providers import Completion

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "target.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    os.system(f'cd /d "{repo}" && git init && git add -A && git commit -m init')

    prof = Profile(
        name="test-o23-keep",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        file_scope_whitelist=("src/",), protected=("tests/*",),
        implementer_rules="t", reviewer_priorities="t",
    )
    register(prof)

    def impl(model, messages, **kw):
        return Completion(text="<<<ESCALATE>>>\nno\n<<<END ESCALATE>>>", model=model)

    with patch("agent_loop.developer.driver.chat", side_effect=impl):
        run_developer(
            repo, "a defect", prof, "impl", [], arbiter_model="",
            max_turns=1, apply=False, keep_worktree=True,
        )

    kept = sorted(tmp_path.glob("agentloop-DEV-*"))
    assert kept, f"the worktree was deleted despite keep_worktree=True: {list(tmp_path.iterdir())}"
    for k in kept:
        workspace.prune(repo, str(k))


# --------------------------------------------------------------------------
# 3b. an ORPHANED worktree directory: on disk, unknown to git
# --------------------------------------------------------------------------
#
# Found live while verifying item 3. After a real test-mode run,
# `agentloop-REVIEW_MODE_FINDINGS_LOG-testgen-42184` was still on disk and
# `--prune` said "pruned 0 worktree(s)". The directory was EMPTY and unregistered:
# `git worktree remove --force` had deleted the contents but could not remove the
# directory itself (Windows was holding the handle), and the `git worktree prune`
# that follows then dropped the registration. `list_stale` asks git, so from that
# moment nothing could see it.
#
# It matters because `open_workspace` REFUSES to start when the path already
# exists, the path carries the pid, and Windows recycles pids -- so a later run
# of the same ticket can fail with "worktree path already exists" and `--prune`
# will not fix it.
def _bare_repo(tmp_path, name="repo"):
    import subprocess

    repo = tmp_path / name
    repo.mkdir()
    (repo / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
        cwd=repo, check=True,
    )
    return repo


def test_an_empty_orphaned_worktree_directory_is_found_and_removed(tmp_path):
    repo = _bare_repo(tmp_path)
    orphan = tmp_path / "agentloop-GHOST-1234"
    orphan.mkdir()

    assert Path(orphan) in [Path(p) for p in workspace.list_stale(repo)], (
        "an orphan git no longer tracks was invisible to --prune"
    )
    workspace.prune(repo, str(orphan))
    assert not orphan.exists(), "the orphan survived prune"


def test_a_NON_empty_orphan_is_reported_but_not_deleted(tmp_path, capsys):
    """Conservative on purpose. An unregistered directory with contents may be a
    crashed run worth a post-mortem, and prune must not be the thing that
    destroys the evidence it exists to help you read.

    The MESSAGE is asserted, not just the survival, and that distinction was
    found by mutation: `Path.rmdir()` refuses a non-empty directory by itself, so
    deleting the explicit emptiness check leaves the directory intact either way
    and the survival assertion alone cannot tell the two apart. rmdir is the
    safety; the check is what makes the operator's message true instead of
    "could not remove empty worktree directory ... the directory is not empty".
    """
    repo = _bare_repo(tmp_path)
    orphan = tmp_path / "agentloop-GHOST-5678"
    orphan.mkdir()
    (orphan / "something.py").write_text("work in progress", encoding="utf-8")

    assert Path(orphan) in [Path(p) for p in workspace.list_stale(repo)]
    workspace.prune(repo, str(orphan))
    assert orphan.exists(), "prune deleted a non-empty orphan"
    assert (orphan / "something.py").exists()

    out = capsys.readouterr().out
    assert "NOT empty" in out, out
    assert "by hand" in out, "tell the operator what to do about it"
    assert "could not remove empty" not in out, (
        "reported a non-empty directory as an empty one it failed to remove"
    )


def test_a_live_worktree_of_another_run_is_not_treated_as_an_orphan(tmp_path):
    """The scan must not delete a worktree a concurrent run is using. A live one
    is registered with git, so it goes down the normal path."""
    repo = _bare_repo(tmp_path)
    with workspace.open_workspace(repo, "LIVE", keep=True) as ws:
        root = ws.root
        assert root.exists()
        # Registered, so visible -- and still on disk while the run holds it.
        assert Path(root) in [Path(p) for p in workspace.list_stale(repo)]
        assert root.exists()
    workspace.prune(repo, str(root))


def test_an_unrelated_sibling_directory_is_left_alone(tmp_path):
    repo = _bare_repo(tmp_path)
    other = tmp_path / "my-other-project"
    other.mkdir()
    assert Path(other) not in [Path(p) for p in workspace.list_stale(repo)]
