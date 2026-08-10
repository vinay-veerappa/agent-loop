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
        try:
            body.encode("ascii")
        except UnicodeEncodeError as exc:
            problems.append(f"{rid}: non-ASCII output ({exc})")
        opens = sum(strip_code_fn(ln).count("{") for ln in body.splitlines())
        closes = sum(strip_code_fn(ln).count("}") for ln in body.splitlines())
        if opens != closes:
            problems.append(f"{rid}: unbalanced braces ({opens} open vs {closes} close)")
        orig_indent = len(r.text) - len(r.text.lstrip())
        new_indent = len(body) - len(body.lstrip())
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
# Gate 2 - compile
# --------------------------------------------------------------------------
_DIAG = re.compile(r"\b(error|warning)\s+[A-Z]{2}\d{3,}")


def _run(cmd: str, cwd: Path, timeout: int) -> Tuple[int, str]:
    proc = subprocess.run(
        cmd, shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )
    return proc.returncode, (proc.stdout or "") + "\n" + (proc.stderr or "")


def _digest(output: str, limit: int = 40) -> str:
    seen: Set[str] = set()
    uniq: List[str] = []
    for ln in output.splitlines():
        ln = ln.strip()
        if _DIAG.search(ln) and ln not in seen:
            seen.add(ln)
            uniq.append(ln)
    return "\n".join(uniq[:limit]) or output[-4000:]


def check_compile(cmd: str, repo: Path, timeout: int = 900) -> GateResult:
    """The gate that catches every invented symbol."""
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
#   2. The pytest format: "N failed, M passed" (or "M passed in N.NNs")
_FAIL_LINE = re.compile(r"^\s*\[FAIL\]\s*(?P<msg>.+?)\s*$", re.MULTILINE)
# Also catch pytest FAILED lines: "FAILED tests/test_x.py::test_name"
_FAIL_PYTEST = re.compile(r"^FAILED\s+(?P<msg>\S+::\S+)", re.MULTILINE)
_RESULTS = re.compile(r"RESULTS:\s*Passed\s*=\s*(\d+),\s*Failed\s*=\s*(\d+)")
# pytest summary: "1 failed, 17 passed" or "17 passed" or "1 failed, 17 passed in 4.71s"
_PYTEST_SUMMARY = re.compile(r"(\d+)\s+failed.*?(\d+)\s+passed|(\d+)\s+passed(?!\s+in\s)", re.DOTALL)


@dataclass
class TestOutcome:
    failures: Set[str] = field(default_factory=set)
    passed: int = 0
    failed: int = 0
    ran: bool = False
    raw: str = ""

    @property
    def counted(self) -> bool:
        return self.ran


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
    
    # Try pytest summary format: "N failed, M passed" or just "M passed"
    # Look for the last occurrence (the summary line at the end)
    for m in re.finditer(r"(\d+)\s+failed.*?(\d+)\s+passed", output):
        return TestOutcome(
            failures=failures,
            passed=int(m.group(2)),
            failed=int(m.group(1)),
            ran=True,
            raw=output,
        )
    for m in re.finditer(r"(\d+)\s+passed(?:\s+in\s+[\d.]+s)?\s*$", output, re.MULTILINE):
        return TestOutcome(
            failures=failures,
            passed=int(m.group(1)),
            failed=0,
            ran=True,
            raw=output,
        )
    
    return TestOutcome(failures=failures, ran=False, raw=output)


def run_tests(cmd: str, repo: Path, timeout: int = 900) -> TestOutcome:
    try:
        _, out = _run(cmd, repo, timeout)
    except subprocess.TimeoutExpired:
        return TestOutcome(ran=False, raw=f"test run timed out after {timeout}s")
    return parse_tests(out)


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
                "runner did not reach RESULTS (aborted or timed out)",
                out.raw[-4000:],
                secs,
                feedback="The test runner did not finish. Its output ends without a "
                "RESULTS line, so no conclusion can be drawn about your patch.",
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
        if any(t.lower() in f.lower() for f in out.failures)
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