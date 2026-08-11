"""Verbatim text I/O must work on every Python this package supports.

`Path.read_text(newline=...)` / `Path.write_text(newline=...)` only accept the
`newline` keyword on Python 3.13+. The package declares `requires-python
>=3.10`, and the whole CRLF story (regions.read_source, developer._edit_file,
workspace.export_patch) depends on newline translation being off -- so on a
3.10-3.12 interpreter those three code paths raised TypeError and every ticket
died at region extraction. It went unseen because the dev machine runs 3.14.

The functional tests below pass on 3.13+ either way, so they cannot catch a
reintroduction of the `Path.*_text(newline=)` form. `test_no_path_text_newline_kwarg`
is the guard that can.
"""
import re
from pathlib import Path

import pytest

from agent_loop._io import read_text_verbatim, write_text_verbatim
from agent_loop.regions import read_source

SRC = Path(__file__).resolve().parent.parent / "src" / "agent_loop"


def test_read_text_verbatim_preserves_crlf(tmp_path):
    """CRLF survives the read -- it is not universalised to LF."""
    p = tmp_path / "crlf.txt"
    p.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    assert read_text_verbatim(p) == "one\r\ntwo\r\nthree\r\n"


def test_read_text_verbatim_preserves_lf(tmp_path):
    """LF stays LF on Windows, where the platform terminator is CRLF."""
    p = tmp_path / "lf.txt"
    p.write_bytes(b"one\ntwo\nthree\n")
    assert read_text_verbatim(p) == "one\ntwo\nthree\n"


def test_write_text_verbatim_does_not_translate(tmp_path):
    """Bytes on disk are exactly the terminators the caller supplied."""
    p = tmp_path / "out.txt"
    write_text_verbatim(p, "a\nb\n")
    assert p.read_bytes() == b"a\nb\n"

    write_text_verbatim(p, "a\r\nb\r\n")
    assert p.read_bytes() == b"a\r\nb\r\n"


def test_roundtrip_is_byte_identical(tmp_path):
    """Read then write with no edit must not change a single byte.

    This is the invariant that keeps a two-line patch from becoming a
    whole-file diff on a repo whose terminators disagree with the platform.
    """
    for raw in (b"x\r\ny\r\n", b"x\ny\n", b"x\r\ny", b"mixed\r\nlf\nend\r\n"):
        p = tmp_path / "rt.txt"
        p.write_bytes(raw)
        write_text_verbatim(p, read_text_verbatim(p))
        assert p.read_bytes() == raw, raw


def test_read_source_reports_crlf_on_all_versions(tmp_path):
    """regions.read_source is the call that crashed on 3.12 -- exercise it."""
    p = tmp_path / "mod.py"
    p.write_bytes(b"def f():\r\n    return 1\r\n")
    lines, newline, had_trailing = read_source(p)
    assert lines == ["def f():", "    return 1"]
    assert newline == "\r\n"
    assert had_trailing is True


def test_read_source_reports_lf(tmp_path):
    p = tmp_path / "mod.py"
    p.write_bytes(b"def f():\n    return 1")
    lines, newline, had_trailing = read_source(p)
    assert lines == ["def f():", "    return 1"]
    assert newline == "\n"
    assert had_trailing is False


def test_no_path_text_newline_kwarg():
    """No module may pass `newline=` to Path.read_text/Path.write_text.

    That form is a silent 3.13+ dependency: it works on the dev interpreter and
    raises TypeError on a supported one. Use agent_loop._io instead.
    """
    pattern = re.compile(r"\.(?:read_text|write_text)\s*\([^)]*newline\s*=", re.S)
    offenders = []
    for py in SRC.rglob("*.py"):
        # _io.py is the sanctioned wrapper; its docstring names the banned form
        # in order to explain the ban.
        if py.name == "_io.py":
            continue
        hit = pattern.search(py.read_text(encoding="utf-8"))
        if hit:
            line = py.read_text(encoding="utf-8")[: hit.start()].count("\n") + 1
            offenders.append(f"{py.relative_to(SRC)}:{line}")
    assert not offenders, (
        "Path.read_text/write_text called with newline= (breaks on Python < 3.13); "
        "use agent_loop._io.read_text_verbatim / write_text_verbatim: " + ", ".join(offenders)
    )
