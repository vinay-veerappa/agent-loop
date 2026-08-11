"""
plan_mode.py
============
Plan mode: input is a defect description, output is a ticket JSON with
regions and acceptance test names. The LLM uses the graph (passive
injection, phase 3) to localize the defect and propose regions.

The output is reviewed by the panel + arbiter for completeness before
being promoted to a ticket file.

Phase 6 of the execution plan.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import arbiter as arbiter_mod
from . import config
from . import gates, profiles, regions, workspace
from .context import build_context_slice
from .memory import inject_settled
from .providers import Completion, ProviderError, chat


PLAN_SYSTEM = """You are a senior software engineer planning a fix for a defect.
Your job is to analyze the defect, localize it in the codebase using the
context provided, and produce a ticket JSON that describes the fix.

OUTPUT FORMAT - obey exactly:
<<<TICKET>>>
{{
  "id": "TICKET_ID",
  "title": "short title",
  "defect": "what is wrong",
  "spec": "what the fix should do",
  "regions": [
    {{"id": "REGION_ID", "file": "path/to/file.py", "anchor": "unique anchor string in the file"}}
  ],
  "expect_green": ["test_name_that_should_pass_after_fix"]
}}
<<<END TICKET>>>
<<<NOTES>>>
- why these regions, why these tests
<<<END NOTES>>>
"""


def run_plan(
    repo: Path,
    defect_description: str,
    profile: profiles.Profile,
    implementer: str,
    reviewers: Sequence[str],
    arbiter_model: str = "",
    max_rounds: int = 4,
    fast_plan: bool = False,
) -> Dict[str, Any]:
    """Run plan mode: defect -> ticket JSON (reviewed by panel+arbiter).

    Args:
        repo: the repo root
        defect_description: the defect to analyze
        profile: the language profile
        implementer: the model to use for planning
        reviewers: the panel models
        arbiter_model: the arbiter model (empty = skip arbitration)
        max_rounds: max revision rounds
        fast_plan: if True, use a single reviewer instead of the full panel

    Returns:
        a result dict with the final ticket JSON and verdict
    """
    tid = "PLAN"
    art = repo / "logs" / "agent_loop" / tid
    art.mkdir(parents=True, exist_ok=True)

    result: Dict[str, Any] = {"ticket": tid, "rounds": [], "plan": None, "verdict": ""}

    # Phase 5: inject auto-extracted settled decisions
    effective_settled = inject_settled(profile.settled, repo)

    prompt = f"# Defect to analyze\n\n{defect_description}\n\n"
    prompt += "## Context\n"
    prompt += f"Language: {profile.language}\n"
    prompt += f"File suffixes: {', '.join(profile.file_suffixes)}\n"
    prompt += f"Build: {profile.build_cmd or '(none)'}\n"
    prompt += f"Test: {profile.test_cmd or '(none)'}\n"

    history = [
        {"role": "system", "content": PLAN_SYSTEM},
        {"role": "user", "content": prompt},
    ]

    final = "MAX_ROUNDS_EXHAUSTED"

    for rnd in range(1, max_rounds + 1):
        try:
            # Phase 4: compact history before the implementer call
            if rnd > 1:
                from .compaction import compact_history, history_token_count
                before = history_token_count(history)
                history = compact_history(history, rnd, profile)
                after = history_token_count(history)
                if after < before:
                    print(f"           [compaction] {before} -> {after} tokens")
            _c = config.get().mode("plan")
            out = chat(implementer, history, max_tokens=_c.max_tokens, think=_c.think)
        except ProviderError as exc:
            result["rounds"].append({"round": rnd, "error": str(exc)})
            final = "IMPLEMENTER_UNREACHABLE"
            break

        raw = out.text
        (art / f"r{rnd}_plan_raw.txt").write_text(raw, encoding="utf-8")
        print(f"  round {rnd}: plan {out.usage_line()}")

        # Parse the ticket JSON from the response
        ticket = _parse_ticket(raw)
        if not ticket:
            history += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": "Your output did not contain a parseable <<<TICKET>>> block. Re-emit with the correct format."},
            ]
            continue

        # Verify regions resolve against the current tree
        try:
            regs = regions.extract(repo, ticket.get("regions", []), profile)
            print(f"           regions: {len(regs)} resolved OK")
        except regions.RegionError as exc:
            history += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"Region extraction failed: {exc}. Fix the anchors and re-emit."},
            ]
            continue

        # If fast_plan: skip panel+arbiter, just accept
        if fast_plan:
            result["plan"] = ticket
            result["verdict"] = "APPROVE"
            final = "APPROVE"
            print("           [fast-plan] accepted without panel review")
            break

        # Panel review of the plan
        from .loop import review_panel, PanelResult
        review_prompt = (
            f"# Plan review for defect: {defect_description[:200]}\n\n"
            f"## Proposed ticket\n```json\n{json.dumps(ticket, indent=2)}\n```\n\n"
            f"## Resolved regions\n" + "\n".join(f"- {r.id}: {r.file} lines {r.lines_1based}" for r in regs) + "\n\n"
            "Review this plan: are the regions correct? Are the acceptance tests adequate?\n"
        )
        panel = review_panel(
            reviewers, review_prompt, profile.reviewer_system, art, rnd,
            deadline_secs=config.get().loop.panel_deadline_secs,
        )
        desc = ", ".join(f"{v.model.split(':')[0]}={v.status}({v.blockers})" for v in panel.votes)
        print(f"           [panel] {panel.verdict or 'INVALID'}  [{desc}]")

        if panel.unanimous_approve:
            result["plan"] = ticket
            result["verdict"] = "APPROVE"
            final = "APPROVE"
            break

        if not panel.valid:
            final = "PANEL_UNREACHABLE"
            break

        # Arbiter
        if arbiter_model:
            from .loop import Finding
            all_findings = [f for v in panel.votes if v.counted for f in v.finding_list]
            adj = arbiter_mod.adjudicate(
                arbiter_model, ticket, all_findings,
                "plan review", "", settled=effective_settled,
                rules=profile.arbiter_rules,
            )
            (art / f"r{rnd}_arbiter.txt").write_text(adj.raw or adj.error, encoding="utf-8")
            if adj.ok:
                print(f"           [arbiter] {adj.summary()}")
                if adj.recommendation == arbiter_mod.SHIP:
                    result["plan"] = ticket
                    result["verdict"] = "ARBITER_SHIP"
                    final = "ARBITER_SHIP"
                    break
                if adj.recommendation == arbiter_mod.ESCALATE:
                    final = "ESCALATED"
                    break

            feedback = panel.findings
            history += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"Plan review returned {panel.verdict}.\n\nFINDINGS:\n{feedback}\n\nFix the plan and re-emit."},
            ]
        else:
            feedback = panel.findings
            history += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"Plan review returned {panel.verdict}.\n\nFINDINGS:\n{feedback}\n\nFix the plan and re-emit."},
            ]

    result["verdict"] = final
    (art / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    if result["plan"]:
        (art / "plan.json").write_text(json.dumps(result["plan"], indent=2), encoding="utf-8")
        print(f"  PLAN -> {art / 'plan.json'}")
    return result


def _parse_ticket(raw: str) -> Optional[Dict[str, Any]]:
    """Parse a <<<TICKET>>> block from the raw response."""
    m = re.search(r"<<<TICKET>>>\s*(\{.*?\})\s*<<<END\s*TICKET>>>", raw, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None