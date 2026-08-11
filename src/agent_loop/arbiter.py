"""
arbiter.py
==========
Adjudicates reviewer findings. The rung that was missing.

The panel was doing two incompatible jobs at once. Reviewers are told to
"assume the implementer is confident and wrong", which makes them good at
DETECTION and structurally incapable of ADJUDICATION -- an adversarial reviewer
has no stopping rule, so it always produces something. Requiring unanimous
APPROVE from two of them is therefore not a high bar but an unreachable one on
any region large enough to keep offering new surface.

T2 demonstrated it precisely: round 1 produced 11 distinct findings, round 3
produced 13, and the two sets did not overlap at all. Every finding was fixed;
each rewrite of a 168-line method simply exposed different ground. Three rounds,
no convergence, and no mechanism to say "these three matter, the rest do not".

The arbiter sees what neither reviewer does -- the ticket, the patch, the
mechanical gate results, and BOTH reviewers' findings together -- and rules on
each finding. Only upheld findings go back to the implementer.

Authority is deliberately bounded:
  * It cannot overturn a mechanical gate. Compile errors, test regressions and
    lock-scope violations are facts, not opinions.
  * It cannot ship. It recommends; a human runs --apply. On an addon that moves
    real money, a model does not get the last word on naked-position risk.
  * It cannot dismiss a BLOCKER on its own authority. Rejecting one is allowed;
    rejecting one *and* recommending SHIP is not, because two labelled corpus
    cases show it doing exactly that to findings that were correct. See
    `_blocker_indices`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .providers import ProviderError, chat

UPHELD, REJECTED, OUT_OF_SCOPE = "UPHELD", "REJECTED", "OUT_OF_SCOPE"
SHIP, REVISE, ESCALATE = "SHIP", "REVISE", "ESCALATE"

# The domain paragraph a profile does not supply. Deliberately generic: the
# previous text described a NinjaTrader risk-guard AddOn and demanded that an
# UPHELD finding "state the concrete sequence of events that loses money or
# leaves a position unprotected". Carried into a repo that does not move money,
# that is not a high bar but an unmeetable one -- no finding can clear it, so
# the arbiter rejects everything it is shown and recommends SHIP. The domain
# and its stakes now come from Profile.arbiter_rules.
DEFAULT_ARBITER_RULES = """You are the arbiter for a patch to a production codebase.

An UPHELD finding must name a concrete, reachable failure: specific inputs or sequence of events,
and the wrong behaviour that results. "Could be clearer", "might be safer", and "consider also
handling" are NOT upheld.

An unsound SHIP here reaches production, so prefer ESCALATE over a confident wrong answer."""


_ARBITER_CONTRACT = """
Adversarial reviewers have raised findings against a patch that has ALREADY passed every
mechanical gate that applies to it. Those gate results are facts and you may not contradict them.

Your job is NOT to find new defects. Do not review the code afresh. Your job is to rule on the
findings you are given, because the reviewers cannot: they were instructed to assume the
implementer is confident and wrong, so they systematically over-produce, and nothing downstream
distinguishes a finding that matters from one that is merely conceivable.

Rule on EVERY finding, using its number:

  UPHELD       - real, caused by this patch, and blocks. State the concrete failure.
  REJECTED     - wrong. The claimed mechanism does not hold, it contradicts a mechanical gate,
                 the code already handles it, or it restates a settled decision.
  OUT_OF_SCOPE - real, but pre-existing or belonging to a different ticket. This patch does not
                 have to fix everything wrong with the file; it has to fix its own defect without
                 introducing new ones.

Then recommend:
  SHIP     - no upheld findings AND no reviewer filed a BLOCKER. The patch closes its defect and
             introduces no new risk.
  REVISE   - upheld findings remain; the implementer gets ONLY those.
  ESCALATE - you cannot rule safely: the reviewers disagree on a load-bearing fact, the patch is
             too large to reason about, or the ticket itself looks wrong. Say what a human must
             decide.

A BLOCKER you believe is wrong does NOT license SHIP. Rule it REJECTED and say why the mechanism
does not hold, then recommend ESCALATE so a human confirms it. The reviewers do over-produce -- but
a blocking finding they got right and you dismissed is the one mistake this loop cannot absorb, and
it has happened. If you recommend SHIP over a BLOCKER anyway it is converted to ESCALATE and your
rationale is handed to a human as-is, so write it for them.

You are the last automated gate before a human, not a rubber stamp.

OUTPUT FORMAT - obey exactly:
<<<RULINGS>>>
- [UPHELD|REJECTED|OUT_OF_SCOPE] #<n>: one sentence of reasoning
<<<END RULINGS>>>
<<<RECOMMENDATION>>>
SHIP | REVISE | ESCALATE
<<<END RECOMMENDATION>>>
<<<RATIONALE>>>
2-5 sentences a human arbiter can act on without re-reading the patch.
<<<END RATIONALE>>>
<<<SETTLED>>>
- findings you REJECTED that are likely to recur on future tickets and should be recorded as
  permanently settled, one per line (write "- NONE" if none)
<<<END SETTLED>>>
"""


def arbiter_system(rules: str = "") -> str:
    """The arbiter system prompt for a given codebase."""
    return (rules.strip() or DEFAULT_ARBITER_RULES) + "\n" + _ARBITER_CONTRACT


# Kept for callers that want the generic prompt without a profile.
ARBITER_SYSTEM = arbiter_system()

# Bracket punctuation around the verdict is decoration, not signal: the ruling
# is identified by the leading "-", the verdict keyword and the "#n". Requiring
# exact brackets cost two real adjudications -- glm-5.2 emitted
# "- [ [REJECTED] #11: ..." on T2 round 1, and on T3 round 2 it dropped the
# brackets entirely ("- REJECTED #1: ..."), which left all eight findings
# unruled and turned a SHIP into a spurious ESCALATE.
_RULING_RE = re.compile(
    r"^-[\s\[\]*_]*(UPHELD|REJECTED|OUT_OF_SCOPE)[\s\[\]*_]*#(\d+)\s*:?\s*(.*)$",
    re.MULTILINE,
)


@dataclass
class Ruling:
    index: int
    verdict: str
    reason: str


@dataclass
class Adjudication:
    ok: bool  # the arbiter answered and was parseable
    recommendation: str = ""
    rulings: List[Ruling] = field(default_factory=list)
    rationale: str = ""
    settled: List[str] = field(default_factory=list)
    raw: str = ""
    error: str = ""
    usage: str = ""
    # The rendered user prompt actually sent. Recorded so a replay can re-send it
    # byte-for-byte: rebuilding it from findings cannot reproduce the original
    # (the ticket, diff and round history are not all recoverable), and a replay
    # against a different prompt measures nothing.
    prompt: str = ""

    def by(self, verdict: str) -> List[Ruling]:
        return [r for r in self.rulings if r.verdict == verdict]

    @property
    def upheld_indices(self) -> List[int]:
        return [r.index for r in self.by(UPHELD)]

    def summary(self) -> str:
        return (
            f"{self.recommendation or 'INVALID'} "
            f"(upheld={len(self.by(UPHELD))} rejected={len(self.by(REJECTED))} "
            f"out-of-scope={len(self.by(OUT_OF_SCOPE))})"
        )


_MARKER_RE = re.compile(r"<<<(?:END )?[A-Z_ ]+>>>")


def _section(text: str, name: str) -> str:
    """Extract a section, tolerating a mismatched or missing terminator.

    A strict opener/closer match silently returns "" for a section whose END tag
    is wrong, and there is no way to tell that from "the model said nothing".
    glm-5.2 closed RATIONALE with `<<<END SETTLED>>>` and omitted the
    `<<<SETTLED>>>` opener entirely on BOTH T2 arbitration rounds, so the
    rationale and all six nominated settled decisions were discarded every time
    -- silently defeating the mechanism that exists to stop reviewers
    re-litigating known false positives.
    """
    open_m = re.search(rf"<<<{name}>>>\r?\n?", text)
    if open_m:
        rest = text[open_m.end():]
        close_m = re.search(rf"<<<END {name}>>>", rest)
        if close_m:
            return rest[: close_m.start()].strip()
        # Terminator missing or misnamed: run to the next marker of any kind.
        nxt = _MARKER_RE.search(rest)
        return (rest[: nxt.start()] if nxt else rest).strip()

    # No opener at all. If a closer exists, the body is whatever sits between it
    # and the marker before it -- recoverable, and better than dropping content.
    closers = list(re.finditer(rf"<<<END {name}>>>", text))
    if not closers:
        return ""
    last = closers[-1]
    prior = [m for m in _MARKER_RE.finditer(text) if m.end() <= last.start()]
    start = prior[-1].end() if prior else 0
    return text[start: last.start()].strip()


def build_prompt(
    ticket: Dict[str, Any],
    findings: Sequence[Any],
    gate_summary: str,
    patch_diff: str,
    settled: Sequence[str],
    round_history: str = "",
    context: str = "",
) -> str:
    parts = [
        f"# TICKET {ticket['id']}: {ticket['title']}",
        "",
        "## Defect this patch must close",
        ticket["defect"].strip(),
        "",
        "## Mechanical gates (facts - you may not contradict these)",
        gate_summary or "(none run)",
        "",
    ]
    if settled:
        parts += [
            "## Already-settled decisions",
            "A finding that restates one of these is REJECTED by definition.",
            "",
        ] + [f"- {s}" for s in settled] + [""]
    if round_history:
        parts += ["## Convergence history", round_history, ""]
    if context:
        # Phase 3 says the ranked slice reaches the implementer, the reviewer
        # AND the arbiter. It never reached the arbiter, which is the one role
        # ruling on "will this break callers?" claims without seeing callers.
        parts += ["## Graph context (callers, callees, tests)", context, ""]
    parts += ["## Findings to rule on", ""]
    for i, f in enumerate(findings, 1):
        parts.append(f"#{i} [{f.severity}] (from {f.model})\n{f.text}\n")
    parts += [
        "## The patch under review (unified diff)",
        "```diff",
        patch_diff[:60000] if patch_diff.strip() else "(no diff available)",
        "```",
        "",
        f"Rule on all {len(findings)} findings by number, then recommend.",
    ]
    return "\n".join(parts)


def _blocker_indices(findings: Sequence[Any]) -> List[int]:
    """1-based indices of the BLOCKER-severity findings.

    BLOCKER only, not `Finding.blocking` -- that property also covers MAJOR, and
    an adversarial reviewer with no stopping rule produces a MAJOR on almost
    every round, so escalating on those would escalate everything and the
    arbiter would stop meaning anything at all.

    It does NOT exclude upheld blockers, because it cannot be reached with one:
    its only caller runs after `rec == SHIP and any(UPHELD)` has already become
    REVISE, so no UPHELD ruling survives to that point. An `i not in upheld`
    clause was written here first and SURVIVED mutation -- deleted rather than
    kept as decoration. What protects the ordering is a test
    (`test_an_upheld_blocker_still_revises_rather_than_escalating`), which fails
    if the two downgrades are ever swapped.

    `getattr` rather than `f.severity` for the replay path, which is the one
    caller that can reach here without `build_prompt` having already required
    the attribute. A finding with no severity is not treated as a blocker.
    """
    return [
        i
        for i, f in enumerate(findings, 1)
        if str(getattr(f, "severity", "")).upper() == "BLOCKER"
    ]


def _arbiter_max_tokens(model: str, fallback: int) -> int:
    """The arbiter's output budget, per model, from the registry then config.

    Sibling of `_arbiter_think` and added for the same reason it exists: the
    budget was a literal default (`max_tokens: int = 24000`) that `loop.py`'s
    call never passed, so `roles.arbiter.max_tokens` was dead configuration --
    the exact condition `ModelRegistry.max_tokens_for` was written to end, and
    which its docstring already describes (O58).
    """
    from . import config
    from .models import DEFAULT_REGISTRY

    try:
        role_default = config.get().roles["arbiter"].max_tokens
    except (KeyError, AttributeError):
        role_default = fallback
    return DEFAULT_REGISTRY.max_tokens_for(model, "arbiter", role_default)


def _arbiter_think() -> bool:
    """Whether the arbiter reasons before answering, per config.

    Falls back to False if the role is missing rather than raising: an
    unconfigured arbiter should still adjudicate, and False is the measured
    default.
    """
    from . import config

    try:
        return config.get().roles["arbiter"].think
    except (KeyError, AttributeError):
        return False


def adjudicate(
    model: str,
    ticket: Dict[str, Any],
    findings: Sequence[Any],
    gate_summary: str,
    patch_diff: str,
    settled: Sequence[str] = (),
    round_history: str = "",
    max_tokens: Optional[int] = None,
    timeout: int = 900,
    context: str = "",
    rules: str = "",
    prompt_override: str = "",
) -> Adjudication:
    """Rule on findings. Never raises -- an unreachable arbiter yields ok=False,
    which the caller must treat as "not adjudicated", never as approval.

    `rules` is the consumer's Profile.arbiter_rules: what "blocks" means in
    this codebase and what an unsound SHIP costs there.

    `max_tokens` is an OVERRIDE. Left unset it comes from the registry and
    config, per model -- see `_arbiter_max_tokens`.

    `prompt_override` sends a previously recorded prompt verbatim instead of
    building one. Only replay should use it: holding the prompt constant is the
    entire point of a replay, and build_prompt cannot reproduce a recorded prompt
    from findings alone.
    """
    if not findings:
        return Adjudication(True, SHIP, rationale="No findings to adjudicate.")
    prompt = prompt_override or build_prompt(
        ticket, findings, gate_summary, patch_diff, settled, round_history, context
    )
    try:
        out = chat(
            model,
            [
                {"role": "system", "content": arbiter_system(rules)},
                {"role": "user", "content": prompt},
            ],
            max_tokens=_arbiter_max_tokens(model, 24000)
            if max_tokens is None else max_tokens,
            timeout=timeout,
            # From config, not hardcoded. This was a literal `think=False` while
            # config.py ALSO declared think=False for the arbiter role -- the two
            # agreed only by coincidence, which is the exact failure config.py
            # was created to end, and it meant changing the config flag did
            # nothing at all. The measured answer is still False; see the
            # arbiter role in config.py for the numbers.
            think=_arbiter_think(),
        )
    except ProviderError as exc:
        return Adjudication(False, error=str(exc), prompt=prompt)

    text = out.text or ""
    rec_raw = _section(text, "RECOMMENDATION").upper()
    rec = next((c for c in (ESCALATE, REVISE, SHIP) if c in rec_raw), "")
    if not rec:
        return Adjudication(False, raw=text, error="no parseable recommendation", usage=out.usage_line(), prompt=prompt)

    rulings = [
        Ruling(int(m.group(2)), m.group(1), m.group(3).strip())
        for m in _RULING_RE.finditer(_section(text, "RULINGS"))
        if 1 <= int(m.group(2)) <= len(findings)
    ]
    settled_out = [
        ln.lstrip("- ").strip()
        for ln in _section(text, "SETTLED").splitlines()
        if ln.strip().lstrip("- ").strip().upper() not in ("", "NONE")
    ]

    # A SHIP recommendation that silently skipped findings is not a ruling, it
    # is an omission. Downgrade rather than trust it.
    ruled = {r.index for r in rulings}
    unruled = [i for i in range(1, len(findings) + 1) if i not in ruled]
    if rec == SHIP and unruled:
        return Adjudication(
            True,
            ESCALATE,
            rulings,
            rationale=(
                f"Arbiter recommended SHIP but did not rule on finding(s) "
                f"{unruled}. Escalated rather than accepted."
            ),
            settled=settled_out,
            raw=text,
            usage=out.usage_line(),
            prompt=prompt,
        )
    if rec == SHIP and any(r.verdict == UPHELD for r in rulings):
        rec = REVISE  # self-contradiction: upheld findings cannot ship

    # O28/O20: SHIP is unavailable while a BLOCKER stands dismissed. Ruling on
    # every finding is not the same as ruling WELL, which is what the check
    # above assumed -- corpus case 2 ruled on all thirty and rejected four real
    # defects, one of them a position flip stated with its losing sequence. An
    # upheld blocker has already become REVISE by here, so what remains is a
    # blocker the arbiter addressed and waved through.
    if rec == SHIP:
        dismissed = _blocker_indices(findings)
        if dismissed:
            named = ", ".join(f"#{i}" for i in dismissed)
            return Adjudication(
                True,
                ESCALATE,
                rulings,
                rationale=(
                    f"Arbiter recommended SHIP while dismissing BLOCKER finding(s) {named}. "
                    "Whether a blocking finding holds is a call a human makes here. "
                    f"Arbiter's rationale: {_section(text, 'RATIONALE')}"
                ),
                settled=settled_out,
                raw=text,
                usage=out.usage_line(),
                prompt=prompt,
            )

    return Adjudication(
        True, rec, rulings, _section(text, "RATIONALE"), settled_out, text,
        usage=out.usage_line(), prompt=prompt,
    )


# --------------------------------------------------------------------------
# Convergence
# --------------------------------------------------------------------------
def thrashing(history: List[Tuple[int, set]], min_rounds: int = 3) -> Optional[str]:
    """Detect a loop that is generating new surface as fast as it fixes old.

    `history` is [(blocking_count, {signatures}), ...] oldest first. Thrash is
    consecutive rounds whose findings do not overlap while the count fails to
    fall -- the implementer is complying, the reviewers are not repeating
    themselves, and the patch is still not converging. Three rounds of that is
    enough; T2 spent three proving it and would have spent a fourth.
    """
    if len(history) < min_rounds:
        return None
    recent = history[-min_rounds:]
    counts = [c for c, _ in recent]
    overlaps = [
        len(recent[i][1] & recent[i + 1][1]) for i in range(len(recent) - 1)
    ]
    if any(overlaps):
        return None
    if counts[-1] < counts[0]:
        return None
    return (
        f"no convergence over {min_rounds} rounds: blocking findings "
        f"{' -> '.join(map(str, counts))} with zero overlap between consecutive "
        f"rounds. Each revision is exposing new surface rather than closing the "
        f"defect; more rounds will not help. Split the ticket into smaller "
        f"regions or arbitrate the findings by hand."
    )
