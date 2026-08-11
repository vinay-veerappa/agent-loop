"""
O36 entry point: plan a FEATURE, decomposed, each part test-first.

Two decisions, both made by the user rather than inferred:

  1. "I do expect a feature to get broken down into smaller parts."
  2. "A feature should also go through the same TDD cycle."

Together they answer question 4 (what the acceptance criterion for a feature is)
without inventing a second one: it is the SAME criterion. Every ticket in a
feature plan names the tests that must go red and then green, so each part is
gated by the ladder the defect path already uses. Nothing new is required of the
loop -- the O34 feature exception is what lets those tests be red for the right
reason before the code exists.

The consequence that needs handling: a decomposed plan is ORDERED, and ticket 2's
regions may live in files ticket 1 has not created yet. Validating the whole plan
against the current tree would reject every plan that builds on itself, so
validation walks the tickets in order and carries forward the files earlier
tickets create.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop import cli, plan_mode
from agent_loop.profiles import Profile, register
from agent_loop.providers import Completion


PROFILE = Profile(
    name="test-o36-feature",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    test_sources=("tests/*.py",),
    context_token_budget=3000,
    implementer_rules="t", reviewer_priorities="t",
)
register(PROFILE)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "import os\n"
        "\n"
        "\n"
        "def main():\n"
        "    return 0\n",
        encoding="utf-8",
    )
    return tmp_path


def _ticket(tid, regions_, expect_green, depends_on=None):
    t = {
        "id": tid,
        "title": f"part {tid}",
        "defect": "the feature does not exist yet",
        "spec": f"build part {tid}",
        "regions": regions_,
        "expect_green": expect_green,
    }
    if depends_on:
        t["depends_on"] = depends_on
    return t


def _reply(tickets):
    parts = []
    for t in tickets:
        parts.append("<<<TICKET>>>\n" + json.dumps(t) + "\n<<<END TICKET>>>")
    parts.append("<<<NOTES>>>\nbecause\n<<<END NOTES>>>")
    return Completion(text="\n".join(parts), model="m")


TWO_PART_PLAN = [
    _ticket(
        "F1",
        [{"id": "R1", "file": "src/widget.py", "op": "create"}],
        ["tests/test_widget.py::test_widget_exists"],
    ),
    # Part 2 edits a file part 1 creates: unresolvable against the tree as it is
    # now, and legitimate all the same.
    _ticket(
        "F2",
        [{"id": "R1", "file": "src/widget.py", "op": "insert", "anchor": "def widget"},
         {"id": "R2", "file": "src/app.py", "op": "insert", "anchor": "import os", "kind": "line"}],
        ["tests/test_widget.py::test_widget_is_wired_in"],
        depends_on=["F1"],
    ),
]


def _run(repo, tickets, **kw):
    with patch.object(plan_mode, "chat", return_value=_reply(tickets)):
        return plan_mode.run_plan(
            repo, "add a widget subsystem", PROFILE, "impl", [],
            max_rounds=1, fast_plan=True, feature=True, **kw,
        )


# --------------------------------------------------------------------------
# decision 1: a feature is broken into ordered parts
# --------------------------------------------------------------------------
def test_a_feature_plan_can_emit_several_tickets(repo):
    result = _run(repo, TWO_PART_PLAN)
    assert result.get("plan"), result
    assert [t["id"] for t in result["plan"]] == ["F1", "F2"], "order must be preserved"


def test_the_written_file_is_the_wrapper_shape_the_loop_reads(repo):
    _run(repo, TWO_PART_PLAN)
    written = repo / "logs" / "agent_loop" / "PLAN" / "plan.json"
    tickets = cli.load_tickets(written)
    assert [t["id"] for t in tickets] == ["F1", "F2"]


def test_a_single_part_feature_is_still_a_list(repo):
    """One part is a degenerate decomposition, not a different shape. A caller
    that has to branch on "one or many" will get it wrong."""
    result = _run(repo, [TWO_PART_PLAN[0]])
    assert isinstance(result["plan"], list)
    assert len(result["plan"]) == 1


def test_a_defect_plan_still_returns_a_single_ticket(repo):
    """feature=False is the old contract, and callers depend on it."""
    with patch.object(plan_mode, "chat", return_value=_reply([
        _ticket("T1", [{"id": "R1", "file": "src/app.py", "anchor": "def main"}],
                ["tests/test_app.py::test_main"]),
    ])):
        result = plan_mode.run_plan(
            repo, "main returns the wrong code", PROFILE, "impl", [],
            max_rounds=1, fast_plan=True,
        )
    assert isinstance(result["plan"], dict), "a defect plan must stay one ticket"
    assert result["plan"]["id"] == "T1"


# --------------------------------------------------------------------------
# decision 2: every part goes through the same TDD cycle
# --------------------------------------------------------------------------
def test_a_part_with_no_acceptance_tests_is_refused(repo):
    """The whole point of decision 2. A part with no expect_green cannot be
    gated by the ladder, so the plan is not usable however good it reads."""
    bad = [
        TWO_PART_PLAN[0],
        _ticket("F2", [{"id": "R1", "file": "src/app.py", "op": "insert",
                        "anchor": "import os", "kind": "line"}], []),
    ]
    result = _run(repo, bad)
    assert not result.get("plan"), "a part with no red test must not be accepted"
    assert "expect_green" in (result.get("error") or result.get("verdict") or "")


def test_the_prompt_demands_a_test_per_part(repo):
    seen = {}

    def fake_chat(model, messages, **kw):
        seen["prompt"] = "\n".join(m["content"] for m in messages)
        return _reply(TWO_PART_PLAN)

    with patch.object(plan_mode, "chat", side_effect=fake_chat):
        plan_mode.run_plan(
            repo, "add a widget subsystem", PROFILE, "impl", [],
            max_rounds=1, fast_plan=True, feature=True,
        )
    p = seen["prompt"].lower()
    assert "expect_green" in p
    assert "fail" in p and "before" in p, "the red-first requirement must be stated"
    assert "tests/*.py" in seen["prompt"], "and where tests are allowed to live"


def test_the_prompt_asks_for_a_decomposition(repo):
    seen = {}

    def fake_chat(model, messages, **kw):
        seen["prompt"] = "\n".join(m["content"] for m in messages)
        return _reply(TWO_PART_PLAN)

    with patch.object(plan_mode, "chat", side_effect=fake_chat):
        plan_mode.run_plan(
            repo, "add a widget subsystem", PROFILE, "impl", [],
            max_rounds=1, fast_plan=True, feature=True,
        )
    p = seen["prompt"].lower()
    assert "smallest" in p or "smaller" in p or "decompos" in p
    assert "op" in p and "create" in p, "the ops must be documented to the model"


# --------------------------------------------------------------------------
# the ordering consequence: later parts build on earlier ones
# --------------------------------------------------------------------------
def test_a_later_part_may_touch_a_file_an_earlier_part_creates(repo):
    """F2 inserts into src/widget.py, which does not exist until F1 runs.
    Validating the plan against the tree as it is now would reject every plan
    that builds on itself -- which is every real feature."""
    result = _run(repo, TWO_PART_PLAN)
    assert result.get("plan"), result.get("error") or result.get("verdict")


def test_a_part_touching_a_file_nobody_creates_is_still_refused(repo):
    """The relaxation is scoped to files an EARLIER part creates. A typo in a
    path must not be waved through by it."""
    bad = [
        TWO_PART_PLAN[0],
        _ticket("F2", [{"id": "R1", "file": "src/nowhere.py", "op": "insert",
                        "anchor": "def x"}],
                ["tests/test_widget.py::test_x"], depends_on=["F1"]),
    ]
    result = _run(repo, bad)
    assert not result.get("plan"), "an unresolvable region was accepted"


def test_two_parts_cannot_both_create_the_same_file(repo):
    """The second create would refuse at extract time anyway, once the first has
    run. Catching it in the plan is cheaper than discovering it three tickets in."""
    bad = [
        TWO_PART_PLAN[0],
        _ticket("F2", [{"id": "R1", "file": "src/widget.py", "op": "create"}],
                ["tests/test_widget.py::test_again"]),
    ]
    result = _run(repo, bad)
    assert not result.get("plan"), "the same file was created twice"


# --------------------------------------------------------------------------
# through main(argv), because that is the wiring that has been broken twice
# --------------------------------------------------------------------------
def test_the_cli_accepts_feature_and_forwards_it():
    captured = {}

    def fake_run_plan(repo, description, profile, implementer, reviewers, **kw):
        captured["description"] = description
        captured.update(kw)
        return {"plan": [{"id": "F1"}], "verdict": "APPROVE"}

    with patch("agent_loop.plan_mode.run_plan", fake_run_plan):
        code = cli.main([
            "--mode", "plan", "--profile", "test-o36-feature",
            "--feature", "add a widget subsystem",
        ])
    assert code == 0
    assert captured.get("feature") is True, "--feature did not reach run_plan"
    assert captured["description"] == "add a widget subsystem"


def test_the_cli_still_accepts_defect_and_does_not_set_feature():
    captured = {}

    def fake_run_plan(repo, description, profile, implementer, reviewers, **kw):
        captured.update(kw)
        captured["description"] = description
        return {"plan": {"id": "T1"}, "verdict": "APPROVE"}

    with patch("agent_loop.plan_mode.run_plan", fake_run_plan):
        code = cli.main([
            "--mode", "plan", "--profile", "test-o36-feature",
            "--defect", "main returns the wrong code",
        ])
    assert code == 0
    assert not captured.get("feature")


def test_plan_mode_needs_one_of_defect_or_feature(capsys):
    code = cli.main(["--mode", "plan", "--profile", "test-o36-feature"])
    assert code == 2
    out = capsys.readouterr().out
    assert "--defect" in out and "--feature" in out


def test_defect_and_feature_together_are_refused(capsys):
    """They select different prompts and different output shapes. Silently
    preferring one would make the other a no-op the caller cannot see."""
    code = cli.main([
        "--mode", "plan", "--profile", "test-o36-feature",
        "--defect", "d", "--feature", "f",
    ])
    assert code == 2
    assert "both" in capsys.readouterr().out.lower()


# --------------------------------------------------------------------------
# a feature request names nothing that exists, so symbols cannot orient it
# --------------------------------------------------------------------------
#
# Found on the first live run. The plan came back well-formed -- four ordered
# parts, ops correct, expect_green on each -- and every file was under a
# `patchgate/` package that does not exist. The model had no idea the code lives
# in `src/agent_loop/`, because O31's context is keyed on SYMBOLS and a feature
# request mentions none: `extract_intent_symbols` returned [] and the context was
# "".
#
# That is structural, not a tuning problem. For a feature the question is not
# "where is the thing you named" but "where does new code GO", and only the layout
# answers it.
def test_layout_context_names_the_real_source_roots(repo):
    from agent_loop import context

    out = context.build_layout_context(repo, PROFILE)
    assert "src/" in out or "src" in out, out
    assert "tests" in out, out


def test_layout_context_is_produced_for_a_request_with_no_symbols(repo):
    from agent_loop import context

    intent = "add a flag that writes machine-readable output to stdout"
    assert context.extract_intent_symbols(intent) == []
    assert context.build_layout_context(repo, PROFILE).strip(), (
        "a request that names nothing still needs to know where code lives"
    )


def test_the_feature_prompt_carries_the_layout(repo):
    seen = {}

    def fake_chat(model, messages, **kw):
        seen["prompt"] = "\n".join(m["content"] for m in messages)
        return _reply(TWO_PART_PLAN)

    with patch.object(plan_mode, "chat", side_effect=fake_chat):
        plan_mode.run_plan(
            repo, "add a flag that writes machine-readable output", PROFILE, "impl", [],
            max_rounds=1, fast_plan=True, feature=True,
        )
    assert "src/app.py" in seen["prompt"], (
        "the feature prompt must show where code lives, or the model invents a "
        f"package: {seen['prompt'][:400]}"
    )


def test_the_layout_respects_the_file_scope_whitelist(tmp_path):
    """A profile that may only edit `scripts/` must not be shown the whole repo as
    a candidate home for new files."""
    from agent_loop import context

    for d in ("scripts", "web", "data"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "mod.py").write_text("x = 1\n", encoding="utf-8")
    prof = Profile(
        name="test-o36-scope",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        file_scope_whitelist=("scripts/",),
        implementer_rules="t", reviewer_priorities="t",
    )
    register(prof)

    out = context.build_layout_context(tmp_path, prof)
    assert "scripts" in out
    assert "web/mod.py" not in out and "data/mod.py" not in out, out
