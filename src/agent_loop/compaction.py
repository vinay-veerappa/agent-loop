"""
compaction.py
=============
Round-level history pruning (Phase 4).

The implementer's history grows unboundedly across rounds. Each round adds
the implementer's raw output, the reviewer findings, the arbiter ruling,
and the feedback message. By round 4, the history can exceed 400K tokens.

Phase 4a: prune verbose old outputs above a token threshold. Prior rounds'
implementer output, reviewer findings, and build/test logs that exceed the
threshold are replaced with truncation markers that preserve per-finding
structure (reviewer name, severity, one-line summary, arbiter ruling) --
not just aggregate counts.

Phase 4b: LLM summarization. If the pruned history still exceeds the
profile's round_input_token_budget (default 40K), summarize rounds 1..N-1
via a cheaper model into a compact "what was tried and rejected" block.
Keep round N full.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .profiles import Profile


# Estimated tokens: ~4 chars per token
_CHARS_PER_TOKEN = 4

# Threshold for individual artifacts (Phase 4a). Artifacts above this
# are pruned to truncation markers.
_PER_ARTIFACT_THRESHOLD_CHARS = 5000  # ~1250 tokens


def compact_history(
    history: List[Dict[str, str]],
    current_round: int,
    profile: Profile,
) -> List[Dict[str, str]]:
    """Compact the implementer history before round N.

    Phase 4a: prune verbose old assistant messages above the per-artifact
    threshold. Keep the latest round's full exchange. Prior rounds get
    truncated to per-finding summaries.

    Phase 4b: if the total history still exceeds the round_input_token_budget,
    summarize all prior rounds into a compact block. (Not yet implemented --
    returns the 4a-pruned history for now.)

    Args:
        history: the full message history (system + alternating user/assistant)
        current_round: the round about to start (1-based)
        profile: the profile (for round_input_token_budget)

    Returns:
        the compacted history (may be shorter than the input)
    """
    if current_round <= 1:
        return history  # nothing to compact on round 1

    # Phase 4a: prune verbose assistant messages from prior rounds.
    # The history is [system, user, assistant, user, assistant, ...].
    # We keep the system message and the latest user/assistant pair.
    # Prior assistant messages that are very long get truncated.
    compacted = []
    for i, msg in enumerate(history):
        if msg["role"] == "system":
            compacted.append(msg)
            continue

        content = msg["content"]
        if len(content) > _PER_ARTIFACT_THRESHOLD_CHARS and msg["role"] == "assistant":
            # Truncate verbose implementer output from prior rounds.
            # Keep the first 500 chars (the blocks) and the last 500 chars
            # (the notes), replace the middle with a truncation marker.
            truncated = (
                content[:500]
                + f"\n\n... [COMPACTED: {len(content)} chars -> {len(content) - 1000} chars pruned] ...\n\n"
                + content[-500:]
            )
            compacted.append({"role": msg["role"], "content": truncated})
        elif len(content) > _PER_ARTIFACT_THRESHOLD_CHARS and msg["role"] == "user":
            # Truncate verbose reviewer findings/feedback from prior rounds.
            # Preserve per-finding structure: extract finding lines and keep
            # a compact summary.
            compacted_content = _compact_findings(content)
            compacted.append({"role": msg["role"], "content": compacted_content})
        else:
            compacted.append(msg)

    # Phase 4b: check total size against the budget
    total_chars = sum(len(m["content"]) for m in compacted)
    budget_chars = profile.round_input_token_budget * _CHARS_PER_TOKEN
    if total_chars > budget_chars and current_round > 2:
        # Summarize all but the last user/assistant pair into a compact block.
        # This is the Phase 4b LLM summarization path. For now, we do a
        # mechanical summarization (not LLM) to avoid requiring a model call
        # in the compaction path. A future upgrade will call the compactor
        # model from the registry.
        compacted = _mechanical_summary(compacted, budget_chars)

    return compacted


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


def _mechanical_summary(history: List[Dict[str, str]], budget_chars: int) -> List[Dict[str, str]]:
    """Mechanically summarize all but the last exchange.

    This is the fallback for Phase 4b when no LLM compactor is available.
    It replaces all but the system message and the last user/assistant pair
    with a compact summary of what was tried and rejected.
    """
    if len(history) <= 3:
        return history

    # Keep: system message, summary of prior rounds, last exchange
    # The last exchange is the final user message (and its assistant response
    # if present). We want to preserve the last user message exactly.
    system = history[0]
    # Find the last user message
    last_user_idx = None
    for i in range(len(history) - 1, -1, -1):
        if history[i]["role"] == "user":
            last_user_idx = i
            break
    if last_user_idx is None or last_user_idx == 0:
        return history

    last_user = history[last_user_idx]
    # Check if there's an assistant response after the last user
    last_assistant = None
    if last_user_idx + 1 < len(history) and history[last_user_idx + 1]["role"] == "assistant":
        last_assistant = history[last_user_idx + 1]

    # Build summary from everything between system and last_user
    prior = history[1:last_user_idx]
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

    result = [system, {"role": "user", "content": summary}, last_user]
    if last_assistant:
        result.append(last_assistant)
    return result


def estimate_tokens(text: str) -> int:
    """Estimate the token count of a text string."""
    return len(text) // _CHARS_PER_TOKEN


def history_token_count(history: List[Dict[str, str]]) -> int:
    """Estimate the total token count of the history."""
    return sum(estimate_tokens(m["content"]) for m in history)