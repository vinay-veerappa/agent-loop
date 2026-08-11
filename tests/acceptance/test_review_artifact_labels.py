"""Review mode must point the reader at the findings, not at the prompt.

`run_review` printed `findings -> <art>/review_prompt.txt`. That file is the
INPUT: the rendered review prompt with the whole diff appended. Following the
label hands the reader a copy of their own diff, from which the only available
conclusion is that the review found nothing -- while the actual findings sit
unread in `r1_review_<model>.txt`.

Found by smoke-running review mode under O7. The mislabel is invisible to every
existing test because nothing asserted on what the mode prints.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop import review_mode
from agent_loop.loop import Finding, PanelResult, Vote
from agent_loop.profiles import Profile, register


def _profile():
    p = Profile(
        name="test-review-artifacts",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        implementer_rules="t", reviewer_priorities="t",
    )
    register(p)
    return p


def _panel(art: Path) -> PanelResult:
    """A panel that answered, with one real finding -- and the per-reviewer
    artifact that `review_panel` writes as a side effect."""
    (art / "r1_review_rev-a.txt").write_text(
        "<<<VERDICT>>>\nREVISE\n<<<END VERDICT>>>\n", encoding="utf-8")
    # `counted` is a property derived from status, not a field.
    vote = Vote(
        model="rev-a", status="REVISE",
        finding_list=[Finding(model="rev-a", severity="MAJOR", text="the thing is wrong")],
    )
    return PanelResult(votes=[vote], verdict="REVISE", valid=True)


def _run(tmp_path):
    prof = _profile()
    art_holder = {}

    def fake_panel(reviewers, prompt, system, art, **kw):
        art_holder["art"] = art
        return _panel(art)

    with patch.object(review_mode, "collect_diff", return_value="--- a\n+++ b\n+x\n"), \
         patch.object(review_mode, "changed_files", return_value=["src/a.py"]), \
         patch.object(review_mode, "commit_subjects", return_value="a commit"), \
         patch.object(review_mode, "review_panel", side_effect=fake_panel), \
         patch.object(review_mode, "append_ledger"):
        review_mode.run_review(
            tmp_path, base="HEAD~1", head="HEAD", profile=prof,
            reviewers=["rev-a"], arbiter_model="",
        )
    return art_holder["art"]


def test_the_prompt_artifact_is_not_labelled_findings(tmp_path, capsys):
    _run(tmp_path)
    out = capsys.readouterr().out

    for line in out.splitlines():
        if "review_prompt.txt" in line:
            assert "findings" not in line.lower(), (
                f"the prompt is labelled as findings: {line!r}"
            )


def test_the_findings_line_names_the_reviewer_artifact(tmp_path, capsys):
    _run(tmp_path)
    out = capsys.readouterr().out

    findings_lines = [ln for ln in out.splitlines() if "findings" in ln.lower()]
    assert findings_lines, "review mode must say where the findings are"
    assert any("r1_review_rev-a.txt" in ln for ln in findings_lines), (
        f"the findings line must name the file the findings are in: {findings_lines}"
    )


def test_the_findings_count_is_reported(tmp_path, capsys):
    """A count is what tells the reader whether opening the file is worth it,
    and it is the one number the prompt artifact could never convey."""
    _run(tmp_path)
    out = capsys.readouterr().out
    # Assert the count as a delimited token. A bare `"1" in line` passes on any
    # digit in the temp path, which is how the first version of this test passed
    # against the unfixed code.
    findings_lines = [ln for ln in out.splitlines() if "findings" in ln.lower()]
    assert any("(1)" in ln for ln in findings_lines), findings_lines
