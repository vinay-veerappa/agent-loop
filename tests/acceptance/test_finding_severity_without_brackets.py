"""Findings without brackets are still parsed.

minimax-m3 has twice emitted ``- BLOCKER ...`` (no square brackets) instead
of the prompt-specified ``- [BLOCKER] ...``. The finding regex required
brackets, so every finding was silently dropped, ``findings_total`` read
zero, and the arbiter was never consulted — leaving BLOCKERs unreviewed.

The regex now accepts both forms (brackets optional). This test verifies
that a reviewer who omits the brackets still has its findings counted and
its BLOCKERs flagged.
"""
from __future__ import annotations

from agent_loop.loop import parse_review


def test_blocker_without_brackets_is_parsed():
    text = (
        "<<<VERDICT>>>\nREVISE\n<<<END VERDICT>>>\n"
        "<<<FINDINGS>>>\n"
        "- BLOCKER code_sandbox.py: the validator misses attribute calls\n"
        "<<<END FINDINGS>>>\n"
        "<<<REQUIRED>>>\n- fix the validator\n<<<END REQUIRED>>>"
    )
    vote = parse_review(text, "minimax-m3:cloud")
    assert vote.status == "REVISE"
    assert len(vote.finding_list) == 1, (
        f"a reviewer without brackets lost its findings; got "
        f"{len(vote.finding_list)}"
    )
    assert vote.finding_list[0].severity == "BLOCKER"
    assert vote.blockers == 1


def test_major_without_brackets_is_parsed():
    text = (
        "<<<VERDICT>>>\nREVISE\n<<<END VERDICT>>>\n"
        "<<<FINDINGS>>>\n"
        "- MAJOR tools.py: env={} breaks Windows\n"
        "<<<END FINDINGS>>>\n"
        "<<<REQUIRED>>>\n- seed SystemRoot\n<<<END REQUIRED>>>"
    )
    vote = parse_review(text, "minimax-m3:cloud")
    assert len(vote.finding_list) == 1
    assert vote.finding_list[0].severity == "MAJOR"
    assert vote.blockers == 1, "MAJOR is blocking"


def test_minor_without_brackets_is_parsed():
    text = (
        "<<<VERDICT>>>\nREVISE\n<<<END VERDICT>>>\n"
        "<<<FINDINGS>>>\n"
        "- MINOR docs: typo in section 1\n"
        "<<<END FINDINGS>>>\n"
        "<<<REQUIRED>>>\n- fix typo\n<<<END REQUIRED>>>"
    )
    vote = parse_review(text, "minimax-m3:cloud")
    assert len(vote.finding_list) == 1
    assert vote.finding_list[0].severity == "MINOR"
    assert vote.blockers == 0, "MINOR is not blocking"


def test_mixed_bracketed_and_unbracketed_findings():
    text = (
        "<<<VERDICT>>>\nREVISE\n<<<END VERDICT>>>\n"
        "<<<FINDINGS>>>\n"
        "- [BLOCKER] R1: bracketed blocker\n"
        "- MAJOR R2: unbracketed major\n"
        "- [MINOR] R3: bracketed minor\n"
        "<<<END FINDINGS>>>\n"
        "<<<REQUIRED>>>\n- fix all\n<<<END REQUIRED>>>"
    )
    vote = parse_review(text, "minimax-m3:cloud")
    assert len(vote.finding_list) == 3
    severities = [f.severity for f in vote.finding_list]
    assert severities == ["BLOCKER", "MAJOR", "MINOR"]
    assert vote.blockers == 2, "BLOCKER + MAJOR are blocking"


def test_bracketed_findings_still_work():
    """The original format (with brackets) must not regress."""
    text = (
        "<<<VERDICT>>>\nREVISE\n<<<END VERDICT>>>\n"
        "<<<FINDINGS>>>\n"
        "- [BLOCKER] R1: the lock is held\n"
        "<<<END FINDINGS>>>\n"
        "<<<REQUIRED>>>\n- release the lock\n<<<END REQUIRED>>>"
    )
    vote = parse_review(text, "glm-5.2:cloud")
    assert len(vote.finding_list) == 1
    assert vote.finding_list[0].severity == "BLOCKER"