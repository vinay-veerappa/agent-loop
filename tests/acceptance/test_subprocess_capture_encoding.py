"""Every subprocess capture must pin an explicit encoding.

MEASURED 2026-08-15, on `nt8-mcp-bridge`. Its test harness prints two assertion messages
containing a warning sign. `subprocess.run(..., text=True)` with no `encoding=` decodes as
**cp1252 on Windows**, so the reader thread died:

    UnicodeDecodeError: 'charmap' codec can't decode byte 0x8f in position 19212
    ERROR T1: baseline test run produced no parseable result summary

Two things make this worth a permanent gate rather than a one-line fix.

**The visible error blames the consumer.** "produced no parseable result summary" reads as *your
repo's test output is in a shape I do not understand*, so the natural response is to go and change
the consumer's runner. Nothing in the message mentions encoding. That harness had never once been
runnable by this loop, and nothing said so.

**And the 633-test suite passed identically before and after the fix**, because no test had ever
fed non-ASCII through a gate. A fix with no test is a fix that comes back -- this is the same class
already recorded twice in the consumer repos (`nt8-riskguard`'s mutation batteries, then
`nt8-mcp-bridge`'s), which is what makes it a class rather than a bug.

The second half of the pin, `errors='replace'`, is deliberate: a mojibake character in a diagnostic
is worth infinitely more than losing the run, and a gate's product is the diagnostic.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "agent_loop"


def _capture_calls(path: Path):
    """
    Yield (path, lineno, kwargs) for every subprocess call that captures text output.

    ⚠️ `utf-8-sig`, not `utf-8`: `src/agent_loop/selftest.py` carries a BOM, and plain utf-8
    leaves it in the text as U+FEFF, which `ast.parse` rejects as an invalid non-printable
    character. Caught by this gate on its own first run. A gate that cannot read a file must
    say so rather than skip it -- see the unparseable branch in the test below.
    """
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name not in ("run", "Popen", "check_output"):
            continue
        kw = {k.arg for k in node.keywords if k.arg}
        # Only captures that DECODE. A binary capture has no encoding to get wrong.
        if "text" in kw or "universal_newlines" in kw or "encoding" in kw:
            yield path, node.lineno, kw


def test_every_text_capture_pins_an_encoding():
    """The gate itself. Fails in the direction that matters: an unpinned capture."""
    unpinned = []
    unreadable = []
    seen = 0
    for py in sorted(SRC.glob("*.py")):
        try:
            calls = list(_capture_calls(py))
        except SyntaxError as exc:
            # NOT a skip. A file this gate cannot parse is a file it is not inspecting, and a
            # check that silently drops its subject is the failure mode this repo keeps finding.
            unreadable.append(f"{py.name} ({exc.msg})")
            continue
        for path, lineno, kw in calls:
            seen += 1
            if "encoding" not in kw:
                unpinned.append(f"{path.name}:{lineno}")

    assert not unreadable, (
        "these sources could not be parsed, so this gate did not inspect them at all: "
        + ", ".join(unreadable)
    )

    # ⚠️ POSITIVE CONTROL. Without it this passes vacuously the day the walker stops
    # recognising a call shape -- which is how a check starts reporting a clean tree because
    # it is looking at nothing. State what was actually inspected.
    assert seen >= 8, (
        f"only {seen} text-decoding subprocess call(s) found in {SRC}; the AST walk has "
        "probably stopped matching the call shape, so this gate would pass having "
        "inspected nothing"
    )

    assert not unpinned, (
        "these subprocess captures decode without an explicit encoding, so on Windows they "
        "decode as cp1252 and one non-ASCII byte in a build or test line kills the run with "
        "an error that blames the consumer's output format: " + ", ".join(unpinned)
    )


def test_every_text_capture_also_replaces_undecodable_bytes():
    """`encoding='utf-8'` alone still RAISES on a byte that is not valid UTF-8."""
    strict = []
    for py in sorted(SRC.glob("*.py")):
        for path, lineno, kw in _capture_calls(py):
            if "encoding" in kw and "errors" not in kw:
                strict.append(f"{path.name}:{lineno}")
    assert not strict, (
        "these captures pin an encoding but not errors='replace', so a byte that is not valid "
        "UTF-8 still raises and still loses the whole run: " + ", ".join(strict)
    )


@pytest.mark.skipif(sys.platform != "win32", reason="cp1252 default is Windows-specific")
def test_the_hazard_is_real_on_this_platform():
    """
    Drives the actual failure rather than asserting about source text, because a source gate
    proves less. Without an encoding this raises; with one it returns the text.
    """
    from agent_loop.gates import _run

    # ⚠️ THE EXACT CHARACTER MATTERS, and getting it wrong is how this test first passed while
    # proving nothing. U+26A0 alone is UTF-8 `E2 9A A0`, and cp1252 decodes all three bytes
    # happily (as mojibake) -- so it does NOT raise. It is the VARIATION SELECTOR U+FE0F that
    # carries the undefined byte: `EF B8 8F`, and 0x8F is exactly the byte the live failure
    # reported. Mojibake is survivable; an undecodable byte is what loses the run.
    cmd = (
        'python -c "import sys; sys.stdout.reconfigure(encoding=\'utf-8\'); '
        'print(\'\\u26a0\\ufe0f done\')"'
    )

    # ⚠️ THE UNPINNED FORM DOES NOT RAISE IN THE CALLER. The UnicodeDecodeError happens on the
    # READER THREAD, so `subprocess.run` returns normally and hands back `stdout=None`. That is
    # what makes this defect so hard to read from the outside: there is no traceback at the call
    # site, just a result object whose output is silently missing, and every downstream parser
    # then reports "no parseable result summary". Asserting a raise here is the obvious test and
    # it fails -- the observable is the None.
    bad = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    assert bad.stdout is None, (
        "expected the unpinned capture to lose its output to a dead reader thread; if this "
        "starts passing, the platform default changed and this whole gate needs re-deriving"
    )

    # The real one survives it and hands back a usable diagnostic.
    code, out = _run(cmd, Path.cwd(), timeout=60)
    assert code == 0
    assert "done" in out
