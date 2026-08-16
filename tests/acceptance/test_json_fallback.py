"""
Acceptance test for Wave 4.4: JSON output fallback in parse_review (C-4).

Some models emit structured JSON instead of the text protocol despite being
asked for <<<VERDICT>>> blocks. A model that returns
{"verdict": "APPROVE", "findings": []} is a valid review, not an unparseable
one. The JSON fallback catches this case without replacing the text protocol.
"""
from agent_loop.loop import parse_review, APPROVE, REVISE, REJECT


def test_parse_review_json_fallback_approve():
    """A JSON response with verdict=APPROVE is parsed."""
    text = '{"verdict": "APPROVE", "findings": []}'
    vote = parse_review(text, "test-model")
    assert vote.status == APPROVE
    assert vote.blockers == 0


def test_parse_review_json_fallback_revise_with_findings():
    """A JSON response with verdict=REVISE and findings is parsed."""
    text = '''{
        "verdict": "REVISE",
        "findings": [
            {"severity": "BLOCKER", "text": "the lock is held during a broker call"},
            {"severity": "MAJOR", "text": "race condition in OnExecution"}
        ]
    }'''
    vote = parse_review(text, "test-model")
    assert vote.status == REVISE
    # Finding.blocking includes both BLOCKER and MAJOR, so blockers=2.
    assert vote.blockers == 2
    assert len(vote.finding_list) == 2
    assert "lock is held" in vote.finding_list[0].text


def test_parse_review_json_fallback_in_markdown_fence():
    """A JSON response wrapped in a markdown fence is parsed."""
    text = '''Here is my review:
```json
{"verdict": "REJECT", "findings": [{"severity": "BLOCKER", "text": "fundamentally wrong"}]}
```
That's all.'''
    vote = parse_review(text, "test-model")
    assert vote.status == REJECT
    assert vote.blockers == 1
    assert "fundamentally wrong" in vote.finding_list[0].text


def test_parse_review_text_protocol_still_works():
    """The text protocol is still the primary path; JSON is a fallback."""
    text = (
        "<<<VERDICT>>>\n"
        "APPROVE\n"
        "<<<END VERDICT>>>\n"
        "<<<FINDINGS>>>\n"
        "- NONE\n"
        "<<<END FINDINGS>>>\n"
        "<<<REQUIRED>>>\n"
        "- NONE\n"
        "<<<END REQUIRED>>>"
    )
    vote = parse_review(text, "test-model")
    assert vote.status == APPROVE


def test_parse_review_json_with_required():
    """A JSON response with required instructions is parsed."""
    text = '{"verdict": "REVISE", "findings": [{"severity": "MAJOR", "text": "bug"}], "required": ["fix the bug"]}'
    vote = parse_review(text, "test-model")
    assert vote.status == REVISE
    assert "fix the bug" in vote.required


def test_parse_review_unparseable_still_returns_unparseable():
    """A response that is neither text protocol nor JSON returns UNPARSEABLE."""
    text = "I cannot review this patch because I don't understand it."
    vote = parse_review(text, "test-model")
    assert vote.status == "UNPARSEABLE"