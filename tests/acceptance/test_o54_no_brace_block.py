"""
O54 — `kind=decl` on a declaration with no braces swallows the rest of the class.

Found while checking that O53 had actually unblocked `McpBridgeAddOn.cs`:

    private static string CopierConfigFile => Path.Combine(...);   // line 3600
    private object CopierConfig(string body)                       // line 3603

The second resolves to 3603-3715, which is right. The first resolves to
**3600-3715** — one line of intent, 116 lines of region — because `_brace_block`
finds no brace on the anchor line and keeps counting until the *next* member's
block closes. It prints OK.

This is the third member of a family: O40 (an anchor spanning lines can never
match), O47 (two regions covering the same lines), and now this. In all three the
anchor is individually correct and the result is silently wrong, which is why no
gate catches them — a region that is too big is still a region.

The stakes are not cosmetic. A ticket that meant to change one line hands the
implementer 116 lines to re-emit, and every line it does not reproduce exactly is
a silent deletion of working code.

`decl` is the C# profile's DEFAULT kind, so this is what a region spec gets when
it does not say otherwise.
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

# The shape from McpBridgeAddOn.cs, reduced.
BRIDGE = """public class Bridge
{
    private static string CopierConfigFile => Path.Combine(Dir, "copier_config.json");

    private object CopierConfig(string body)
    {
        if (body == null)
        {
            return null;
        }
        return body;
    }
}
"""


def test_an_expression_bodied_member_is_refused_not_overshot():
    with pytest.raises(regions.RegionError) as exc:
        regions.find_region(BRIDGE.splitlines(), "CopierConfigFile", "decl", CS)
    msg = str(exc.value)
    assert "kind" in msg and "line" in msg, (
        "the message must say what to do instead, the way the multi-line anchor "
        f"error does; got: {msg}"
    )


def test_the_overshoot_it_replaces_would_have_reached_the_end_of_the_class():
    """Pins the actual damage, so this test fails if the guard is removed rather
    than merely relaxed: without it the one-line member resolves to 11 lines."""
    with pytest.raises(regions.RegionError):
        start, end = regions.find_region(
            BRIDGE.splitlines(), "CopierConfigFile", "decl", CS
        )
        assert (start, end) != (2, 12), "this is the defect, not the fix"


def test_the_same_anchor_with_kind_line_still_works():
    start, end = regions.find_region(
        BRIDGE.splitlines(), "CopierConfigFile", "line", CS
    )
    assert (start, end) == (2, 2)


def test_the_neighbouring_real_method_is_unaffected():
    start, end = regions.find_region(
        BRIDGE.splitlines(), "private object CopierConfig(string body)", "decl", CS
    )
    assert (start, end) == (4, 11)


# ---------------------------------------------------------------------------
# what must NOT be refused -- a guard that over-fires is worse than the defect
# ---------------------------------------------------------------------------
def test_a_signature_whose_brace_is_on_the_next_line_still_resolves():
    """The overwhelmingly common C# layout. If this is refused, the profile's
    default kind stops working on almost every method in the codebase."""
    src = """public class X
{
    public int Method(int a)
    {
        return a;
    }
}
"""
    assert regions.find_region(src.splitlines(), "public int Method", "decl", CS) == (2, 5)


def test_a_multi_line_signature_still_resolves():
    src = """public class X
{
    public int Method(
        int a,
        int b)
    {
        return a + b;
    }
}
"""
    assert regions.find_region(src.splitlines(), "public int Method(", "decl", CS) == (2, 7)


def test_a_one_line_method_with_braces_still_resolves():
    src = """public class X
{
    public int Method() { return 1; }

    public int Other() { return 2; }
}
"""
    assert regions.find_region(src.splitlines(), "public int Method", "decl", CS) == (2, 2)


def test_a_field_initialised_with_braces_still_resolves():
    src = """public class X
{
    private static readonly int[] Xs = new[] { 1, 2, 3 };

    public int Method() { return 1; }
}
"""
    assert regions.find_region(src.splitlines(), "private static readonly int[] Xs", "decl", CS) == (2, 2)


def test_a_trailing_semicolon_inside_a_line_comment_does_not_trigger_the_guard():
    """The check reads the STRIPPED line. A comment that happens to end in a
    semicolon is not a declaration without a body.

    The comment must END with the semicolon. A first draft put it mid-comment
    ("returns a; nothing else"), where the raw and stripped lines agree about the
    last character and the mutation that reads the raw line survives."""
    src = """public class X
{
    public int Method(int a) // returns a;
    {
        return a;
    }
}
"""
    assert regions.find_region(src.splitlines(), "public int Method", "decl", CS) == (2, 5)


def test_a_semicolon_inside_a_string_does_not_confuse_the_neighbouring_method():
    src = """public class X
{
    public string Method()
    {
        return "a;b";
    }
}
"""
    assert regions.find_region(src.splitlines(), "public string Method", "decl", CS) == (2, 5)


# ---------------------------------------------------------------------------
# the other declarations with no body
# ---------------------------------------------------------------------------
def test_an_abstract_declaration_is_refused():
    src = """public abstract class X
{
    public abstract int Method(int a);

    public int Other() { return 1; }
}
"""
    with pytest.raises(regions.RegionError):
        regions.find_region(src.splitlines(), "public abstract int Method", "decl", CS)


def test_extract_named_block_does_not_return_an_overshooting_body():
    """This one only ever feeds a reviewer, but showing them the wrong 100 lines
    as 'the acceptance test' is how a review gets spent on the wrong code."""
    src = """public class X
{
    public int Method() => 1;

    public int Other()
    {
        return 2;
    }
}
"""
    assert regions.extract_named_block(src, "Method", CS) is None
