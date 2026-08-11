"""The promote command this loop prints must not delete its own input.

The live failure, following the loop's own instruction verbatim after an
ARBITER_SHIP on a real ticket:

    promote: --resume-raw logs\\agent_loop\\CM1\\r3_impl_raw.txt --allow-unapproved --apply

    ERROR CM1: FileNotFoundError: No such file or directory:
               'logs\\agent_loop\\CM1\\r3_impl_raw.txt'

The file existed when the command was issued. `run_ticket` purges every
`r*_*.txt` in the artifact directory before the round loop starts, and the printed
hint is built from that same directory -- so the resume source is always inside the
purge's blast radius. The run deleted it, then raised FileNotFoundError naming it.

The collateral is the worse half: the purge also took the r1/r2/r3 review files,
the arbiter rulings, and the build and test logs. `final.patch` survived only
because it is not an `r*` file, and it was the only reason the candidate was
recoverable at all.

These tests drive `run_ticket` against a real git repo. An earlier draft asserted
against a HELPER that mirrored run_ticket's ordering, which would have passed
whatever run_ticket did -- a double is not evidence.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_loop import loop as loop_mod
from agent_loop.loop import run_ticket
from agent_loop.profiles import Profile, register

PY = f'"{os.sys.executable}"'


def _profile(name):
    p = Profile(
        name=name, language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent", preprocessor_directives=(),
        build_cmd=f"{PY} -m py_compile {{files}}",
        test_cmd=f"{PY} -c \"print('==== 1 passed in 0.1s ====')\"",
        lock_name="", risk_calls=(),
        implementer_rules="t", reviewer_priorities="t",
    )
    register(p)
    return p


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "target.py").write_text(
        "def double(x):\n    return x + 2\n", encoding="utf-8")
    os.system(f'cd /d "{repo}" && git init && git add -A && git commit -m init')
    return repo


TICKET = {
    "id": "CM1",
    "title": "t",
    "defect": "d",
    "spec": "s",
    "regions": [{"id": "R1", "file": "src/target.py", "anchor": "def double(x):"}],
}

CANDIDATE = (
    "<<<BLOCK R1>>>\n"
    "def double(x):\n"
    "    return x * 2\n"
    "<<<END BLOCK R1>>>\n"
)


def _seed_artifacts(repo: Path) -> Path:
    """A prior run's artifacts, exactly as they sit when promote is invoked."""
    art = repo / "logs" / "agent_loop" / "CM1"
    art.mkdir(parents=True)
    (art / "r3_impl_raw.txt").write_text(CANDIDATE, encoding="utf-8")
    (art / "r3_review_glm.txt").write_text("findings", encoding="utf-8")
    (art / "r3_arbiter.txt").write_text("SHIP", encoding="utf-8")
    (art / "r2_impl_raw.txt").write_text("older candidate", encoding="utf-8")
    (art / "final.patch").write_text("diff --git a/x b/x\n", encoding="utf-8")
    return art


def test_promoting_the_printed_path_does_not_delete_it(tmp_path, monkeypatch):
    """The exact live case, driven through run_ticket."""
    repo = _make_repo(tmp_path)
    art = _seed_artifacts(repo)
    prof = _profile("resume-live")

    # No model may be reached: a resume must not call the implementer at all.
    def _no_chat(*a, **k):
        raise AssertionError("resume-raw must not call the implementer")

    monkeypatch.setattr(loop_mod, "chat", _no_chat)

    result = run_ticket(
        repo, TICKET, prof, "impl", [],
        max_rounds=1, apply=False, allow_unapproved=True,
        resume_raw=str(art / "r3_impl_raw.txt"),
    )

    assert "FileNotFoundError" not in str(result.get("error", "")), result.get("error")
    # The candidate was read and re-persisted under this round's name.
    assert (art / "r1_impl_raw.txt").read_text(encoding="utf-8").strip() == CANDIDATE.strip()


def test_a_bad_resume_path_deletes_nothing(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    art = _seed_artifacts(repo)
    prof = _profile("resume-typo")
    monkeypatch.setattr(loop_mod, "chat", lambda *a, **k: pytest.fail("no model"))

    with pytest.raises(FileNotFoundError) as exc:
        run_ticket(
            repo, TICKET, prof, "impl", [],
            max_rounds=1, allow_unapproved=True,
            resume_raw=str(art / "r9_impl_raw.txt"),   # typo
        )

    msg = str(exc.value)
    assert "Nothing has been deleted" in msg or "Nothing has been " in msg, msg
    assert "final.patch" in msg, "the surviving recovery path is not named"

    # Every artifact must still be there.
    for name in ("r3_impl_raw.txt", "r3_review_glm.txt", "r3_arbiter.txt",
                 "r2_impl_raw.txt", "final.patch"):
        assert (art / name).is_file(), f"a typo destroyed {name}"


def test_a_normal_run_still_purges_stale_round_artifacts(tmp_path, monkeypatch):
    """The purge exists for a reason (T4/T5): the logs must match the round count."""
    repo = _make_repo(tmp_path)
    art = _seed_artifacts(repo)
    prof = _profile("resume-purge")

    # The REAL Completion, not a hand-rolled stub. A stub with only `text` and a
    # `usage_line()` got as far as `result["cost_usd"] += out.cost_usd` and raised
    # AttributeError -- a double that omits a field the caller uses reports a
    # defect in the code under test.
    from agent_loop.providers import Completion

    monkeypatch.setattr(
        loop_mod, "chat",
        lambda *a, **k: Completion(text=CANDIDATE, model="stub"),
    )

    run_ticket(repo, TICKET, prof, "impl", [], max_rounds=1, allow_unapproved=True)

    # r2_* and r3_* belonged to a previous run and must be gone, so result.json's
    # round count cannot be contradicted by files on disk.
    assert not (art / "r3_impl_raw.txt").exists()
    assert not (art / "r2_impl_raw.txt").exists()
    assert not (art / "r3_arbiter.txt").exists()
    assert (art / "final.patch").is_file(), "final.patch is not an r* file"
