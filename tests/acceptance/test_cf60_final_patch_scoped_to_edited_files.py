"""CF-60: `final.patch` carries tracked `.pyc` binaries.

Measured on tvDownloadOHLC T43, 2026-09-28. The consumer repo tracks some
`__pycache__/*.pyc` files. Running the test gate in the worktree rewrote
them, and `export_patch`'s unrestricted `git diff` picked them up as binary
hunks with no full index line. `git apply logs/agent_loop/T43/final.patch`
then failed outright ("cannot apply binary patch ... without full index
line"), and only `git apply --include=<region file>` applied the patch.

Wanted: the candidate patch contains only the files the implementer edited
-- the ticket's region files (Region.file already names a `create` region's
file too), never other files the test run happened to modify in the
worktree.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from agent_loop import workspace


def _git_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "target.py").write_text("def f():\n    return 42\n", encoding="utf-8")
    # A tracked "compiled artifact" -- stands in for a tracked __pycache__/*.pyc
    # the way the consumer repo actually has one. Binary content matters: it
    # is what makes git diff emit a hunk with no full index line.
    (repo / "src" / "cached.bin").write_bytes(b"\x00\x01original\xff")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo, check=True,
    )
    return repo


def test_final_patch_excludes_a_tracked_file_the_test_run_rewrote(tmp_path):
    repo = _git_repo(tmp_path)
    with workspace.open_workspace(repo, "CF60A") as ws:
        # The implementer's own edit -- this is what a region-based apply
        # would have produced.
        (ws.root / "src" / "target.py").write_text(
            "def f():\n    return 43\n", encoding="utf-8"
        )
        # Something OTHER than the implementer rewrote a tracked binary file
        # in the worktree -- the CF-60 shape (the gate's own pytest run
        # rewriting a tracked __pycache__/*.pyc).
        (ws.root / "src" / "cached.bin").write_bytes(b"\x00\x01rewritten\xff")

        dest = ws.root.parent / "final.patch"
        patch = ws.export_patch(dest, paths=["src/target.py"])

    assert patch is not None
    text = patch.read_text(encoding="utf-8", errors="replace")
    assert "target.py" in text
    assert "cached.bin" not in text, (
        "final.patch must not carry a file the implementer did not edit:\n" + text
    )


def test_without_the_restriction_the_rewritten_file_rides_along(tmp_path):
    """Negative control: proves the scenario really does leak the unrelated
    file when export_patch is NOT given the edited-files list -- i.e. that
    the assertion above is discriminating, not vacuously true."""
    repo = _git_repo(tmp_path)
    with workspace.open_workspace(repo, "CF60B") as ws:
        (ws.root / "src" / "target.py").write_text(
            "def f():\n    return 43\n", encoding="utf-8"
        )
        (ws.root / "src" / "cached.bin").write_bytes(b"\x00\x01rewritten\xff")

        dest = ws.root.parent / "final.patch"
        patch = ws.export_patch(dest)  # unrestricted, as before the fix

    assert patch is not None
    text = patch.read_text(encoding="utf-8", errors="replace")
    assert "target.py" in text
    assert "cached.bin" in text
