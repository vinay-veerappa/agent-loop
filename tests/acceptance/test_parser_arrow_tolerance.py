"""
Acceptance test for T-REL4: review/arbiter parsers tolerate `>>` closers.

The block parser (BLOCK_RE in loop.py) uses `>{2,}` to tolerate a model that
closes with `>>` instead of `>>>` (kimi-k2.7-code did this on T3). The review
parser (parse_review.section) and the arbiter parser (_section in arbiter.py)
used literal `>>>`, so a model that drops one `>` from a BLOCK closer (accepted
by BLOCK_RE) may also drop one from a VERDICT or RULINGS closer, producing
UNPARSEABLE on the same model that successfully emitted its blocks.

This test verifies both parsers accept `>>` closers.
"""
from agent_loop.loop import parse_review
from agent_loop.arbiter import _section


def test_parse_review_accepts_double_arrow_closer():
    """parse_review accepts `>>` instead of `>>>` on the VERDICT closer."""
    text = (
        "<<<VERDICT>>\n"
        "APPROVE\n"
        "<<<END VERDICT>>\n"
        "<<<FINDINGS>>\n"
        "- NONE\n"
        "<<<END FINDINGS>>\n"
        "<<<REQUIRED>>\n"
        "- NONE\n"
        "<<<END REQUIRED>>"
    )
    vote = parse_review(text, "test-model")
    assert vote.status == "APPROVE", f"expected APPROVE, got {vote.status}"


def test_parse_review_accepts_triple_arrow_closer():
    """parse_review still accepts the standard `>>>` closer."""
    text = (
        "<<<VERDICT>>>\n"
        "REVISE\n"
        "<<<END VERDICT>>>\n"
        "<<<FINDINGS>>>\n"
        "- [BLOCKER] X: invented problem\n"
        "<<<END FINDINGS>>>\n"
        "<<<REQUIRED>>>\n"
        "- do something\n"
        "<<<END REQUIRED>>>"
    )
    vote = parse_review(text, "test-model")
    assert vote.status == "REVISE"
    assert vote.blockers == 1


def test_arbiter_section_accepts_double_arrow_closer():
    """arbiter._section accepts `>>` instead of `>>>` on the closer."""
    text = (
        "<<<RULINGS>>\n"
        "- [UPHELD] #1: real defect\n"
        "<<<END RULINGS>>\n"
        "<<<RECOMMENDATION>>\n"
        "REVISE\n"
        "<<<END RECOMMENDATION>>\n"
        "<<<RATIONALE>>\n"
        "One upheld finding.\n"
        "<<<END RATIONALE>>"
    )
    assert "UPHELD" in _section(text, "RULINGS")
    assert "REVISE" in _section(text, "RECOMMENDATION")
    assert "One upheld finding" in _section(text, "RATIONALE")


def test_arbiter_section_accepts_triple_arrow_closer():
    """arbiter._section still accepts the standard `>>>` closer."""
    text = (
        "<<<RULINGS>>>\n"
        "- [REJECTED] #1: not a defect\n"
        "<<<END RULINGS>>>\n"
        "<<<RECOMMENDATION>>>\n"
        "SHIP\n"
        "<<<END RECOMMENDATION>>>\n"
        "<<<RATIONALE>>>\n"
        "All rejected.\n"
        "<<<END RATIONALE>>>\n"
        "<<<SETTLED>>>\n"
        "- NONE\n"
        "<<<END SETTLED>>>"
    )
    assert "REJECTED" in _section(text, "RULINGS")
    assert "SHIP" in _section(text, "RECOMMENDATION")
    assert "All rejected" in _section(text, "RATIONALE")
    assert _section(text, "SETTLED") == "- NONE"


def test_memory_extract_settled_accepts_double_arrow_closer():
    """extract_settled accepts `>>` instead of `>>>` on the SETTLED closer."""
    from agent_loop.memory import extract_settled

    raw = (
        "<<<SETTLED>>\n"
        "- decision one\n"
        "- decision two\n"
        "<<<END SETTLED>>"
    )
    decisions = extract_settled(raw)
    assert len(decisions) == 2
    assert "decision one" in decisions[0]
    assert "decision two" in decisions[1]