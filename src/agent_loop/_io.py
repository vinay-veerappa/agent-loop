"""
_io.py
======
Verbatim text I/O: read and write a file without translating line terminators.

Every line-ending fix in this package depends on reading and writing text with
newline translation switched off. `Path.read_text(newline=...)` and
`Path.write_text(newline=...)` do that, but the `newline` parameter was only
added to those two methods in **Python 3.13**. On 3.10-3.12 they raise
`TypeError: Path.read_text() got an unexpected keyword argument 'newline'`,
which means the CRLF handling in regions.py, developer/tools.py and
workspace.py crashed outright on any interpreter below 3.13 -- including a
consumer venv on 3.12 -- while passing on a 3.14 dev machine.

`open()` has accepted `newline` since Python 3.0, so these two helpers work on
every version this package claims to support. Use them instead of the
`Path.*_text(newline=...)` form; `tests/test_verbatim_io.py` enforces that.
"""
from __future__ import annotations

from pathlib import Path


def read_text_verbatim(path: Path) -> str:
    """Read UTF-8 text with line terminators preserved exactly as stored.

    Universal-newline translation is off, so "\\r\\n" stays "\\r\\n" and the
    caller can tell what the file actually uses.
    """
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write_text_verbatim(path: Path, text: str) -> int:
    """Write UTF-8 text exactly as given, translating no line terminators.

    Whatever terminators are in `text` are the terminators on disk, so a caller
    that rebuilt a file with its own newline does not get it rewritten in the
    platform's.
    """
    with open(path, "w", encoding="utf-8", newline="") as fh:
        return fh.write(text)
