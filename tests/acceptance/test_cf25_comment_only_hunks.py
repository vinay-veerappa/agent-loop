"""CF-25 -- the implementer rewrote 11 unrelated comments to strip non-ASCII.

The model was handed region text containing `⚠️`, box-drawing characters and `//`
comment syntax. It emitted `WARNING:`, `---` and `///` in their place. Every gate
passed because the *code* was identical -- only comment text changed -- and no
gate compares the emitted block against the original line-by-line.

Two fixes, both tested here:

1. **Prompt fidelity rule** -- `build_implement_prompt` now tells the model to
   reproduce untouched lines byte-for-byte, including non-ASCII and comment
   syntax.

2. **`check_comment_drift` gate** -- a BLOCKING gate (ok=False) that uses
   difflib to align orig vs new lines (handles line-count changes), then
   compares code-stripped forms. When every changed line is code-identical
   but raw-different, the only thing that changed is the comment. Exempt
   when the ticket asks for documentation changes.
"""
from __future__ import annotations

from agent_loop import gates, regions
from agent_loop.loop import build_implement_prompt
from agent_loop.profiles import Profile

CS = Profile(
    name="stub-cs", language="csharp", file_suffixes=(".cs",), line_comment="//",
    block_comment=("/*", "*/"), block_kind="decl", preprocessor_directives=(),
    build_cmd="true", test_cmd="true", lock_name="", risk_calls=(),
)


def _make_region(rid: str, text: str, op: str = "replace") -> regions.Region:
    """Build a Region directly without touching the filesystem."""
    from pathlib import Path
    return regions.Region(
        id=rid, file="stub.cs", path=Path("stub.cs"), anchor="anchor",
        kind="decl", start_line=0, end_line=text.count("\n"), text=text, op=op,
    )


def _strip(line: str) -> str:
    return regions.strip_code(line, CS)


TICKET = {"id": "T1", "title": "test", "defect": "d", "spec": "fix the bug",
          "regions": [], "expect_green": []}
DOC_TICKET = {"id": "T2", "title": "docs", "defect": "d",
              "spec": "Update the documentation comments in the Run method",
              "regions": [], "expect_green": []}

# ---------------------------------------------------------------------------
# the prompt now carries the fidelity rule
# ---------------------------------------------------------------------------
def test_prompt_tells_model_to_preserve_non_ascii():
    """CF-25 fix 1: the implementer prompt must instruct the model to reproduce
    untouched lines byte-for-byte, including non-ASCII glyphs and comment
    syntax."""
    prompt = build_implement_prompt(TICKET, [], CS)
    assert "byte-for-byte" in prompt
    assert "non-ASCII" in prompt
    assert "comment" in prompt and "syntax" in prompt


# ---------------------------------------------------------------------------
# check_comment_drift -- the blocking gate
# ---------------------------------------------------------------------------
ORIGINAL = """        // ⚠️ `!= 0.0` AND NOT `> 0`. An account whose equity has gone NEGATIVE is reporting a
        bool IsBreach = stateModel.Positions.Count > 0;"""

# The model stripped ⚠️ to WARNING: and left the code line untouched
DRIFTED = """        // WARNING: `!= 0.0` AND NOT `> 0`. An account whose equity has gone NEGATIVE is reporting a
        bool IsBreach = stateModel.Positions.Count > 0;"""

# A real code change: the condition is different
CODE_CHANGE = """        // ⚠️ `!= 0.0` AND NOT `> 0`. An account whose equity has gone NEGATIVE is reporting a
        bool IsBreach = stateModel.Positions.Count >= 1;"""

# A mixed block: one comment-only change AND one real code change
MIXED = """        // WARNING: `!= 0.0` AND NOT `> 0`. An account whose equity has gone NEGATIVE is reporting a
        bool IsBreach = stateModel.Positions.Count >= 1;"""

# A line-count change (real edit adds a line)
LINE_ADDED = """        // ⚠️ `!= 0.0` AND NOT `> 0`. An account whose equity has gone NEGATIVE is reporting a
        bool IsBreach = stateModel.Positions.Count > 0;
        // new comment"""


def test_comment_drift_blocks_comment_only_change():
    """CF-25: a block that differs only in comment text is BLOCKED (ok=False)."""
    r = _make_region("R1", ORIGINAL)
    result = gates.check_comment_drift([r], {"R1": DRIFTED}, _strip, CS, ticket=TICKET)
    assert not result.ok  # blocking, not advisory
    assert "R1" in result.detail
    assert "1 comment-only" in result.detail


def test_comment_drift_clean_when_code_also_changes():
    """CF-25: a block where the code line also changes is not blocked."""
    r = _make_region("R1", ORIGINAL)
    result = gates.check_comment_drift([r], {"R1": CODE_CHANGE}, _strip, CS, ticket=TICKET)
    assert result.ok
    assert result.detail == ""


def test_comment_drift_clean_on_mixed_block():
    """CF-25: a block with BOTH a comment rewrite AND a real code change must
    NOT be blocked — the real code change is the signal, and blocking on the
    comment would be a false positive."""
    r = _make_region("R1", ORIGINAL)
    result = gates.check_comment_drift([r], {"R1": MIXED}, _strip, CS, ticket=TICKET)
    assert result.ok
    assert result.detail == ""


def test_comment_drift_clean_when_identical():
    """CF-25: a block that is byte-identical is not blocked."""
    r = _make_region("R1", ORIGINAL)
    result = gates.check_comment_drift([r], {"R1": ORIGINAL}, _strip, CS, ticket=TICKET)
    assert result.ok
    assert result.detail == ""


def test_comment_drift_clean_when_line_added():
    """CF-25: a block that adds a comment line is blocked — adding a comment
    the ticket did not ask for is the same noise as rewriting one. But a
    block that adds a CODE line is not blocked."""
    r = _make_region("R1", ORIGINAL)
    result = gates.check_comment_drift([r], {"R1": LINE_ADDED}, _strip, CS, ticket=TICKET)
    # LINE_ADDED adds a pure comment line. difflib sees the existing lines as
    # "equal" and the new comment as an insertion. The insertion strips to
    # empty (pure comment) → comment_only_change. real_changes=0 → blocks.
    assert not result.ok, f"pure-comment insertion should block: {result.detail}"


def test_comment_drift_skipped_for_create_regions():
    """CF-25: create regions have no original text to compare against."""
    r = _make_region("R1", "", op="create")
    result = gates.check_comment_drift([r], {"R1": "anything"}, _strip, CS, ticket=TICKET)
    assert result.ok
    assert result.detail == ""


def test_comment_drift_feedback_tells_model_to_fix():
    """CF-25: the feedback string must tell the model what went wrong and
    instruct it to re-emit with the original comments."""
    r = _make_region("R1", ORIGINAL)
    result = gates.check_comment_drift([r], {"R1": DRIFTED}, _strip, CS, ticket=TICKET)
    assert "byte-for-byte" in result.feedback
    assert "non-ASCII" in result.feedback
    assert "Re-emit" in result.feedback


# ---------------------------------------------------------------------------
# difflib-based alignment: line-count-changing reflows
# ---------------------------------------------------------------------------
BOX_ORIG = """        // ── helpers ───────────────────────────────────────────────────────────────────
        void DoWork() { }"""

BOX_DRIFTED = """        // --- helpers -------------------------------------------------------------------
        void DoWork() { }"""


def test_comment_drift_detects_box_drawing_reflow():
    """CF-25: the specific box-drawing → ASCII dash reflow measured in the
    consumer repo is detected and blocked. Same line count, difflib aligns
    them, code-stripped forms match → comment-only change."""
    r = _make_region("R1", BOX_ORIG)
    result = gates.check_comment_drift([r], {"R1": BOX_DRIFTED}, _strip, CS, ticket=TICKET)
    assert not result.ok
    assert "R1" in result.detail


# A comment reflow that changes line count (merging two comment lines into one)
REFLOW_LONG = """        // This is a long comment that was split across two lines
        // because it was too wide for one line.
        void DoWork() { }"""

REFLOW_SHORT = """        // This is a long comment that was split across two lines because it was too wide for one line.
        void DoWork() { }"""


def test_comment_drift_detects_reflow_merging_lines():
    """CF-25: a comment reflow that merges two comment lines into one (changing
    line count) is detected by difflib. The replaced line is comment-only,
    the deleted line is pure comment, and the code line is equal."""
    r = _make_region("R1", REFLOW_LONG)
    result = gates.check_comment_drift([r], {"R1": REFLOW_SHORT}, _strip, CS, ticket=TICKET)
    assert not result.ok, f"should block: {result.detail}"


# ---------------------------------------------------------------------------
# Documentation ticket exemption
# ---------------------------------------------------------------------------
def test_comment_drift_allowed_for_doc_ticket():
    """CF-25: a ticket that asks for documentation/comment changes is exempt —
    the model is SUPPOSED to rewrite comments there."""
    r = _make_region("R1", ORIGINAL)
    result = gates.check_comment_drift([r], {"R1": DRIFTED}, _strip, CS, ticket=DOC_TICKET)
    assert result.ok  # advisory for doc tickets
    assert "doc ticket" in result.summary


# A ticket that mentions "comment" incidentally, not as a documentation request
INCIDENTAL_COMMENT_TICKET = {"id": "T3", "title": "fix", "defect": "d",
    "spec": "Delete the comment on line 5 and fix the bug in the condition",
    "regions": [], "expect_green": []}


def test_comment_drift_blocks_on_incidental_comment_mention():
    """CF-25 (arbiter #3): a ticket that says "delete the comment on line 5"
    is NOT a documentation ticket — the word "comment" is incidental. The
    gate must still BLOCK comment drift."""
    r = _make_region("R1", ORIGINAL)
    result = gates.check_comment_drift([r], {"R1": DRIFTED}, _strip, CS,
                                        ticket=INCIDENTAL_COMMENT_TICKET)
    assert not result.ok, (
        "incidental 'comment' mention should not exempt from the gate"
    )


# ---------------------------------------------------------------------------
# Comment insertion at top (not a rewrite — should not block)
# ---------------------------------------------------------------------------
COMMENT_ONLY_ORIG = """        // first comment
        // second comment"""

COMMENT_ONLY_INSERT = """        // NEW comment inserted here
        // first comment
        // second comment"""


def test_comment_drift_blocks_comment_insertion():
    """CF-25: inserting a new comment line (pure comment insertion) IS
    comment drift — the model added noise the ticket did not ask for.
    difflib correctly identifies this as a pure-comment insertion."""
    r = _make_region("R1", COMMENT_ONLY_ORIG)
    result = gates.check_comment_drift([r], {"R1": COMMENT_ONLY_INSERT}, _strip, CS, ticket=TICKET)
    assert not result.ok, f"should block: {result.detail}"