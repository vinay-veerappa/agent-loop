"""
loop.py
=======
Round driver: implement -> gate ladder -> review panel -> arbitrate -> apply.

The two behaviours that distinguish this from the predecessor:

1. A reviewer that did not answer has NOT voted. The old panel wrapped every
   reviewer call in a bare `except` that fabricated a REVISE verdict, and
   `parse_review("")` also returns REVISE -- so an unreachable or silent model
   was indistinguishable from a dissenting one. Ticket T2 burned four rounds
   and ~2.5 hours against a gate that was closed from round 1 because one
   reviewer returned empty every time and, in the final round, both 502'd.
   Here an unreachable reviewer aborts the round instead of silently vetoing.

2. The panel carries a wall-clock deadline. T2's round 4 hung for 2h03m under
   a nominal 900s per-request timeout, because the timeout bounds one request
   and nothing bounded the set.
"""
from __future__ import annotations

import concurrent.futures
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import arbiter, gates, profiles, regions, workspace
from .providers import Completion, ProviderError, chat

# `>{2,}` rather than `>>>`: kimi-k2.7-code closed a block with `>>` on T3 and
# then reproduced the same typo on every retry, so three implementer rounds were
# spent and the ticket exhausted over one missing angle bracket -- while the
# static gate reported the block as "missing from model output", which is the
# one thing it was not. Marker punctuation is not what the gate is here to check.
BLOCK_RE = re.compile(
    r"<<<BLOCK\s+id=\"(?P<id>[^\"]+)\"\s*>{2,}\r?\n(?P<body>.*?)<<<END\s+id=\"(?P=id)\"\s*>{2,}",
    re.DOTALL,
)

APPROVE, REVISE, REJECT = "APPROVE", "REVISE", "REJECT"
UNREACHABLE, UNPARSEABLE = "UNREACHABLE", "UNPARSEABLE"
_RANK = {APPROVE: 0, REVISE: 1, REJECT: 2}


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def parse_blocks(text: str) -> Tuple[Dict[str, str], str]:
    blocks = {m.group("id"): m.group("body").rstrip("\n") for m in BLOCK_RE.finditer(text)}
    m = re.search(r"<<<NOTES\s*>{2,}\r?\n(.*?)<<<END NOTES\s*>{2,}", text, re.DOTALL)
    return blocks, (m.group(1).strip() if m else "")


@dataclass
class Finding:
    """One reviewer finding, addressable so the arbiter can rule on it."""

    model: str
    severity: str  # BLOCKER / MAJOR / MINOR
    text: str

    @property
    def signature(self) -> str:
        """Stable-ish identity for cross-round comparison. Used to detect the
        loop thrashing: if consecutive rounds share no signatures and the count
        does not fall, the implementer is generating new surface as fast as the
        reviewers can attack it, and more rounds will not converge."""
        words = re.sub(r"[^a-zA-Z ]", " ", self.text).split()
        return " ".join(w.lower() for w in words[:8])

    @property
    def blocking(self) -> bool:
        return self.severity in ("BLOCKER", "MAJOR")


_FINDING_RE = re.compile(r"^-\s*\[(BLOCKER|MAJOR|MINOR)\]\s*(.+?)$", re.MULTILINE)


@dataclass
class Vote:
    model: str
    status: str  # APPROVE / REVISE / REJECT / UNREACHABLE / UNPARSEABLE
    findings: str = ""
    required: str = ""
    blockers: int = 0
    secs: float = 0.0
    error: str = ""
    usage: str = ""
    finding_list: List[Finding] = field(default_factory=list)

    @property
    def counted(self) -> bool:
        """Only a parsed verdict from a reachable model is a vote."""
        return self.status in _RANK


def parse_review(text: str, model: str) -> Vote:
    """Parse a reviewer response. An empty or structurally missing verdict is
    UNPARSEABLE, never a silent REVISE -- that conflation is what made the
    panel unable to approve anything."""
    if not text or not text.strip():
        return Vote(model, UNPARSEABLE, error="empty response body")

    def section(name: str) -> str:
        m = re.search(rf"<<<{name}>>>\r?\n(.*?)<<<END {name}>>>", text, re.DOTALL)
        return m.group(1).strip() if m else ""

    raw = section("VERDICT").upper()
    verdict = next((c for c in (REJECT, REVISE, APPROVE) if c in raw), "")
    if not verdict:
        return Vote(model, UNPARSEABLE, error=f"no verdict marker in {len(text)} chars")
    findings = section("FINDINGS")
    items = [
        Finding(model, m.group(1).upper(), m.group(2).strip())
        for m in _FINDING_RE.finditer(findings)
        if m.group(2).strip().upper() not in ("NONE", "- NONE")
    ]
    blockers = sum(1 for f in items if f.blocking)
    return Vote(model, verdict, findings, section("REQUIRED"), blockers, finding_list=items)


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------
def build_implement_prompt(ticket: Dict[str, Any], regs: Sequence[regions.Region]) -> str:
    parts = [
        f"# TICKET {ticket['id']}: {ticket['title']}",
        "",
        "## Defect",
        ticket["defect"].strip(),
        "",
        "## Required change",
        ticket["spec"].strip(),
        "",
    ]
    if ticket.get("context"):
        parts += ["## Additional context you must respect", ticket["context"].strip(), ""]
    parts.append("## Regions to rewrite")
    for r in regs:
        parts += [
            "",
            f'### REGION id="{r.id}"  file={r.file}  lines {r.lines_1based}',
            f"Purpose: {r.note}" if r.note else "",
            "```csharp",
            r.text,
            "```",
        ]
    parts += ["", "Return one block per region id above, in the same order. No other output."]
    return "\n".join(p for p in parts if p)


def extract_test_sources(repo: Path, names: Sequence[str], globs: Sequence[str]) -> str:
    """Pull the named test methods out of the (read-only) test sources.

    Reviewers cannot judge whether the suite is complete without seeing it, and
    the suite is a first-class artifact here, not an assumption. Brace-matched
    from the signature so the whole method comes across intact.
    """
    if not names or not globs:
        return ""
    out: List[str] = []
    for g in globs:
        for path in sorted(repo.glob(g)):
            src = path.read_text(encoding="utf-8", errors="replace")
            for name in names:
                # Must be the DECLARATION, not a call site: require at least one
                # modifier and a return type ahead of the name. Matching bare
                # `Name(` finds the invocation in Main() first and ships the
                # wrong text to the reviewer.
                m = re.search(
                    r"^[ \t]*(?:(?:private|public|internal|protected|static|async|override|virtual)"
                    rf"\s+)+[\w<>\[\],\.]+\s+{re.escape(name)}\s*\(",
                    src,
                    re.MULTILINE,
                )
                if not m:
                    continue
                start = m.start()
                brace = src.find("{", m.end())
                if brace < 0:
                    continue
                depth, i = 0, brace
                while i < len(src):
                    if src[i] == "{":
                        depth += 1
                    elif src[i] == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    i += 1
                out.append(f"// --- {path.name}: {name} ---\n{src[start:i + 1]}")
    return "\n\n".join(out)


def build_review_prompt(
    ticket: Dict[str, Any],
    regs: Sequence[regions.Region],
    blocks: Dict[str, str],
    notes: str,
    profile: profiles.Profile,
    orchestrator_note: str,
    gate_summary: str,
) -> str:
    parts = [
        f"# TICKET {ticket['id']}: {ticket['title']}",
        "",
        "## Defect the patch claims to fix",
        ticket["defect"].strip(),
        "",
        "## Required change",
        ticket["spec"].strip(),
        "",
    ]
    if gate_summary:
        # Reviewers used to review blind to whether the patch compiled or passed
        # tests, and wasted findings asserting it did not.
        parts += ["## Mechanical gates already passed", gate_summary, ""]
    settled = list(profile.settled)
    if orchestrator_note:
        settled.append(orchestrator_note.strip())
    if settled:
        parts += [
            "## SETTLED DECISIONS - AUTHORITATIVE, DO NOT RE-LITIGATE",
            "The arbiter has already decided these. They SUPERSEDE the ticket text wherever they "
            "conflict. Do NOT raise a finding that contradicts one, and do not report "
            "directive-compliant code as a spec violation.",
            "",
        ] + [f"- {s}" for s in settled] + [""]
    tests_src = ticket.get("_acceptance_tests_src", "")
    if tests_src:
        # The reviewer is shown the acceptance tests READ-ONLY. It cannot edit them
        # (gate 0 makes the verifier unreachable) but it must be able to judge
        # whether they are complete and whether they would actually fail if the
        # defect came back -- see reviewer priority 5.
        parts += [
            "## Acceptance tests for this ticket (READ-ONLY - you may not propose editing these)",
            "These were written BEFORE the patch and were failing at baseline; they now pass.",
            "Judge their completeness and accuracy, and name any behaviour they do not cover.",
            "",
            "```csharp",
            tests_src,
            "```",
            "",
        ]
    parts += ["## Implementer notes", notes.strip() or "(none)", ""]
    for r in regs:
        parts += [
            "",
            f'## REGION "{r.id}" ({r.file})',
            "### BEFORE",
            "```csharp",
            r.text,
            "```",
            "### AFTER (proposed)",
            "```csharp",
            blocks.get(r.id, "(MISSING - implementer did not return this region)"),
            "```",
        ]
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Panel
# --------------------------------------------------------------------------
@dataclass
class PanelResult:
    votes: List[Vote]
    verdict: str  # worst counted verdict, or "" when the panel is invalid
    valid: bool  # every reviewer answered
    findings: str = ""
    required: str = ""

    @property
    def unanimous_approve(self) -> bool:
        return self.valid and bool(self.votes) and all(v.status == APPROVE for v in self.votes)

    @property
    def unreachable(self) -> List[Vote]:
        return [v for v in self.votes if not v.counted]


def review_panel(
    reviewers: Sequence[str],
    prompt: str,
    system: str,
    art: Path,
    rnd: int,
    deadline_secs: int = 1800,
    max_tokens: int = 24000,
    think: Optional[bool] = False,
) -> PanelResult:
    """Run reviewers concurrently. Different families miss different things, so
    a panel finds strictly more than any single reviewer. The verdict is the
    WORST returned: any reviewer may block, none may unblock on another's
    behalf.

    If any reviewer is unreachable the panel is INVALID -- the round cannot be
    decided and must be retried rather than counted as a rejection.

    Thinking is OFF by default. The reviewer's output contract is a structured
    verdict plus findings, so chain-of-thought is spent and then discarded --
    and on a reasoning model it crowds out the answer entirely. Measured on a
    T2-sized review with deepseek-v4-pro: thinking on took 159s, burned the
    full 24k-token budget on 90k chars of reasoning and returned NO verdict;
    thinking off took 21s, 2.7k tokens, and returned ten findings.
    """

    def one(model: str) -> Vote:
        t0 = time.time()
        try:
            out: Completion = chat(
                model,
                [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                think=think,
            )
        except ProviderError as exc:
            return Vote(model, UNREACHABLE, secs=round(time.time() - t0, 1), error=str(exc))
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", model)
        (art / f"r{rnd}_review_{safe}.txt").write_text(out.text or "", encoding="utf-8")
        v = parse_review(out.text, model)
        v.secs = out.secs
        v.usage = out.usage_line()
        return v

    votes: List[Vote] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(reviewers))) as pool:
        futures = {pool.submit(one, m): m for m in reviewers}
        try:
            for fut in concurrent.futures.as_completed(futures, timeout=deadline_secs):
                votes.append(fut.result())
        except concurrent.futures.TimeoutError:
            # Bound the SET of calls, not just each one. T2 hung 2h03m here.
            for fut, model in futures.items():
                if not fut.done():
                    fut.cancel()
                    votes.append(Vote(model, UNREACHABLE, error=f"panel deadline {deadline_secs}s exceeded"))

    valid = all(v.counted for v in votes) and len(votes) == len(reviewers)
    counted = [v for v in votes if v.counted]
    verdict = max(counted, key=lambda v: _RANK[v.status]).status if counted else ""

    fnd = "\n\n".join(
        f"### From {v.model} (verdict {v.status})\n{v.findings}"
        for v in counted
        if v.findings.strip() not in ("", "- NONE", "NONE")
    )
    req = "\n\n".join(
        f"### Required by {v.model}\n{v.required}"
        for v in counted
        if v.required.strip() not in ("", "- NONE", "NONE")
    )
    return PanelResult(votes, verdict, valid, fnd or "- NONE", req or "- NONE")


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------
def append_ledger(repo: Path, record: Dict[str, Any]) -> None:
    """Append-only. The predecessor's summary.json was rewritten wholesale per
    invocation and still records T1 as unapplied even though T1 is committed."""
    p = repo / "logs" / "agent_loop" / "ledger.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
def _history_note(convergence: List[Tuple[int, set]]) -> str:
    """Give the arbiter the shape of the loop so far. A flat blocking count
    with no overlap between rounds is the signature of a patch that cannot
    converge, and is exactly when ESCALATE is the right call."""
    if len(convergence) < 2:
        return ""
    lines = [f"round {i+1}: {c} blocking finding(s)" for i, (c, _) in enumerate(convergence)]
    overlap = len(convergence[-1][1] & convergence[-2][1])
    lines.append(f"findings shared between the last two rounds: {overlap}")
    return "\n".join(lines)


@dataclass
class RoundRecord:
    round: int
    stage: str
    ok: bool
    summary: str
    detail: str = ""
    cost_usd: float = 0.0
    secs: float = 0.0


def run_ticket(
    repo: Path,
    ticket: Dict[str, Any],
    profile: profiles.Profile,
    implementer: str,
    reviewers: Sequence[str],
    max_rounds: int = 4,
    apply: bool = False,
    allow_unapproved: bool = False,
    resume_raw: str = "",
    orchestrator_note: str = "",
    panel_deadline: int = 1800,
    keep_worktree: bool = False,
    arbiter_model: str = "",
) -> Dict[str, Any]:
    tid = ticket["id"]
    art = repo / "logs" / "agent_loop" / tid
    art.mkdir(parents=True, exist_ok=True)
    result: Dict[str, Any] = {"ticket": tid, "rounds": [], "applied": False, "cost_usd": 0.0}
    convergence: List[Tuple[int, set]] = []

    region_files = sorted({r["file"] for r in ticket["regions"]})
    g0 = gates.check_protected_paths(region_files, profile.protected or gates.DEFAULT_PROTECTED)
    print(f"  [protected] {g0.summary}")
    if not g0.ok:
        result["final_verdict"] = "TICKET_REJECTED"
        result["detail"] = g0.detail
        print(f"  REFUSED: {g0.detail}")
        append_ledger(repo, {"ticket": tid, "verdict": "TICKET_REJECTED", "detail": g0.detail})
        return result

    with workspace.open_workspace(repo, tid, keep=keep_worktree) as ws:
        print(f"  [worktree] {ws.root.name} @ {ws.base_commit[:8]}")
        if profile.test_cmd:
            workspace.capture_baseline(ws, profile.test_cmd, gates.parse_tests)
            print(f"  [baseline] {ws.baseline_note}; {len(ws.baseline)} expected failure(s)")
            result["baseline"] = sorted(ws.baseline)

            # Test-first: every acceptance test must ALREADY BE FAILING here.
            # If it is not, one of two things is true and both are fatal -- the
            # name is a typo (so the gate would silently never fire and prove
            # nothing), or the test passes without the fix (so it does not
            # actually test the defect). Refuse the ticket rather than run a
            # vacuous gate; a gate that cannot fail is worse than no gate.
            expect_green = list(ticket.get("expect_green", ()))
            not_red = [
                t for t in expect_green
                if not any(t.lower() in f.lower() for f in ws.baseline)
            ]
            if not_red:
                print(f"  REFUSED: expect_green test(s) not failing at baseline: {not_red}")
                result["final_verdict"] = "TICKET_REJECTED"
                result["detail"] = (
                    f"expect_green names {not_red} are not in the baseline failure set. "
                    "Either the name is wrong, or the test passes without the fix and so "
                    "does not test the defect. Write the failing test first."
                )
                return result
            if expect_green:
                print(f"  [test-first] {len(expect_green)} acceptance test(s) red at baseline")
                ticket = dict(ticket, _acceptance_tests_src=extract_test_sources(
                    ws.root, expect_green, profile.test_sources))

        regs = regions.extract(ws.root, ticket["regions"], profile)
        for r in regs:
            print(f"    region {r.id:<24} {r.file} lines {r.lines_1based}")

        impl_prompt = build_implement_prompt(ticket, regs)
        if orchestrator_note:
            impl_prompt += (
                "\n\n## ORCHESTRATOR DIRECTIVE (overrides the reviewer if they conflict)\n"
                + orchestrator_note.strip()
            )
        (art / "00_implement_prompt.md").write_text(impl_prompt, encoding="utf-8")
        history = [
            {"role": "system", "content": profile.implementer_system},
            {"role": "user", "content": impl_prompt},
        ]

        blocks: Dict[str, str] = {}
        final = "MAX_ROUNDS_EXHAUSTED"

        for rnd in range(1, max_rounds + 1):
            # ---- implement
            if rnd == 1 and resume_raw:
                raw = Path(resume_raw).read_text(encoding="utf-8")
                # Persist the resumed candidate under this round's name. Without
                # this the round-1 artifact is whatever a PREVIOUS run left there,
                # while every "resume with"/"promote:" hint below is built from the
                # round number -- so the loop cheerfully tells you to promote a file
                # it never reviewed. On T3 that hint pointed at a stale candidate
                # carrying two upheld findings, one of them a naked-risk defect.
                (art / f"r{rnd}_impl_raw.txt").write_text(raw, encoding="utf-8")
                print(f"  round {rnd}: resumed from {Path(resume_raw).name}")
            else:
                try:
                    # Implementer keeps thinking (it is planning a patch, not
                    # filling a template) but needs headroom: kimi spent 104k
                    # chars reasoning and still emitted 27.9k output tokens.
                    out = chat(implementer, history, max_tokens=48000)
                except ProviderError as exc:
                    print(f"  round {rnd}: implementer unreachable -- {exc}")
                    result["rounds"].append(RoundRecord(rnd, "implement", False, str(exc)).__dict__)
                    final = "IMPLEMENTER_UNREACHABLE"
                    break
                raw = out.text
                result["cost_usd"] += out.cost_usd
                (art / f"r{rnd}_impl_raw.txt").write_text(raw, encoding="utf-8")
                print(f"  round {rnd}: implement {out.usage_line()}")

            blocks, notes = parse_blocks(raw)

            # ---- gate ladder, cheapest first. Each rung only runs if the one
            # below it passed, so a patch that does not compile never costs a
            # test run, and one that fails tests never costs a reviewer.
            gate_results: List[gates.GateResult] = [
                gates.check_static(regs, blocks, lambda ln: regions.strip_code(ln, profile), profile)
            ]
            touched: List[str] = []
            if gate_results[-1].ok:
                touched = regions.apply(regs, blocks)
                if profile.build_cmd:
                    gc = gates.check_compile(profile.build_cmd, ws.root)
                    (art / f"r{rnd}_build.txt").write_text(gc.detail, encoding="utf-8")
                    gate_results.append(gc)
                if gate_results[-1].ok and profile.test_cmd:
                    gt, _ = gates.check_tests(
                        profile.test_cmd, ws.root, ws.baseline,
                        expect_green=ticket.get("expect_green", ()),
                    )
                    (art / f"r{rnd}_tests.txt").write_text(
                        gt.detail or gt.summary, encoding="utf-8"
                    )
                    gate_results.append(gt)
                if gate_results[-1].ok:
                    gate_results.append(
                        gates.check_lock_scope(
                            regs, blocks, lambda ln: regions.strip_code(ln, profile), profile
                        )
                    )

            for x in gate_results:
                print(f"           [{x.name}] {'ok' if x.ok else 'FAIL'} - {x.summary}")

            failed = next((x for x in gate_results if not x.ok), None)
            if failed:
                # The candidate is not viable; take it back out so the next
                # round starts from clean source.
                if touched:
                    ws.revert(touched)
                result["rounds"].append(
                    RoundRecord(rnd, failed.name, False, failed.summary, failed.detail[:4000]).__dict__
                )
                history += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": failed.feedback or failed.summary},
                ]
                continue

            # ---- panel
            gate_summary = "; ".join(f"{x.name}: {x.summary}" for x in gate_results)
            prompt = build_review_prompt(
                ticket, regs, blocks, notes, profile, orchestrator_note, gate_summary
            )
            panel = review_panel(
                reviewers, prompt, profile.reviewer_system, art, rnd, deadline_secs=panel_deadline
            )
            desc = ", ".join(f"{v.model.split(':')[0]}={v.status}({v.blockers})" for v in panel.votes)
            print(f"           [panel] {panel.verdict or 'INVALID'}  [{desc}]")

            result["rounds"].append(
                RoundRecord(
                    rnd,
                    "review",
                    panel.unanimous_approve,
                    f"{panel.verdict or 'INVALID'} [{desc}]",
                    panel.findings[:8000],
                ).__dict__
            )

            if not panel.valid:
                # A reviewer that could not be reached has not voted. This is
                # NOT a rejection: stop cleanly, keep the candidate on disk, and
                # let the arbiter resume from it once the provider is healthy.
                # The predecessor silently scored this as a REVISE and burned
                # the round -- see the module docstring.
                who = ", ".join(f"{v.model} ({v.error})" for v in panel.unreachable)
                print(f"           panel INVALID - NOT a rejection. Unreachable: {who}")
                print(f"           resume with --resume-raw {art / f'r{rnd}_impl_raw.txt'}")
                final = "PANEL_UNREACHABLE"
                break

            if panel.unanimous_approve:
                # Candidate is already applied in the worktree and cleared every
                # gate; leave it in place for export and promotion.
                final = "APPROVE"
                break

            # ---- arbitration: which of these findings actually block?
            all_findings = [f for v in panel.votes if v.counted for f in v.finding_list]
            blocking = [f for f in all_findings if f.blocking]
            convergence.append((len(blocking), {f.signature for f in blocking}))

            adj = None
            if arbiter_model and all_findings:
                adj = arbiter.adjudicate(
                    arbiter_model,
                    ticket,
                    all_findings,
                    gate_summary,
                    ws.diff(),
                    settled=profile.settled,
                    round_history=_history_note(convergence),
                )
                (art / f"r{rnd}_arbiter.txt").write_text(
                    adj.raw or adj.error, encoding="utf-8"
                )
                if adj.ok:
                    print(f"           [arbiter] {adj.summary()}  {adj.usage}")
                    if adj.settled:
                        print(f"           [arbiter] nominates {len(adj.settled)} finding(s) as settled")
                else:
                    print(f"           [arbiter] could not rule: {adj.error[:90]}")

            result["rounds"][-1]["arbiter"] = adj.summary() if adj and adj.ok else None

            if adj and adj.ok and adj.recommendation == arbiter.ESCALATE:
                final = "ESCALATED"
                print(f"           ESCALATED: {adj.rationale[:200]}")
                break

            if adj and adj.ok and adj.recommendation == arbiter.SHIP:
                # Gates pass and the arbiter upholds nothing. It recommends;
                # it does not ship. A human runs --apply.
                final = "ARBITER_SHIP"
                print("           arbiter recommends SHIP - human sign-off required")
                break

            stall = arbiter.thrashing(convergence)
            if stall:
                final = "NOT_CONVERGING"
                print(f"           STOPPING: {stall}")
                break

            # Only upheld findings go back. Feeding all of them is what drove
            # the rewrite churn that generated the next round's findings.
            if adj and adj.ok and adj.upheld_indices:
                keep = [all_findings[i - 1] for i in adj.upheld_indices]
                feedback = "\n".join(f"- [{f.severity}] {f.text}" for f in keep)
                dropped = len(all_findings) - len(keep)
                print(f"           [arbiter] {len(keep)} finding(s) upheld, {dropped} dropped")
            else:
                feedback = panel.findings

            ws.revert(touched)
            history += [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        f"A review panel returned {panel.verdict}. An arbiter has already "
                        f"discarded the findings that do not block; those below are the ones "
                        f"that do.\n\nFINDINGS:\n{feedback}\n\n"
                        "Fix exactly these and re-emit ALL blocks in full. Do not make unrelated "
                        "changes -- every extra edit creates new surface for the next review."
                    ),
                },
            ]

        result["final_verdict"] = final
        result["cost_usd"] = round(result["cost_usd"], 4)

        # ---- arbitration
        if blocks and not gates.check_static(regs, blocks, lambda ln: regions.strip_code(ln, profile), profile).ok:
            blocks = {}
        if blocks:
            (art / "final_blocks.json").write_text(
                json.dumps({r.id: blocks.get(r.id, "") for r in regs}, indent=2), encoding="utf-8"
            )
            if final == "APPROVE" or allow_unapproved:
                # On an arbiter override the candidate was reverted when its
                # round ended, so put it back before exporting.
                if not ws.dirty_files():
                    regions.apply(regs, blocks)
                patch = ws.export_patch(art / "final.patch")
                if apply:
                    moved = ws.promote(sorted({r.file for r in regs}))
                    result["applied"] = True
                    result["touched"] = moved
                    tag = "" if final == "APPROVE" else " (UNAPPROVED - arbiter override)"
                    print(f"  APPLIED{tag} -> {', '.join(moved)}")
                    print("  review with `git diff` and commit explicit paths; nothing is staged.")
                else:
                    print(f"  approved, not applied (no --apply). Patch: {patch}")
            else:
                # Write a readable diff even on failure: final_blocks.json is
                # JSON-escaped C# and unreadable, and a human has to decide what
                # happens next.
                if not ws.dirty_files():
                    regions.apply(regs, blocks)
                patch = ws.export_patch(art / "final.patch")
                ws.revert(sorted({r.file for r in regs}))
                if final == "ARBITER_SHIP":
                    # Deliberately not auto-applied. The arbiter filters and
                    # recommends; on an addon that moves real money a human
                    # signs off. Promote with --allow-unapproved --apply.
                    print(f"  ARBITER RECOMMENDS SHIP - awaiting human sign-off.")
                    print(f"    review: {patch}")
                    print(f"    promote: --resume-raw {art / f'r{rnd}_impl_raw.txt'} --allow-unapproved --apply")
                else:
                    print(f"  NOT APPLIED: verdict={final}. Patch for review: {patch}")

    (art / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    append_ledger(
        repo,
        {
            "ticket": tid,
            "verdict": result["final_verdict"],
            "applied": result["applied"],
            "rounds": len(result["rounds"]),
            "cost_usd": result["cost_usd"],
        },
    )
    return result
