"""
gates.py
========
Mechanical checks a candidate patch must clear before any model opinion counts.

Language-agnostic: lock-scope patterns, preprocessor directives, and
protected-path patterns come from the Profile, not hardcoded.

Ordering is deliberate and cost-ascending: the free checks run first, so a
patch that leaked a marker or invented a symbol never reaches a paid reviewer.

    protected -> static -> compile -> test -> lock-scope -> (panel) -> (arbiter)

Every gate here is deterministic. That is the point: a reviewer can be talked
out of a finding, a compiler cannot. Where a gate and the panel disagree, the
gate wins.
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .profiles import Profile, DEFAULT_PROTECTED


@dataclass
class GateResult:
    name: str
    ok: bool
    summary: str
    detail: str = ""
    secs: float = 0.0
    feedback: str = ""


# --------------------------------------------------------------------------
# Gate 0 - protected paths (anti reward-hacking)
# --------------------------------------------------------------------------
def check_protected_paths(
    region_files: Sequence[str], protected: Sequence[str] = DEFAULT_PROTECTED
) -> GateResult:
    """Refuse a ticket whose regions overlap anything that grades the work."""
    from fnmatch import fnmatch

    hits = []
    for f in region_files:
        norm = f.replace("\\", "/")
        for pat in protected:
            if fnmatch(norm, pat) or fnmatch(Path(norm).name, pat):
                hits.append(f"{f} matches protected pattern {pat!r}")
    if hits:
        return GateResult(
            "protected",
            False,
            f"{len(hits)} region(s) target the verifier",
            "\n".join(hits),
            feedback="This ticket is malformed and must not run: it would let the "
            "patch edit the code that grades it.",
        )
    return GateResult("protected", True, f"{len(region_files)} region file(s) clear of verifier")


# --------------------------------------------------------------------------
# Gate 1 - static
# --------------------------------------------------------------------------
def check_static(regions, blocks: Dict[str, str], strip_code_fn, profile: Profile) -> GateResult:
    """Shape checks that need no toolchain. Cheap, so they run first."""
    problems: List[str] = []
    for r in regions:
        rid = r.id
        if rid not in blocks:
            problems.append(f"{rid}: missing from model output")
            continue
        body = blocks[rid]
        if not body.strip():
            problems.append(f"{rid}: empty replacement")
            continue
        if profile.ascii_only:
            try:
                body.encode("ascii")
            except UnicodeEncodeError as exc:
                problems.append(f"{rid}: non-ASCII output ({exc})")
        # Braces delimit blocks only in brace-delimited languages. In Python a
        # brace is a dict/set literal, so counting them proves nothing and can
        # fail a valid patch; the compile gate catches real syntax errors.
        if profile.block_kind == "decl":
            opens = sum(strip_code_fn(ln).count("{") for ln in body.splitlines())
            closes = sum(strip_code_fn(ln).count("}") for ln in body.splitlines())
            if opens != closes:
                problems.append(f"{rid}: unbalanced braces ({opens} open vs {closes} close)")
        # Compare the FIRST LINE's indent, which is what this check is about.
        # Measuring the whole block with lstrip() also eats leading newlines and
        # reports an indent the first line does not have.
        orig_first = (r.text.splitlines() or [""])[0]
        new_first = (body.splitlines() or [""])[0]
        orig_indent = len(orig_first) - len(orig_first.lstrip())
        new_indent = len(new_first) - len(new_first.lstrip())
        if orig_indent != new_indent:
            problems.append(f"{rid}: leading indentation changed ({orig_indent} -> {new_indent})")
        if "<<<" in body:
            problems.append(f"{rid}: marker leaked into body")
        if profile.preprocessor_directives:
            n_if = len(re.findall(r"^\s*#if", body, re.MULTILINE))
            n_endif = len(re.findall(r"^\s*#endif", body, re.MULTILINE))
            if n_if != n_endif:
                problems.append(f"{rid}: unbalanced #if/#endif ({n_if}/{n_endif})")
    if problems:
        return GateResult(
            "static",
            False,
            f"{len(problems)} problem(s)",
            "\n".join(problems),
            feedback="Your output failed mechanical validation before review. Fix these "
            "and re-emit ALL blocks:\n" + "\n".join(f"- {p}" for p in problems),
        )
    return GateResult("static", True, f"{len(regions)} block(s) well-formed")


# --------------------------------------------------------------------------
# Gate 1.5 - lint (optional, between static and compile)
# --------------------------------------------------------------------------
def check_lint(
    cmd: str,
    repo: Path,
    timeout: int = 300,
    files: Sequence[str] = (),
) -> GateResult:
    """Run the profile's linter command if configured.

    Every finding a linter can make is a finding you're paying a model to
    make and an arbiter to adjudicate. Cheap, deterministic, and it
    shrinks the panel's surface. The linter runs before compile (cheaper)
    and before tests (faster feedback).

    If the profile has no ``lint_cmd``, this gate is skipped (returns ok).

    If ``cmd`` contains the literal ``{files}`` placeholder, it is replaced
    with the quoted paths in ``files``; if ``files`` is empty the gate
    passes without running the linter.
    """
    if not cmd:
        return GateResult("lint", True, "no linter configured")
    if "{files}" in cmd:
        if not files:
            return GateResult("lint", True, "no files to lint")
        cmd = cmd.replace("{files}", " ".join(f'"{f}"' for f in files))
    t0 = time.time()
    try:
        code, out = _run(cmd, repo, timeout)
    except subprocess.TimeoutExpired:
        return GateResult("lint", False, f"timed out after {timeout}s",
                          feedback="Linter timed out.")
    secs = round(time.time() - t0, 1)
    if code == 0:
        return GateResult("lint", True, "lint clean", out, secs)
    d = lint_digest(out)
    return GateResult(
        "lint",
        False,
        "lint FAILED",
        out,
        secs,
        feedback=(
            "Your patch has lint errors. Fix these before the compiler runs:\n\n"
            + d
            + "\n\nFix every lint error and re-emit ALL blocks in full."
        ),
    )


# --------------------------------------------------------------------------
# Gate 2 - compile
# --------------------------------------------------------------------------
_DIAG = re.compile(r"\b(error|warning)\s+[A-Z]{2}\d{3,}")


def _run(cmd: str, cwd: Path, timeout: int) -> Tuple[int, str]:
    # ⚠️ `text=True` WITHOUT AN EXPLICIT ENCODING DECODES AS cp1252 ON WINDOWS, and one
    # non-ASCII byte in a build or test line kills the reader thread with UnicodeDecodeError.
    # Every gate runs through here, so the whole ticket dies -- and it does NOT surface as an
    # encoding problem. Measured 2026-08-15 on `nt8-mcp-bridge`, whose harness prints two
    # assertion messages containing `⚠️`:
    #
    #   UnicodeDecodeError: 'charmap' codec can't decode byte 0x8f in position 19212
    #   ERROR T1: baseline test run produced no parseable result summary
    #
    # The visible message blames the consumer's test output for being unparseable, so the repo
    # looks misconfigured. That harness had never been runnable by this loop and nothing said so.
    #
    # `errors='replace'` as well as the encoding: a mojibake character in a diagnostic is worth
    # infinitely more than losing the run, and a gate's product is the DIAGNOSTIC, not fidelity.
    proc = subprocess.run(
        cmd, shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + "\n" + (proc.stderr or "")


# A linter diagnostic, across the shapes the profiles actually use. `_DIAG` above
# matches only MSBuild's `error CS1234`, so on ruff or eslint output NOTHING
# matched and `_digest` fell through to `output[-4000:]` -- handing the model a raw
# tail to find the errors in. Feedback the model cannot act on is a gate that only
# looks like one.
_LINT_DIAG = re.compile(
    r"""(
        \b(?:error|warning)\s+[A-Z]{2}\d{3,}        # MSBuild: error CS1002
      | :\d+:\d+:\s*[A-Z]+\d+\b                     # ruff: path:12:1: F401
      | ^\s*\d+:\d+\s+(?:error|warning)\b           # eslint: "  12:1  error ..."
      | \b(?:error|warning):\s                      # gcc/clang, tsc
    )""",
    re.VERBOSE | re.MULTILINE,
)


def lint_digest(output: str, limit: int = 40) -> str:
    """The diagnostic lines from a linter's output, deduplicated.

    Separate from `_digest` rather than a widened version of it: the compile gate
    wants MSBuild's shape specifically, and broadening that regex would make the
    C# compile digest start matching prose. Same reason `op` is not folded into
    `kind` -- two callers, two jobs.
    """
    seen: Set[str] = set()
    uniq: List[str] = []
    for ln in output.splitlines():
        ln = ln.strip()
        if ln and _LINT_DIAG.search(ln) and ln not in seen:
            seen.add(ln)
            uniq.append(ln)
    return "\n".join(uniq[:limit]) or output[-4000:]


def _digest(output: str, limit: int = 40) -> str:
    seen: Set[str] = set()
    uniq: List[str] = []
    for ln in output.splitlines():
        ln = ln.strip()
        if _DIAG.search(ln) and ln not in seen:
            seen.add(ln)
            uniq.append(ln)
    return "\n".join(uniq[:limit]) or output[-4000:]


def check_compile(
    cmd: str, repo: Path, timeout: int = 900, files: Sequence[str] = ()
) -> GateResult:
    """The gate that catches every invented symbol.

    A `{files}` placeholder in the profile's build_cmd is substituted with the
    files this patch actually touched. Without it a profile can only name a
    fixed target, and a build_cmd that compiles some OTHER file passes no
    matter what the patch did -- a gate that cannot fail, which is worse than
    no gate at all.
    """
    if "{files}" in cmd:
        if not files:
            return GateResult("compile", True, "no files to compile")
        cmd = cmd.replace("{files}", " ".join(f'"{f}"' for f in files))
    t0 = time.time()
    try:
        code, out = _run(cmd, repo, timeout)
    except subprocess.TimeoutExpired:
        return GateResult("compile", False, f"timed out after {timeout}s", feedback="Build timed out.")
    secs = round(time.time() - t0, 1)
    if code == 0:
        return GateResult("compile", True, "build succeeded", out, secs)
    d = _digest(out)
    return GateResult(
        "compile",
        False,
        "build FAILED",
        out,
        secs,
        feedback=(
            "Your patch DOES NOT COMPILE. You may only reference members that already "
            "exist in the file or that you define inside the regions you were given - "
            "you cannot rely on helpers, fields, or changed call signatures elsewhere "
            f"in the file. Compiler output:\n\n{d}\n\nFix every error and re-emit ALL blocks in full."
        ),
    )


# --------------------------------------------------------------------------
# Gate 3 - test, against a frozen expected-failure baseline
# --------------------------------------------------------------------------
# Two output formats are parsed:
#   1. The NT8 dotnet test format: "RESULTS: Passed = N, Failed = M"
#   2. The pytest terminal summary: "===== 1 failed, 16 passed in 2.31s ====="
_FAIL_LINE = re.compile(r"^\s*\[FAIL\]\s*(?P<msg>.+?)\s*$", re.MULTILINE)
# Also catch pytest FAILED / ERROR lines: "FAILED tests/test_x.py::test_name"
_FAIL_PYTEST = re.compile(r"^FAILED\s+(?P<msg>\S+::\S+)", re.MULTILINE)
_ERROR_PYTEST = re.compile(r"^ERROR\s+(?P<msg>\S+)", re.MULTILINE)
_RESULTS = re.compile(r"RESULTS:\s*Passed\s*=\s*(\d+),\s*Failed\s*=\s*(\d+)")

# The pytest summary is the last '='-padded line. Counts are read by KEYWORD
# rather than by position: matching "N failed, M passed" positionally meant
# that "17 passed, 1 warning in 2.31s" and "15 passed, 2 skipped in 1.02s" --
# both entirely ordinary green runs -- parsed as "the runner never finished",
# which failed the gate and made capture_baseline refuse to establish any
# baseline at all. A warning must not be able to abort a ticket.
_PYTEST_PADDED_LINE = re.compile(r"^=+\s*(?P<body>.*?)\s*=+\s*$", re.MULTILINE)
_PYTEST_COUNT = re.compile(
    r"(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b"
)


@dataclass
class TestOutcome:
    failures: Set[str] = field(default_factory=set)
    passed: int = 0
    failed: int = 0
    ran: bool = False
    raw: str = ""
    # Suite-level errors (pytest collection errors, fixture errors). Distinct
    # from failures: an errored suite did not report a verdict on the tests it
    # never reached, so it cannot serve as a baseline.
    errors: int = 0

    @property
    def counted(self) -> bool:
        return self.ran


def _pytest_counts(output: str) -> Dict[str, int]:
    """Read counts by keyword from pytest's terminal summary line."""
    bodies = [m.group("body") for m in _PYTEST_PADDED_LINE.finditer(output)]
    # Consider the padded summary lines last-first, then any line carrying
    # counts, so `-p no:cacheprovider` style output still parses.
    candidates = list(reversed(bodies)) + list(reversed(output.splitlines()))
    for line in candidates:
        if "no tests ran" in line.lower():
            return {}
        found = _PYTEST_COUNT.findall(line)
        if found:
            counts: Dict[str, int] = {}
            for n, kind in found:
                kind = "errors" if kind.startswith("error") else kind
                counts[kind] = counts.get(kind, 0) + int(n)
            return counts
    return {}


def parse_tests(output: str) -> TestOutcome:
    # Collect failures from both NT8 [FAIL] format and pytest FAILED format
    failures = {m.group("msg") for m in _FAIL_LINE.finditer(output)}
    failures.update(m.group("msg") for m in _FAIL_PYTEST.finditer(output))

    # Try NT8 format first
    m = _RESULTS.search(output)
    if m:
        return TestOutcome(
            failures=failures,
            passed=int(m.group(1)),
            failed=int(m.group(2)),
            ran=True,
            raw=output,
        )

    counts = _pytest_counts(output)
    n_err = counts.get("errors", 0)
    if n_err:
        failures.update(m.group("msg") for m in _ERROR_PYTEST.finditer(output))
    if counts.keys() & {"passed", "failed", "errors"}:
        return TestOutcome(
            failures=failures,
            passed=counts.get("passed", 0),
            failed=counts.get("failed", 0) + n_err,
            # A run that only errored reported no verdicts; it did not "run".
            ran=bool(counts.keys() & {"passed", "failed"}),
            raw=output,
            errors=n_err,
        )

    return TestOutcome(failures=failures, ran=False, raw=output)


def run_tests(cmd: str, repo: Path, timeout: int = 900) -> TestOutcome:
    try:
        _, out = _run(cmd, repo, timeout)
    except subprocess.TimeoutExpired:
        return TestOutcome(ran=False, raw=f"test run timed out after {timeout}s")
    return parse_tests(out)


# An exception name, as it appears before the colon in a traceback. The suffix
# set keeps this from matching ordinary prose in a failure message; `Failed` is
# pytest's own, produced by `pytest.raises` when nothing is raised.
_EXC = r"[A-Za-z_][\w.]*(?:Error|Exception|Warning)|Failed"
# `E   AttributeError: 'dict' object has no attribute 'votes'`
_E_NAMED = re.compile(rf"^\s*E\s+(?P<name>{_EXC})\b\s*:", re.M)
# `E       assert 5 == 6` -- a bare assert, rewritten by pytest, carries NO
# exception name anywhere in the gutter. This is the COMMON case and omitting it
# would classify most genuine acceptance tests as "never reached an assertion".
_E_BARE_ASSERT = re.compile(r"^\s*E\s+assert\b", re.M)
# `tests/t.py:288: AssertionError` -- the last line of a --tb=short frame.
_TB_TAIL = re.compile(rf"^\S.*:\d+:\s+(?P<name>{_EXC})\s*$", re.M)
# `FAILED tests/t.py::test_a - TypeError: ...`, the only shape under --tb=no.
_SUMMARY_NAMED = re.compile(rf"^FAILED\s+\S+\s+-\s+(?P<name>{_EXC})\b", re.M)
_SUMMARY_BARE_ASSERT = re.compile(r"^FAILED\s+\S+\s+-\s+assert\b", re.M)

_ASSERTION_KINDS = frozenset({"AssertionError", "Failed"})

# For a FEATURE, these are legitimate evidence of red rather than a broken test.
# The natural first test for code that does not exist yet imports a module that
# does not exist yet, so it fails on the name -- and that failure IS the thing
# being demonstrated. Without this, the O34 gate refuses every feature on arrival.
#
# Deliberately narrow: a TypeError or a ZeroDivisionError inside a stub is a
# broken test whether the work is a feature or a fix, so the exception covers only
# the "this name is not there" family.
_NOT_YET_KINDS = frozenset({
    "ImportError", "ModuleNotFoundError", "AttributeError", "NameError",
})


def is_feature_ticket(ticket: Dict[str, Any]) -> bool:
    """Is this ticket authoring code that does not exist yet?

    True when any region creates a file, or when the ticket says so explicitly.
    Derived rather than required, so a caller that has already declared
    `op: create` does not have to say it twice.
    """
    if not ticket:
        return False
    if str(ticket.get("kind", "")).lower() == "feature":
        return True
    return any(
        str(r.get("op", "")).lower() == "create"
        for r in ticket.get("regions") or ()
        if isinstance(r, dict)
    )


def failure_kinds(raw: str) -> Set[str]:
    """The exception types a test run ended its failures with.

    Exists because the test-first check could count failures but not read them,
    so it called a test "failing at baseline (correct)" when the test had died in
    its own scaffolding and never reached an assertion (O34).

    Returns an empty set when nothing can be identified -- a non-pytest runner,
    for instance. Empty means "cannot tell", NOT "no assertion": see
    `reached_an_assertion`.
    """
    kinds: Set[str] = set()
    for pattern in (_E_NAMED, _TB_TAIL, _SUMMARY_NAMED):
        kinds.update(m.group("name") for m in pattern.finditer(raw))
    if _E_BARE_ASSERT.search(raw) or _SUMMARY_BARE_ASSERT.search(raw):
        kinds.add("AssertionError")
    return kinds


def reached_an_assertion(kinds: Set[str], feature: bool = False) -> Optional[bool]:
    """Did any failure get as far as asserting something?

    True  - at least one failure was an assertion. The test ran and disagreed.
    False - failures were identified and none was an assertion, so every one of
            them died before testing anything. A red phase built on that proves
            nothing about the defect.
    None  - nothing identifiable, so this cannot be decided. Reported as unknown
            rather than as a refusal: the NT8 profile's runner prints
            `[FAIL] Suite.Test` and no exception at all, and a check that fails
            every run on a runner it does not understand would just be turned off.

    `feature=True` also accepts a missing NAME as evidence. A feature's first test
    imports something that has not been written, so it fails with ImportError or
    AttributeError -- which for a defect fix means "the test is broken" and for a
    feature means "the code is not there yet, which is the point". Same output,
    opposite meaning, and only the caller knows which job it is.
    """
    if not kinds:
        return None
    if kinds & _ASSERTION_KINDS:
        return True
    if feature and kinds & _NOT_YET_KINDS:
        return True
    return False


def names_match(name: str, failure: str) -> bool:
    """Does `failure` name the test `name`?

    Whole-identifier match, not substring: `test_foo` must not be considered
    satisfied by a failure in `test_foo_bar`. Substring matching let a
    misspelled or prefix-shaped expect_green entry silently satisfy the
    test-first check, which is the one check standing between the loop and a
    vacuous gate.
    """
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", failure, re.IGNORECASE) is not None


def check_tests(
    cmd: str,
    repo: Path,
    baseline: Set[str],
    timeout: int = 900,
    expect_green: Sequence[str] = (),
) -> Tuple[GateResult, TestOutcome]:
    """Compare this run's failure set against the frozen baseline."""
    t0 = time.time()
    out = run_tests(cmd, repo, timeout)
    secs = round(time.time() - t0, 1)

    if not out.counted:
        return (
            GateResult(
                "test",
                False,
                "runner produced no parseable result summary (aborted or timed out)",
                out.raw[-4000:],
                secs,
                feedback="The test runner did not finish. Its output ends without a "
                "result summary, so no conclusion can be drawn about your patch.",
            ),
            out,
        )

    new = sorted(out.failures - baseline)
    fixed = sorted(baseline - out.failures)
    note = f"{out.passed} passed, {out.failed} failed"
    if fixed:
        note += f", {len(fixed)} expected failure(s) now green"

    still_red = [
        t for t in expect_green
        if any(names_match(t, f) for f in out.failures)
    ]
    if still_red and not new:
        return (
            GateResult(
                "test",
                False,
                f"{len(still_red)} acceptance test(s) still failing; {note}",
                "STILL FAILING (this ticket exists to make these pass):\n"
                + "\n".join(f"  - {t}" for t in still_red),
                secs,
                feedback=(
                    "Your patch does not close the defect. These tests define this "
                    "ticket's acceptance criteria and are STILL FAILING:\n\n"
                    + "\n".join(f"- {t}" for t in still_red)
                    + "\n\nThey are correct and you may not change them. Re-read the "
                    "defect and the failing assertion text, then re-emit ALL blocks in full."
                ),
            ),
            out,
        )

    if new:
        detail = "REGRESSIONS (not in baseline):\n" + "\n".join(f"  - {f}" for f in new)
        if fixed:
            detail += "\n\nNewly passing:\n" + "\n".join(f"  - {f}" for f in fixed)
        return (
            GateResult(
                "test",
                False,
                f"{len(new)} regression(s); {note}",
                detail,
                secs,
                feedback=(
                    "Your patch BREAKS tests that passed before it. These failures are "
                    "new and are not part of the known-failing baseline:\n\n"
                    + "\n".join(f"- {f}" for f in new)
                    + "\n\nFix them and re-emit ALL blocks in full."
                ),
            ),
            out,
        )
    if expect_green:
        note += f"; all {len(expect_green)} acceptance test(s) green"
    return GateResult("test", True, f"no regressions; {note}", "\n".join(fixed), secs), out


# --------------------------------------------------------------------------
# Gate 4 - lock scope
# --------------------------------------------------------------------------
def check_lock_scope(regions, blocks: Dict[str, str], strip_code_fn,
                     profile: Profile) -> GateResult:
    """Flag broker/risk calls reachable inside a lock, using the profile's
    lock_name and risk_calls. If the profile has no lock_name, the gate
    is skipped (returns ok)."""
    if not profile.lock_name:
        return GateResult("lock-scope", True, f"no lock primitive in {profile.language}")

    flags: List[str] = []
    pat = re.compile(r"lock\s*\(\s*" + re.escape(profile.lock_name) + r"\s*\)")
    risk_re = re.compile(r"\.(?:" + "|".join(re.escape(c.lstrip(".")) for c in profile.risk_calls) + r")\s*\(")
    for r in regions:
        body = blocks.get(r.id)
        if not body:
            continue
        depth = 0
        lock_depths: List[int] = []
        pending_lock = False
        for ln in body.splitlines():
            code = strip_code_fn(ln)
            events = (
                [(m.start(), "lock") for m in pat.finditer(code)]
                + [(m.start(), "risk") for m in risk_re.finditer(code)]
                + [(i, "open") for i, c in enumerate(code) if c == "{"]
                + [(i, "close") for i, c in enumerate(code) if c == "}"]
            )
            for _, kind in sorted(events):
                if kind == "lock":
                    pending_lock = True
                elif kind == "open":
                    depth += 1
                    if pending_lock:
                        lock_depths.append(depth)
                        pending_lock = False
                elif kind == "close":
                    if lock_depths and depth == lock_depths[-1]:
                        lock_depths.pop()
                    depth -= 1
                elif kind == "risk" and lock_depths:
                    flags.append(f"{r.id}: risk call under {profile.lock_name} -> {ln.strip()}")
    if flags:
        return GateResult(
            "lock-scope",
            False,
            f"{len(flags)} risk call(s) under {profile.lock_name}",
            "\n".join(flags),
            feedback="Mechanical lock-scope violations detected:\n"
            + "\n".join(f"- {f}" for f in flags),
        )
    return GateResult("lock-scope", True, f"no risk calls under {profile.lock_name}")