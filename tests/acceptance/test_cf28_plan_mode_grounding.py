"""CF-28 -- plan mode invented a file tree, spent 4 rounds failing to anchor it,
and discarded the good spec.

The planner was given a behavioural defect description that named no files.
`build_intent_context` returned "" (no symbols to find), so the planner saw
nothing about the real tree and invented `src/Overtrading/...` -- a directory
that does not exist in the repo at all. Region extraction raised
`RegionError: file does not exist` on every round, and the loop retried four
times, each round re-emitting the same invented structure with cosmetic
changes, because nothing in the feedback told the model the *architecture*
was wrong rather than the *format*.

Three fixes:

1. **Ground the planner in the real tree.** The defect path now gets
   `build_layout_context` (the file listing the feature path already had),
   so the planner sees real paths before it proposes regions.

2. **Fail fast on a non-existent file.** A file that does not exist will not
   start existing on round 2. The loop now breaks after round 1 on a
   "file does not exist" error, instead of burning 4 rounds.

3. **Save the spec to plan_partial.json.** When regions fail but the spec is
   sound, the ticket is "one search away from usable" (CF-28). The spec is
   preserved with the region errors so the operator can fix the anchors and
   re-run, instead of the sound engineering being thrown away into
   plan_rejected.json.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop import plan_mode
from agent_loop.profiles import Profile, register


PROFILE = Profile(
    name="test-cf28",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    implementer_rules="test", reviewer_priorities="test",
)
register(PROFILE)


# A ticket that names a file which does not exist in the repo
BAD_TICKET_RAW = (
    "<<<TICKET>>>\n"
    + json.dumps({
        "id": "T1",
        "title": "fix the duplicate entry bug",
        "defect": "duplicate orders are not refused",
        "spec": "add a suppression window",
        "regions": [{"id": "R1", "file": "src/Overtrading/DuplicateEntryRule.cs",
                      "anchor": "Evaluate("}],
        "expect_green": ["tests/test_dup.py::test_one"],
    })
    + "\n<<<END TICKET>>>\n"
)

# A ticket that names a real file with a bad anchor (should retry, not fail fast)
GOOD_FILE_BAD_ANCHOR_RAW = (
    "<<<TICKET>>>\n"
    + json.dumps({
        "id": "T1",
        "title": "fix the duplicate entry bug",
        "defect": "duplicate orders are not refused",
        "spec": "add a suppression window",
        "regions": [{"id": "R1", "file": "src/agent_loop/loop.py",
                      "anchor": "ThisAnchorDoesNotExist("}],
        "expect_green": ["tests/test_dup.py::test_one"],
    })
    + "\n<<<END TICKET>>>\n"
)

# A ticket with TWO regions: one in a non-existent file, one with a bad anchor
# in a real file. Both errors should be collected.
MIXED_ERRORS_RAW = (
    "<<<TICKET>>>\n"
    + json.dumps({
        "id": "T1",
        "title": "fix the bug",
        "defect": "something is wrong",
        "spec": "fix it",
        "regions": [
            {"id": "R1", "file": "src/Nonexistent.cs", "anchor": "class Foo"},
            {"id": "R2", "file": "src/agent_loop/loop.py", "anchor": "BadAnchor("},
        ],
        "expect_green": ["tests/test_x.py::test_one"],
    })
    + "\n<<<END TICKET>>>\n"
)


# ---------------------------------------------------------------------------
# Fix 1: the defect path gets layout context (the real file tree)
# ---------------------------------------------------------------------------
def test_defect_path_receives_layout_context(tmp_path):
    """CF-28: the defect path must get build_layout_context, not just
    build_intent_context."""
    src = tmp_path / "src" / "agent_loop"
    src.mkdir(parents=True)
    (src / "loop.py").write_text("def f():\n    pass\n", encoding="utf-8")

    seen = {}

    def fake_chat(model, messages, **kw):
        seen["prompt"] = "\n".join(m["content"] for m in messages)
        from agent_loop.providers import Completion
        return Completion(text="no ticket here", model=model)

    with patch.object(plan_mode, "chat", side_effect=fake_chat):
        plan_mode.run_plan(tmp_path, "a defect", PROFILE, "impl", [], max_rounds=1)

    assert "Where code lives" in seen["prompt"], (
        "the defect path must be grounded in the real file tree"
    )


# ---------------------------------------------------------------------------
# Fix 2: fail fast on a non-existent file (no retry)
# ---------------------------------------------------------------------------
def test_fail_fast_on_nonexistent_file(tmp_path, capsys):
    """CF-28: a file that does not exist will not appear on round 2. The loop
    must break after round 1, not burn 4 rounds."""
    from agent_loop.providers import Completion

    call_count = {"n": 0}

    def fake_chat(model, messages, **kw):
        call_count["n"] += 1
        return Completion(text=BAD_TICKET_RAW, model=model)

    with patch.object(plan_mode, "chat", side_effect=fake_chat):
        result = plan_mode.run_plan(
            tmp_path, "a defect", PROFILE, "impl", [], max_rounds=4,
        )

    assert call_count["n"] == 1, (
        f"a non-existent file must fail fast (1 model call), not retry "
        f"({call_count['n']} calls)"
    )
    assert result["verdict"] == "PLAN_REJECTED_FILE_NOT_FOUND"


def test_retry_on_bad_anchor_in_real_file(tmp_path, capsys):
    """CF-28: a bad anchor inside a REAL file may legitimately need a retry."""
    from agent_loop.providers import Completion

    src = tmp_path / "src" / "agent_loop"
    src.mkdir(parents=True)
    (src / "loop.py").write_text("def real_function():\n    pass\n", encoding="utf-8")

    call_count = {"n": 0}

    def fake_chat(model, messages, **kw):
        call_count["n"] += 1
        return Completion(text=GOOD_FILE_BAD_ANCHOR_RAW, model=model)

    with patch.object(plan_mode, "chat", side_effect=fake_chat):
        result = plan_mode.run_plan(
            tmp_path, "a defect", PROFILE, "impl", [], max_rounds=4,
        )

    assert call_count["n"] > 1, "a bad anchor in a real file should retry"
    assert result["verdict"] != "PLAN_REJECTED_FILE_NOT_FOUND"


# ---------------------------------------------------------------------------
# Fix 3: plan_partial.json preserves the spec when regions fail
# ---------------------------------------------------------------------------
def test_plan_partial_saved_on_nonexistent_file(tmp_path):
    """CF-28: when a region's file does not exist, the spec is saved to
    plan_partial.json with the region errors, not thrown away."""
    from agent_loop.providers import Completion

    def fake_chat(model, messages, **kw):
        return Completion(text=BAD_TICKET_RAW, model=model)

    with patch.object(plan_mode, "chat", side_effect=fake_chat):
        plan_mode.run_plan(tmp_path, "a defect", PROFILE, "impl", [], max_rounds=4)

    partial_path = tmp_path / "logs" / "agent_loop" / "PLAN" / "plan_partial.json"
    assert partial_path.exists(), "plan_partial.json must be written when regions fail"
    partial = json.loads(partial_path.read_text(encoding="utf-8"))
    assert "tickets" in partial
    assert partial["tickets"][0]["spec"] == "add a suppression window"
    assert "region_errors" in partial
    assert len(partial["region_errors"]) > 0
    assert "file does not exist" in partial["region_errors"][0]


def test_plan_partial_collects_all_region_errors(tmp_path):
    """CF-28: when multiple regions fail, ALL errors are collected, not just
    the first."""
    from agent_loop.providers import Completion

    src = tmp_path / "src" / "agent_loop"
    src.mkdir(parents=True)
    (src / "loop.py").write_text("def real_function():\n    pass\n", encoding="utf-8")

    def fake_chat(model, messages, **kw):
        return Completion(text=MIXED_ERRORS_RAW, model=model)

    with patch.object(plan_mode, "chat", side_effect=fake_chat):
        plan_mode.run_plan(tmp_path, "a defect", PROFILE, "impl", [], max_rounds=4)

    partial_path = tmp_path / "logs" / "agent_loop" / "PLAN" / "plan_partial.json"
    assert partial_path.exists()
    partial = json.loads(partial_path.read_text(encoding="utf-8"))
    assert len(partial["region_errors"]) == 2, (
        f"expected 2 region errors, got {len(partial['region_errors'])}"
    )


def test_plan_partial_has_actionable_note(tmp_path):
    """CF-28: the partial plan must tell the operator what to do — fix the
    anchors and re-run."""
    from agent_loop.providers import Completion

    def fake_chat(model, messages, **kw):
        return Completion(text=BAD_TICKET_RAW, model=model)

    with patch.object(plan_mode, "chat", side_effect=fake_chat):
        plan_mode.run_plan(tmp_path, "a defect", PROFILE, "impl", [], max_rounds=4)

    partial_path = tmp_path / "logs" / "agent_loop" / "PLAN" / "plan_partial.json"
    partial = json.loads(partial_path.read_text(encoding="utf-8"))
    assert "note" in partial
    assert "fix" in partial["note"].lower()
    assert "re-run" in partial["note"].lower()