"""
O1: two tickets that touch one file must both be able to land.

`Workspace.promote` is a `shutil.copy2` per file, not a patch application. Each
ticket's patch is produced in its own worktree from the same base, so a whole-file
copy carries only that ticket's change. F4 and F5 both edited
`src/agent_loop/report.py`; promoting both in either order meant the second copy
reverted the first. The dirty-target guard downgrades that from silent data loss
to a `WorkspaceError`, which is the right failure -- but the capability is still
missing, and the F1-F6 patches had to be landed with `git apply` by hand.

What promotion must do instead: apply the worktree's diff for the named files,
so non-overlapping changes to one file compose. Three properties, in priority
order:

  1. non-overlapping edits to the same file COMPOSE (the missing capability);
  2. genuinely overlapping edits still REFUSE, with the file left intact --
     never half-applied;
  3. a human's uncommitted work is still never destroyed, and `force=True` still
     means "overwrite anyway" (both already covered in test_defect_regressions.py
     and preserved here so a fix cannot trade one guarantee for the other).
"""
import subprocess
from pathlib import Path

import pytest

from agent_loop import workspace

# The two edit sites must be further apart than git's default 3 lines of hunk
# context, or they are not "non-overlapping" in patch terms at all: each hunk
# would carry the other's lines as context and could never apply on top of it.
# The real O1 case was F4 and F5 editing two functions in report.py hundreds of
# lines apart, which this models. test_edits_inside_one_context_window_refuse
# pins what happens when they are adjacent instead.
_FILLER = "".join(f"# filler {i}\n" for i in range(10))

TWO_FUNCS = (
    "def head():\n"
    "    return 'HEAD'\n"
    "\n"
    + _FILLER
    + "\n"
    "def tail():\n"
    "    return 'TAIL'\n"
)


def _repo(tmp_path: Path, body: str = TWO_FUNCS) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "m.py").write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo, check=True,
    )
    return repo


def test_two_tickets_touching_one_file_both_land(tmp_path):
    """The O1 capability: two worktrees off the same base, one file, both promote.

    Ticket A rewrites head(); ticket B rewrites tail(). Neither patch contains
    the other's change, so a whole-file copy on the second promote reverts the
    first.
    """
    repo = _repo(tmp_path)
    live = repo / "src" / "m.py"

    with workspace.open_workspace(repo, "O1_A") as ws_a:
        t = ws_a.root / "src" / "m.py"
        t.write_text(t.read_text(encoding="utf-8").replace("'HEAD'", "'A_WAS_HERE'"),
                     encoding="utf-8")
        ws_a.promote(["src/m.py"])

    assert "A_WAS_HERE" in live.read_text(encoding="utf-8")

    # B's worktree is cut from HEAD, which does NOT contain A's change -- A was
    # promoted, not committed. This is the exact situation that loses A.
    with workspace.open_workspace(repo, "O1_B") as ws_b:
        t = ws_b.root / "src" / "m.py"
        t.write_text(t.read_text(encoding="utf-8").replace("'TAIL'", "'B_WAS_HERE'"),
                     encoding="utf-8")
        ws_b.promote(["src/m.py"])

    final = live.read_text(encoding="utf-8")
    assert "B_WAS_HERE" in final, "B's change did not land"
    assert "A_WAS_HERE" in final, "B's promote reverted A -- this is O1"


def test_conflicting_promote_refuses_and_leaves_the_file_intact(tmp_path):
    """Overlapping edits must refuse cleanly, never half-apply.

    A partially applied patch is worse than a refusal: it leaves a file no
    reviewer approved and no ticket describes.
    """
    repo = _repo(tmp_path)
    live = repo / "src" / "m.py"

    with workspace.open_workspace(repo, "O1_C") as ws_a:
        t = ws_a.root / "src" / "m.py"
        t.write_text(t.read_text(encoding="utf-8").replace("'HEAD'", "'FIRST'"),
                     encoding="utf-8")
        ws_a.promote(["src/m.py"])

    before = live.read_text(encoding="utf-8")
    assert "FIRST" in before

    # B rewrites the SAME line A did.
    with workspace.open_workspace(repo, "O1_D") as ws_b:
        t = ws_b.root / "src" / "m.py"
        t.write_text(t.read_text(encoding="utf-8").replace("'HEAD'", "'SECOND'"),
                     encoding="utf-8")
        with pytest.raises(workspace.WorkspaceError):
            ws_b.promote(["src/m.py"])

    after = live.read_text(encoding="utf-8")
    assert after == before, "a refused promote must not modify the file at all"
    assert "SECOND" not in after


def test_promote_still_refuses_to_destroy_uncommitted_human_work(tmp_path):
    """Property 3 -- the guarantee the worktree exists to provide.

    A human edit to the same lines the patch rewrites must still refuse, and the
    error must still say what to do about it.
    """
    repo = _repo(tmp_path)
    live = repo / "src" / "m.py"
    live.write_text(TWO_FUNCS.replace("'HEAD'", "'MY WORK'"), encoding="utf-8")

    with workspace.open_workspace(repo, "O1_E") as ws:
        t = ws.root / "src" / "m.py"
        t.write_text(TWO_FUNCS.replace("'HEAD'", "'PATCH'"), encoding="utf-8")
        with pytest.raises(workspace.WorkspaceError, match="uncommitted"):
            ws.promote(["src/m.py"])
        assert "MY WORK" in live.read_text(encoding="utf-8")

        # force=True remains an explicit override for a caller who means it.
        ws.promote(["src/m.py"], force=True)
        assert "PATCH" in live.read_text(encoding="utf-8")


def test_edits_inside_one_context_window_refuse_rather_than_merge(tmp_path):
    """Two edits closer than git's 3 lines of context cannot compose -- and the
    refusal is the correct outcome, not a gap.

    `git apply --3way` WOULD merge these, but on a genuine conflict it writes
    conflict markers into the live file and only then returns non-zero, which
    destroys atomicity (and it implies --index, staging the result behind the
    user's back). A refusal the caller can act on beats a merge nobody reviewed.
    This test exists so that trade-off stays deliberate: if someone later adopts
    --3way, this test tells them what they are giving up.
    """
    tight = "def a():\n    return 1\ndef b():\n    return 2\n"
    repo = _repo(tmp_path, body=tight)
    live = repo / "src" / "m.py"

    with workspace.open_workspace(repo, "O1_TIGHT_A") as ws_a:
        t = ws_a.root / "src" / "m.py"
        t.write_text(tight.replace("return 1", "return 'A'"), encoding="utf-8")
        ws_a.promote(["src/m.py"])

    before = live.read_text(encoding="utf-8")

    with workspace.open_workspace(repo, "O1_TIGHT_B") as ws_b:
        t = ws_b.root / "src" / "m.py"
        t.write_text(tight.replace("return 2", "return 'B'"), encoding="utf-8")
        with pytest.raises(workspace.WorkspaceError):
            ws_b.promote(["src/m.py"])

    assert live.read_text(encoding="utf-8") == before, (
        "a refused promote must leave the file untouched -- no conflict markers"
    )
    assert "<<<<<<<" not in live.read_text(encoding="utf-8")


def test_promote_still_works_on_a_clean_target(tmp_path):
    """The ordinary path must not regress: clean live file, plain promotion."""
    repo = _repo(tmp_path)
    with workspace.open_workspace(repo, "O1_F") as ws:
        t = ws.root / "src" / "m.py"
        t.write_text(TWO_FUNCS.replace("'TAIL'", "'CLEAN'"), encoding="utf-8")
        assert ws.promote(["src/m.py"]) == ["src/m.py"]
    assert "CLEAN" in (repo / "src" / "m.py").read_text(encoding="utf-8")


def test_promote_reports_a_missing_file_rather_than_silently_skipping(tmp_path):
    repo = _repo(tmp_path)
    with workspace.open_workspace(repo, "O1_G") as ws:
        with pytest.raises(workspace.WorkspaceError, match="missing"):
            ws.promote(["src/does_not_exist.py"])


def test_promote_preserves_crlf_line_endings(tmp_path):
    """Promotion must not rewrite a CRLF file in LF, or the reverse.

    This is the same class as the export_patch defect: a promotion that
    normalises terminators turns a one-line change into a whole-file diff.
    """
    body = TWO_FUNCS.replace("\n", "\r\n")
    repo = _repo(tmp_path, body=body)
    with workspace.open_workspace(repo, "O1_H") as ws:
        t = ws.root / "src" / "m.py"
        t.write_bytes(t.read_bytes().replace(b"'TAIL'", b"'CRLF_OK'"))
        ws.promote(["src/m.py"])
    raw = (repo / "src" / "m.py").read_bytes()
    assert b"CRLF_OK" in raw
    assert b"\r\n" in raw, "CRLF was normalised away by promote"
    assert raw.count(b"\n") == raw.count(b"\r\n"), "mixed terminators after promote"
