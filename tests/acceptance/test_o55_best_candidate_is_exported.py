"""
O55 — the loop exports the LAST candidate, even when an earlier one was better.

From the CM2 run against the consumer:

    round 1: [test] FAIL - 1 regression(s); 827 passed, 1 failed
    round 2: [test] ok - no regressions; 828 passed, 0 failed; all 10 acceptance
             test(s) green   -> panel REVISE, arbiter REVISE (upheld=3)
    round 3: [compile] FAIL - build FAILED
    NOT APPLIED: verdict=MAX_ROUNDS_EXHAUSTED. Patch for review: final.patch

`final.patch` held **round 3** — the one that does not compile, because round 3
invented a type called `CopierMode` when the real one is `CopierExecutionMode`.
Round 2 passed every mechanical gate and was not represented in the export at
all. The operator is handed a patch that fails the compile gate and told to
review it, while a working one is discarded.

Same family as O38 (a rejected plan was discarded entirely) and O50 (the promote
command deleted its own input): the run produced something usable and the harness
threw it away on the way out.

The candidate is technically recoverable from `r2_impl_raw.txt`, but nothing in
the output says round 2 was the good one — the run reports only its last round,
so recovering it requires reading three build logs to work out which round to
resume from.

Fixed by tracking the last candidate that passed EVERY mechanical gate and
exporting that on a non-promotable exit, naming the round it came from. A
promotable verdict still exports its own approved candidate: substituting an
older one there would ship code the panel never saw.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_loop import loop as loop_mod
from agent_loop.loop import run_ticket
from agent_loop.profiles import Profile, register
from agent_loop.providers import Completion

PY = f'"{os.sys.executable}"'

GOOD = (
    '<<<BLOCK id="R1">>>\n'
    "def double(x):\n"
    "    return x * 2\n"
    '<<<END id="R1">>>\n'
)
# Passes the static gate (the block is well-formed) and fails the compile gate,
# which is exactly round 3's shape: valid-looking code naming something that does
# not exist.
BROKEN = (
    '<<<BLOCK id="R1">>>\n'
    "def double(x):\n"
    "    return x * ( 2\n"
    '<<<END id="R1">>>\n'
)

# A second green candidate, distinguishable from GOOD in the exported diff.
GOOD_V2 = (
    '<<<BLOCK id="R1">>>\n'
    "def double(x):\n"
    "    return x * 2 + 0\n"
    '<<<END id="R1">>>\n'
)

REVISE_BODY = (
    "<<<VERDICT>>>\nREVISE\n<<<END VERDICT>>>\n"
    "<<<FINDINGS>>>\n- [MAJOR] R1: not convinced\n<<<END FINDINGS>>>\n"
    "<<<REQUIRED>>>\n- do better\n<<<END REQUIRED>>>"
)
APPROVE_BODY = (
    "<<<VERDICT>>>\nAPPROVE\n<<<END VERDICT>>>\n"
    "<<<FINDINGS>>>\n- NONE\n<<<END FINDINGS>>>\n"
    "<<<REQUIRED>>>\n- NONE\n<<<END REQUIRED>>>"
)

TICKET = {
    "id": "CM2",
    "title": "t",
    "defect": "d",
    "spec": "s",
    "regions": [{"id": "R1", "file": "src/target.py", "anchor": "def double(x):"}],
}


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


def _scripted(monkeypatch, impl_bodies, review_bodies=(REVISE_BODY,)):
    """`chat` that walks scripts of implementer and reviewer responses.

    Both are lists, and both hold their last entry once exhausted, so a test
    only has to spell out the rounds it cares about.
    """
    state = {"i": 0, "r": 0}

    def fake_chat(model, messages, **kw):
        if model == "impl":
            body = impl_bodies[min(state["i"], len(impl_bodies) - 1)]
            state["i"] += 1
            return Completion(text=body, model=model)
        body = review_bodies[min(state["r"], len(review_bodies) - 1)]
        state["r"] += 1
        return Completion(text=body, model=model)

    monkeypatch.setattr(loop_mod, "chat", fake_chat)
    return state


def test_a_later_broken_round_does_not_replace_an_earlier_green_one(tmp_path, monkeypatch):
    """The CM2 case: round 1 green, round 2 fails compile, rounds exhausted."""
    repo = _make_repo(tmp_path)
    prof = _profile("o55-live")
    _scripted(monkeypatch, [GOOD, BROKEN])

    result = run_ticket(
        repo, TICKET, prof, "impl", ["rev"], max_rounds=2, apply=False,
    )

    patch = (repo / "logs" / "agent_loop" / "CM2" / "final.patch").read_text(encoding="utf-8")
    assert "x * 2" in patch, (
        "the exported patch must be the candidate that passed every gate; "
        f"got:\n{patch}"
    )
    assert "x * ( 2" not in patch, (
        "the exported patch is the one that fails the compile gate -- this is "
        "the defect"
    )


def test_the_result_names_the_round_that_was_exported(tmp_path, monkeypatch):
    """Recovering the good candidate means knowing WHICH round it was. Without
    this the operator reads three build logs to find out."""
    repo = _make_repo(tmp_path)
    prof = _profile("o55-names")
    _scripted(monkeypatch, [GOOD, BROKEN])

    result = run_ticket(repo, TICKET, prof, "impl", ["rev"], max_rounds=2, apply=False)

    assert result.get("exported_round") == 1, (
        f"expected the exported patch to be attributed to round 1; got "
        f"{result.get('exported_round')!r}"
    )


def test_the_last_round_is_still_exported_when_it_is_the_green_one(tmp_path, monkeypatch):
    """The ordinary case must not change: when the final round passes its gates,
    it is the candidate with the most feedback addressed and it wins."""
    repo = _make_repo(tmp_path)
    prof = _profile("o55-normal")
    # Round 1 is broken, round 2 is good -- the reverse order.
    _scripted(monkeypatch, [BROKEN, GOOD])

    result = run_ticket(repo, TICKET, prof, "impl", ["rev"], max_rounds=2, apply=False)

    patch = (repo / "logs" / "agent_loop" / "CM2" / "final.patch").read_text(encoding="utf-8")
    assert "x * 2" in patch
    assert result.get("exported_round") == 2


def test_an_approved_run_exports_its_own_candidate(tmp_path, monkeypatch):
    """An earlier candidate must NEVER be substituted into a promotable verdict:
    the panel approved the one it was shown, not an older one.

    This needs TWO green rounds that differ. A first draft approved on round 1,
    where "the approved candidate" and "the last green candidate" are the same
    object — so swapping one for the other changed nothing and the mutation
    survived."""
    repo = _make_repo(tmp_path)
    prof = _profile("o55-approve")
    # Round 1 passes its gates and is sent back; round 2 passes and is approved.
    _scripted(monkeypatch, [GOOD, GOOD_V2], review_bodies=[REVISE_BODY, APPROVE_BODY])

    result = run_ticket(repo, TICKET, prof, "impl", ["rev"], max_rounds=2, apply=False)

    assert result["final_verdict"] == "APPROVE"
    assert result.get("exported_round") == 2
    patch = (repo / "logs" / "agent_loop" / "CM2" / "final.patch").read_text(encoding="utf-8")
    assert "x * 2 + 0" in patch, (
        "the approved round 2 candidate must be exported, not the green round 1 one"
    )


def test_allow_unapproved_does_not_promote_a_candidate_that_failed_a_gate(tmp_path, monkeypatch):
    """`--allow-unapproved --apply` is the command this loop PRINTS after an
    ARBITER_SHIP. If the last round failed a gate, that command used to land a
    patch that does not compile: `promotable` was true on the waiver alone.

    A waiver of the REVIEW is not a waiver of the compiler. Gates are facts --
    the arbiter is not allowed to overturn one either."""
    repo = _make_repo(tmp_path)
    prof = _profile("o55-waiver")
    _scripted(monkeypatch, [GOOD, BROKEN])

    result = run_ticket(
        repo, TICKET, prof, "impl", ["rev"],
        max_rounds=2, apply=True, allow_unapproved=True,
    )

    assert not result.get("applied"), (
        "a candidate that failed the compile gate was applied to the live repo"
    )
    assert result.get("exported_round") == 1
    patch = (repo / "logs" / "agent_loop" / "CM2" / "final.patch").read_text(encoding="utf-8")
    assert "x * ( 2" not in patch
    # And the live repo is untouched.
    assert "x + 2" in (repo / "src" / "target.py").read_text(encoding="utf-8")


def test_nothing_is_exported_when_no_round_ever_passed_a_gate(tmp_path, monkeypatch):
    """There is no good candidate to fall back to, and inventing one by
    exporting a broken round as though it were vetted would be worse than the
    defect. The broken candidate is still written, because a human has to see
    what happened -- it just must not be labelled as gate-passing."""
    repo = _make_repo(tmp_path)
    prof = _profile("o55-none")
    _scripted(monkeypatch, [BROKEN, BROKEN])

    result = run_ticket(repo, TICKET, prof, "impl", ["rev"], max_rounds=2, apply=False)

    assert result.get("exported_round") is None, (
        "no round passed its gates, so no round may be attributed as the "
        "exported one"
    )
    patch_file = repo / "logs" / "agent_loop" / "CM2" / "final.patch"
    if patch_file.exists():
        assert "x * 2" not in patch_file.read_text(encoding="utf-8"), (
            "a candidate that never existed must not appear in the export"
        )
