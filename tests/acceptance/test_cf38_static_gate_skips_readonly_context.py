"""
CF-38: the static gate must not validate a READONLY region's block.

Measured in tvDownloadOHLC 2026-09-26 (T2 of tickets_spine_p1). The symbol scan
auto-attached a one-line read-only context region, the source line
`        if b.minute >= IB_START_MIN && b.minute < IB_END_MIN {`. The implementer
echoed it back verbatim, as it echoes every block. The static gate brace-counted
it (1 open, 0 close) and failed the round. The same failure recurred every round
and nothing the model could do would clear it, while apply_blocks skips readonly
blocks anyway (CF-31). The gate held a region the patch never writes to a shape
that region never had.
"""
from pathlib import Path

from agent_loop import gates, regions
from agent_loop.profiles import Profile

RS = Profile(
    name="test-cf38-rust",
    language="rust", file_suffixes=(".rs",), line_comment="//",
    block_comment=("/*", "*/"), block_kind="decl",
    implementer_rules="t", reviewer_priorities="t",
)

_LINE = "        if b.minute >= IB_START_MIN && b.minute < IB_END_MIN {"


def _regs():
    edit = regions.Region("E", "a.rs", Path("a.rs"), "fn f", "decl", 0, 2,
                          "fn f() {\n    1\n}")
    ctx = regions.Region("CTX", "g.rs", Path("g.rs"), "IB_START_MIN", "line", 82, 82,
                         _LINE, op=regions.READONLY)
    return [edit, ctx]


def test_a_verbatim_readonly_line_with_an_open_brace_passes():
    blocks = {"E": "fn f() {\n    2\n}", "CTX": _LINE}
    res = gates.check_static(_regs(), blocks, lambda ln: regions.strip_code(ln, RS), RS)
    assert res.ok, res.detail


def test_a_readonly_block_the_model_omitted_is_not_a_problem():
    res = gates.check_static(_regs(), {"E": "fn f() {\n    2\n}"},
                             lambda ln: regions.strip_code(ln, RS), RS)
    assert res.ok, res.detail


def test_an_editable_block_is_still_checked():
    # Negative control: the skip must not swallow the check it sits beside.
    blocks = {"E": "fn f() {\n    2\n", "CTX": _LINE}
    res = gates.check_static(_regs(), blocks, lambda ln: regions.strip_code(ln, RS), RS)
    assert not res.ok and "E: unbalanced braces" in res.detail, res.detail
