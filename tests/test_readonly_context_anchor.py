"""CF-55: the auto-attached read-only context region must have a UNIQUE anchor.

The fallback used to be the first line of a 3-line window, unchecked. In C# that
line is routinely `/// <summary>`, so the region failed to resolve and the whole
run ended with RegionError before round 1 (tvDownloadOHLC T26, 2026-09-27).
"""
from pathlib import Path

from agent_loop import cli
from agent_loop import regions

CS = """namespace N
{
    /// <summary>
    /// A leg.
    /// </summary>
    public enum Leg { Entry, Stop }

    /// <summary>
    /// Thrown by the wrapper.
    /// </summary>
    public sealed class SpineException : Exception
    {
        /// <summary>
        public SpineException(string m) : base(m) { }
    }
}
"""


def _ticket():
    return {"id": "T1", "regions": []}


REPEATED = """class A
{
    /// <summary>
    /// The first tag.
    /// </summary>
        string Tag;
}
class B
{
    /// <summary>
    /// The second tag.
    /// </summary>
        string Tag;
}
"""


def test_the_attached_anchor_occurs_once(tmp_path, monkeypatch):
    # The declaration line repeats and the window's first line is
    # `/// <summary>`: exactly the shape the old fallback returned unchecked.
    monkeypatch.chdir(tmp_path)
    Path("A.cs").write_text(REPEATED, encoding="utf-8")
    t = _ticket()
    cli._attach_readonly_context(t, "A.cs", "Tag", profile=None)
    assert len(t["regions"]) == 1
    anchor = t["regions"][0]["anchor"]
    assert REPEATED.count(anchor) == 1, anchor
    assert anchor == "/// The first tag.", anchor  # nearest unique line to the first hit


def test_the_declaration_line_is_preferred_when_unique():
    lines = CS.splitlines()
    i = next(n for n, ln in enumerate(lines) if "class SpineException" in ln)
    got = cli._unique_anchor_near(CS, lines, i, lines[i])
    assert got == lines[i].strip()


def test_a_repeated_declaration_falls_to_the_nearest_unique_line():
    # `/// <summary>` repeats; from it, the nearest unique line is chosen.
    lines = CS.splitlines()
    i = 2  # the first `/// <summary>`
    got = cli._unique_anchor_near(CS, lines, i, lines[i])
    assert got is not None and CS.count(got) == 1
    assert got == "/// A leg."  # one line below; the line above (`{`) repeats


def test_no_unique_line_attaches_nothing(tmp_path, monkeypatch):
    # Negative control: every line repeats, so nothing is attached -- and the
    # run is not killed by an unresolvable region.
    text = "x Sym;\nx Sym;\n"
    monkeypatch.chdir(tmp_path)
    Path("B.cs").write_text(text, encoding="utf-8")
    t = _ticket()
    cli._attach_readonly_context(t, "B.cs", "Sym", profile=None)
    assert t["regions"] == []
