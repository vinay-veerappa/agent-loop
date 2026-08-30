"""CF-33 -- an empty --reviewers produced a "valid" review with ZERO model
calls: empty verdict, zero findings, 0.2s runtime.

The operator ran `--mode review` with no --reviewers. main() resolved a
default panel from the registry (glm-5.2 + deepseek-v4-flash), printed the
family-policy checks against it, and then `_review(args, profile)` -- which
takes only (args, profile) -- re-parsed the RAW ``args.reviewers`` string.
Empty string -> ``[]`` -> ``review_panel([], ...)`` -> ``valid = all([]) and
len([]) == len([])`` -> vacuously valid. The run printed:

    findings (0) -> (no reviewer output)
    REVIEW MODE IS ADVISORY. It changes nothing; read the findings and decide.

and result.json said ``panel_valid: true`` with zero findings and 0.2 seconds.
Nothing failed. Nothing even hinted that no reviewer ever ran.

Why the defect survived: patch/plan/developer modes take the resolved panel
as a parameter, so their paths were correct. Review mode was the only mode
that re-derived its panel from the raw flag -- an inconsistency inside one
dispatch, invisible until someone ran review mode without the flag.

Fixes (three layers, so the root cause AND the structure both close):

1. **cli._review(args, profile, reviewers, arbiter)** -- review mode now
   receives the RESOLVED panel, same as every other mode. ``--reviewers``
   still overrides (main() applies the override before the hand-off).

2. **review_mode.run_review refuses an empty reviewer list** -- a loud
   ``ReviewError`` instead of a silent zero-opinion review. Protects
   programmatic callers, not just the CLI.

3. **review_panel raises on an empty reviewer list** -- the validity rule was
   vacuously true for zero reviewers; the structure now makes a zero-member
   panel impossible rather than merely unlikely.

4. **result.json records ``reviewers``** -- the actual panel is in the
   artifacts, so a zero/one-member panel is auditable without reverse-
   engineering it from the vote rows.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop import cli, review_mode
from agent_loop.loop import review_panel, PanelResult, Vote
from agent_loop.models import reload_default_registry
from agent_loop.profiles import Profile, register
from agent_loop import config as al_config


PROFILE = Profile(
    name="test-cf33",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    build_cmd="true", test_cmd="true",
    implementer_rules="test", reviewer_priorities="test",
)
register(PROFILE)


def _fake_vote(model: str, finding_list=()) -> Vote:
    return Vote(
        model=model,
        status="APPROVE",
        findings="<<<VERDICT>>>\nAPPROVE\n<<<END VERDICT>>>\n<<<FINDINGS>>>\n<<<END FINDINGS>>>",
        required="",
        blockers=0,
        secs=1.0,
    )


# ---------------------------------------------------------------------------
# Root cause: main() resolves a default panel; _review must receive it
# ---------------------------------------------------------------------------

def test_default_reviewer_role_is_a_multi_member_panel():
    """Pre-condition for the regression: the default registry's reviewer role
    carries the multi-member panel. main() computes this; before CF-33,
    _review threw it away and re-parsed the empty raw flag."""
    registry = reload_default_registry(al_config.DEFAULTS)
    members = [c.name for c in registry.get_all("reviewer")]
    assert len(members) >= 2, (
        f"the default registry must carry a multi-member reviewer panel for "
        f"this regression to be meaningful; got {members}"
    )


def test_review_mode_refuses_empty_reviewer_list(tmp_path):
    """Fix 2: run_review raises ReviewError on an empty panel instead of
    producing a vacuously-valid review. The guard fires before diff
    collection, so a plain directory suffices as `repo`."""
    with pytest.raises(review_mode.ReviewError, match="EMPTY reviewer list"):
        review_mode.run_review(
            tmp_path, base="HEAD~1", head="HEAD", profile=PROFILE,
            reviewers=[], intent="i",
        )


def test_review_panel_refuses_empty_reviewer_list(tmp_path):
    """Fix 3: the structural guard -- the validity rule all([]) and len([])==len([])
    was vacuously TRUE for zero reviewers; an empty panel is now impossible."""
    with pytest.raises(ValueError, match="empty reviewer list"):
        review_panel([], "prompt", "system", tmp_path, rnd=1)


def test_result_json_records_the_reviewers(tmp_path, monkeypatch):
    """Fix 4: the actual panel lands in result.json so a zero/one-member
    review is auditable from the artifacts."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "base")
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    git("commit", "-am", "head")

    panel_members = ["glm-test:a", "glm-test-b:cloud"]
    seen = {}

    def fake_panel(reviewers, prompt, system, art, rnd, deadline_secs=1800,
                   max_tokens=None, think=None):
        seen["reviewers"] = list(reviewers)
        votes = [_fake_vote(m) for m in reviewers]
        return PanelResult(votes=votes, verdict="APPROVE", valid=True)

    monkeypatch.setattr(review_mode, "review_panel", fake_panel)

    record = review_mode.run_review(
        repo, base="HEAD~1", head="HEAD", profile=PROFILE,
        reviewers=panel_members, intent="i",
    )
    assert seen["reviewers"] == panel_members, "the panel got the resolved list"

    saved = json.loads(
        (Path(record["artifacts"]) / "result.json").read_text(encoding="utf-8")
    )
    assert saved["reviewers"] == panel_members, (
        "result.json must record the actual panel (CF-33 fix 4)"
    )
    assert len(saved["reviewers"]) >= 1


def test_review_panel_still_accepts_one_and_two_member_panels(tmp_path):
    """The CF-33 guard must not over-correct: a one-member panel is legal
    (main() WARNS about the missing viewpoint; it does not refuse)."""
    with patch("agent_loop.loop.chat") as fake_chat:
        out = type("O", (), {})()
        out.text = "<<<VERDICT>>>\nAPPROVE\n<<<END VERDICT>>>\n<<<FINDINGS>>>\n<<<END FINDINGS>>>"
        out.secs = 1.0
        out.input_tokens = 10
        out.output_tokens = 10
        out.usage_line = lambda: "10/10"
        fake_chat.return_value = out

        one = review_panel(["m1"], "p", "s", tmp_path, rnd=1)
        assert one.valid

        two = review_panel(["m1", "m2"], "p", "s", tmp_path, rnd=1)
        assert two.valid