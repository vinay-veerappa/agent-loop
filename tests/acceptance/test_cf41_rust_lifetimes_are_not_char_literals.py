"""
CF-41: a Rust lifetime is not a character literal.

Measured in tvDownloadOHLC 2026-09-26 (T6 of tickets_spine_p2). The implementer
wrote a valid `impl RiskBudget` containing

    fn reason_str(r: &DoneReason) -> &'static str {

and the static gate refused it as "unbalanced braces (50 open vs 51 close)" for
four rounds, until the ticket ended ARBITER_NEVER_RAN. Every quote reader in
`regions.py` treated `'` as opening a literal that runs to the NEXT `'`; with
none on the line it swallowed the rest, `{` included. In Rust `'` is a lifetime
or a loop label as often as a char, and in every C-family language a char
literal has a fixed shape. The same reader sets region BOUNDARIES
(`find_region`) and masks block comments, so the defect was not only the gate's.
"""
from pathlib import Path

import pytest

from agent_loop import gates, regions
from agent_loop.profiles import Profile


def _profile(language, line_comment="//", block_comment=("/*", "*/"), **kw):
    return Profile(
        name=f"test-cf41-{language}", language=language,
        file_suffixes=(".x",), line_comment=line_comment,
        block_comment=block_comment, block_kind="decl",
        implementer_rules="t", reviewer_priorities="t", **kw,
    )


RS = _profile("rust")
CS = _profile("csharp")
PY = _profile("python", line_comment="#", block_comment=())


def _braces(line, profile):
    s = regions.strip_code(line, profile)
    return s.count("{") - s.count("}")


@pytest.mark.parametrize("line", [
    "    fn reason_str(r: &DoneReason) -> &'static str {",
    "impl<'a> Parser<'a> {",
    "    'outer: loop {",
    "fn f<'a, 'b: 'a>(x: &'a str, y: &'b str) -> &'a str {",
])
def test_a_lifetime_or_label_leaves_the_brace_counted(line):
    assert _braces(line, RS) == 1, regions.strip_code(line, RS)


@pytest.mark.parametrize("line", [
    "    if c == '{' {",
    "    if c == '}' {",
    "    let q = '\\''; if x {",
    "    let e = '\\u{7B}'; if x {",
    "    let b = b'{'; if x {",
])
def test_a_real_char_literal_is_still_blanked(line):
    # Negative control: the shape rule must not stop blanking braces INSIDE a
    # char literal, or '{' would count as code.
    assert _braces(line, RS) == 1, regions.strip_code(line, RS)


def test_csharp_char_literals_read_as_before():
    assert _braces("if (c == '{') {", CS) == 1
    assert _braces("if (c == '\\'') {", CS) == 1


def test_a_python_single_quoted_string_is_still_a_string():
    # Negative control across languages: in Python `'` delimits a string of
    # any length, so the char shape must NOT apply there.
    assert _braces("x = '{ not a brace }' + '{'", PY) == 0
    assert _braces("d = {'k': '}'}", PY) == 0


def test_the_override_wins_over_the_language():
    js_like = _profile("typescript")
    assert not js_like.char_quote()
    assert _profile("typescript", single_quote_is_char=True).char_quote()
    assert not _profile("rust", single_quote_is_char=False).char_quote()


def test_the_static_gate_accepts_the_measured_block():
    body = (
        "impl RiskBudget {\n"
        "    fn reason_str(r: &DoneReason) -> &'static str {\n"
        "        match r {\n"
        "            DoneReason::Losers => \"losers\",\n"
        "        }\n"
        "    }\n"
        "}"
    )
    reg = regions.Region("budget_impl", "risk.rs", Path("risk.rs"), "impl RiskBudget {",
                         "decl", 0, 2, "impl RiskBudget {\n    x\n}")
    res = gates.check_static([reg], {"budget_impl": body},
                             lambda ln: regions.strip_code(ln, RS), RS)
    assert res.ok, res.detail
    # Negative control: a block that really is unbalanced is still refused.
    res = gates.check_static([reg], {"budget_impl": body[:-1]},
                             lambda ln: regions.strip_code(ln, RS), RS)
    assert not res.ok and "unbalanced braces" in res.detail, res.detail


def test_a_region_ends_where_its_block_ends(tmp_path):
    src = (
        "impl A {\n"
        "    fn name(&self) -> &'static str {\n"
        "        \"a\"\n"
        "    }\n"
        "}\n"
        "\n"
        "impl B {\n"
        "    fn b() {}\n"
        "}\n"
    )
    (tmp_path / "a.rs").write_text(src, encoding="utf-8")
    RS_FILE = _profile("rust")
    object.__setattr__(RS_FILE, "file_suffixes", (".rs",))
    regs = regions.extract(tmp_path, [{"id": "A", "file": "a.rs", "anchor": "impl A {"}], RS_FILE)
    assert (regs[0].start_line, regs[0].end_line) == (0, 4), regs[0].text


def test_a_lifetime_does_not_hide_a_block_comment():
    lines = ["let s: &'static str = x; /* {", "} */ let y = 1;"]
    masked, problem = regions._mask_block_comments(lines, RS)
    assert not problem, problem
    assert "{" not in masked[0] and "}" not in masked[1], masked
