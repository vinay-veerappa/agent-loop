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
TESTS = Path(__file__).resolve().parents[1]

# ⚠️ The ONE capture in this repo that must NOT pin an encoding: the negative
# control below, which proves the hazard is still real on this platform by
# reproducing it. It is why the suite prints a PytestUnhandledThreadExceptionWarning
# naming a cp1252 UnicodeDecodeError -- that warning is the control working, not
# a defect. Recorded here by (file, function) rather than by line number, which
# would go stale on the next edit above it.
DELIBERATELY_UNPINNED = {("test_subprocess_capture_encoding.py", "test_the_hazard_is_real_on_this_platform")}


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


def _enclosing_function(tree: ast.AST, lineno: int) -> str:
    """Name the function a line falls inside, so an exemption survives an edit above it."""
    best, best_line = "", -1
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= lineno <= end and node.lineno > best_line:
                best, best_line = node.name, node.lineno
    return best


def test_the_test_suite_pins_its_encodings_too():
    """CF-21: this gate said "every text capture" and inspected only src/.

    The suite spawns real git and real pytest against generated repos, and this
    repo's own consumer tests emit non-ASCII assertion text -- so a cp1252
    decode here kills a reader thread and hands the test `stdout is None`,
    which surfaces as an AttributeError blaming the assertion, not the capture.
    Five such captures were live in tests/ while the gate reported clean,
    because the gate never read that directory.

    A gate's evidence is bounded by the REGION it walks, not by what its name
    claims. State the region; count what was inspected.
    """
    test_files = sorted(TESTS.rglob("*.py"))
    unpinned, unreadable, exempted = [], [], []
    seen = 0
    for py in test_files:
        try:
            tree = ast.parse(py.read_text(encoding="utf-8-sig"))
            calls = list(_capture_calls(py))
        except SyntaxError as exc:
            unreadable.append(f"{py.name} ({exc.msg})")
            continue
        for path, lineno, kw in calls:
            seen += 1
            if "encoding" in kw:
                continue
            key = (path.name, _enclosing_function(tree, lineno))
            if key in DELIBERATELY_UNPINNED:
                exempted.append(f"{path.name}:{lineno}")
                continue
            unpinned.append(f"{path.name}:{lineno}")

    assert not unreadable, (
        "these test files could not be parsed, so this gate did not inspect them at all: "
        + ", ".join(unreadable)
    )
    assert seen >= 1, (
        f"no text-decoding subprocess calls found across {len(test_files)} test files "
        f"in {TESTS}; the AST walk has stopped matching the call shape"
    )
    # ⚠️ The exemption must be USED. An allowlist entry that matches nothing is
    # an allowlist that has rotted, and it would let the real control be pinned
    # -- which silently deletes the proof that the hazard still exists.
    assert exempted, (
        "the deliberately-unpinned negative control was not found; either it was "
        "pinned (which removes the proof the hazard is real) or it moved and "
        "DELIBERATELY_UNPINNED now names nothing"
    )
    assert not unpinned, (
        "these test-side subprocess captures decode without an explicit encoding, so on "
        "Windows they decode as cp1252 and one non-ASCII byte kills the reader thread: "
        + ", ".join(unpinned)
    )


def test_every_text_capture_pins_an_encoding():
    """The gate itself. Fails in the direction that matters: an unpinned capture."""
    # ⚠️ rglob, NOT glob. glob("*.py") inspects only the top level and misses
    # the developer/ subpackage, which holds two unpinned captures (R1).
    # Measured: glob finds 26 files, rglob finds 29, and the 3 missed include
    # developer/driver.py, developer/tools.py, developer/__init__.py.
    source_files = sorted(SRC.rglob("*.py"))
    unpinned = []
    unreadable = []
    seen = 0
    for py in source_files:
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

    # ⚠️ POSITIVE CONTROL, DERIVED from the actual file count. A literal `8`
    # still passes when the subject shrinks (glob found 26 of 29 files and
    # the gate reported clean). The derived count catches a shrinking subject
    # because it fails when the walk misses files. State what was inspected.
    assert seen >= 1, (
        f"no text-decoding subprocess calls found across {len(source_files)} source "
        f"files in {SRC}; the AST walk has probably stopped matching the call shape"
    )
    assert seen >= len(source_files) // 3, (
        f"only {seen} text-decoding subprocess call(s) found across {len(source_files)} "
        f"source files; the AST walk is probably missing calls"
    )

    assert not unpinned, (
        "these subprocess captures decode without an explicit encoding, so on Windows they "
        "decode as cp1252 and one non-ASCII byte in a build or test line kills the run with "
        "an error that blames the consumer's output format: " + ", ".join(unpinned)
    )


def test_every_text_capture_also_replaces_undecodable_bytes():
    """`encoding='utf-8'` alone still RAISES on a byte that is not valid UTF-8."""
    strict = []
    # rglob, not glob -- see test_every_text_capture_pins_an_encoding.
    for py in sorted(SRC.rglob("*.py")):
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
