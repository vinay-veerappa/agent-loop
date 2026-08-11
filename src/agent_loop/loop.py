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
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import arbiter, config, gates, profiles, regions, workspace
from ._io import write_text_verbatim
from .models import DEFAULT_REGISTRY
from .compaction import compact_history, history_token_count
from .context import check_graph_freshness, build_context_slice
from .memory import (
    save_settled, inject_settled, save_feedback, build_learning_context,
)
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
        """Stable identity for cross-round comparison.

        Normalised to ignore incidental differences: lowercased, stripped
        of digits and punctuation, with whitespace collapsed to a single
        space. The full normalised text is kept so genuinely different
        findings remain distinguishable."""
        lowered = self.text.lower()
        letters_and_space = re.sub(r"[^a-z\s]+", "", lowered)
        normalised = re.sub(r"\s+", " ", letters_and_space).strip()
        if not normalised:
            # If the finding contains no letters, fall back to the lowercased,
            # whitespace-collapsed original so a non-empty finding never has
            # an empty signature.
            normalised = re.sub(r"\s+", " ", lowered).strip() or lowered
        return normalised

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
    # Token usage, so per-role accounting in the ledger reports what the panel
    # actually cost. The ledger used to read these off Vote when Vote had no
    # such fields, behind a hasattr() guard that turned the mistake into a
    # permanent, silent zero.
    input_tokens: int = 0
    output_tokens: int = 0

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
def build_implement_prompt(
    ticket: Dict[str, Any], regs: Sequence[regions.Region], profile: profiles.Profile
) -> str:
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
            f"```{profile.fence}",
            r.text,
            "```",
        ]
    parts += ["", "Return one block per region id above, in the same order. No other output."]
    return "\n".join(p for p in parts if p)


def extract_test_sources(
    repo: Path, names: Sequence[str], globs: Sequence[str], profile: profiles.Profile
) -> str:
    """Pull the named test methods out of the (read-only) test sources.

    Reviewers cannot judge whether the suite is complete without seeing it, and
    the suite is a first-class artifact here, not an assumption.

    The declaration matching and block extent both come from the profile, via
    regions.extract_named_block. The predecessor hardcoded C# (a modifier and a
    return type, then brace matching), so on a Python profile it matched nothing
    and every reviewer was asked to judge test adequacy with no tests shown.
    """
    if not names or not globs:
        return ""
    out: List[str] = []
    for g in globs:
        for path in sorted(repo.glob(g)):
            src = path.read_text(encoding="utf-8", errors="replace")
            for name in names:
                body = regions.extract_named_block(src, name, profile)
                if body:
                    out.append(f"{profile.line_comment} --- {path.name}: {name} ---\n{body}")
    return "\n\n".join(out)


def build_review_prompt(
    ticket: Dict[str, Any],
    regs: Sequence[regions.Region],
    blocks: Dict[str, str],
    notes: str,
    profile: profiles.Profile,
    orchestrator_note: str,
    gate_summary: str,
    settled_decisions: Sequence[str] = (),
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
    # Auto-extracted decisions from prior tickets arrive via settled_decisions;
    # they used to be computed, printed and then dropped, so the store the
    # arbiter writes to was never read back into a prompt.
    settled = list(settled_decisions or profile.settled)
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
            f"```{profile.fence}",
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
            f"```{profile.fence}",
            r.text,
            "```",
            "### AFTER (proposed)",
            f"```{profile.fence}",
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
        v.input_tokens = out.input_tokens
        v.output_tokens = out.output_tokens
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
def failed_gate_names(rounds: Sequence[Dict[str, Any]]) -> List[str]:
    """The distinct gates that blocked any round of a ticket, sorted.

    A named function rather than an inline comprehension because the writer
    half of the gate-failure record was otherwise untestable: the generated
    acceptance tests for this feature hand-built ledger entries and fed them to
    the report, so deleting the write site entirely left them green. This is
    the same defect the feature exists to fix, one level up.

    `stage` is read via .get() rather than [] deliberately. Every RoundRecord
    carries one, but a ledger is append-only and long-lived, and a KeyError
    here would crash a completed ticket at the moment it records its result --
    losing the run's outcome over a malformed round.
    """
    return sorted({
        r.get("stage", "") for r in rounds if not r.get("ok", True)
    } - {""})


def terminal_ledger_record(tid: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """The ledger entry a finished ticket writes.

    Split out of `run_ticket` for the same reason as `failed_gate_names`: this
    is the last line of a full ticket run, so testing it in place would mean
    driving the whole loop. Deleting the gate field from it left every
    generated acceptance test for this feature green.

    A single failing gate is recorded as a string and several as a list, which
    the report counts once per distinct gate. `gate` is OMITTED rather than set
    empty when nothing failed: the report distinguishes "no gate failure" from
    "written before this field existed", and a falsy value would collapse them.
    """
    record: Dict[str, Any] = {
        "ticket": tid,
        "verdict": result["final_verdict"],
        "applied": result["applied"],
        "rounds": len(result["rounds"]),
        "cost_usd": result["cost_usd"],
    }
    failed = failed_gate_names(result["rounds"])
    if failed:
        record["gate"] = failed[0] if len(failed) == 1 else failed
    return record


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
    # Phase 9.3: per-role token accounting
    impl_input_tokens: int = 0
    impl_output_tokens: int = 0
    reviewer_input_tokens: int = 0
    reviewer_output_tokens: int = 0
    arbiter_input_tokens: int = 0
    arbiter_output_tokens: int = 0


def run_ticket(
    repo: Path,
    ticket: Dict[str, Any],
    profile: profiles.Profile,
    implementer: str,
    reviewers: Sequence[str],
    max_rounds: int = 0,   # 0 = use config.loop.max_rounds
    apply: bool = False,
    allow_unapproved: bool = False,
    resume_raw: str = "",
    orchestrator_note: str = "",
    panel_deadline: int = 0,   # 0 = use config.loop.panel_deadline_secs
    keep_worktree: bool = False,
    arbiter_model: str = "",
) -> Dict[str, Any]:
    # 0 means "ask config", so these limits have exactly one literal definition
    # (config.py) rather than one per signature that happens to agree with it.
    _loop_cfg = config.get().loop
    max_rounds = max_rounds or _loop_cfg.max_rounds
    panel_deadline = panel_deadline or _loop_cfg.panel_deadline_secs

    tid = ticket["id"]
    art = repo / "logs" / "agent_loop" / tid
    art.mkdir(parents=True, exist_ok=True)
    result: Dict[str, Any] = {"ticket": tid, "rounds": [], "applied": False,
                              "applied_approved": False, "applied_unapproved": False,
                              "cost_usd": 0.0}
    convergence: List[Tuple[int, set]] = []

    region_files = sorted({r["file"] for r in ticket["regions"]})
    g0 = gates.check_protected_paths(region_files, profile.protected or gates.DEFAULT_PROTECTED)
    print(f"  [protected] {g0.summary}")
    if not g0.ok:
        result["final_verdict"] = "TICKET_REJECTED"
        result["detail"] = g0.detail
        print(f"  REFUSED: {g0.detail}")
        append_ledger(repo, {"ticket": tid, "verdict": "TICKET_REJECTED", "detail": g0.detail, "gate": "protected"})
        return result

    # ---- graph freshness check (Phase 2)
    graph_status = check_graph_freshness(repo, profile)
    if graph_status != "no-project":
        print(f"  [graph] {graph_status}")

    # Phase 5: inject auto-extracted settled decisions from prior runs.
    # Hand-curated decisions in profile.settled take precedence; auto-extracted
    # ones are appended after.
    effective_settled = inject_settled(profile.settled, repo)
    if len(effective_settled) > len(profile.settled):
        extra = len(effective_settled) - len(profile.settled)
        print(f"  [memory] {len(effective_settled)} settled decisions ({extra} from prior runs)")

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
                if not any(gates.names_match(t, f) for f in ws.baseline)
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
                    ws.root, expect_green, profile.test_sources, profile))

        regs = regions.extract(ws.root, ticket["regions"], profile)
        for r in regs:
            print(f"    region {r.id:<24} {r.file} lines {r.lines_1based}")

        # Phase 3: build graph-augmented context slice for the prompts.
        # This is passive injection (Aider-style) -- the LLM receives richer
        # context but never calls graph tools. When the graph cache is empty
        # or the profile has no graph_project, this returns "".
        # The cache lives in the MAIN repo (not the worktree) because the
        # graph indexes the main repo's code.
        # Built once per ticket and reused by the implementer, reviewer and
        # arbiter prompts. Building it per prompt meant every round fired the
        # same set of live graph queries two or three times over.
        context_slice = build_context_slice(repo, regs, profile)
        if context_slice:
            print(f"  [graph] injected {len(context_slice)} chars of context")

        impl_prompt = build_implement_prompt(ticket, regs, profile)
        if context_slice:
            impl_prompt += f"\n\n## Graph context (from code knowledge graph)\n{context_slice}"
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
        arbiter_consulted = False
        # The promote hint below is built from the round that produced the
        # candidate. Reading the loop variable after the loop breaks when the
        # loop never ran (--max-rounds 0), so track it explicitly.
        last_round = 0

        # Purge ALL stale per-round artifacts from prior runs before the loop
        # starts. A prior run may have left r{N}_* files on disk; a resume with
        # --max-rounds 1 would then produce a result.json that says 1 round
        # while r2_* files exist, making the logs lie (T4/T5 bug). Purging at
        # the top of each round only cleans the current round's stale files;
        # purging here cleans ALL rounds' stale files so the on-disk artifacts
        # always match the result.json's round count.
        for stale in art.glob("r*_*.txt"):
            stale.unlink()

        for rnd in range(1, max_rounds + 1):
            last_round = rnd
            out = None  # may not be set on resume-raw path
            # ---- purge stale artifacts from prior runs
            # A prior run may have left r{rnd}_* files on disk; a resume with
            # --max-rounds 1 would then produce a result.json that says 1 round
            # while r2_* files exist, making the logs lie (T4/T5 bug).
            for stale in art.glob(f"r{rnd}_*"):
                stale.unlink()
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
                    # Phase 4: compact history before the implementer call.
                    # Prior rounds' verbose outputs are pruned to truncation
                    # markers; if the total still exceeds the budget, a
                    # mechanical summary replaces all prior rounds.
                    if rnd > 1:
                        before = history_token_count(history)
                        history = compact_history(history, rnd, profile)
                        after = history_token_count(history)
                        if after < before:
                            print(f"           [compaction] {before} -> {after} tokens (pruned {before - after})")
                    # Implementer keeps thinking (it is planning a patch, not
                    # filling a template) but needs headroom: kimi spent 104k
                    # chars reasoning and still emitted 27.9k output tokens.
                    # cache=True: the implementer is the loop's only genuine
                    # multi-turn conversation, so it is the only caller where a
                    # cache write can be read back. Break-even is two requests,
                    # so a ticket that converges in round 1 loses 0.25x of one
                    # prompt and a ticket that reaches round 2 saves 0.9x of the
                    # pinned head on every round after. No-op on ollama/openai.
                    # Budget comes from the registry, not a literal: this call
                    # hardcoded 48000 while models.py declared 48000 for the same
                    # model, so raising it meant editing the loop. O1's first run
                    # died here -- kimi spent 125,070 chars on reasoning and
                    # emitted empty content, and the budget was unreachable.
                    impl_budget = DEFAULT_REGISTRY.max_tokens_for(
                        implementer, "implementer", 48000
                    )
                    out = chat(implementer, history, max_tokens=impl_budget, cache=True)
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
                if profile.lint_cmd:
                    # files=touched, or a lint_cmd containing {files} silently
                    # short-circuits to "no files to lint" on every round -- a
                    # gate that cannot fail, which is what F2 replaced a gate
                    # that could not pass with. The ticket's region covered only
                    # check_lint, so the loop could not fix this half itself.
                    gl = gates.check_lint(profile.lint_cmd, ws.root, files=touched)
                    (art / f"r{rnd}_lint.txt").write_text(gl.detail, encoding="utf-8")
                    gate_results.append(gl)
                if gate_results[-1].ok and profile.build_cmd:
                    gc = gates.check_compile(profile.build_cmd, ws.root, files=touched)
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
                ticket, regs, blocks, notes, profile, orchestrator_note, gate_summary,
                settled_decisions=effective_settled,
            )
            # Phase 3: the reviewer gets the same slice at half the budget, so
            # it can check "will this break callers?" without crowding out the
            # diff it is here to read.
            if context_slice:
                half_budget = profile.context_token_budget * 4 // 2
                reviewer_context = (
                    context_slice
                    if len(context_slice) <= half_budget
                    else context_slice[:half_budget] + "\n... (truncated)"
                )
                prompt += f"\n\n## Graph context for review (callers + types)\n{reviewer_context}"
            # Phase 9: inject learning feedback (rejected/upheld findings from prior tickets)
            learning_ctx = build_learning_context(repo)
            if learning_ctx:
                prompt += f"\n\n{learning_ctx}"
            # Record the FULLY rendered review prompt -- after graph context and
            # learning feedback are appended, i.e. exactly the bytes the panel
            # sees. Replay re-sends this verbatim; without it, replay had to
            # rebuild an approximation and a verdict flip measured the prompt
            # difference rather than the change under test (O2).
            # write_text_verbatim, NOT write_text: on Windows write_text
            # translates "\n" to "\r\n", so the recorded prompt would differ from
            # the prompt actually sent on every single line -- and a replay
            # re-sending it would not be byte-for-byte after all.
            write_text_verbatim(art / f"r{rnd}_review_prompt.md", prompt)
            panel = review_panel(
                reviewers, prompt, profile.reviewer_system, art, rnd, deadline_secs=panel_deadline
            )
            desc = ", ".join(f"{v.model.split(':')[0]}={v.status}({v.blockers})" for v in panel.votes)
            print(f"           [panel] {panel.verdict or 'INVALID'}  [{desc}]")

            # Sum token usage across reviewers
            rev_in = sum(v.input_tokens for v in panel.votes)
            rev_out = sum(v.output_tokens for v in panel.votes)
            # Implementer usage; `out` is unset on the resume-raw path.
            impl_in = out.input_tokens if out is not None else 0
            impl_out = out.output_tokens if out is not None else 0

            result["rounds"].append(
                RoundRecord(
                    rnd,
                    "review",
                    panel.unanimous_approve,
                    f"{panel.verdict or 'INVALID'} [{desc}]",
                    panel.findings[:8000],
                    impl_input_tokens=impl_in,
                    impl_output_tokens=impl_out,
                    reviewer_input_tokens=rev_in,
                    reviewer_output_tokens=rev_out,
                ).__dict__
            )

            if not panel.valid:
                # Quorum check: if >= ceil(2/3 * len(reviewers)) answered and
                # all are APPROVE, proceed as APPROVE with panel_partial metadata.
                # A 2-of-3 panel where both answered APPROVE is different from
                # a 0-of-3 panel; hard-stopping on quorum-met unanimous APPROVE
                # wastes a candidate that the panel approved.
                counted = [v for v in panel.votes if v.counted]
                quorum = math.ceil(2 * len(reviewers) / 3) if reviewers else 1
                if len(counted) >= quorum and all(v.status == APPROVE for v in counted):
                    # A quorum approval is NOT a unanimous one: a reviewer that
                    # was never reached cannot approve on the panel's behalf, so
                    # this is recorded as partial and promotes as unapproved.
                    result["panel_partial"] = True
                    result["panel_partial_missing"] = [v.model for v in panel.unreachable]
                    final = "APPROVE_PARTIAL"
                    print(
                        f"           panel quorum {len(counted)}/{len(reviewers)} all APPROVE; "
                        f"recorded as PARTIAL (unreached: "
                        f"{', '.join(v.model for v in panel.unreachable)})"
                    )
                    break

                # A reviewer that could not be reached has not voted. This is
                # NOT a rejection: stop cleanly, keep the candidate on disk, and
                # let the arbiter resume from it once the provider is healthy.
                who = ", ".join(f"{v.model} ({v.error})" for v in panel.unreachable)
                print(f"           panel INVALID - NOT a rejection. Unreachable: {who}")
                print(f"           resume with --resume-raw {art / f'r{rnd}_impl_raw.txt'}")
                if touched:
                    ws.revert(touched)
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
                    settled=effective_settled,
                    round_history=_history_note(convergence),
                    context=context_slice,
                    rules=profile.arbiter_rules,
                )
                (art / f"r{rnd}_arbiter.txt").write_text(
                    adj.raw or adj.error, encoding="utf-8"
                )
                # As with the review prompt: recorded so a replay can re-send it
                # verbatim. build_prompt cannot reconstruct it -- the diff and
                # round history are not recoverable from the corpus (O2).
                if adj.prompt:
                    write_text_verbatim(
                        art / f"r{rnd}_arbiter_prompt.md", adj.prompt
                    )
                if adj.ok:
                    arbiter_consulted = True
                    print(f"           [arbiter] {adj.summary()}  {adj.usage}")
                    if adj.settled:
                        print(f"           [arbiter] nominates {len(adj.settled)} finding(s) as settled")
                        # Phase 5: persist arbiter-nominated settled decisions
                        saved = save_settled(repo, tid, adj.settled)
                        if saved:
                            print(f"           [memory] saved {saved} settled decision(s) to store")

                    # Phase 9: save learning feedback for each finding's ruling.
                    # The ruling carries an INDEX, not a finding: join back to
                    # all_findings to record the finding's own text, severity
                    # and author. Recording ruling.reason as the finding text
                    # (and every severity as BLOCKER) meant later reviewers were
                    # shown the arbiter's rationale labelled as a known false
                    # positive -- the opposite of the intended lesson.
                    for ruling in adj.rulings:
                        if not 1 <= ruling.index <= len(all_findings):
                            continue
                        f = all_findings[ruling.index - 1]
                        save_feedback(
                            repo,
                            tid,
                            rnd,
                            f.model,
                            f.text,
                            f.severity,
                            ruling.verdict,  # UPHELD / REJECTED / OUT_OF_SCOPE
                        )
                else:
                    print(f"           [arbiter] could not rule: {adj.error[:90]}")
                    # The arbiter is unreachable. Falling through to feeding
                    # ALL findings back is the T2 failure mode the arbiter
                    # exists to prevent. Break with ARBITER_DEADLOCK instead.
                    if touched:
                        ws.revert(touched)
                    final = "ARBITER_DEADLOCK"
                    break

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
            # PANEL_REJECT signal: if the panel's worst verdict was REJECT,
            # tell the implementer to rethink the approach, not just tweak details.
            is_reject = panel.verdict == "REJECT"
            history += [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        (f"A review panel REJECTED this approach. The arbiter has already "
                         f"discarded the findings that do not block; those below are the ones "
                         f"that do. RETHINK THE APPROACH — do not just tweak these lines.\n\n"
                         f"FINDINGS:\n{feedback}\n\n"
                         "Re-emit ALL blocks in full with a fundamentally different approach.\n")
                        if is_reject else
                        (f"A review panel returned {panel.verdict}. An arbiter has already "
                         f"discarded the findings that do not block; those below are the ones "
                         f"that do.\n\nFINDINGS:\n{feedback}\n\n"
                         "Fix exactly these and re-emit ALL blocks in full. Do not make unrelated "
                         "changes -- every extra edit creates new surface for the next review.")
                    ),
                },
            ]

        # Distinguish "ran with arbiter, still not converging" from
        # "ran without arbiter" (e.g. --max-rounds 1 + panel REVISE +
        # arbiter disabled or never reached). The two cases are materially
        # different: the first means the arbiter tried and could not converge;
        # the second means the arbiter never had a chance.
        if final == "MAX_ROUNDS_EXHAUSTED" and not arbiter_consulted:
            final = "ARBITER_NEVER_RAN"

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
                    result["applied_approved"] = (final == "APPROVE")
                    result["applied_unapproved"] = (final != "APPROVE")
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
                if final in ("ARBITER_SHIP", "APPROVE_PARTIAL"):
                    # Deliberately not auto-applied. The arbiter filters and
                    # recommends; a human signs off. Same for a quorum-only
                    # approval, where one reviewer never voted at all.
                    if final == "ARBITER_SHIP":
                        print("  ARBITER RECOMMENDS SHIP - awaiting human sign-off.")
                    else:
                        print(
                            "  PANEL APPROVED ON QUORUM ONLY "
                            f"({len(result.get('panel_partial_missing', []))} reviewer(s) never voted)"
                            " - awaiting human sign-off."
                        )
                    print(f"    review: {patch}")
                    print(
                        f"    promote: --resume-raw {art / f'r{last_round}_impl_raw.txt'} "
                        "--allow-unapproved --apply"
                    )
                else:
                    print(f"  NOT APPLIED: verdict={final}. Patch for review: {patch}")

    (art / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    append_ledger(repo, terminal_ledger_record(tid, result))
    return result
