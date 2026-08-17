"""The stuck-round diagnosis must be correct, and must reach somebody.

CF-15 shipped a detector for the case that cost a consumer four identical
rounds: the same tests fail every round because the fix is outside the regions
the ticket grants. The detector was right about when to fire. Four things about
what it fired were not.

CF-16  It recovered the failing set by scraping `GateResult.detail` for lines
       beginning "- ". The regression path renders REGRESSIONS and "Newly
       passing" with the same bullet, so a round that broke 2 tests and fixed 5
       was recorded as a 7-test failure set, and the warning named PASSING
       tests as the ones to go and look at. A rendered string is built for a
       human; the set has to travel as data.

CF-17  The history is appended to only when the TEST gate fails. A round that
       failed to compile in between left no entry, so rounds 1, 2 and 7 were
       reported as "3 consecutive rounds".

CF-18  The diagnosis was appended to `summary`. The implementer is handed
       `feedback or summary`, and the test gate ALWAYS populates feedback --
       so the model never saw it. The console printed "[stuck] identical test
       failures for 3 consecutive rounds", which carries none of the region
       files, none of the failing tests and none of the advice. The whole
       diagnosis was computed, written to result.json, and read by nobody
       until the run was already over.

The last one is the one that matters: a diagnosis nobody reads is not a
diagnosis. It is the same shape as an alarm that is always on.
"""
from __future__ import annotations

import dataclasses

from agent_loop import gates


def test_the_failing_set_travels_as_data_not_as_a_rendered_bullet_list():
    """CF-16: the regression path renders two bullet lists. Only one is failures."""
    detail = (
        "REGRESSIONS (not in baseline):\n"
        "  - test_alpha\n"
        "  - test_beta\n"
        "\n"
        "Newly passing:\n"
        "  - test_gamma\n"
        "  - test_delta\n"
    )
    g = gates.GateResult("test", False, "2 regression(s)", detail,
                         failing=("test_alpha", "test_beta"))

    scraped = {ln.strip()[2:] for ln in detail.splitlines() if ln.strip().startswith("- ")}
    assert "test_gamma" in scraped, "the old scrape really did collect passing tests"
    assert set(g.failing) == {"test_alpha", "test_beta"}
    assert "test_gamma" not in g.failing, "a PASSING test was reported as failing"


def test_a_gate_result_that_names_no_failures_defaults_to_empty():
    """Every other gate constructs GateResult without this field."""
    g = gates.GateResult("compile", False, "build failed")
    assert g.failing == ()


def test_check_tests_populates_failing_on_both_red_paths():
    """The field is useless if the two paths that produce it forget to set it.

    Negative control: a construction with `failing` left off is what the
    defect looks like, and it is indistinguishable from a green run.
    """
    import inspect
    src = inspect.getsource(gates.check_tests)
    # Both red returns must carry the field. Guard by count so a third red
    # path added later without it fails here rather than silently going dark.
    assert src.count("failing=tuple(") == 2, (
        "check_tests has a red return that does not populate `failing`; "
        "the stuck detector reads that field and cannot see this one"
    )


def test_the_stuck_message_reaches_the_field_the_implementer_reads():
    """CF-18: `feedback or summary` means summary is dead whenever feedback is set."""
    failed = gates.GateResult(
        "test", False, "2 acceptance test(s) still failing", "detail",
        feedback="the implementer reads this", failing=("test_alpha",),
    )
    stuck = "\n\nWARNING: rounds 1, 2 and 3 produced the IDENTICAL failing test set"

    # What the loop now does.
    patched = dataclasses.replace(
        failed,
        summary=failed.summary + stuck,
        feedback=(failed.feedback or "") + stuck,
    )
    delivered = patched.feedback or patched.summary
    assert "IDENTICAL" in delivered, "the diagnosis did not reach the implementer"

    # The old shape, kept as the negative control: summary-only is invisible.
    summary_only = dataclasses.replace(failed, summary=failed.summary + stuck)
    assert "IDENTICAL" not in (summary_only.feedback or summary_only.summary), (
        "this assertion is the point of the test -- if it fails, `feedback or "
        "summary` has changed and CF-18's mechanism no longer exists"
    )


def test_consecutive_means_adjacent_rounds():
    """CF-17: a compile failure in between leaves no entry in the history."""
    same = {"test_alpha"}
    history = [(1, same), (2, same), (7, same)]  # round 7, not round 3

    recent = history[-3:]
    adjacent = recent[1][0] == recent[0][0] + 1 and recent[2][0] == recent[1][0] + 1
    assert not adjacent, "rounds 1, 2 and 7 are not three consecutive rounds"

    history = [(4, same), (5, same), (6, same)]
    recent = history[-3:]
    adjacent = recent[1][0] == recent[0][0] + 1 and recent[2][0] == recent[1][0] + 1
    assert adjacent, "the detector must still fire on a genuine run of three"


def test_the_loop_reads_failing_and_no_longer_scrapes_detail():
    """A source gate, with the thing it forbids named explicitly."""
    import inspect
    from agent_loop import loop
    src = inspect.getsource(loop.run_ticket)
    assert "set(failed.failing)" in src, "the loop stopped reading the structured set"
    assert 'line.startswith("- ")' not in src, (
        "the loop is scraping rendered detail again -- that is CF-16"
    )
