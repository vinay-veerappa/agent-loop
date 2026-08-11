"""
O53 — a block comment anywhere in a file refused the whole file.

`guard_unsupported_syntax` rejected any file containing `/*`, on the grounds that
the brace matcher would misread it. The matcher genuinely would: `strip_code` is
per-line and stateless, so a `{` inside a comment counts as a real brace.

The cost was paid in the consumer. `McpBridgeAddOn.cs` contains exactly two
occurrences, both `catch { /* indexer or access threw */ }` on one line at 1247
and 1268, and those two lines made all 1300 of them uneditable by the loop —
which is what blocked slice 3 of the copier ratio feature, the slice that makes
the shipped slice 1 reachable from the UI at all. `RiskGuardAddOnTests.cs` has
one, so every acceptance test for that addon has had to be hand-written.

The fix is a masker over the whole text that blanks block-comment spans while
respecting the constructs that can *contain* the token: line comments, string
literals, char literals, and C# verbatim strings. What it cannot model — an
unterminated block comment, and raw string literals — is still refused, and now
says which.

The invariant that makes this safe to land: **on a file with no block comment the
masker is the identity**, so nothing that resolves today can move.
"""
from __future__ import annotations

import pytest

from agent_loop import regions
from agent_loop.profiles import Profile

CS = Profile(
    name="stub-cs", language="csharp", file_suffixes=(".cs",), line_comment="//",
    block_comment=("/*", "*/"), block_kind="decl", preprocessor_directives=(),
    build_cmd="true", test_cmd="true", lock_name="", risk_calls=(),
)
PY = Profile(
    name="stub-py", language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent", preprocessor_directives=(),
    build_cmd="true", test_cmd="true", lock_name="", risk_calls=(),
)


def _region(src: str, anchor: str, profile: Profile = CS):
    return regions.find_region(src.splitlines(), anchor, "decl", profile)


# ---------------------------------------------------------------------------
# the shape that actually blocked the feature
# ---------------------------------------------------------------------------
BRIDGE = """public class Bridge
{
    public object ReadField(object t, string name)
    {
        try
        {
            return t.GetType().GetProperty(name).GetValue(t);
        }
        catch { /* indexer or access threw */ }
        return null;
    }

    public int After()
    {
        return 1;
    }
}
"""


def test_an_inline_block_comment_no_longer_refuses_the_file():
    regions.guard_unsupported_syntax(
        __import__("pathlib").Path("Bridge.cs"), BRIDGE, CS
    )


def test_the_region_still_ends_where_the_method_ends():
    start, end = _region(BRIDGE, "public object ReadField")
    lines = BRIDGE.splitlines()
    assert lines[start].strip().startswith("public object ReadField")
    assert lines[end].strip() == "}"
    assert end == 10, f"expected the method to close at line 11 (0-based 10), got {end}"


# ---------------------------------------------------------------------------
# what the guard was protecting against — the matcher must now actually cope
# ---------------------------------------------------------------------------
UNBALANCED = """public class X
{
    public int Method(int a)
    {
        /* a closing brace } that is not real */
        return a;
    }

    public int Next() { return 0; }
}
"""


def test_braces_inside_a_block_comment_are_not_counted():
    """This is the miscount the whole-file refusal existed to prevent: without
    masking, the comment's fake `}` drops the depth to zero and the method
    appears to end four lines early.

    The comment's brace must be UNBALANCED for this to test anything. A first
    draft used `{ ... }` inside the comment, which nets to zero and passes with
    no fix at all — the same "assertion the unfixed code also satisfies" shape
    that produced a useless test twice in the previous session."""
    start, end = _region(UNBALANCED, "public int Method")
    assert UNBALANCED.splitlines()[end].strip() == "}"
    assert end == 6


MULTILINE = """public class X
{
    public int Method(int a)
    {
        /* a comment
           spanning several lines
           with a stray { brace
        */
        return a;
    }
}
"""


def test_a_block_comment_spanning_lines_is_masked_across_all_of_them():
    start, end = _region(MULTILINE, "public int Method")
    assert end == 9, f"expected the method to close at 0-based 9, got {end}"


# ---------------------------------------------------------------------------
# the constructs that can CONTAIN the token, which is where a naive fix breaks
# ---------------------------------------------------------------------------
def test_a_comment_token_inside_a_string_is_not_a_comment():
    src = """public class X
{
    public string Method()
    {
        var s = "/* not a comment";
        return s;
    }
}
"""
    start, end = _region(src, "public string Method")
    assert end == 6, "the string opened a phantom comment that swallowed the file"


def test_a_comment_token_inside_a_line_comment_is_not_a_comment():
    src = """public class X
{
    public int Method()
    {
        // see /* elsewhere
        return 1;
    }
}
"""
    start, end = _region(src, "public int Method")
    assert end == 6


def test_a_comment_token_inside_a_verbatim_string_is_not_a_comment():
    """C# verbatim strings have no backslash escapes and double a quote to
    embed one, so the ordinary string scanner mis-terminates them."""
    src = '''public class X
{
    public string Method()
    {
        var p = @"C:\\logs\\/* not a comment";
        return p;
    }
}
'''
    start, end = _region(src, "public string Method")
    assert end == 6


def test_a_verbatim_string_spanning_lines_does_not_open_a_comment():
    src = '''public class X
{
    public string Method()
    {
        var p = @"line one
/* still inside the string
line three";
        return p;
    }
}
'''
    start, end = _region(src, "public string Method")
    assert end == 8


def test_a_doubled_quote_does_not_end_a_verbatim_string():
    """It has to span LINES, and two drafts on one line proved why.

    Misreading `""` as "close, then reopen" desyncs by one quote — and one quote
    always reopens an ordinary string that swallows the `/*` before the line
    ends. So on a single line the naive scanner is wrong and gets the right
    answer, and the mutation that deletes doubled-quote handling survives every
    single-line case that can be written.

    Across lines it cannot recover: ordinary strings do not carry to the next
    line, so the desync leaves the following line as bare code and its `/*`
    opens a comment that runs to end of file."""
    from pathlib import Path

    src = '''public class X
{
    public string Method()
    {
        var p = @"a ""b""
/* still inside the string
done";
        return p;
    }
}
'''
    regions.guard_unsupported_syntax(Path("X.cs"), src, CS)
    start, end = _region(src, "public string Method")
    assert end == 8


# ---------------------------------------------------------------------------
# what is still refused, and it must say which
# ---------------------------------------------------------------------------
def test_an_unterminated_block_comment_is_still_refused():
    from pathlib import Path

    src = "public class X\n{\n    /* opened and never closed\n    public int M() { return 1; }\n}\n"
    with pytest.raises(regions.RegionError) as exc:
        regions.guard_unsupported_syntax(Path("X.cs"), src, CS)
    msg = str(exc.value)
    assert "unterminated" in msg.lower(), (
        "the old guard refused every block comment with the same message, so "
        "asserting only on the line number passes without the fix"
    )
    assert ":3" in msg, "the message must name the line it was opened on"


def test_a_raw_string_literal_is_refused_because_it_is_not_modelled():
    from pathlib import Path

    src = 'public class X\n{\n    var s = """not modelled""";\n}\n'
    with pytest.raises(regions.RegionError) as exc:
        regions.guard_unsupported_syntax(Path("X.cs"), src, CS)
    assert "raw string" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# the invariant that makes this safe to land
# ---------------------------------------------------------------------------
def test_masking_is_the_identity_on_a_file_with_no_block_comment():
    """Everything that resolves today must keep resolving to the same lines.
    Stated as an invariant rather than a differential against the consumer,
    because this package cannot import that repo."""
    src = open(regions.__file__.replace("regions.py", "loop.py"), encoding="utf-8").read()
    lines = src.splitlines()
    masked, problem = regions._mask_block_comments(lines, CS)
    assert problem == ""
    assert masked == lines, "a file with no /* must come back byte-identical"


def test_masking_is_the_identity_when_the_profile_has_no_block_comment_syntax():
    lines = ["def f():", "    return '/* not applicable */'"]
    masked, problem = regions._mask_block_comments(lines, PY)
    assert masked == lines
    assert problem == ""


def test_masking_preserves_line_count_and_column_positions():
    """Indentation drives the indent-block finder and line indices drive every
    region span, so the mask must be length-preserving, not a deletion."""
    lines = ["    x = 1; /* c */ y = 2;", "    z = 3;"]
    masked, _ = regions._mask_block_comments(lines, CS)
    assert len(masked) == len(lines)
    assert [len(m) for m in masked] == [len(l) for l in lines]
    assert masked[0].startswith("    x = 1; ")
    assert "/*" not in masked[0] and "c" not in masked[0].split("y")[0][10:]
    assert masked[1] == lines[1]
