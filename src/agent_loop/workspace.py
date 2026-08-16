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
            # A liveness probe must not be able to throw. tasklist prints process names, and
            # one non-ASCII name would otherwise take down the run on a cp1252 decode.
            encoding="utf-8",
            errors="replace",
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
        ["git", *args], cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
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
    # CF-2: the full test output from the baseline run, so the test-first
    # gate can distinguish "test not found at all" (uncommitted/typo) from
    # "test found but passing" (vacuous gate). Both were reported as
    # "not failing at baseline", sending the operator to re-read assertions
    # when the fix was one `git commit` away.
    baseline_raw: str = ""

    def run(self, cmd: str, timeout: int = 900) -> Tuple[int, str]:
        """Run a shell command with the worktree as cwd."""
        proc = subprocess.run(
            cmd, shell=True, cwd=str(self.root), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
        )
        return proc.returncode, (proc.stdout or "") + "\n" + (proc.stderr or "")

    def revert(self, files: Sequence[str]) -> None:
        """Discard candidate edits. Safe here, and only here: this checkout is
        disposable and holds nothing a human authored.

        Also removes untracked files created by `create` or `write_test`
        regions. `git checkout -- <file>` only restores tracked files, so a
        candidate that created a new file and then failed a gate left an
        orphaned file in the worktree. The next round's `git diff` would then
        show it, and `promote` could land a file the panel never saw. See
        AGENT_LOOP_THIRD_REVIEW.md E-section 2.
        """
        for f in files:
            # Try to restore the tracked version.
            _git(self.root, "checkout", "--", f, check=False)
            # If the file was intent-added (`git add -N`), it is in the index
            # but has no content there -- `git checkout --` restores it to the
            # HEAD version, which for a new file is empty/nonexistent. But the
            # file may still exist on disk. Check if the file has a real entry
            # in the HEAD commit; if not, remove the on-disk copy.
            # `git ls-files --error-unmatch` exits non-zero for untracked files,
            # but for intent-added files it exits 0 (the file IS in the index).
            # The right check: does `git show HEAD:<file>` succeed? If not, the
            # file is not in HEAD, so it was created by this run -- remove it.
            try:
                _git(self.root, "show", f"HEAD:{f}", check=True, timeout=10)
                # File is in HEAD -- tracked, restored by checkout. Done.
            except WorkspaceError:
                # File is NOT in HEAD. It was created by this run (or was
                # intent-added). Remove it and unstage it.
                p = self.root / f
                if p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass
                # Also remove from the index if it was intent-added.
                _git(self.root, "rm", "--cached", "--", f, check=False)

    def dirty_files(self) -> List[str]:
        out = _git(self.root, "status", "--porcelain")
        return [ln[3:].strip() for ln in out.splitlines() if ln.strip()]

    def stage_new_files(self, files: Sequence[str]) -> List[str]:
        """Intent-to-add files a `create` region wrote, so they reach the diff.

        `diff()` is `git diff`, which does not show untracked files. Without this
        an `op=create` region produces a patch that references a module the patch
        itself does not add -- and `promote` then lands a change whose own code is
        missing. The red phase hit the same wall with a new test file and solved
        it the same way; this is that fix generalised to source.

        `--intent-to-add` puts the path in the index for diff purposes WITHOUT
        staging content, so nothing is committed behind the caller's back.
        A path that was never written is skipped rather than raising: a `create`
        region whose body the model never emitted is a no-op, not an error.
        """
        staged: List[str] = []
        for f in files:
            if not (self.root / f).exists():
                continue
            rc = _git(self.root, "add", "-N", f, check=False)
            del rc
            staged.append(f)
        return staged

    def diff(self, paths: Sequence[str] | None = None) -> str:
        """The worktree diff, with content line endings intact.

        Optionally restricted to *paths*. Deliberately NOT via _git(): that
        decodes with text=True, whose universal-newline handling eats the CR
        of a CRLF source line, because git's own line separator follows it.
        Combined with export_patch writing through platform newline
        translation, the exported patch ended up CRLF no matter what the file
        was -- so it applied to CRLF sources by luck and was rejected on every
        LF source, with `git apply` reporting only "patch does not apply".
        """
        cmd = ["git", "diff"]
        if paths:
            cmd.extend(["--"] + [str(p) for p in paths])
        proc = subprocess.run(
            cmd, cwd=str(self.root), capture_output=True, timeout=300
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
        """Apply approved worktree changes back into the live repo.

        Instead of copying whole files, we generate a patch for the requested
        files in the worktree and apply it to the live repo. This lets a later
        ticket compose its edits with an earlier promoted ticket's uncommitted
        changes as long as the two change sets do not overlap.

        Because a half-applied patch would leave files in a state no reviewer
        approved, the whole patch is verified with ``git apply --check`` before
        anything is written, and plain ``git apply`` is all-or-nothing.

        Deliberately NOT ``--3way``. A 3-way merge is not needed to compose
        non-overlapping edits -- their context lines are untouched, so the hunks
        match -- and on a genuine conflict ``--3way`` does not refuse: it merges,
        writes CONFLICT MARKERS into the live file, and *then* returns non-zero.
        That silently traded away the atomicity this method exists to provide
        (caught by test_conflicting_promote_refuses_and_leaves_the_file_intact).
        It also implies ``--index``, which would stage the result behind the
        user's back.

        Known limit, and it is a safe one: two edits closer together than git's
        3 lines of hunk context each carry the other's lines as context, so the
        second is refused rather than composed. That is a refusal the caller can
        act on (commit the first, then promote the second), not data loss.

        ``force=True`` keeps its original meaning: overwrite live targets by
        plain file copy.
        """
        files = list(files)
        for f in files:
            if not (self.root / f).exists():
                raise WorkspaceError(f"cannot promote missing file: {f}")

        if force:
            moved: List[str] = []
            for f in files:
                src = self.root / f
                dst = self.repo / f
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                moved.append(f)
            return moved

        # Build a byte-accurate patch for exactly the files being promoted.
        patch_text = self.diff(paths=files)
        patch_bytes = patch_text.encode("utf-8", errors="replace")
        if not patch_bytes.strip():
            # Live already matches the worktree base for these files.
            return files

        # Verify the whole change can apply cleanly before touching any file.
        check = subprocess.run(
            ["git", "apply", "--check", "-"],
            cwd=str(self.repo),
            input=patch_bytes,
            capture_output=True,
        )
        if check.returncode != 0:
            dirty = [f for f in files if self._live_is_dirty(f)]
            if dirty:
                raise WorkspaceError(
                    "refusing to promote over uncommitted changes in: "
                    + ", ".join(dirty)
                    + ". The patch would overwrite work that is not in git. "
                    "Commit or stash those files first, then promote."
                )
            raise WorkspaceError(
                "refusing to promote: changes overlap in "
                + ", ".join(files)
            )

        apply = subprocess.run(
            ["git", "apply", "-"],
            cwd=str(self.repo),
            input=patch_bytes,
            capture_output=True,
        )
        if apply.returncode != 0:
            raise WorkspaceError(
                "promotion failed: git apply reported: "
                + apply.stderr.decode("utf-8", "replace").strip()
            )
        return files

    def _live_is_dirty(self, path: str) -> bool:
        """Does the LIVE repo have uncommitted changes to this path?"""
        out = _git(self.repo, "status", "--porcelain", "--", path, check=False)
        return bool(out.strip())


def list_stale(repo: Path) -> List[str]:
    """Worktrees left behind by crashed runs, from git AND from the filesystem.

    Asking git alone was not enough. Observed live: after a real run,
    `agentloop-<TICKET>-testgen-42184` was still on disk while `--prune` reported
    "pruned 0 worktree(s)". `git worktree remove --force` had deleted the
    contents but could not remove the DIRECTORY (Windows was holding a handle),
    and the `git worktree prune` that follows then dropped the registration -- so
    from that moment git had no record of it and this function was blind to it.

    That is not merely untidy: `open_workspace` REFUSES to start when its target
    path exists, the path carries the pid, and pids get recycled. A later run of
    the same ticket can fail with "worktree path already exists" and `--prune`
    would not have fixed it.
    """
    out = _git(repo, "worktree", "list", "--porcelain", check=False)
    found = []
    for ln in out.splitlines():
        if ln.startswith("worktree ") and "agentloop-" in ln:
            found.append(ln.split(" ", 1)[1].strip())

    # Orphans: named like ours, sitting where we put them, unknown to git.
    known = {Path(p).resolve() for p in found}
    parent = repo.resolve().parent
    for child in sorted(parent.glob("agentloop-*")):
        if child.is_dir() and child.resolve() not in known:
            found.append(str(child))
    return found


def prune(repo: Path, path: Optional[str] = None) -> None:
    """Remove one worktree, or sweep git's records; optionally both.

    An orphaned directory git no longer tracks is removed only when it is EMPTY.
    A non-empty orphan is left alone and reported: it may be a crashed run whose
    contents are the post-mortem, and prune must not be the thing that destroys
    the evidence it exists to help you read. A live worktree belonging to a
    concurrent run is registered with git, so it never reaches that branch.
    """
    if path:
        _git(repo, "worktree", "remove", "--force", path, check=False)
        target = Path(path)
        if target.is_dir():
            if any(target.iterdir()):
                print(
                    f"  worktree directory still present and NOT empty, left alone: {target}\n"
                    f"    (git no longer tracks it. Read it, then delete it by hand.)"
                )
            else:
                try:
                    target.rmdir()
                except OSError as exc:
                    print(f"  could not remove empty worktree directory {target}: {exc}")
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
    ws.baseline_raw = outcome.raw
