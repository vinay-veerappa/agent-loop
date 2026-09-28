"""CF-58: a region begins at its anchor, so the doc comment directly above the
anchored declaration -- usually the declaration's CONTRACT -- never reached the
implementer.

Measured on tvDownloadOHLC T26 (C#, SpineCore.cs): the `SpineIntent` region
starts at `public sealed class SpineIntent`; the `/// <summary>` above it is
the only place the intent wire form is written down
(`"order":{"type":"stop_market","price":p}`). The prompt carried none of it,
so four rounds parsed `order` as a string and 8 acceptance tests stayed red on
`FormatException: order not a string`.

The comment block is shown READ-ONLY above the region's code: it is not part of
the region's line range, so nothing about what is replaced changes.
"""
from __future__ import annotations

import pytest

from agent_loop import regions
from agent_loop.profiles import Profile, register


CS = Profile(
    name="test-cf58-cs",
    language="csharp", file_suffixes=(".cs",), line_comment="//",
    block_comment=("/*", "*/"), block_kind="decl",
    implementer_rules="t", reviewer_priorities="t",
)
register(CS)

PY = Profile(
    name="test-cf58-py",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    implementer_rules="t", reviewer_priorities="t",
)
register(PY)

SRC_CS = (
    "namespace N\n"
    "{\n"
    "    public sealed class Other { }\n"
    "\n"
    "    /// <summary>Wire form:\n"
    "    /// {\"order\":{\"type\":\"stop_market\",\"price\":p}}\n"
    "    /// </summary>\n"
    "    [System.Serializable]\n"
    "    public sealed class Intent\n"
    "    {\n"
    "        public int X;\n"
    "    }\n"
    "\n"
    "    public sealed class Bare\n"
    "    {\n"
    "    }\n"
    "\n"
    "    [System.Obsolete]\n"
    "    public sealed class Attributed\n"
    "    {\n"
    "    }\n"
    "}\n"
)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "A.cs").write_text(SRC_CS, encoding="utf-8")
    return tmp_path


def _prompt(regs, profile):
    from agent_loop.loop import build_implement_prompt
    return build_implement_prompt({"id": "T1", "title": "t", "defect": "d", "spec": "s"}, regs, profile)


def test_the_doc_above_a_region_reaches_the_implement_prompt(repo):
    regs = regions.extract(repo, [{"id": "R", "file": "A.cs", "anchor": "public sealed class Intent"}], CS)
    prompt = _prompt(regs, CS)
    assert '{"order":{"type":"stop_market","price":p}}' in prompt
    # shown BEFORE the region's code, and labelled as not part of the region
    assert prompt.index("stop_market") < prompt.index("public int X;")
    assert "read-only" in prompt.lower()


def test_the_doc_is_not_part_of_the_region(repo):
    """The replaced span is unchanged: the region still starts at its anchor."""
    (r,) = regions.extract(repo, [{"id": "R", "file": "A.cs", "anchor": "public sealed class Intent"}], CS)
    assert r.text.lstrip().startswith("public sealed class Intent")
    assert "stop_market" not in r.text


def test_leading_comment_walks_over_attributes_and_stops_at_code(repo):
    (r,) = regions.extract(repo, [{"id": "R", "file": "A.cs", "anchor": "public sealed class Intent"}], CS)
    doc = regions.leading_comment(r, CS)
    assert doc.splitlines()[0].strip() == "/// <summary>Wire form:"
    assert "[System.Serializable]" in doc
    assert "class Other" not in doc
    # the region's own first line is the region's, not the doc's
    assert doc.splitlines()[-1].strip() == "[System.Serializable]"


def test_attributes_alone_are_not_a_doc(repo):
    (r,) = regions.extract(repo, [{"id": "R", "file": "A.cs", "anchor": "public sealed class Attributed"}], CS)
    assert regions.leading_comment(r, CS) == ""


def test_a_region_with_no_comment_above_gets_none(repo):
    """Negative control: a blank line or code above means no doc, and the
    prompt carries no empty doc section."""
    (r,) = regions.extract(repo, [{"id": "R", "file": "A.cs", "anchor": "public sealed class Bare"}], CS)
    assert regions.leading_comment(r, CS) == ""
    assert "read-only" not in _prompt([r], CS).lower().split("### region")[1]


def test_a_long_doc_is_cut_from_the_top_and_says_so(tmp_path):
    body = "".join(f"# line {i}\n" for i in range(500))
    (tmp_path / "m.py").write_text(body + "def f():\n    return 1\n", encoding="utf-8")
    (r,) = regions.extract(tmp_path, [{"id": "R", "file": "m.py", "anchor": "def f"}], PY)
    doc = regions.leading_comment(r, PY)
    assert "# line 499" in doc, "the lines nearest the declaration are kept"
    assert "# line 0\n" not in doc
    assert "truncated" in doc.lower()


def test_a_create_region_has_no_doc(tmp_path):
    (r,) = regions.extract(tmp_path, [{"id": "N", "file": "new.py", "op": "create"}], PY)
    assert regions.leading_comment(r, PY) == ""
