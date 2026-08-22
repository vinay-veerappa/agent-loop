"""
arbiter.py
==========
Adjudicates reviewer findings using the INVERTED approach: reject only what is
demonstrably wrong, keep everything else.

The original arbiter asked "is this finding correct?" — a semantic judgment
that requires understanding intent. LLMs cannot do this reliably, so the
arbiter defaulted to conservatism and rejected everything (0/5 correct
findings upheld across most models on the labelled corpus).

The inverted arbiter asks "is this finding DEMONSTRABLY WRONG?" — a much
narrower question with five concrete rejection criteria. When in doubt, KEEP:
the implementer and the human reviewer can judge correctness, but they cannot
act on findings that were silently dropped. The burden of proof is on
rejection, not on keepment.

Measured improvement (O3 corpus, 5 correct findings out of 6, 3 reps each):
  Model          Current (uphold)  Inverted (reject)
  deepseek-flash       0.0/5            5.0/5
  deepseek-pro         0.0/5            5.0/5
  glm-5.2              0.7/5            5.0/5
  kimi-k3              0.3/5            5.0/5
  qwen3.5              0.0/5            5.0/5
  minimax-m3           N/A              4.7/5
  kimi-k2.7-code       2.3/5            3.7/5
  mistral-large-3      2.0/5            3.0/5
  gemini-3.7-flash     N/A              3.0/5

Zero false positives across all 42 runs — no model kept a wrong finding.

Authority is deliberately bounded:
  * It cannot overturn a mechanical gate. Compile errors, test regressions and
    lock-scope violations are facts, not opinions.
  * It cannot ship. It recommends; a human runs --apply.
  * It cannot dismiss a BLOCKER on its own authority. Rejecting one is allowed;
    rejecting one *and* recommending SHIP is not, because a blocking finding
    the arbiter wrongly rejected is the one mistake this loop cannot absorb.
    See `_blocker_indices`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .providers import ProviderError, chat

# Verdict constants — KEEP replaces UPHELD, REJECT replaces REJECTED/OUT_OF_SCOPE.
# UPHELD/REJECTED kept as aliases for backward compatibility with memory.py and
# any external consumers that read the ledger's `ruling` field.
KEEP, REJECT = "KEEP", "REJECT"
UPHELD, REJECTED, OUT_OF_SCOPE = KEEP, REJECT, REJECT  # aliases
SHIP, REVISE, ESCALATE = "SHIP", "REVISE", "ESCALATE"

# The domain paragraph a profile does not supply. Deliberately generic.
DEFAULT_ARBITER_RULES = """You are the arbiter for a patch to a production codebase.

The patch has already passed every mechanical gate. Reviewers raised findings
against it. Your job is to identify findings that are DEMONSTRABLY WRONG and
should be dropped. Everything you do NOT reject goes back to the implementer.

The burden of proof is on REJECTION, not on keeping. When in doubt, KEEP."""


_ARBITER_CONTRACT = """
Adversarial reviewers have raised findings against a patch that has ALREADY passed every
mechanical gate that applies to it. Those gate results are facts and you may not contradict them.

Your job is NOT to find new defects. Do not review the code afresh. Your job is NOT to judge
whether each finding is "correct" — that requires understanding intent, which is a semantic
judgment you cannot reliably make. Your job is narrower and more grounded: identify findings
that are DEMONSTRABLY WRONG and should be dropped.

A finding is DEMONSTRABLY WRONG if any of these apply:
  1. CONTRADICTS A GATE: the finding claims the code doesn't compile or tests fail, but the
     gate results show they pass.
  2. CODE DOESN'T EXIST: the finding references code, variables, or functions that are not
     in the patch or the surrounding context.
  3. OUT OF SCOPE: the finding is about pre-existing code the patch didn't touch, or is named
     in the ticket's scope block as deliberately excluded.
  4. RESTATES A SETTLED DECISION: the finding contradicts a decision that was already settled
     on a prior ticket.
  5. MECHANISM DOESN'T HOLD: the specific failure the finding describes cannot actually occur
     given the code as written.

For each finding, rule:
  REJECT     - demonstrably wrong, drop it (cite which of the 5 criteria above)
  KEEP       - cannot be demonstrably rejected; it goes to the implementer

Everything you do NOT reject goes back to the implementer. The burden of proof is on rejection,
not on keep. When in doubt, KEEP — the implementer and the human reviewer can judge correctness;
your job is to remove noise, not to gatekeep.

Then recommend:
  SHIP     - no findings survive (all rejected). The patch closes its defect and introduces
             no new risk.
  REVISE   - findings survive; the implementer gets the ones you did NOT reject.

A BLOCKER you believe is wrong does NOT license SHIP. Rule it REJECT and say which criterion
applies, then recommend REVISE so a human confirms the rejection. A blocking finding the
reviewers got right and you dismissed is the one mistake this loop cannot absorb, and it has
happened. If you recommend SHIP over a BLOCKER anyway it is converted to ESCALATE and your
rationale is handed to a human as-is, so write it for them.

You are the last automated gate before a human, not a rubber stamp.

OUTPUT FORMAT - obey exactly:
<<<RULINGS>>>
- [REJECT|KEEP] #<n>: one sentence citing the criterion (1-5) or "no rejection criterion met"
<<<END RULINGS>>>
<<<RECOMMENDATION>>>
SHIP | REVISE
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

# The ruling parser — KEEP/REJECT instead of UPHELD/REJECTED/OUT_OF_SCOPE.
# Tolerates bracket decoration (the comment below documents two real
# adjudications glm-5.2 broke by emitting variant bracket punctuation).
_RULING_RE = re.compile(
    r"^-[\s\[\]*_]*(REJECT|KEEP)[\s\[\]*_]*#(\d+)\s*:?\s*(.*)$",
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
    prompt: str = ""

    def by(self, verdict: str) -> List[Ruling]:
        return [r for r in self.rulings if r.verdict == verdict]

    @property
    def upheld_indices(self) -> List[int]:
        """Findings that survive (were NOT rejected). Backward-compatible name
        — callers that check `upheld_indices` get the surviving findings."""
        return [r.index for r in self.by(KEEP)]

    @property
    def kept_indices(self) -> List[int]:
        """Same as upheld_indices — the findings that survive to the implementer."""
        return [r.index for r in self.by(KEEP)]

    @property
    def rejected_indices(self) -> List[int]:
        return [r.index for r in self.by(REJECT)]

    def summary(self) -> str:
        kept = len(self.by(KEEP))
        rejected = len(self.by(REJECT))
        return (
            f"{self.recommendation or 'INVALID'} "
            f"(kept={kept} rejected={rejected})"
        )


_MARKER_RE = re.compile(r"<<<(?:END )?[A-Z_ ]+>{2,}")


def _section(text: str, name: str) -> str:
    """Extract a section, tolerating a mismatched or missing terminator.

    A strict opener/closer match silently returns "" for a section whose END tag
    is wrong, and there is no way to tell that from "the model said nothing".
    glm-5.2 closed RATIONALE with `<<<END SETTLED>>>` and omitted the
    `<<<SETTLED>>>` opener entirely on BOTH T2 arbitration rounds, so the
    rationale and all six nominated settled decisions were discarded every time
    -- silently defeating the mechanism that exists to stop reviewers
    re-litigating known false positives.

    Tolerates `>>` closers (not just `>>>`), matching the block parser's
    `>{2,}` tolerance. A model that drops one `>` from a BLOCK closer may also
    drop one from a RULING or RATIONALE closer, and the arbiter's unruled
    findings were the cost on T3. See AGENT_LOOP_THIRD_REVIEW.md N9.
    """
    open_m = re.search(rf"<<<{name}>{{2,}}\r?\n?", text)
    if open_m:
        rest = text[open_m.end():]
        close_m = re.search(rf"<<<END {name}>{{2,}}", rest)
        if close_m:
            return rest[: close_m.start()].strip()
        nxt = _MARKER_RE.search(rest)
        return (rest[: nxt.start()] if nxt else rest).strip()

    closers = list(re.finditer(rf"<<<END {name}>{{2,}}", text))
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
        ticket.get("defect", "").strip(),
        "",
    ]
    # CF-10: the ticket's scope/context deserves its OWN labelled block.
    ticket_context = ticket.get("context", "").strip()
    if ticket_context:
        parts += [
            "## Ticket scope (what this patch must NOT touch)",
            "The ticket's context field names things that are deliberately out of scope. "
            "A finding whose subject this block names is REJECT criterion #3 (out of scope).",
            "",
            ticket_context,
            "",
        ]
    parts += [
        "## Mechanical gates (facts - you may not contradict these)",
        gate_summary or "(none run)",
        "",
    ]
    if settled:
        parts += [
            "## Already-settled decisions",
            "A finding that restates one of these is REJECT criterion #4 (restates settled).",
            "",
        ] + [f"- {s}" for s in settled] + [""]
    if round_history:
        parts += ["## Convergence history", round_history, ""]
    if context:
        parts += ["## Graph context (callers, callees, tests)", context, ""]
    parts += ["## Findings to rule on", ""]
    for i, f in enumerate(findings, 1):
        parts.append(f"#{i} [{f.severity}] (from {f.model})\n{f.text}\n")
    parts += [
        "## The patch under review (unified diff)",
        "```diff",
        _truncate_diff(patch_diff, 60000),
        "```",
        "",
        f"Rule on all {len(findings)} findings by number. REJECT only if demonstrably "
        f"wrong (cite the criterion 1-5). KEEP everything else.",
    ]
    return "\n".join(parts)


def _blocker_indices(findings: Sequence[Any]) -> List[int]:
    """1-based indices of the BLOCKER-severity findings.

    BLOCKER only, not `Finding.blocking` -- that property also covers MAJOR, and
    an adversarial reviewer with no stopping rule produces a MAJOR on almost
    every round, so escalating on those would escalate everything and the
    arbiter would stop meaning anything at all.
    """
    return [
        i
        for i, f in enumerate(findings, 1)
        if str(getattr(f, "severity", "")).upper() == "BLOCKER"
    ]


_MAX_DIFF_CHARS = 60000


def _truncate_diff(patch_diff: str, max_chars: int = _MAX_DIFF_CHARS) -> str:
    """Truncate a diff to max_chars, inserting a visible marker at the cut."""
    diff = patch_diff.strip()
    if not diff:
        return "(no diff available)"
    if len(diff) <= max_chars:
        return diff
    omitted = len(diff) - max_chars
    return (
        diff[:max_chars]
        + f"\n\n... [DIFF TRUNCATED: {omitted} chars omitted -- "
        f"the patch is larger than {max_chars} chars. If a finding references "
        f"code past this point you cannot evaluate it; do not reject it.] ..."
    )


def _arbiter_max_tokens(model: str, fallback: int) -> int:
    from . import config
    from .models import DEFAULT_REGISTRY

    try:
        role_default = config.get().roles["arbiter"].max_tokens
    except (KeyError, AttributeError):
        role_default = fallback
    return DEFAULT_REGISTRY.max_tokens_for(model, "arbiter", role_default)


def _arbiter_think() -> bool:
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

    Inverted approach: REJECT only demonstrably wrong findings. Everything not
    rejected (KEEP) goes back to the implementer. The burden of proof is on
    rejection, not on keepment.
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
            think=_arbiter_think(),
            cache=True,
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
    # SHIP with surviving (KEEP) findings is a self-contradiction.
    if rec == SHIP and any(r.verdict == KEEP for r in rulings):
        rec = REVISE

    # O28/O20: SHIP is unavailable while a BLOCKER stands rejected. A BLOCKER
    # the arbiter rejected and then recommended SHIP over is the one mistake
    # this loop cannot absorb — the safety rule converts it to ESCALATE so a
    # human confirms the rejection.
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
    themselves, and the patch is still not converging.
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