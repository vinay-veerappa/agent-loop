"""
O28/O20 — SHIP is unavailable while a BLOCKER stands rejected.

Two labelled corpus cases say the arbiter does not discriminate. Case 2, from a
real plan review of the NT8 trade copier, is the sharp one: the panel returned
REVISE (glm=7, minimax=13), the arbiter ruled `SHIP (upheld=0 rejected=26
out-of-scope=4)`, and human labelling found FOUR real defects among the rejected
— one of them a signed exit quantity that INCREASES a follower position sitting
opposite the leader, stated by glm with the exact losing sequence this profile's
`arbiter_rules` demand. Across four SHIP rulings in that session the arbiter
upheld 0 of 66 findings.

The existing downgrade covers only SHIP-with-*unruled*-findings, and case 2 ruled
on all thirty. So the gap is a BLOCKER the arbiter addressed and dismissed.

An UPHELD blocker already forces REVISE, so in practice this rule means: SHIP
requires that no counted reviewer filed a BLOCKER at all. That is deliberate and
costs little — the loop stops and waits for a human on ESCALATE and on SHIP
alike, so no rounds are burned; what changes is that the human is told to decide
rather than told a model approved it.
"""
from unittest.mock import patch

from agent_loop import arbiter
from agent_loop.providers import Completion


class _F:
    """Stands in for loop.Finding — every caller passes one of those."""

    def __init__(self, severity, text="a finding", model="glm-5.2:cloud"):
        self.severity = severity
        self.text = text
        self.model = model
        self.blocking = severity in ("BLOCKER", "MAJOR")


TICKET = {"id": "T1", "title": "t", "defect": "d"}


def _answer(rulings, recommendation, rationale="The patch is sound."):
    body = "\n".join(f"- [{verdict}] #{i}: because" for i, verdict in rulings)
    return (
        f"<<<RULINGS>>>\n{body}\n<<<END RULINGS>>>\n"
        f"<<<RECOMMENDATION>>>\n{recommendation}\n<<<END RECOMMENDATION>>>\n"
        f"<<<RATIONALE>>>\n{rationale}\n<<<END RATIONALE>>>\n"
        "<<<SETTLED>>>\n- NONE\n<<<END SETTLED>>>"
    )


def _adjudicate(findings, rulings, recommendation, rationale="The patch is sound."):
    def fake_chat(model, messages, **kw):
        return Completion(text=_answer(rulings, recommendation, rationale), model=model)

    with patch.object(arbiter, "chat", side_effect=fake_chat):
        return arbiter.adjudicate("arb", TICKET, findings, "gates ok", "diff")


def test_a_rejected_blocker_makes_ship_unavailable():
    """Case 2 in miniature: every finding ruled, the blocker dismissed, SHIP."""
    adj = _adjudicate(
        [_F("BLOCKER", "signed exit qty flips a follower sitting opposite"), _F("MINOR")],
        [(1, "REJECTED"), (2, "REJECTED")],
        "SHIP",
    )
    assert adj.ok
    assert adj.recommendation == arbiter.ESCALATE, (
        "an arbiter that rejects a correctly-stated blocker and ships is the "
        "case this rule exists for"
    )


def test_the_escalation_names_the_blocker_a_human_must_judge():
    adj = _adjudicate(
        [_F("MINOR"), _F("BLOCKER", "naked position between submit and accept")],
        [(1, "REJECTED"), (2, "REJECTED")],
        "SHIP",
    )
    assert "#2" in adj.rationale or "[2]" in adj.rationale, (
        "the human has to be told WHICH finding to read; a bare ESCALATE sends "
        "them back through all of them"
    )


def test_the_arbiters_own_rationale_survives_the_downgrade():
    """The reason it thought the blocker did not hold is the thing worth reading."""
    adj = _adjudicate(
        [_F("BLOCKER", "off-tick stop price")],
        [(1, "REJECTED")],
        "SHIP",
        rationale="The clamp two lines above already rounds to the tick.",
    )
    assert "clamp two lines above" in adj.rationale


def test_an_out_of_scope_blocker_also_blocks_ship():
    """OUT_OF_SCOPE is the other way to dismiss one, and it is the softer word."""
    adj = _adjudicate(
        [_F("BLOCKER", "pre-existing naked window")],
        [(1, "OUT_OF_SCOPE")],
        "SHIP",
    )
    assert adj.recommendation == arbiter.ESCALATE


def test_major_and_minor_findings_do_not_block_ship():
    """The rule is BLOCKER-only. Escalating on MAJOR would escalate everything,
    since an adversarial reviewer with no stopping rule always produces one."""
    adj = _adjudicate(
        [_F("MAJOR"), _F("MINOR")],
        [(1, "REJECTED"), (2, "OUT_OF_SCOPE")],
        "SHIP",
    )
    assert adj.recommendation == arbiter.SHIP


def test_an_upheld_blocker_still_revises_rather_than_escalating():
    """Pre-existing behaviour: upheld findings go back to the implementer. That
    is a working loop, not a human decision, and must not become an ESCALATE."""
    adj = _adjudicate(
        [_F("BLOCKER"), _F("MINOR")],
        [(1, "UPHELD"), (2, "REJECTED")],
        "SHIP",
    )
    assert adj.recommendation == arbiter.REVISE


def test_a_rejected_blocker_does_not_disturb_an_honest_revise():
    """The downgrade applies to SHIP only; REVISE already routes back."""
    adj = _adjudicate(
        [_F("BLOCKER"), _F("MAJOR")],
        [(1, "REJECTED"), (2, "UPHELD")],
        "REVISE",
    )
    assert adj.recommendation == arbiter.REVISE
    assert adj.upheld_indices == [2]


def test_unruled_findings_still_escalate_first():
    """The older downgrade must keep its own rationale: 'did not rule on' is a
    different failure from 'ruled and was wrong', and they want different reads."""
    adj = _adjudicate([_F("MINOR"), _F("MINOR")], [(1, "REJECTED")], "SHIP")
    assert adj.recommendation == arbiter.ESCALATE
    assert "did not rule" in adj.rationale


def test_escalated_is_not_promotable_which_is_where_the_rule_gets_its_teeth():
    """Without this the change would only be a label.

    `--apply` writes the patch to the working tree on any PROMOTABLE verdict, and
    ARBITER_SHIP is one. An unattended run whose arbiter dismissed a blocker used
    to land that patch; ESCALATED cannot."""
    from agent_loop import loop

    assert "ARBITER_SHIP" in loop.PROMOTABLE
    assert "ESCALATED" not in loop.PROMOTABLE
    assert "ESCALATED" not in loop.DEVELOPER_PROMOTABLE


def test_the_contract_tells_the_arbiter_the_rule_it_will_be_held_to():
    """A mechanical downgrade the model cannot see produces a verdict that
    contradicts its own rationale. State it in the prompt as well.

    Asserting merely that "BLOCKER" appears would pass on the word's incidental
    uses in the ruling vocabulary — an assertion the pre-fix text also satisfies
    is the most common way to write a test that verifies nothing."""
    contract = arbiter.arbiter_system()
    assert "does NOT license SHIP" in contract, (
        "the arbiter must be told the rule it is held to, in the prompt"
    )
    # And it must survive a consumer supplying its own domain rules, which
    # replace DEFAULT_ARBITER_RULES but not the contract.
    custom = arbiter.arbiter_system("Domain: a CSV parser. Blocking means silent data loss.")
    assert "does NOT license SHIP" in custom
