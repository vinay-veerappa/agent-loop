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
import sys
from pathlib import Path

from . import profiles, regions, workspace
from .loop import run_ticket
from .models import DEFAULT_REGISTRY


def _list(tickets, profile) -> int:
    """Confirm every ticket's regions still resolve against the current tree."""
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

    if not args.defect:
        print("--mode plan needs --defect (the defect description)")
        return 2

    result = run_plan(
        Path("."),
        args.defect,
        profile,
        implementer,
        reviewers,
        arbiter_model=arbiter,
        max_rounds=args.max_rounds,
        fast_plan=args.fast_plan,
    )
    print(f"\n==== PLAN RESULT ====")
    print(f"verdict: {result.get('verdict', '?')}")
    if result.get("plan"):
        print(f"plan: logs/agent_loop/PLAN/plan.json")
    return 0 if result.get("plan") else 1


def _test(args, profile, implementer) -> int:
    """Test mode: defect + ticket -> failing acceptance tests."""
    from .test_mode import run_test

    if not args.defect:
        print("--mode test needs --defect (the defect description)")
        return 2
    if not args.tickets:
        print("--mode test needs --tickets (path to the ticket JSON from plan mode)")
        return 2

    spec = json.loads(Path(args.tickets).read_text(encoding="utf-8"))
    tickets = spec["tickets"]
    ticket = tickets[0] if tickets else {}
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
    )
    print(f"\n==== TEST RESULT ====")
    if result.get("test_code"):
        print(f"tests written to: {args.test_file}")
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
    )
    print(f"\n==== DEVELOPER RESULT ====")
    print(f"verdict: {result.get('verdict', '?')}")
    if result.get("patch"):
        print(f"patch: {result['patch']}")
    if result.get("summary"):
        print(f"summary: {result['summary']}")
    return 0 if result.get("verdict") == "DONE" else 1


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
    """Docs mode: diff -> documentation updates."""
    from .docs_mode import run_docs

    if not args.review_base:
        print("--mode docs needs --review-base (e.g. HEAD~1)")
        return 2

    result = run_docs(
        Path("."), args.review_base, profile, implementer,
        output_path=args.test_file or "docs/UPDATES.md",
    )
    print(f"\n==== DOCS RESULT ====")
    if result.get("docs"):
        print(f"docs written to: {result.get('output_path', 'docs/UPDATES.md')}")
    elif result.get("error"):
        print(f"error: {result['error']}")
    return 0 if result.get("docs") else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agent_loop")
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
    ap.add_argument("--max-rounds", type=int, default=4)
    ap.add_argument("--apply", action="store_true", help="promote an approved patch into the live tree")
    ap.add_argument(
        "--allow-unapproved",
        action="store_true",
        help="arbiter override: export/promote even without unanimous APPROVE",
    )
    ap.add_argument("--resume-raw", default="", help="reuse an rN_impl_raw.txt as round 1")
    ap.add_argument("--orchestrator-note", default="", help="authoritative directive; outranks reviewers")
    ap.add_argument("--panel-deadline", type=int, default=1800, help="wall-clock seconds for the whole panel")
    ap.add_argument("--keep-worktree", action="store_true", help="leave the worktree for post-mortem")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--prune", action="store_true", help="remove worktrees left by crashed runs")

    # ---- modes -----------------------------------------------------------
    ap.add_argument("--mode", choices=("patch", "review", "plan", "test", "developer", "brainstorm", "docs"), default="patch")
    ap.add_argument("--defect", default="", help="plan/test mode: the defect description")
    ap.add_argument("--fast-plan", action="store_true", help="plan mode: skip panel+arbiter, use single reviewer")
    ap.add_argument("--test-file", default="tests/acceptance/test_generated.py", help="test mode: where to write tests")
    ap.add_argument("--review-base", default="", help="review mode: base ref (e.g. main, HEAD~3)")
    ap.add_argument("--review-head", default="HEAD", help="review mode: head ref")
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

    if not args.profile:
        print("--profile is required (e.g. --profile nt8-riskguard)")
        return 2
    profile = profiles.get(args.profile)

    # Resolve model defaults from the registry
    registry = DEFAULT_REGISTRY
    implementer = args.implementer or registry.get("implementer").name
    reviewers_str = args.reviewers or ",".join(c.name for c in registry.get_all("reviewer"))
    arbiter = args.arbiter or registry.get("arbiter").name

    # Validate the model mix
    reviewers = [m.strip() for m in reviewers_str.split(",") if m.strip()]
    try:
        registry.validate(implementer, reviewers, arbiter)
    except ValueError as exc:
        print(f"  MODEL VALIDATION ERROR: {exc}")
        return 2
    if len(reviewers) < 2:
        # The panel's whole claim is that different families miss different
        # things. One reviewer is not a panel, and nothing else says so.
        print(
            f"  WARNING: panel has one member ({reviewers[0] if reviewers else 'none'}). "
            "Pass --reviewers with two models from different families for an "
            "adversarial panel."
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

    spec = json.loads(Path(args.tickets).read_text(encoding="utf-8"))
    tickets = spec["tickets"]

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
    ok = ("APPROVE", "APPROVE_PARTIAL", "ARBITER_SHIP")
    failed = [r for r in results if r.get("final_verdict") not in ok]
    if failed:
        print(f"{len(failed)} of {len(results)} ticket(s) did not produce a promotable candidate")
    return 1 if failed or not results else 0


if __name__ == "__main__":
    sys.exit(main())
