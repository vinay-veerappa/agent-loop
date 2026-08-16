"""
compaction.py
=============
Round-level history pruning (Phase 4).

The implementer's history grows unboundedly across rounds. Each round adds
the implementer's raw output, the reviewer findings, the arbiter ruling,
and the feedback message. By round 4, the history can exceed 400K tokens.

Phase 4a: prune verbose old outputs above a token threshold. PRIOR rounds'
implementer output, reviewer findings, and build/test logs that exceed the
threshold are replaced with truncation markers that preserve per-finding
structure (reviewer name, severity, one-line summary, arbiter ruling) --
not just aggregate counts.

Phase 4b: if the pruned history still exceeds the profile's
round_input_token_budget (default 40K), replace rounds 1..N-1 with a single
summary of "what was tried and rejected" -- mechanical first because it is free
and deterministic, and a cheap-model summary only if the mechanical one still
does not fit.

Two things are never compacted, at any budget:

  * the system prompt and the IMPLEMENT PROMPT (history[0] and history[1]).
    Every round ends "re-emit ALL blocks in full", so an implementer that has
    lost the ticket and the region source cannot do the one thing it was asked
    to do. See pin_count().
  * the newest exchange -- the candidate under revision and the feedback about
    it. That is the text the next round edits.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .profiles import Profile


class CompactionError(RuntimeError):
    """Raised when the pinned head alone exceeds the round input budget.

    The system prompt and the implement prompt (history[0] and history[1]) are
    never compacted -- every round ends "re-emit ALL blocks in full", so an
    implementer that has lost the ticket and the region source cannot comply.
    If those two messages alone exceed the profile's round_input_token_budget,
    no amount of compaction can make the prompt fit. The loop would proceed to
    send an oversized prompt to the provider, resulting in a context_length
    error AFTER preparation work and possibly paid calls.

    Failing here, BEFORE the provider call, lets the caller refuse or split the
    ticket rather than waste a round. The message names the actionable fix
    (reduce region size / split the ticket), not "your ticket is too big."
    """


# Estimated tokens: ~4 chars per token
_CHARS_PER_TOKEN = 4

# Threshold for individual artifacts (Phase 4a). Artifacts above this
# are pruned to truncation markers.
_PER_ARTIFACT_THRESHOLD_CHARS = 5000  # ~1250 tokens


def pin_count(history: List[Dict[str, str]]) -> int:
    """How many leading messages are never compacted.

    history[0] is the system prompt and history[1] is the IMPLEMENT PROMPT --
    the ticket, the spec and the verbatim source of every region. Both are
    load-bearing for every later round, because each round's instruction ends
    "re-emit ALL blocks in full": an implementer that has lost the region text
    cannot comply with the only thing it was asked to do. Compaction used to
    fold history[1] into a one-line summary, so from round 3 the model was
    asked to re-emit blocks it could no longer see.
    """
    if len(history) > 1 and history[1].get("role") == "user":
        return 2
    return 1


def _truncate_middle(content: str) -> str:
    return (
        content[:500]
        + f"\n\n... [COMPACTED: {len(content)} chars -> {len(content) - 1000} chars pruned] ...\n\n"
        + content[-500:]
    )


def compact_history(
    history: List[Dict[str, str]],
    current_round: int,
    profile: Profile,
) -> List[Dict[str, str]]:
    """Compact the implementer history before round N.

    Phase 4a: prune verbose messages from PRIOR rounds above the per-artifact
    threshold. The system prompt, the implement prompt, and the newest exchange
    are kept verbatim.

    Phase 4b: if the pruned history still exceeds round_input_token_budget,
    replace the prior rounds with a single summary -- mechanical first, and an
    LLM summary only if the mechanical one is still too big to fit.

    Admission check: if the pinned head (system + implement prompt) alone
    exceeds the budget, raise CompactionError. No compaction can help -- the
    pinned content is never removable -- and proceeding would send an oversized
    prompt to the provider for a guaranteed context_length error.

    Args:
        history: the full message history (system + alternating user/assistant)
        current_round: the round about to start (1-based)
        profile: the profile (for round_input_token_budget)

    Returns:
        the compacted history (may be shorter than the input)

    Raises:
        CompactionError: if the pinned head alone exceeds the budget.
    """
    if current_round <= 1:
        return history  # nothing to compact on round 1

    pinned = pin_count(history)

    # Admission check: if the pinned head alone exceeds the budget, no
    # compaction can make the prompt fit. The pinned content is never removable
    # (the system prompt and the implement prompt carry the ticket spec and
    # verbatim region source that every round needs). Failing here, before the
    # provider call, lets the caller refuse or split the ticket.
    budget_chars = profile.round_input_token_budget * _CHARS_PER_TOKEN
    pinned_chars = sum(len(history[i]["content"]) for i in range(min(pinned, len(history))))
    if pinned_chars > budget_chars:
        raise CompactionError(
            f"the pinned head (system prompt + implement prompt) is "
            f"{pinned_chars} chars (~{pinned_chars // _CHARS_PER_TOKEN} tokens), "
            f"which alone exceeds the round input budget of "
            f"{profile.round_input_token_budget} tokens "
            f"({budget_chars} chars). No compaction can make this fit -- the "
            f"pinned content is never removable. Reduce the region size or "
            f"split the ticket into smaller regions."
        )

    # The newest exchange is the candidate under revision plus the feedback
    # about it. Truncating that is self-defeating -- it is the text the next
    # round edits -- and the old loop truncated it because it walked every
    # message including the last.
    protected_from = max(pinned, len(history) - 2)

    compacted: List[Dict[str, str]] = list(history[:pinned])
    for i in range(pinned, len(history)):
        msg = history[i]
        content = msg["content"]
        if i >= protected_from or len(content) <= _PER_ARTIFACT_THRESHOLD_CHARS:
            compacted.append(msg)
        elif msg["role"] == "assistant":
            # Verbose implementer output from a prior round: keep the head
            # (the blocks) and the tail (the notes).
            compacted.append({"role": "assistant", "content": _truncate_middle(content)})
        else:
            # Reviewer findings/feedback from a prior round: keep per-finding
            # structure rather than an aggregate count.
            compacted.append({"role": "user", "content": _compact_findings(content)})

    # Phase 4b: check total size against the budget.
    budget_chars = profile.round_input_token_budget * _CHARS_PER_TOKEN
    if _chars(compacted) > budget_chars and current_round > 2:
        # Mechanical summarization first: it is free and deterministic. Only
        # pay for a model call if the free path still does not fit.
        mechanical = _mechanical_summary(compacted, budget_chars)
        if _chars(mechanical) > budget_chars:
            compacted = _llm_summary(compacted, budget_chars, profile) or mechanical
        else:
            compacted = mechanical

    return compacted


def _chars(history: List[Dict[str, str]]) -> int:
    return sum(len(m["content"]) for m in history)


def _llm_summary(
    history: List[Dict[str, str]],
    budget_chars: int,
    profile: Profile,
) -> Optional[List[Dict[str, str]]]:
    """Summarize prior rounds via a compactor model from the registry.

    Returns None if no compactor is available or the call fails (caller
    falls back to _mechanical_summary).
    """
    try:
        from .models import DEFAULT_REGISTRY
        config = DEFAULT_REGISTRY.get("compactor")
    except KeyError:
        return None

    head, prior, last_user, last_assistant = _split_for_summary(history)
    if not prior:
        return None

    body, covered, total = _select_for_summary(prior)
    if not body.strip():
        return None

    coverage = (
        "" if covered == total else
        f"\n\nNOTE: this is {covered} of {total} prior messages -- the oldest "
        f"{total - covered} did not fit and are NOT represented below."
    )
    prompt = (
        "Summarize the following prior rounds of an implementer-reviewer loop. "
        "Focus on what was tried, what findings were raised, and what was rejected. "
        "An approach that was rejected must appear in your summary with the reason, "
        "because the next round will otherwise propose it again. "
        "Be concise (max 500 words).\n\n"
        f"{body}\n\n"
        "Summary:"
    )

    from .providers import chat, ProviderError

    try:
        out = chat(config.name, [
            {"role": "system", "content": "You are a code review summarizer. Be concise."},
            {"role": "user", "content": prompt},
        ], max_tokens=config.max_tokens, think=False)
    except ProviderError as exc:
        # Expected: the caller falls back to the mechanical summary. Say so --
        # this path had never run in a real loop, and a blanket `except
        # Exception: return None` made a working compactor and a broken one
        # look identical from the outside.
        print(f"  [compaction] LLM summary unavailable ({exc}); using mechanical")
        return None
    except Exception as exc:  # noqa: BLE001 - deliberately reported, not hidden
        print(f"  [compaction] LLM summary FAILED ({type(exc).__name__}: {exc}); using mechanical")
        return None

    summary = out.text.strip()
    if not summary:
        print("  [compaction] LLM summary was empty; using mechanical")
        return None
    if len(summary) > budget_chars // 2:
        summary = summary[: budget_chars // 2] + "\n... (summary truncated)"
    label = (
        "[PRIOR ROUNDS SUMMARY (LLM compacted"
        + ("" if covered == total else f", covers {covered}/{total} messages")
        + "):]"
    )
    return _assemble(head, f"{label}\n{summary}", last_user, last_assistant)


def _select_for_summary(prior: List[Dict[str, str]]) -> Tuple[str, int, int]:
    """As much of `prior` as the compactor may read, newest first.

    Returns (rendered text in original order, messages covered, messages total).

    Two rules, both learned from what this replaced -- a flat
    `content[:2000]` per message and `[:20000]` overall, which at the point
    Phase 4b fires showed the model about a tenth of the history:

    * Budget from config, not a literal, and sized above the trigger threshold
      so the ordinary case is summarised whole.
    * When it does not all fit, drop the OLDEST first and keep whole messages.
      Half a finding is worse than no finding: it reads as complete and the
      implementer cannot tell the difference.
    """
    from . import config as _config

    budget = _config.get().loop.compactor_input_token_budget * _CHARS_PER_TOKEN
    chosen: List[Dict[str, str]] = []
    used = 0
    for msg in reversed(prior):
        rendered = f"[{msg['role']}]: {msg['content']}"
        if used + len(rendered) > budget and chosen:
            break
        # A single message larger than the whole budget is the one case where
        # truncating is better than dropping: it is usually the implementer's
        # raw output, and its head carries the approach that was tried.
        if len(rendered) > budget:
            rendered = rendered[:budget] + "\n... (this message truncated)"
        chosen.append({"role": msg["role"], "content": rendered})
        used += len(rendered)
    chosen.reverse()
    return "\n\n".join(m["content"] for m in chosen), len(chosen), len(prior)


def _compact_findings(content: str) -> str:
    """Compact verbose findings/feedback into per-finding summaries.

    Preserves the structure: reviewer name, severity, one-line summary,
    arbiter ruling. Not just aggregate counts.
    """
    # Extract finding lines: "- [BLOCKER|MAJOR|MINOR] ..."
    finding_re = re.compile(r"^-\s*\[(BLOCKER|MAJOR|MINOR)\]\s*(.+?)$", re.MULTILINE)
    findings = finding_re.findall(content)

    if not findings:
        # Not a findings message; just truncate
        return (
            content[:500]
            + f"\n\n... [COMPACTED: {len(content)} chars pruned] ...\n\n"
            + content[-500:]
        )

    # Build a compact summary
    lines = [f"[COMPACTED FINDINGS ({len(findings)} total):]"]
    for severity, text in findings[:10]:  # keep first 10
        # One-line summary: first 80 chars of the finding text
        summary = text[:80] + ("..." if len(text) > 80 else "")
        lines.append(f"- [{severity}] {summary}")
    if len(findings) > 10:
        lines.append(f"... ({len(findings) - 10} more findings pruned)")

    # Preserve the instruction at the end (the "Fix exactly these..." part)
    instruction_re = re.compile(r"(Fix exactly these.*?)(?:$)", re.DOTALL)
    instruction = instruction_re.search(content)
    if instruction:
        lines.append("")
        lines.append(instruction.group(1)[:200])

    return "\n".join(lines)


def _split_for_summary(history):
    """(pinned head, prior rounds, last user message, trailing assistant).

    `prior` is empty when there is nothing between the pinned head and the
    newest exchange, which is the signal not to summarize at all.
    """
    pinned = pin_count(history)
    last_user_idx = None
    for i in range(len(history) - 1, pinned - 1, -1):
        if history[i]["role"] == "user":
            last_user_idx = i
            break
    if last_user_idx is None or last_user_idx < pinned:
        return list(history[:pinned]), [], None, None
    last_assistant = None
    if last_user_idx + 1 < len(history) and history[last_user_idx + 1]["role"] == "assistant":
        last_assistant = history[last_user_idx + 1]
    return (
        list(history[:pinned]),
        list(history[pinned:last_user_idx]),
        history[last_user_idx],
        last_assistant,
    )


def _assemble(head, summary: str, last_user, last_assistant) -> List[Dict[str, str]]:
    """head + summary + newest exchange, keeping roles strictly alternating.

    The summary is an ASSISTANT turn on purpose. Emitting it as a second user
    turn after the pinned implement prompt produced [system, user, user], which
    the Anthropic Messages API rejects with a 400 -- and a 400 is not retried,
    so compaction turned into IMPLEMENTER_UNREACHABLE on that backend.
    """
    result = list(head) + [{"role": "assistant", "content": summary}]
    if last_user is not None:
        result.append(last_user)
    if last_assistant is not None:
        result.append(last_assistant)
    return result


def _mechanical_summary(history: List[Dict[str, str]], budget_chars: int) -> List[Dict[str, str]]:
    """Mechanically summarize the prior rounds.

    Replaces everything between the pinned head (system + implement prompt) and
    the newest exchange with a compact summary of what was tried and rejected.
    """
    head, prior, last_user, last_assistant = _split_for_summary(history)
    if not prior:
        return history

    summary_lines = ["[PRIOR ROUNDS SUMMARY (mechanically compacted):]"]
    for msg in prior:
        if msg["role"] == "assistant":
            first_line = msg["content"].split("\n")[0][:80] if msg["content"] else "(empty)"
            summary_lines.append(f"- Implementer output ({len(msg['content'])} chars): {first_line}...")
        elif msg["role"] == "user":
            finding_count = msg["content"].count("[BLOCKER]") + msg["content"].count("[MAJOR]") + msg["content"].count("[MINOR]")
            if finding_count > 0:
                summary_lines.append(f"- Review feedback ({finding_count} findings)")
            else:
                first_line = msg["content"].split("\n")[0][:80] if msg["content"] else "(empty)"
                summary_lines.append(f"- Feedback: {first_line}...")

    summary = "\n".join(summary_lines)
    if len(summary) > budget_chars // 2:
        summary = summary[: budget_chars // 2] + "\n... (summary truncated)"

    return _assemble(head, summary, last_user, last_assistant)


def estimate_tokens(text: str) -> int:
    """Estimate the token count of a text string."""
    return len(text) // _CHARS_PER_TOKEN


def history_token_count(history: List[Dict[str, str]]) -> int:
    """Estimate the total token count of the history."""
    return sum(estimate_tokens(m["content"]) for m in history)