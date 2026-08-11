"""Two regions in one file may not cover the same line.

`apply()` splices per file bottom-up, which is correct for disjoint spans and
silently wrong for nested ones: the outer replacement rewrites the same lines the
inner one rewrites, so one edit is lost or duplicated depending on order.

A real plan produced this and EVERY gate passed it -- the validator resolved all
four regions, both reviewers filed fifteen findings without mentioning it, and the
arbiter shipped. Region 1 was the whole of `CalculateFollowerQuantity` (429-534)
and region 2 a branch inside it (441-462); region 3 (382-427) contained region 4
(404-424). Each anchor is individually correct, which is what makes it easy for a
model to emit and hard for a reviewer to see.
"""
from __future__ import annotations

import pytest

from agent_loop import regions
from agent_loop.profiles import Profile

PROFILE = Profile(
    name="stub", language="csharp", file_suffixes=(".cs",), line_comment="//",
    block_comment=(), block_kind="decl", preprocessor_directives=(),
    build_cmd="true", test_cmd="true", lock_name="", risk_calls=(),
)

SRC = """namespace X
{
    public class Engine
    {
        public int Outer(int a)
        {
            if (a == 1)
            {
                return 1;
            }
            return 0;
        }

        public int Other(int b)
        {
            return b;
        }
    }
}
"""


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "Engine.cs").write_text(SRC, encoding="utf-8")
    return tmp_path


def _spec(rid, anchor, **kw):
    d = {"id": rid, "file": "Engine.cs", "anchor": anchor}
    d.update(kw)
    return d


def test_nested_regions_are_rejected(repo):
    with pytest.raises(regions.RegionError) as exc:
        regions.extract(repo, [
            _spec("R1", "public int Outer(int a)"),
            _spec("R2", "if (a == 1)"),
        ], PROFILE)
    msg = str(exc.value)
    assert "nested inside" in msg
    assert "R2" in msg and "R1" in msg
    # Both spans, 1-based, so the reader can see WHICH pair to fix.
    assert "7-10" in msg, f"the nested span is missing: {msg}"
    assert "5-12" in msg, f"the containing span is missing: {msg}"


def test_order_in_the_ticket_does_not_matter(repo):
    """The narrower region listed FIRST must fail identically."""
    with pytest.raises(regions.RegionError) as exc:
        regions.extract(repo, [
            _spec("R2", "if (a == 1)"),
            _spec("R1", "public int Outer(int a)"),
        ], PROFILE)
    assert "nested inside" in str(exc.value)


def test_identical_anchors_are_rejected(repo):
    with pytest.raises(regions.RegionError):
        regions.extract(repo, [
            _spec("R1", "public int Outer(int a)"),
            _spec("R2", "public int Outer(int a)"),
        ], PROFILE)


def test_disjoint_regions_in_one_file_are_fine(repo):
    out = regions.extract(repo, [
        _spec("R1", "public int Outer(int a)"),
        _spec("R2", "public int Other(int b)"),
    ], PROFILE)
    assert [r.id for r in out] == ["R1", "R2"]


def test_same_span_in_DIFFERENT_files_is_fine(repo):
    (repo / "Two.cs").write_text(SRC, encoding="utf-8")
    out = regions.extract(repo, [
        _spec("R1", "public int Outer(int a)"),
        {"id": "R2", "file": "Two.cs", "anchor": "public int Outer(int a)"},
    ], PROFILE)
    assert len(out) == 2


def test_a_create_region_never_conflicts(repo):
    """A create has no span, so it cannot overlap anything."""
    out = regions.extract(repo, [
        _spec("R1", "public int Outer(int a)"),
        {"id": "R2", "file": "New.cs", "op": "create"},
        {"id": "R3", "file": "New2.cs", "op": "create"},
    ], PROFILE)
    assert len(out) == 3


def test_an_insert_inside_a_replaced_span_is_still_rejected(repo):
    """`insert` writes after its anchor, which is inside the replaced text."""
    with pytest.raises(regions.RegionError):
        regions.extract(repo, [
            _spec("R1", "public int Outer(int a)"),
            _spec("R2", "return 1;", op="insert", kind="line"),
        ], PROFILE)


def test_adjacent_but_not_touching_is_allowed(repo):
    """Line N and line N+1 are distinct spans and must both be permitted."""
    out = regions.extract(repo, [
        _spec("R1", "if (a == 1)", kind="line"),
        _spec("R2", "return 1;", kind="line"),
    ], PROFILE)
    assert len(out) == 2
