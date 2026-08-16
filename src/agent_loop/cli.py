"""
cli.py
======
Entry point.  python -m agent_loop --help

Any agent that can run a shell command can drive this: there is no interactive
mode and no hidden state. Every decision lands in logs/agent_loop/<TICKET>/ and
logs/agent_loop/ledger.jsonl.

Consumers register their profiles via the profiles.register() function before
calling main(), or by passing --profile-module which imports a Python module
that registers profiles at import time.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path

from . import config, models, profiles, regions, workspace
from .loop import DEVELOPER_PROMOTABLE, PROMOTABLE, run_ticket
from .models import DEFAULT_REGISTRY


class TicketFileError(Exception):
    """A ticket file that cannot be read as tickets."""


def load_tickets(path: Path) -> list:
    """Read a ticket file, accepting every shape anything in this package emits.

    Two call sites used to do `spec["tickets"]` directly. Plan mode writes a
    BARE ticket object, so `--mode plan` -> `--mode test` and `--mode plan` ->
    `--tickets` both died on `KeyError: 'tickets'` -- a traceback from inside a
    dict subscript, naming neither the file nor the expected shape. That is the
    whole of O33: plan mode's only purpose is to feed the loop, and its output
    had never been loadable by it.

    Accepted:
      {"tickets": [...]}   the canonical wrapper, and what plan mode writes now
      [...]                a bare list
      {...}                a single bare ticket -- every plan.json already on disk

    A ticket is recognised by having an `id`; that is the field both consumers
    index on, so anything without one could not be selected or reported anyway.
    """
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TicketFileError(f"no such ticket file: {path}") from None
    except json.JSONDecodeError as exc:
        raise TicketFileError(f"{path} is not valid JSON: {exc}") from None

    if isinstance(spec, dict) and "tickets" in spec:
        tickets = spec["tickets"]
    elif isinstance(spec, list):
        tickets = spec
    elif isinstance(spec, dict) and "id" in spec:
        tickets = [spec]
    else:
        raise TicketFileError(
            f"{path} has no tickets. Expected {{'tickets': [...]}}, a list of "
            f"tickets, or a single ticket object with an 'id'; "
            f"found a {type(spec).__name__} with keys "
            f"{sorted(spec)[:6] if isinstance(spec, dict) else '(n/a)'}."
        )

    if not isinstance(tickets, list) or not tickets:
        raise TicketFileError(f"{path} contains no tickets.")
    missing = [i for i, t in enumerate(tickets) if not isinstance(t, dict) or "id" not in t]
    if missing:
        raise TicketFileError(f"{path}: ticket(s) at index {missing} have no 'id'.")
    return tickets


def _looks_like_code(token: str, spec: str = "") -> bool:
    """CF-1: return True if a capitalized token looks like a code identifier.

    A single-word ALL-CAPS token (CSS, DETAIL, GOES) is prose in every
    house style. A token looks like code if:
      - it contains an underscore (snake_case, even ALL_CAPS constants)
      - it has an interior lowercase->uppercase transition (camelCase:
        parseDate) OR an uppercase-then-lowercase transition (PascalCase:
        StringBuilder). A two-letter token like "Do" or "If" has no
        interior transition in either direction and is prose.
    """
    if "_" in token:
        return True
    has_upper = False
    has_lower = False
    for ch in token:
        if ch.isupper():
            has_upper = True
        elif ch.islower():
            has_lower = True
    # ALL-CAPS with no lowercase = prose (CSS, DETAIL, SCOPE)
    if has_upper and not has_lower:
        return False
    # Mixed case: require an interior transition, not just has_upper+has_lower.
    # "Do" (D=upper, o=lower) has both but no interior transition — it's
    # a sentence-initial English word. "parseDate" has lower->upper at
    # position 5. "StringBuilder" has upper->lower at position 1.
    if has_upper and has_lower and len(token) >= 3:
        for i in range(1, len(token)):
            if token[i - 1].islower() and token[i].isupper():
                return True  # camelCase transition
        # PascalCase: first char upper, second char lower, rest mixed.
        # "Do" (len 2) fails len>=3. "Five" (len 4) has no interior
        # transition: F-i-v-e is upper-lower-lower-lower, no transition.
        # "StringBuilder" has S-t (upper->lower at pos 0->1) which is
        # the PascalCase signature. Check for it explicitly:
        if token[0].isupper() and token[1].islower():
            # But "Five" also has this pattern. Require at least one
            # more upper later, or an underscore (already handled).
            # True PascalCase has multiple uppercase letters.
            return sum(1 for c in token if c.isupper()) >= 2
    return False


def _list(tickets, profile) -> int:
    """Confirm every ticket's regions still resolve against the current tree.

    Also validates expect_green against the test failure set (O65) and warns
    when a capitalized identifier in spec/context is not declared inside any
    region of the same file (O66).
    """
    from . import gates

    bad = 0
    for t in tickets:
        files = sorted({r["file"] for r in t["regions"]})
        g = gates.check_protected_paths(files, profile.protected or gates.DEFAULT_PROTECTED)
        flag = "" if g.ok else "  [REFUSED: targets the verifier]"
        print(f"{t['id']:<5} {t['title']}{flag}")
        for spec in t["regions"]:
            try:
                r = regions.extract(Path("."), [spec], profile)[0]
                print(f"      OK   {r.id:<24} {r.file} {r.lines_1based}")
            except regions.RegionError as exc:
                bad += 1
                print(f"      FAIL {spec['id']:<24} {exc}")

        # O65: validate expect_green against the test failure set. A string
        # matching no current failure is a vacuous gate (it can never go
        # red-then-green, so the run can pass without the implementation having
        # done anything). A failure no expect_green claims is a forgotten
        # criterion. Both are caught by a set difference in both directions.
        expect_green = t.get("expect_green", ())
        if expect_green and profile.test_cmd:
            try:
                outcome = gates.run_tests(profile.test_cmd, Path("."))
                if outcome.ran:
                    for name in expect_green:
                        if not any(gates.names_match(name, f) for f in outcome.failures):
                            bad += 1
                            print(f"      WARN expect_green '{name}' matches no current failure "
                                  f"-- vacuous gate (can never go red-then-green)",
                                  file=sys.stderr)
                    for fail in sorted(outcome.failures):
                        if not any(gates.names_match(name, fail) for name in expect_green):
                            print(f"      NOTE failure '{fail}' is not in expect_green "
                                  f"-- forgotten criterion?",
                                  file=sys.stderr)
                else:
                    print(f"      WARN cannot validate expect_green: test runner produced no summary",
                          file=sys.stderr)
            except Exception as exc:
                print(f"      WARN cannot validate expect_green: {exc}",
                      file=sys.stderr)

        # O66: warn when a capitalized identifier in spec/context is not
        # declared inside any region of the same file. Regions are not only the
        # editing window, they are the model's entire view of the file. A type
        # named in spec whose declaration falls outside every region is
        # invisible to the implementer, and an invisible type gets a plausible
        # guess -- four rounds of invented member names, measured.
        #
        # CF-1: the heuristic treated any capitalised token as a symbol, so a
        # well-written ticket that uses ALL-CAPS for emphasis (THE, DO, SCOPE)
        # produced ~20 warnings with zero real identifiers. Only warn for
        # tokens that look like code: contains _, has a camelCase/PascalCase
        # interior transition, is followed by ( or . in the spec text, or
        # appears inside backticks. A single-word ALL-CAPS token is prose.
        import re as _re
        for spec_entry in t["regions"]:
            spec_text = t.get("spec", "") + " " + t.get("context", "")
            file_path = spec_entry.get("file", "")
            # Find capitalized identifiers in the spec.
            caps = set(_re.findall(r"\b[A-Z][a-zA-Z0-9_]+\b", spec_text))
            # Skip common English words that look like identifiers.
            caps -= {"The", "This", "That", "These", "Those", "When", "Where",
                     "Which", "What", "Each", "Every", "All", "None", "True",
                     "False", "None", "AND", "OR", "NOT", "ID", "OK", "FAIL"}
            # CF-1: filter to code-like tokens only.
            # A single-word ALL-CAPS token (CSS, DETAIL, GOES) is prose in
            # every house style. A token looks like code if:
            #   - it contains an underscore (snake_case)
            #   - it has an interior lowercase->uppercase transition (camelCase)
            #   - it is followed by ( or . in the spec text (a call or access)
            #   - it appears inside backticks in the spec text
            # Check for backtick-wrapped and call/access patterns.
            # CF-1 residual: the call_tokens regex caught sentence-ending
            # periods ('SCOPE.' in 'm. SCOPE. Thi') because \s*[.(] matches
            # a period followed by anything. Require no whitespace between
            # the . and the identifier: Foo.Bar, not 'SCOPE. The'.
            # For calls, Foo(x) or Foo("x") -- allow whitespace before the
            # opening paren but require content inside it.
            backtick_tokens = set(_re.findall(r"`([A-Z][a-zA-Z0-9_]+)`", spec_text))
            call_tokens = set(_re.findall(
                r'\b([A-Z][a-zA-Z0-9_]+)\.[a-zA-Z_]', spec_text))      # Foo.Bar (no space)
            call_tokens |= set(_re.findall(
                r'\b([A-Z][a-zA-Z0-9_]+)\s*\(\s*[\w"\']', spec_text))  # Foo(x) or Foo("x")
            caps = {c for c in caps if _looks_like_code(c, spec_text)
                    or c in backtick_tokens or c in call_tokens}
            if not caps or not file_path:
                continue
            # Try to resolve the region and check if the identifiers appear in it.
            try:
                r = regions.extract(Path("."), [spec_entry], profile)[0]
                region_text = r.text
            except regions.RegionError:
                continue  # already reported above
            # Read the full file to check if the identifier is declared outside the region.
            full_path = Path(".") / file_path
            if not full_path.exists():
                continue
            try:
                file_text = full_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for cap in sorted(caps):
                # Is the identifier mentioned in the region text?
                if cap in region_text:
                    continue
                # Is it mentioned anywhere in the file?
                if cap not in file_text:
                    print(f"      WARN '{cap}' named in spec but not found in {file_path} "
                          f"-- model will guess; add its declaration to a read-only region",
                          file=sys.stderr)

    return 1 if bad else 0


def _review(args, profile) -> int:
    """Adversarial review of already-written code. Reports; never edits."""
    from . import gates, review_mode

    if not args.review_base:
        print("--mode review needs --review-base (e.g. --review-base HEAD~1)")
        return 2

    intent = args.review_intent
    if args.review_intent_file:
        intent = Path(args.review_intent_file).read_text(encoding="utf-8")

    gate_summary = ""
    if args.review_verify:
        print("  verifying build + tests before review ...")
        parts = []
        if profile.build_cmd:
            b = gates.check_compile(profile.build_cmd, Path("."))
            parts.append(f"build: {'PASS' if b.ok else 'FAIL'} ({b.summary})")
        else:
            parts.append("build: (no build_cmd in profile)")
        if profile.test_cmd:
            t = gates.run_tests(profile.test_cmd, Path("."))
            # `ran`, not `reached_results`: TestOutcome has never had a
            # reached_results attribute, so --review-verify raised
            # AttributeError before it could report anything.
            parts.append(
                f"tests: {t.passed} passed, {len(t.failures)} failed"
                + ("" if t.ran else " (RUNNER PRODUCED NO SUMMARY - treat as unknown)")
            )
        else:
            parts.append("tests: (no test_cmd in profile)")
        gate_summary = "\n".join(parts)
        print("  " + gate_summary.replace("\n", "\n  "))

    try:
        review_mode.run_review(
            Path("."),
            base=args.review_base,
            head=args.review_head,
            paths=args.review_paths,
            profile=profile,
            reviewers=[m.strip() for m in args.reviewers.split(",") if m.strip()],
            arbiter_model=args.arbiter,
            intent=intent,
            title=args.review_title,
            gate_summary=gate_summary,
            orchestrator_note=args.orchestrator_note,
            panel_deadline=args.panel_deadline,
        )
    except review_mode.ReviewError as exc:
        print(f"  REVIEW ERROR: {exc}")
        return 2
    return 0


def _plan(args, profile, implementer, reviewers, arbiter) -> int:
    """Plan mode: defect -> ticket JSON (reviewed by panel+arbiter)."""
    from .plan_mode import run_plan

    # They select different system prompts and produce different output shapes
    # (one ticket vs an ordered list), so silently preferring one would make the
    # other a no-op the caller cannot see.
    if args.defect and args.feature:
        print("--mode plan takes --defect OR --feature, not both")
        return 2
    if not args.defect and not args.feature:
        print("--mode plan needs --defect (a defect to fix) or --feature (something new to build)")
        return 2

    result = run_plan(
        Path("."),
        args.feature or args.defect,
        profile,
        implementer,
        reviewers,
        arbiter_model=arbiter,
        max_rounds=args.max_rounds,
        fast_plan=args.fast_plan,
        feature=bool(args.feature),
    )
    print(f"\n==== PLAN RESULT ====")
    print(f"verdict: {result.get('verdict', '?')}")
    if result.get("error"):
        print(f"error: {result['error']}")
    plan = result.get("plan")
    if plan:
        n = len(plan) if isinstance(plan, list) else 1
        print(f"plan: logs/agent_loop/PLAN/plan.json ({n} part(s))")
        if isinstance(plan, list):
            for t in plan:
                print(f"  {t.get('id', '?'):<6} {t.get('title', '')}")
    return 0 if plan else 1


def _test(args, profile, implementer) -> int:
    """Test mode: defect + ticket -> failing acceptance tests."""
    from .test_mode import run_test

    if not args.defect:
        print("--mode test needs --defect (the defect description)")
        return 2
    if not args.tickets:
        print("--mode test needs --tickets (path to the ticket JSON from plan mode)")
        return 2

    try:
        tickets = load_tickets(Path(args.tickets))
    except TicketFileError as exc:
        print(f"  {exc}")
        return 2
    ticket = tickets[0]
    if args.ticket:
        for t in tickets:
            if t["id"] == args.ticket[0]:
                ticket = t
                break

    result = run_test(
        Path("."),
        args.defect,
        ticket,
        profile,
        implementer,
        test_file=args.test_file,
        path_isolated=args.path_isolated,
    )
    print(f"\n==== TEST RESULT ====")
    if result.get("test_code"):
        # `result`, not `args`: the path is derived from the profile when the flag
        # is not given, so echoing the flag printed None.
        print(f"tests written to: {result.get('test_file')}")
    if result.get("error"):
        print(f"error: {result['error']}")
    # An error is a failure even when test code was written: the tests exist
    # but were never confirmed red at baseline, so they are not yet evidence.
    return 0 if (result.get("test_code") and not result.get("error")) else 1


def _developer(args, profile, implementer, reviewers, arbiter) -> int:
    """Developer mode: defect -> autonomous localization + edit -> diff."""
    from .developer.driver import run_developer

    if not args.defect:
        print("--mode developer needs --defect (the defect description)")
        return 2

    result = run_developer(
        Path("."),
        args.defect,
        profile,
        implementer,
        reviewers,
        arbiter_model=arbiter,
        max_turns=args.max_rounds * 5,  # developer mode needs more turns
        apply=args.apply,
        keep_worktree=args.keep_worktree,
    )
    print(f"\n==== DEVELOPER RESULT ====")
    print(f"verdict: {result.get('verdict', '?')}")
    if result.get("patch"):
        print(f"patch: {result['patch']}")
    if result.get("summary"):
        print(f"summary: {result['summary']}")
    return 0 if result.get("verdict") in DEVELOPER_PROMOTABLE else 1


def _brainstorm(args, profile, implementer) -> int:
    """Brainstorm mode: defect -> candidate approaches + trade-offs."""
    from .brainstorm_mode import run_brainstorm

    if not args.defect:
        print("--mode brainstorm needs --defect (the defect description)")
        return 2

    result = run_brainstorm(Path("."), args.defect, profile, implementer)
    print(f"\n==== BRAINSTORM RESULT ====")
    if result.get("approaches"):
        print(f"approaches: logs/agent_loop/BRAINSTORM/approaches.md")
    if result.get("recommendation"):
        print(f"recommendation: {result['recommendation']}")
    return 0 if result.get("approaches") else 1


def _docs(args, profile, implementer) -> int:
    """Docs mode: codebase (or diff) -> documentation.

    Every argument here is passed by KEYWORD. This function used to call
    run_docs(Path("."), args.review_base, profile, implementer, ...)
    positionally against the signature
    run_docs(repo, profile, implementer, docs_type, diff_ref, intent, output_path),
    so `profile` received a ref string, `implementer` received the Profile, and
    `docs_type` received the model name -- every invocation of every sub-mode
    died on "unknown docs type: '<model>'". Docs mode had never run.
    """
    from .docs_mode import run_docs

    # Only the changelog sub-mode reads a diff. Demanding --review-base for the
    # other three made them unreachable even once the call was correct.
    if args.docs_type == "changelog" and not args.review_base:
        print("--mode docs --docs-type changelog needs --review-base (e.g. HEAD~1)")
        return 2
    if args.docs_type in ("design", "prd") and not args.defect:
        print(f"--mode docs --docs-type {args.docs_type} needs --defect")
        return 2

    # NOT args.test_file: that argument defaults to
    # tests/acceptance/test_generated.py, so `args.test_file or <default>` was
    # never falsy and docs mode would have written markdown over a test file.
    output_path = args.docs_out or f"docs/generated/{args.docs_type}.md"

    result = run_docs(
        Path("."),
        profile=profile,
        implementer=implementer,
        docs_type=args.docs_type,
        diff_ref=args.review_base or "HEAD~1",
        intent=args.defect,
        output_path=output_path,
    )
    print(f"\n==== DOCS RESULT ====")
    if result.get("docs"):
        print(f"docs written to: {result.get('output_path', output_path)}")
    elif result.get("error"):
        print(f"error: {result['error']}")
    return 0 if result.get("docs") else 1


def _run_plan(args, profile, implementer, reviewers, arbiter) -> int:
    """Run-plan mode: execute a decomposed plan."""
    from . import run_plan_mode

    if not args.plan:
        print("--mode run-plan needs --plan (path to the plan JSON)")
        return 2

    plan_path = Path(args.plan)
    if not plan_path.is_file():
        print(f"plan file not found: {plan_path}")
        return 2

    result = run_plan_mode.run_plan(
        repo=Path("."),
        plan_path=plan_path,
        profile=profile,
        implementer=implementer,
        reviewers=reviewers,
        arbiter_model=arbiter,
        apply=args.apply,
        max_rounds=args.max_rounds,
        from_part=args.from_part,
        keep_branch=args.keep_branch,
        panel_deadline=args.panel_deadline,
    )

    # Exit code: 0 if all parts applied, 1 if partial or failed.
    return 0 if result.status == "complete" else 1


def main(argv=None) -> int:
    # Line-buffer stdout. A run redirected to a log or a pipe -- which is how any
    # run long enough to background is invoked -- otherwise BLOCK-buffers, so the
    # per-round progress lines appear only when the process exits. A 26-minute
    # run that had completed four rounds looked hung, with the artifact
    # timestamps as the only evidence it was alive.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):  # pragma: no cover - exotic stdout
        pass

    ap = argparse.ArgumentParser(prog="python -m agent_loop")
    ap.add_argument("--version", action="store_true",
                    help="print package version and resolved package path, then exit")
    ap.add_argument("--tickets", default="tickets.json")
    ap.add_argument("--ticket", action="append", help="ticket id (repeatable); default all")
    ap.add_argument("--profile", default="", help="profile name (must be registered)")
    ap.add_argument(
        "--profile-module", default="",
        help="Python module to import that registers profiles at import time",
    )
    ap.add_argument("--implementer", default="")
    ap.add_argument(
        "--reviewers",
        default="",
        help="comma-separated panel; verdict is the worst returned, APPROVE must be unanimous. "
        "Prefix a model with anthropic:/openai:/ollama: to pick a backend.",
    )
    ap.add_argument(
        "--arbiter",
        default="",
        help="model that rules on reviewer findings. Must be from a different family than the reviewers.",
    )
    ap.add_argument(
        "--config", default="",
        help="path to a JSON config overriding agent_loop/config.py defaults "
             "(default: $AGENT_LOOP_CONFIG, then ./agent_loop.config.json)",
    )
    # 0 = "use the configured value". A literal here would be a second
    # definition of the same limit, silently shadowing config.py.
    ap.add_argument("--max-rounds", type=int, default=0)
    ap.add_argument("--apply", action="store_true", help="promote an approved patch into the live tree")
    ap.add_argument(
        "--allow-unapproved",
        action="store_true",
        help="arbiter override: export/promote even without unanimous APPROVE",
    )
    ap.add_argument("--resume-raw", default="", help="reuse an rN_impl_raw.txt as round 1")
    ap.add_argument("--orchestrator-note", default="", help="authoritative directive; outranks reviewers")
    ap.add_argument("--panel-deadline", type=int, default=0, help="wall-clock seconds for the whole panel (0 = configured value)")
    ap.add_argument("--keep-worktree", action="store_true", help="leave the worktree for post-mortem")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--prune", action="store_true", help="remove worktrees left by crashed runs")

    # ---- modes -----------------------------------------------------------
    ap.add_argument("--mode", choices=("patch", "review", "plan", "test", "developer", "brainstorm", "docs", "report", "replay", "run-plan"), default="patch")
    ap.add_argument("--defect", default="", help="plan/test mode: the defect description")
    ap.add_argument(
        "--feature", default="",
        help="plan mode: describe a NEW FEATURE instead of a defect. The plan is "
             "decomposed into ordered parts, each with its own acceptance tests, "
             "and regions may carry op=create for files that do not exist yet.",
    )
    ap.add_argument("--fast-plan", action="store_true", help="plan mode: skip panel+arbiter, use single reviewer")
    ap.add_argument(
        "--plan", default="",
        help="run-plan mode: path to the plan JSON (from --mode plan --feature)",
    )
    ap.add_argument(
        "--from", dest="from_part", default="",
        help="run-plan mode: resume from a specific part (skip earlier parts)",
    )
    ap.add_argument(
        "--keep-branch", action="store_true",
        help="run-plan mode: do not delete the scratch branch on failure",
    )
    # Empty, NOT a Python path. This default was passed unconditionally, so it
    # overrode any profile-derived choice and told a C# project's test writer to
    # emit `.py`. Test mode derives the path from `profile.test_sources` when this
    # is not given.
    ap.add_argument(
        "--test-file", default="",
        help="test mode: where to write tests (default: derived from the profile's test_sources)",
    )
    ap.add_argument(
        "--path-isolated", action="store_true",
        help="test mode: generate tests from the SPEC only, not from the implementation. "
             "Satisfies the TDD independence property (C-section 1): a test generated "
             "from the implementation can be tautological.",
    )
    ap.add_argument(
        "--docs-type", choices=("changelog", "handover", "design", "prd"),
        default="changelog",
        help="docs mode: which document to generate (changelog reads a diff; "
             "design/prd read --defect; handover reads the ledger)",
    )
    ap.add_argument(
        "--docs-out", default="",
        help="docs mode: where to write the document "
             "(default: docs/generated/<docs-type>.md)",
    )
    ap.add_argument("--review-base", default="", help="review mode: base ref (e.g. main, HEAD~3)")
    ap.add_argument("--review-head", default="HEAD", help="review mode: head ref")
    ap.add_argument("--report-last", type=int, default=0, help="report mode: show only the last N tickets (0 = all)")
    ap.add_argument(
        "--replay-dir", nargs="*", default=[],
        help="replay mode: recorded ticket director(ies) to replay "
             "(default: every ticket under logs/agent_loop/). The module "
             "docstring documented this flag long before it existed.",
    )
    ap.add_argument(
        "--review-paths", nargs="*", default=[],
        help="review mode: limit the diff to these paths",
    )
    ap.add_argument("--review-intent", default="", help="review mode: what the change claims to do")
    ap.add_argument(
        "--review-intent-file", default="",
        help="review mode: read the intent from a file",
    )
    ap.add_argument("--review-title", default="", help="review mode: label for the artifacts")
    ap.add_argument(
        "--review-verify", action="store_true",
        help="review mode: run the profile's build+test first so the panel is told the true gate state",
    )
    args = ap.parse_args(argv)

    # CF-4: --version prints the package version and resolved package path.
    # Editable installs are exactly when you doubt which copy is running,
    # so the path is the useful part.
    if args.version:
        import agent_loop as _pkg
        try:
            from importlib.metadata import version as _pkg_version
            ver = _pkg_version("agent-loop")
        except Exception:
            ver = getattr(_pkg, "__version__", "unknown")
        pkg_path = os.path.dirname(getattr(_pkg, "__file__", "") or "")
        print(f"agent-loop {ver}")
        print(f"  resolved: {pkg_path}")
        return 0

    # Install the effective config before anything reads a tunable, then
    # rebuild the model registry from it so role->model bindings and budgets
    # come from the same place.
    try:
        config.set_active(config.load(args.config))
    except (FileNotFoundError, ValueError) as exc:
        print(f"config error: {exc}")
        return 2
    models.reload_default_registry()

    # Import the profile module if specified (registers profiles at import time)
    if args.profile_module:
        importlib.import_module(args.profile_module)

    if args.prune:
        stale = workspace.list_stale(Path("."))
        for p in stale:
            print(f"  removing {p}")
            workspace.prune(Path("."), p)
        workspace.prune(Path("."))
        print(f"pruned {len(stale)} worktree(s)")
        return 0

    # `report` reads the ledger. It has no profile to honour and no panel to be
    # one-membered, so demanding --profile and printing the panel warning were
    # both noise on the one command you run to find out how the loop is doing.
    if args.mode == "report":
        from .report import run_report
        return run_report(Path("."), last_n=args.report_last)

    if not args.profile:
        print("--profile is required (e.g. --profile nt8-riskguard)")
        return 2
    profile = profiles.get(args.profile)

    # Resolve model defaults from the registry
    registry = DEFAULT_REGISTRY
    implementer = args.implementer or registry.get("implementer").name
    # A mode may name its own model. Every non-patch mode used to run on the
    # implementer -- a CODE-specialised model -- including `docs`, which writes
    # prose, and `brainstorm`, which enumerates approaches. Neither is a coding
    # task, and there was no way to say so: ModeSettings had max_tokens and
    # think but no model. An explicit --implementer still wins, and a mode that
    # names nothing still inherits, so this changes no default.
    if not args.implementer:
        try:
            mode_model = config.get().mode(args.mode).model
        except KeyError:
            mode_model = ""  # patch/review/report/replay have no ModeSettings
        if mode_model:
            implementer = mode_model
    reviewers_str = args.reviewers or ",".join(c.name for c in registry.get_all("reviewer"))
    arbiter = args.arbiter or registry.get("arbiter").name

    # Validate the model mix
    reviewers = [m.strip() for m in reviewers_str.split(",") if m.strip()]
    try:
        registry.validate(implementer, reviewers, arbiter)
    except ValueError as exc:
        print(f"  MODEL VALIDATION ERROR: {exc}")
        return 2
    # The panel's whole claim is that different families miss different things, so
    # both halves of the policy are checked: at least two members, and at least two
    # viewpoints. The default now satisfies both (config.check_panel_policy fails
    # the build otherwise), so these fire only when a RUN overrides it.
    if len(reviewers) < 2:
        print(
            f"  WARNING: panel has one member ({reviewers[0] if reviewers else 'none'}). "
            "Pass --reviewers with two models from different families for an "
            "adversarial panel."
        )
    else:
        families = {models.model_family(m) for m in reviewers}
        if len(families) < 2:
            print(
                f"  WARNING: all {len(reviewers)} reviewers are from the same family "
                f"({families.pop()}), so this is one viewpoint twice. Two models "
                "from one family miss the same things."
            )

    if args.mode == "review":
        return _review(args, profile)

    if args.mode == "plan":
        return _plan(args, profile, implementer, reviewers, arbiter)

    if args.mode == "test":
        return _test(args, profile, implementer)

    if args.mode == "developer":
        return _developer(args, profile, implementer, reviewers, arbiter)

    if args.mode == "brainstorm":
        return _brainstorm(args, profile, implementer)

    if args.mode == "docs":
        return _docs(args, profile, implementer)

    if args.mode == "run-plan":
        return _run_plan(args, profile, implementer, reviewers, arbiter)

    if args.mode == "replay":
        from .replay import run_replay_corpus
        if args.replay_dir:
            corpus_dirs = [Path(d) for d in args.replay_dir]
            missing = [str(d) for d in corpus_dirs if not d.is_dir()]
            if missing:
                print(f"no such replay dir(s): {', '.join(missing)}")
                return 2
        else:
            # Find all ticket dirs under logs/agent_loop/
            log_root = Path(".") / "logs" / "agent_loop"
            if not log_root.is_dir():
                print("No recorded tickets found in logs/agent_loop/")
                return 2
            corpus_dirs = sorted(
                d for d in log_root.iterdir()
                if d.is_dir() and (d / "result.json").exists()
            )
        if not corpus_dirs:
            print("No recorded tickets found in logs/agent_loop/")
            return 2
        print(f"Replaying {len(corpus_dirs)} recorded ticket(s)...\n")
        result = run_replay_corpus(
            Path("."),
            corpus_dirs,
            profile,
            reviewers,
            arbiter,
            max_rounds=args.max_rounds,
        )
        # Exit codes are three-valued on purpose. This used to be
        # `0 if flipped == 0 else 1`, which reports SUCCESS for a corpus where
        # every ticket errored -- and since a corpus recorded before prompt
        # recording now errors on every ticket by design, that would be a green
        # CI gate that measured nothing. Distinguish:
        #   2 = could not measure (errors, or an empty corpus)
        #   1 = measured, and a verdict flipped
        #   0 = measured, nothing flipped
        if result["errors"]:
            print(
                f"\n{result['errors']} ticket(s) could not be replayed. "
                "A replay that cannot hold the prompt constant is not a measurement."
            )
            return 2
        if result["total"] == 0:
            return 2
        return 1 if result["flipped"] else 0

    try:
        tickets = load_tickets(Path(args.tickets))
    except TicketFileError as exc:
        print(f"  {exc}")
        return 2

    if args.list:
        return _list(tickets, profile)

    wanted = args.ticket or [t["id"] for t in tickets]
    unknown = [w for w in wanted if not any(t["id"] == w for t in tickets)]
    if unknown:
        print(f"  unknown ticket id(s): {', '.join(unknown)}")
        return 2
    results = []
    for t in tickets:
        if t["id"] not in wanted:
            continue
        print(f"\n=== {t['id']}: {t['title']}")
        try:
            results.append(
                run_ticket(
                    Path("."),
                    t,
                    profile,
                    implementer,
                    reviewers,
                    max_rounds=args.max_rounds,
                    apply=args.apply,
                    allow_unapproved=args.allow_unapproved,
                    resume_raw=args.resume_raw,
                    orchestrator_note=args.orchestrator_note,
                    panel_deadline=args.panel_deadline,
                    keep_worktree=args.keep_worktree,
                    arbiter_model=arbiter,
                )
            )
        except Exception as exc:  # noqa: BLE001 - a driver must report, not crash
            print(f"  ERROR {t['id']}: {type(exc).__name__}: {exc}")
            results.append({"ticket": t["id"], "final_verdict": f"ERROR: {exc}", "applied": False})

    print("\n==== SUMMARY ====")
    total = 0.0
    for r in results:
        total += r.get("cost_usd", 0.0) or 0.0
        print(f"{r['ticket']:<5} {r.get('final_verdict','?'):<22} applied={r.get('applied')}")
    if total:
        print(f"total cost ${total:.4f}")
    # Every requested ticket must have produced a promotable candidate. `any`
    # meant a run of four tickets exited 0 when one passed and three failed,
    # which reads as success to every caller and to CI.
    failed = [r for r in results if r.get("final_verdict") not in PROMOTABLE]
    if failed:
        print(f"{len(failed)} of {len(results)} ticket(s) did not produce a promotable candidate")
    return 1 if failed or not results else 0


if __name__ == "__main__":
    sys.exit(main())
