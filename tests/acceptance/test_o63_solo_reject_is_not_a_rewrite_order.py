"""O63: one reviewer's fixed disposition must not order a rewrite.

MEASURED, 2026-08-11, reviewer bench, one candidate that passed every mechanical
gate, across every arm and repetition:

    glm-5.2            REVISE  x4
    minimax-m3         APPROVE x4
    deepseek-v4-flash  REJECT  x4  (+1 REVISE)
    gemma4:31b         REJECT
    qwen3.5            REJECT

The verdict is a property of the MODEL, not of the patch. That matters because
the panel takes the WORST verdict and REJECT is not merely "worse than REVISE" --
it selects a different branch in the round loop, telling the implementer:

    RETHINK THE APPROACH -- do not just tweak these lines.
    Re-emit ALL blocks in full with a fundamentally different approach.

That is the most destructive instruction the loop can give. With a reviewer stuck
on REJECT it would fire every round, on every candidate, and no amount of fixing
would ever satisfy it -- the loop would discard working code indefinitely.

So a REJECT needs corroboration. Findings are NOT affected: every finding from
every counted reviewer still reaches the arbiter, and a REJECT still blocks
exactly as a REVISE does. Only the strategy signal requires two voices.

These tests drive the REAL review_panel with `chat` stubbed at the seam, rather
than reproducing its aggregation in a helper. A helper that recomputes the rule
under test passes whether or not the production code has it -- which is the
failure mode this repo has hit before.
"""
from __future__ import annotations

import pytest

from agent_loop import loop as loop_mod
from agent_loop.loop import APPROVE, REJECT, REVISE, review_panel
from agent_loop.providers import Completion


def _review_text(verdict: str, finding: str) -> str:
    return (
        f"<<<VERDICT>>>\n{verdict}\n<<<END VERDICT>>>\n"
        f"<<<FINDINGS>>>\n- [MAJOR] R1: {finding}\n<<<END FINDINGS>>>\n"
        f"<<<REQUIRED>>>\n- do the thing\n<<<END REQUIRED>>>\n"
    )


@pytest.fixture
def panel_of(tmp_path, monkeypatch):
    """Run the real review_panel over a scripted verdict per model."""
    def run(*verdicts: str):
        script = {f"m{i}": v for i, v in enumerate(verdicts)}

        def fake_chat(model, messages, **kw):
            return Completion(text=_review_text(script[model], f"{model} spoke"),
                              model=model, output_tokens=100)

        monkeypatch.setattr(loop_mod, "chat", fake_chat)
        return review_panel(list(script), "prompt", "system", tmp_path, 1)
    return run


def test_a_lone_reject_becomes_revise(panel_of):
    """The measured case: deepseek-v4-flash REJECTs, glm says REVISE."""
    assert panel_of(REVISE, REJECT).verdict == REVISE


def test_a_lone_reject_against_an_approve_still_blocks(panel_of):
    """Downgraded to REVISE, NOT to APPROVE. The candidate is still refused --
    only the 'start over' instruction is withheld."""
    assert panel_of(APPROVE, REJECT).verdict == REVISE


def test_two_rejects_still_reject(panel_of):
    """Corroborated, so the rewrite instruction is warranted and still fires.
    Without this the rule would have removed REJECT from the system entirely,
    which is a different change from the one intended."""
    assert panel_of(REJECT, REJECT).verdict == REJECT


def test_a_lone_reject_on_a_three_member_panel_is_still_downgraded(panel_of):
    assert panel_of(APPROVE, REVISE, REJECT).verdict == REVISE


def test_two_of_three_rejecting_is_enough(panel_of):
    assert panel_of(APPROVE, REJECT, REJECT).verdict == REJECT


def test_the_rule_does_not_touch_anything_below_reject(panel_of):
    """Worst-wins is unchanged everywhere except the REJECT branch."""
    assert panel_of(APPROVE, REVISE).verdict == REVISE
    assert panel_of(APPROVE, APPROVE).verdict == APPROVE


def test_the_downgrade_keeps_every_finding(panel_of):
    """The rule must change the STRATEGY signal only. A downgrade that also
    dropped the rejecting reviewer's findings would silently lose the review."""
    panel = panel_of(APPROVE, REJECT)
    assert panel.verdict == REVISE
    assert "m1 spoke" in panel.findings, "the rejecting reviewer's finding survived"
    assert "m0 spoke" in panel.findings
