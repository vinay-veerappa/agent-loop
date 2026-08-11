"""
O33: plan mode's output must be loadable by the things that consume tickets.

Plan mode exists to turn a defect into a ticket the loop can run. It wrote
`plan.json` as a BARE ticket object while both consumers -- `--mode test` and
the main `--tickets` path -- did `spec["tickets"]`. So the documented pipeline

    --mode plan  ->  --mode test  ->  --tickets

died at the first seam with an unhandled `KeyError: 'tickets'`, and had never
worked end to end. README.md documents `--mode test --tickets plan.json`
explicitly, which is the invocation that cannot work.

Two halves, because a fix to either alone leaves a trap:

  * plan mode writes the shape the loaders expect; and
  * ONE loader, used by both call sites, that accepts either shape and reports a
    bad file instead of raising KeyError from inside a dict subscript.

The second half matters for the plan.json files already on disk, which are bare
objects, and for every hand-written ticket file a consumer already has.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop import cli
from agent_loop.profiles import Profile, register


PROFILE = Profile(
    name="test-o33-tickets",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    implementer_rules="test", reviewer_priorities="test",
)
register(PROFILE)


TICKET = {
    "id": "T1",
    "title": "a ticket",
    "defect": "something is wrong",
    "spec": "make it right",
    "regions": [{"id": "R1", "file": "src/a.py", "anchor": "def f"}],
    "expect_green": ["tests/acceptance/test_a.py::test_f"],
}


def _write(tmp_path: Path, payload) -> str:
    p = tmp_path / "tickets.json"
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(p)


# --------------------------------------------------------------------------
# The loader accepts both shapes
# --------------------------------------------------------------------------
def test_the_wrapper_shape_loads(tmp_path):
    assert cli.load_tickets(Path(_write(tmp_path, {"tickets": [TICKET]}))) == [TICKET]


def test_a_bare_ticket_object_loads(tmp_path):
    """This is exactly what plan mode wrote, and what is on disk already."""
    assert cli.load_tickets(Path(_write(tmp_path, TICKET))) == [TICKET]


def test_a_bare_list_of_tickets_loads(tmp_path):
    assert cli.load_tickets(Path(_write(tmp_path, [TICKET]))) == [TICKET]


# --------------------------------------------------------------------------
# ... and refuses anything else with a message, not a KeyError
# --------------------------------------------------------------------------
def test_a_file_of_the_wrong_shape_is_reported_not_raised(tmp_path):
    """`spec["tickets"]` raised KeyError from inside a subscript -- a traceback,
    with no statement of what was expected or what was found."""
    path = Path(_write(tmp_path, {"defects": [TICKET]}))
    with pytest.raises(cli.TicketFileError) as exc:
        cli.load_tickets(path)
    msg = str(exc.value)
    assert "tickets" in msg, msg
    assert str(path) in msg, "say which file"


def test_an_object_without_an_id_is_not_mistaken_for_a_ticket(tmp_path):
    with pytest.raises(cli.TicketFileError):
        cli.load_tickets(Path(_write(tmp_path, {"title": "no id here"})))


def test_a_ticket_inside_the_wrapper_must_also_have_an_id(tmp_path):
    """Distinct from the case above, which never reaches the per-ticket check:
    a bare dict with no `id` is rejected by the shape branch. This one is well
    -formed at the top level and malformed inside, and both consumers index on
    `id` -- `--ticket T1` and `t["id"]` in the run loop. Deleting the per-ticket
    check left the test above green, which is how this gap was found."""
    path = Path(_write(tmp_path, {"tickets": [TICKET, {"title": "no id"}]}))
    with pytest.raises(cli.TicketFileError) as exc:
        cli.load_tickets(path)
    assert "id" in str(exc.value)
    assert "1" in str(exc.value), "say WHICH ticket is malformed"


def test_an_empty_ticket_list_is_reported(tmp_path):
    with pytest.raises(cli.TicketFileError):
        cli.load_tickets(Path(_write(tmp_path, {"tickets": []})))


# --------------------------------------------------------------------------
# Through main(argv), because that is the wiring that was broken
# --------------------------------------------------------------------------
def test_plan_output_can_be_listed_by_the_loop(tmp_path, capsys):
    """`--list` costs no model call and exercises the real loader path.

    The exit code here is about whether the REGIONS resolve -- this fixture's
    `src/a.py` deliberately does not exist -- so assert the thing under test:
    the file parsed and its ticket reached `--list`, instead of raising KeyError
    before the loop saw anything. Exit 2 is reserved for a load failure.
    """
    path = _write(tmp_path, TICKET)
    code = cli.main(["--profile", "test-o33-tickets", "--tickets", path, "--list"])
    out = capsys.readouterr().out  # capsys drains: read it ONCE
    assert "T1" in out and "a ticket" in out, out
    assert code != 2, f"the ticket file failed to load: {out}"


def test_plan_output_can_be_fed_to_test_mode(tmp_path):
    """The documented pipeline. This raised KeyError before reaching run_test."""
    path = _write(tmp_path, TICKET)
    captured = {}

    def fake_run_test(repo, defect, ticket, profile, implementer, **kw):
        captured["ticket"] = ticket
        return {"test_code": "def test_x(): pass"}

    with patch("agent_loop.test_mode.run_test", fake_run_test):
        code = cli.main([
            "--mode", "test", "--profile", "test-o33-tickets",
            "--defect", "d", "--tickets", path, "--ticket", "T1",
        ])
    assert code == 0
    assert captured["ticket"]["id"] == "T1", "the ticket must reach run_test"


def test_a_bad_ticket_file_exits_cleanly_rather_than_tracebacking(tmp_path, capsys):
    path = _write(tmp_path, {"defects": []})
    code = cli.main(["--profile", "test-o33-tickets", "--tickets", path, "--list"])
    assert code == 2, "a malformed ticket file is a usage error"
    assert "tickets" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Plan mode writes the shape it promises
# --------------------------------------------------------------------------
def test_run_plan_writes_a_file_the_loader_accepts(tmp_path):
    from agent_loop import plan_mode

    raw = (
        "<<<TICKET>>>\n" + json.dumps(TICKET) + "\n<<<END TICKET>>>\n"
        "<<<RATIONALE>>>\nbecause\n<<<END RATIONALE>>>\n"
    )

    from agent_loop.providers import Completion

    # fast_plan skips the panel, so this needs two doubles instead of six.
    # `review_panel` is imported INSIDE run_plan, so patching plan_mode.review_panel
    # would silently do nothing -- the first version of this test did that.
    with patch.object(plan_mode, "chat", return_value=Completion(text=raw, model="m")), \
         patch.object(plan_mode, "build_context_slice", return_value=""), \
         patch.object(plan_mode.regions, "extract", return_value=[]):
        plan_mode.run_plan(
            tmp_path, "a defect", PROFILE, "impl", [], max_rounds=1, fast_plan=True,
        )

    written = tmp_path / "logs" / "agent_loop" / "PLAN" / "plan.json"
    assert written.exists(), "plan mode wrote no plan.json"
    assert cli.load_tickets(written)[0]["id"] == "T1"


# --------------------------------------------------------------------------
# The ticket must name tests where the harness is allowed to write them
# --------------------------------------------------------------------------
def test_the_plan_prompt_states_where_tests_live(tmp_path):
    """On the live O7 run, plan produced `expect_green:
    tests/test_review_mode.py::...` -- but this repo's tests live in
    tests/acceptance/ and the profile declares
    test_sources=("tests/acceptance/*.py",). The model was never shown that, so
    it invented a path the test-first machinery is not allowed to write to.
    """
    from agent_loop import plan_mode
    from agent_loop.providers import Completion

    prof = Profile(
        name="test-o33-testsources",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        test_sources=("tests/acceptance/*.py",),
        implementer_rules="test", reviewer_priorities="test",
    )
    register(prof)

    seen = {}

    def fake_chat(model, messages, **kw):
        seen["prompt"] = "\n".join(m["content"] for m in messages)
        return Completion(text="no ticket here", model=model)

    with patch.object(plan_mode, "chat", side_effect=fake_chat):
        plan_mode.run_plan(tmp_path, "a defect", prof, "impl", [], max_rounds=1)

    assert "tests/acceptance/*.py" in seen["prompt"], (
        "the model cannot honour a convention it was never told"
    )
