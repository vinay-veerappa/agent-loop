"""
O61 — a reviewer that is truncated loses EVERY finding, silently.

`parse_review`'s `section()` requires both markers:

    re.search(rf"<<<{name}>>>\\r?\\n(.*?)<<<END {name}>>>", text, re.DOTALL)

so a response cut off mid-FINDINGS matches nothing and yields "". The verdict
block, which closed early in the response, still parses. The result is a counted
**REVISE with zero findings**.

Observed live on CM2 round 2. glm-5.2 degenerated and ran to **190,129 bytes**
containing **1,219 findings, 979 of them BLOCKERs**, and was cut off before
`<<<END FINDINGS>>>`. The loop reported:

    [panel] REVISE  [minimax-m3=APPROVE(0), glm-5.2=REVISE(0)]

Three consequences, and the third is the one that matters:

1. The arbiter is never consulted, because `all_findings` is empty.
2. A whole round is spent re-emitting against empty feedback (round 3: 14.2s and
   2143 tokens to change nothing).
3. **The blocker rule cannot fire.** `SHIP` is unavailable while a BLOCKER stands
   dismissed — but 979 of them parsed as zero, so a truncated reviewer silently
   disarms the gate that was added specifically to stop the arbiter shipping over
   one.

`arbiter.py` already fixed exactly this for its own sections, and the reason is
recorded there: *"A strict opener/closer match silently returns "" for a section
whose END tag is wrong, and there is no way to tell that from 'the model said
nothing'."* The reviewer parser never got the same treatment.

Truncated findings are still findings. Better to act on 1,219 of them than on
none.
"""
from __future__ import annotations

from agent_loop.loop import parse_review

HEAD = (
    "<<<VERDICT>>>\nREVISE\n<<<END VERDICT>>>\n"
    "<<<FINDINGS>>>\n"
)


def test_findings_survive_a_missing_closing_marker():
    text = HEAD + (
        "- [BLOCKER] R1: the lock is held across the broker call\n"
        "- [MAJOR] R1: the exit quantity is signed\n"
        "- [MINOR] R1: a name could be clearer\n"
        # cut off here: no <<<END FINDINGS>>>, no REQUIRED section
    )
    vote = parse_review(text, "glm-5.2:cloud")
    assert vote.status == "REVISE"
    assert len(vote.finding_list) == 3, (
        f"a truncated reviewer lost every finding it produced; got "
        f"{len(vote.finding_list)}"
    )
    assert vote.blockers == 2, "BLOCKER and MAJOR both block"


def test_the_blocker_rule_can_still_see_a_truncated_blocker():
    """The specific consequence worth naming: 979 BLOCKERs parsed as zero, and
    the rule that makes SHIP unavailable over a dismissed BLOCKER is disarmed by
    a reviewer running out of room."""
    text = HEAD + "- [BLOCKER] R1: a naked position between submit and accept\n"
    vote = parse_review(text, "glm-5.2:cloud")
    assert any(f.severity == "BLOCKER" for f in vote.finding_list)


def test_a_well_formed_response_is_unchanged():
    text = (
        "<<<VERDICT>>>\nREVISE\n<<<END VERDICT>>>\n"
        "<<<FINDINGS>>>\n- [MAJOR] R1: something\n<<<END FINDINGS>>>\n"
        "<<<REQUIRED>>>\n- do the thing\n<<<END REQUIRED>>>"
    )
    vote = parse_review(text, "m")
    assert len(vote.finding_list) == 1
    assert "do the thing" in vote.required


def test_findings_do_not_bleed_in_from_a_later_section():
    """The fix must stop at the next marker, not run to end of file. A REQUIRED
    block listing '- [MAJOR] ...' style lines would otherwise be counted twice."""
    text = (
        "<<<VERDICT>>>\nREVISE\n<<<END VERDICT>>>\n"
        "<<<FINDINGS>>>\n- [MAJOR] R1: the real one\n"
        "<<<REQUIRED>>>\n- [MAJOR] R1: this is an instruction, not a finding\n"
    )
    vote = parse_review(text, "m")
    assert len(vote.finding_list) == 1, (
        "the REQUIRED section was swallowed into FINDINGS"
    )
    assert "the real one" in vote.finding_list[0].text


def test_an_empty_body_is_still_unparseable():
    """The older rule stands: no verdict is UNPARSEABLE, never a silent REVISE."""
    assert parse_review("", "m").status == "UNPARSEABLE"
    assert parse_review("no markers at all", "m").status == "UNPARSEABLE"


def test_a_verdict_with_no_findings_section_is_not_invented():
    """An APPROVE that legitimately has nothing to say must stay at zero."""
    text = "<<<VERDICT>>>\nAPPROVE\n<<<END VERDICT>>>\n<<<FINDINGS>>>\n- NONE\n"
    vote = parse_review(text, "m")
    assert vote.status == "APPROVE"
    assert vote.finding_list == []


# ---------------------------------------------------------------------------
# The other half: recovering 1219 findings must not just move the failure
# ---------------------------------------------------------------------------
def test_a_degenerate_reviewer_is_unparseable_not_a_1219_finding_verdict():
    """Recovering truncated findings creates a new way to fail if it stops there.

    The real CM2 artifact yields 1,219 findings once the closing marker is no
    longer required. Feeding those to the arbiter builds a prompt with 1,219
    numbered items and blows its budget with certainty -- trading a silent drop
    for a guaranteed downstream failure.

    A reviewer emitting hundreds of findings is not reviewing, it is repeating:
    normal output on this profile is 4 to 13. Treated as UNPARSEABLE, which
    makes the panel INVALID -- 'NOT a rejection', the same handling as an empty
    response -- so the round is retried instead of being decided on garbage.
    """
    from agent_loop import config

    cap = config.get().loop.max_findings_per_reviewer
    text = HEAD + "".join(
        f"- [BLOCKER] R1: repeated finding number {i}\n" for i in range(cap + 1)
    )
    vote = parse_review(text, "glm-5.2:cloud")
    assert vote.status == "UNPARSEABLE", (
        f"{cap + 1} findings were accepted as a verdict"
    )
    assert str(cap + 1) in (vote.error or ""), (
        f"the error must say how many were returned; got {vote.error!r}"
    )
    assert not vote.counted, "a degenerate vote must not be counted"


def test_a_normal_sized_review_is_not_treated_as_degenerate():
    """glm's real reviews on this profile run 4 to 13 findings. The cap must sit
    far above the working range or it silences honest reviewers."""
    from agent_loop import config

    text = HEAD + "".join(
        f"- [MAJOR] R1: finding number {i}\n" for i in range(13)
    )
    vote = parse_review(text, "glm-5.2:cloud")
    assert vote.status == "REVISE"
    assert len(vote.finding_list) == 13
    assert config.get().loop.max_findings_per_reviewer > 13
