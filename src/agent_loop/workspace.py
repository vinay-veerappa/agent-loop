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

from ._io import write_text_verbatim


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
        """The worktree diff, with content line endings intact.

        Deliberately NOT via _git(): that decodes with text=True, whose
        universal-newline handling eats the CR of a CRLF source line, because
        git's own line separator follows it. Combined with export_patch writing
        through platform newline translation, the exported patch ended up CRLF
        no matter what the file was -- so it applied to CRLF sources by luck and
        was rejected on every LF source, with `git apply` reporting only
        "patch does not apply".
        """
        proc = subprocess.run(
            ["git", "diff"], cwd=str(self.root), capture_output=True, timeout=300
        )
        if proc.returncode != 0:
            raise WorkspaceError(
                f"git diff failed: {proc.stderr.decode('utf-8', 'replace').strip()}"
            )
        return proc.stdout.decode("utf-8", errors="replace")

    def export_patch(self, dest: Path) -> Optional[Path]:
        """Write the worktree's diff so a human arbiter can read the change in
        one file. `final_blocks.json` is JSON-escaped C# and unreadable; the
        arbiter is the last gate and deserves a real diff."""
        d = self.diff()
        if not d.strip():
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        # newline="" writes the diff bytes verbatim. Without it, every line
        # terminator becomes the platform's, which on Windows rewrites an
        # LF-source patch into CRLF and makes `git apply` reject it.
        write_text_verbatim(dest, d)
        return dest

    def promote(self, files: Sequence[str], force: bool = False) -> List[str]:
        """Copy approved files back into the live repo.

        Deliberately a plain file copy rather than a merge or cherry-pick: the
        loop makes no commits in the worktree, so there is nothing to cherry-
        pick, and the user stages and commits the result themselves.

        A plain copy is also how the hazard this module exists to remove gets
        back in: overwriting a live file that has uncommitted edits destroys
        them exactly as `git checkout --` did. So promotion refuses a target
        the human has unsaved work in, and says what to do about it.
        """
        if not force:
            dirty = [f for f in files if self._live_is_dirty(f)]
            if dirty:
                raise WorkspaceError(
                    "refusing to promote over uncommitted changes in: "
                    + ", ".join(dirty)
                    + ". The patch would overwrite work that is not in git. "
                    "Commit or stash those files first, then promote."
                )
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

    def _live_is_dirty(self, path: str) -> bool:
        """Does the LIVE repo have uncommitted changes to this path?"""
        out = _git(self.repo, "status", "--porcelain", "--", path, check=False)
        return bool(out.strip())


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
    # Errors first: an errored run DID produce a summary, so reporting it as
    # "no parseable summary" would send the reader looking for the wrong fault.
    # A collection or fixture error never reported a verdict on the tests it did
    # not reach, and freezing that as "expected" would let the patch inherit a
    # broken suite as its success criterion.
    if getattr(outcome, "errors", 0):
        raise WorkspaceError(
            f"baseline test run reported {outcome.errors} suite-level error(s); "
            "the test command is broken independently of any patch, so it cannot "
            f"establish a baseline. Fix the suite first. Last output:\n{outcome.raw[-1500:]}"
        )
    if not outcome.ran:
        raise WorkspaceError(
            "baseline test run produced no parseable result summary -- cannot "
            "establish which failures are expected, so no regression check is "
            f"possible. Last output:\n{outcome.raw[-1500:]}"
        )
    ws.baseline = set(outcome.failures)
    ws.baseline_note = f"{outcome.passed} passed, {outcome.failed} failed at {ws.base_commit[:8]}"
