"""A thinking mode's budget must fit its ANSWER as well as its reasoning.

On a reasoning model the chain-of-thought is spent from the same budget as the
answer, so a mode that thinks and emits a whole document needs headroom that a
mode emitting one tool call does not.

The live failure: plan mode shipped `max_tokens=48000, think=True`, and a 5.5 KB
five-part feature brief made the planner spend 207,078 characters of reasoning
and return EMPTY CONTENT (eval_count=48000, done_reason=length). The package's
own example config had been warning about that exact number the whole time --
which is the real lesson here: a documented hazard that is also the shipped
default is not documented.

Deliberately NOT a general law like "every thinking mode >= the implementer
role's budget": `developer` keeps 48000 with thinking on for a recorded reason
(its answer is a single tool call, and the smaller ceiling bounds a fifteen-turn
run). Inventing a law that contradicts a documented decision would just get the
law deleted.
"""
from __future__ import annotations

from agent_loop import config

# The number that produced empty content, twice, on two different briefs.
KNOWN_BAD_WITH_THINKING = 48000

# Modes whose answer is a whole document or a whole ticket set, re-emitted in
# full every round. These are the ones that cannot share a tool-call budget.
WHOLE_ARTIFACT_MODES = ("plan", "test")


def test_whole_artifact_thinking_modes_have_headroom():
    cfg = config.get()
    for name in WHOLE_ARTIFACT_MODES:
        ms = cfg.mode(name)
        assert ms.think, f"{name} is expected to think; if that changed, revisit this test"
        assert ms.max_tokens > KNOWN_BAD_WITH_THINKING, (
            f"mode {name!r} thinks and emits a whole artifact, but its budget is "
            f"{ms.max_tokens} -- at or below the {KNOWN_BAD_WITH_THINKING} that "
            f"returned 207,078 chars of reasoning and empty content"
        )


def test_plan_and_test_match_the_implementer_role_budget():
    """These emit patch-sized output, so they get patch-sized budgets."""
    cfg = config.get()
    role = cfg.role("implementer")
    for name in WHOLE_ARTIFACT_MODES:
        assert cfg.mode(name).max_tokens >= role.max_tokens, (
            f"{name} re-emits its whole artifact each round, like an implementer turn"
        )


def test_developer_mode_keeps_its_tighter_budget_on_purpose():
    """Pins the CONTRAST, so a future edit does not "fix" it by raising it too.

    If someone applies the plan-mode reasoning here by analogy, this fails and
    sends them to the comment explaining why one tool call per turn is different.
    """
    ms = config.get().mode("developer")
    assert ms.think
    assert ms.max_tokens == KNOWN_BAD_WITH_THINKING, (
        "developer mode's 48000 is a recorded decision (one tool call per turn, "
        "bounding a fifteen-turn run), not an oversight -- see config.py"
    )
