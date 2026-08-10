"""
workspace.py
============
Run a ticket inside a disposable git worktree, under an exclusive run lock.

The predecessor applied candidates directly to the live tree and reverted with
`git checkout --`, which destroys any uncommitted work in the same files. That
is why the handover has to warn "between tickets you must commit" -- a tool
requirement leaking into the user's git discipline.

A worktree removes the hazard rather than documenting it: the loop gets its own
checkout sharing the repo's object store, the live tree is never written to, and
the same `git checkout --` that was dangerous becomes safe because it is scoped
to a throwaway directory. Applying an approved patch becomes an explicit,
reviewable step at the end instead of a side effect of every round.

This is also what makes the run lock meaningful. Two loops -- or a loop and a
human running `dotnet build` -- racing the same files silently corrupts both.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple


class WorkspaceError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Run lock
# --------------------------------------------------------------------------
# Hand-rolled rather than `filelock`, for one reason that matters here: a
# crashed loop must not leave a permanent lock. This records the holder's PID
# and treats a lock whose process is gone as stale, which filelock does not do.
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError) as exc:
        return isinstance(exc, PermissionError)
    return True


@contextmanager
def run_lock(path: Path, holder: str = "", wait_secs: int = 0) -> Iterator[None]:
    """Exclusive advisory lock. Raises if another live process holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + wait_secs
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, json.dumps({"pid": os.getpid(), "holder": holder, "at": time.time()}).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                info = json.loads(path.read_text() or "{}")
            except (json.JSONDecodeError, OSError):
                info = {}
            pid = int(info.get("pid", 0) or 0)
            if not _pid_alive(pid):
                # Holder died without releasing. Reclaim.
                path.unlink(missing_ok=True)
                continue
            if time.time() >= deadline:
                raise WorkspaceError(
                    f"another agent-loop run holds {path.name} "
                    f"(pid {pid}, holder={info.get('holder','?')}). "
                    f"Wait for it, or delete the lock if you know it is dead."
                )
            time.sleep(2)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Worktree
# --------------------------------------------------------------------------
def _git(repo: Path, *args: str, check: bool = True, timeout: int = 300) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=timeout
    )
    if check and proc.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


@dataclass
class Workspace:
    """An isolated checkout the loop may freely write to and revert."""

    repo: Path
    root: Path
    base_commit: str
    ticket: str
    baseline: Set[str] = field(default_factory=set)
    baseline_note: str = ""

    def run(self, cmd: str, timeout: int = 900) -> Tuple[int, str]:
        """Run a shell command with the worktree as cwd."""
        proc = subprocess.run(
            cmd, shell=True, cwd=str(self.root), capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, (proc.stdout or "") + "\n" + (proc.stderr or "")

    def revert(self, files: Sequence[str]) -> None:
        """Discard candidate edits. Safe here, and only here: this checkout is
        disposable and holds nothing a human authored."""
        for f in files:
            _git(self.root, "checkout", "--", f, check=False)

    def dirty_files(self) -> List[str]:
        out = _git(self.root, "status", "--porcelain")
        return [ln[3:].strip() for ln in out.splitlines() if ln.strip()]

    def diff(self) -> str:
        return _git(self.root, "diff")

    def export_patch(self, dest: Path) -> Optional[Path]:
        """Write the worktree's diff so a human arbiter can read the change in
        one file. `final_blocks.json` is JSON-escaped C# and unreadable; the
        arbiter is the last gate and deserves a real diff."""
        d = self.diff()
        if not d.strip():
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(d, encoding="utf-8")
        return dest

    def promote(self, files: Sequence[str]) -> List[str]:
        """Copy approved files back into the live repo.

        Deliberately a plain file copy rather than a merge or cherry-pick: the
        loop makes no commits in the worktree, so there is nothing to cherry-
        pick, and the user stages and commits the result themselves.
        """
        moved: List[str] = []
        for f in files:
            src = self.root / f
            dst = self.repo / f
            if not src.exists():
                raise WorkspaceError(f"cannot promote missing file: {f}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            moved.append(f)
        return moved


def list_stale(repo: Path) -> List[str]:
    """Worktrees left behind by crashed runs."""
    out = _git(repo, "worktree", "list", "--porcelain", check=False)
    found = []
    for ln in out.splitlines():
        if ln.startswith("worktree ") and "agentloop-" in ln:
            found.append(ln.split(" ", 1)[1].strip())
    return found


def prune(repo: Path, path: Optional[str] = None) -> None:
    if path:
        _git(repo, "worktree", "remove", "--force", path, check=False)
    _git(repo, "worktree", "prune", check=False)


@contextmanager
def open_workspace(
    repo: Path,
    ticket: str,
    base: str = "HEAD",
    keep: bool = False,
    lock_wait: int = 0,
    workdir: Optional[Path] = None,
) -> Iterator[Workspace]:
    """Create an isolated worktree for one ticket, and tear it down after.

    `keep=True` leaves it on disk for post-mortem after a failed run.
    """
    repo = repo.resolve()
    lock_path = repo / "logs" / "agent_loop" / ".runlock"
    root = (workdir or repo.parent) / f"agentloop-{ticket}-{os.getpid()}"

    with run_lock(lock_path, holder=f"ticket={ticket}", wait_secs=lock_wait):
        commit = _git(repo, "rev-parse", base).strip()
        if root.exists():
            raise WorkspaceError(f"worktree path already exists: {root}")
        _git(repo, "worktree", "add", "--detach", str(root), commit)
        ws = Workspace(repo=repo, root=root, base_commit=commit, ticket=ticket)
        try:
            yield ws
        finally:
            if keep:
                print(f"  worktree kept for inspection: {root}")
            else:
                prune(repo, str(root))


def capture_baseline(ws: Workspace, test_cmd: str, parse_tests, timeout: int = 900) -> None:
    """Freeze the expected-failure set BEFORE any candidate is applied.

    Recomputing this mid-run would let a patch that breaks a test simply widen
    the baseline and pass, so it is captured once and treated as immutable for
    the rest of the run.
    """
    if ws.dirty_files():
        raise WorkspaceError(
            "refusing to capture a test baseline from a dirty worktree; "
            "the baseline must describe unmodified code"
        )
    _, out = ws.run(test_cmd, timeout=timeout)
    outcome = parse_tests(out)
    if not outcome.ran:
        raise WorkspaceError(
            "baseline test run did not reach a RESULTS line -- cannot establish "
            "which failures are expected, so no regression check is possible"
        )
    ws.baseline = set(outcome.failures)
    ws.baseline_note = f"{outcome.passed} passed, {outcome.failed} failed at {ws.base_commit[:8]}"
