"""A quorum that equals the panel is not a quorum.

`loop.py` drops a malfunctioning reviewer -- one that times out, or returns
eight times the finding cap -- and proceeds on the survivors, "not allowed to
end the ticket". That rule was written for a three-member panel, where
`ceil(2*3/3) == 2` leaves room for exactly one casualty.

v0.6.6 cut the panel to TWO models. `ceil(2*2/3) == 2`, so the quorum became
unanimity and the drop rule could never fire -- disarmed by a change in a
different file that never mentioned it. Nothing failed; the mechanism simply
stopped having a case.

Measured on a consumer repo, two sessions running: deepseek-v4-flash returned
373 findings and then 853 against a cap of 60. Both times every mechanical
gate had passed, the other reviewer said APPROVE, and the run ended
PANEL_OUTAGE with the patch arbitrated by hand.

⚠️ The general shape: **a rule expressed as a ratio of the population is
disarmed by shrinking the population**, and the code that shrinks it is
nowhere near the code that reads it.
"""
from __future__ import annotations

import math

import pytest


def quorum_for(n_reviewers: int) -> int:
    """The rule as loop.py now computes it."""
    q = math.ceil(2 * n_reviewers / 3) if n_reviewers else 1
    if n_reviewers >= 2:
        q = min(q, n_reviewers - 1)
    return q


@pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 7])
def test_one_malfunctioning_reviewer_is_always_survivable(n):
    """The whole point of the drop rule, at every panel size."""
    assert quorum_for(n) <= n - 1, (
        f"a panel of {n} requires {quorum_for(n)} answers, so one malfunctioning "
        f"member ends the ticket -- which is the behaviour the drop rule exists "
        f"to prevent"
    )


def test_the_two_model_panel_is_the_measured_case():
    """This is the configuration that actually ships."""
    assert quorum_for(2) == 1, (
        "with two reviewers the quorum must be 1; at 2 it is unanimity wearing "
        "the word quorum, and that is what produced PANEL_OUTAGE twice"
    )


@pytest.mark.parametrize("n,expected", [(3, 2), (4, 3), (5, 4), (6, 4)])
def test_the_two_thirds_rule_is_otherwise_unchanged(n, expected):
    """Negative control: this fix must not quietly loosen larger panels.

    For every size where ceil(2n/3) already left room for a casualty, the
    answer must be exactly what it was before.
    """
    assert quorum_for(n) == expected


def test_a_single_reviewer_panel_still_needs_its_one_answer():
    """Degenerate but reachable: n-1 would be 0, which accepts an empty panel."""
    assert quorum_for(1) == 1, "a one-model panel with no answer is not a review"


def test_no_reviewers_does_not_divide_by_anything():
    assert quorum_for(0) == 1


def test_loop_uses_this_rule_and_not_a_bare_ratio():
    """Source gate. A regex cannot see reachability, so pin BOTH halves.

    The negative control is the second assertion: if someone reverts to the
    bare ceil, the first still passes because that line is still there.
    """
    import inspect
    from agent_loop import loop
    src = inspect.getsource(loop.run_ticket)
    assert "math.ceil(2 * len(reviewers) / 3)" in src, "the base rule vanished"
    assert "quorum = min(quorum, len(reviewers) - 1)" in src, (
        "the cap that keeps a quorum from becoming unanimity is gone; on the "
        "shipped two-model panel the drop rule is disarmed again"
    )
