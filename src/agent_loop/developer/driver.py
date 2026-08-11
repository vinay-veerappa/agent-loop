"""
developer/driver.py
===================
Developer mode: the autonomous localization+edit path (Phase 8).

Input: a defect description (no pre-declared regions).
The LLM localizes by querying the graph + tools, then edits, then
the same gate ladder + panel + arbiter reviews the result.

Control flow (AutoCodeRover pattern):
  defect -> explore phase (read-only tools) -> edit phase (edit + build + test)
        -> gate ladder -> panel -> arbiter

The explore phase is read-only (search_code, trace_call_path, read_file).
The edit phase has no search tools (edit_file, run_build, run_tests).
This phase separation prevents the LLM from editing before it understands.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .. import arbiter, config, gates, profiles, regions, workspace
from ..providers import Completion, ProviderError, chat
from .tools import execute_tool, render_tool_docs


DEVELOPER_SYSTEM = """You are an autonomous software engineer fixing a defect in a codebase.

WORK IN TWO PHASES:
1. EXPLORE: Use read_file, search_code, and trace_call_path to understand the defect.
   Find the files and functions that need to change. Do NOT edit yet.
2. EDIT: Use edit_file to make changes. Use run_build and run_tests to verify.
   If the build or tests fail, fix the issue and try again.
   Calling edit_file moves you into the EDIT phase; search_code and
   trace_call_path are no longer available after that, so finish exploring first.

You may not edit the tests or the build files that grade your change. If you
believe a test is wrong, say so in your summary rather than editing it.

When you are done, emit:
<<<DONE>>>
summary of what you changed and why
<<<END DONE>>>

If you cannot fix the defect, emit:
<<<ESCALATE>>>
why you could not fix it
<<<END ESCALATE>>>
"""

EXPLORE_TOOLS = ["read_file", "search_code", "trace_call_path"]
# read_file stays available while editing: verifying the text you just wrote is
# not exploration, and withdrawing it forces the model to edit blind.
EDIT_TOOLS = ["read_file", "edit_file", "run_build", "run_tests"]


def run_developer(
    repo: Path,
    defect_description: str,
    profile: profiles.Profile,
    implementer: str,
    reviewers: Sequence[str],
    arbiter_model: str = "",
    max_turns: int = 30,
    apply: bool = False,
) -> Dict[str, Any]:
    """Run Developer mode: defect -> patched diff (autonomous localization + edit).

    Args:
        repo: the repo root
        defect_description: the defect to fix
        profile: the language profile
        implementer: the model to use
        reviewers: the panel models
        arbiter_model: the arbiter model
        max_turns: max tool-calling turns
        apply: if True, promote the patch to the live tree

    Returns:
        a result dict with the final verdict
    """
    tid = "DEV"
    art = repo / "logs" / "agent_loop" / tid
    art.mkdir(parents=True, exist_ok=True)

    result: Dict[str, Any] = {"ticket": tid, "turns": [], "verdict": "", "patch": None}

    # Developer mode used to edit the LIVE tree: it took no run lock, captured
    # no baseline, and read `git diff` from the repo -- so it destroyed
    # uncommitted work, treated every pre-existing failure as its own
    # regression, and swept the user's unrelated edits into the patch it asked
    # reviewers to approve. It gets the same disposable worktree as patch mode.
    with workspace.open_workspace(repo, tid) as ws:
        print(f"  [worktree] {ws.root.name} @ {ws.base_commit[:8]}")
        if profile.test_cmd:
            try:
                workspace.capture_baseline(ws, profile.test_cmd, gates.parse_tests)
                print(f"  [baseline] {ws.baseline_note}; {len(ws.baseline)} expected failure(s)")
            except workspace.WorkspaceError as exc:
                # No baseline means no regression check. Refuse rather than
                # measure the patch against an empty set and call it clean.
                result["verdict"] = "TEST_BASELINE_UNAVAILABLE"
                result["error"] = str(exc)
                print(f"  REFUSED: {exc}")
                (art / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
                return result
        return _run_turns(
            ws, art, tid, defect_description, profile, implementer, reviewers,
            arbiter_model, max_turns, apply, result,
        )


def _run_turns(
    ws: "workspace.Workspace",
    art: Path,
    tid: str,
    defect_description: str,
    profile: profiles.Profile,
    implementer: str,
    reviewers: Sequence[str],
    arbiter_model: str,
    max_turns: int,
    apply: bool,
    result: Dict[str, Any],
) -> Dict[str, Any]:
    """The tool-calling loop, inside the worktree."""
    repo = ws.root
    edited: List[str] = []

    # The tool-calling loop
    phase = "explore"  # "explore" or "edit"
    available_tools = list(EXPLORE_TOOLS)

    history = [
        {"role": "system", "content": DEVELOPER_SYSTEM + "\n" + render_tool_docs(
            sorted(set(EXPLORE_TOOLS + ["edit_file"]))
        )},
        {"role": "user", "content": f"# Defect to fix\n\n{defect_description}\n\n"
         f"Language: {profile.language}\n"
         f"File scope: {', '.join(profile.file_scope_whitelist) or '(all)'}\n"
         f"Build: {profile.build_cmd or '(none)'}\n"
         f"Test: {profile.test_cmd or '(none)'}\n\n"
         f"Start by exploring the codebase to find the code that needs to change."},
    ]

    for turn in range(1, max_turns + 1):
        try:
            # Multi-turn tool loop: the defect prompt at turns[0] never changes,
            # so it is worth a cache breakpoint across turns. See providers.
            _c = config.get().mode("developer")
            out = chat(implementer, history, max_tokens=_c.max_tokens, think=_c.think, cache=True)
        except ProviderError as exc:
            result["verdict"] = "IMPLEMENTER_UNREACHABLE"
            result["error"] = str(exc)
            break

        # A model may answer with a native `tool_calls` array rather than the
        # text protocol below, and `content` is empty by design when it does.
        # Before this was handled the turn looked blank: with think=True the
        # empty content raised out of the provider as a bogus budget error, and
        # with think=False the loop said "Continue" to a model that had already
        # answered, until it ran out of turns.
        #
        # The native calls are rendered into `raw` so the artifact and the
        # history turn record what the model actually did -- but they are
        # DISPATCHED from out.tool_calls directly, never re-parsed out of the
        # rendered text. A name the text protocol cannot express (it matches
        # \w+, so a namespaced `functions.read_file` would not survive) would
        # otherwise vanish on the way back in, which is this same defect again.
        text_out = out.text
        raw = text_out
        if out.tool_calls:
            raw = "\n".join([text_out.strip(), _render_tool_calls(out.tool_calls)]).strip()
        (art / f"turn{turn}_raw.txt").write_text(raw, encoding="utf-8")
        print(f"  turn {turn}: {out.usage_line()}")

        # Check for completion
        if "<<<DONE>>>" in raw:
            result["verdict"] = "DONE"
            # Extract the summary
            m = re.search(r"<<<DONE>>>\s*(.*?)<<<END\s*DONE>>>", raw, re.DOTALL)
            if m:
                result["summary"] = m.group(1).strip()
            break

        if "<<<ESCALATE>>>" in raw:
            result["verdict"] = "ESCALATED"
            m = re.search(r"<<<ESCALATE>>>\s*(.*?)<<<END\s*ESCALATE>>>", raw, re.DOTALL)
            if m:
                result["summary"] = m.group(1).strip()
            break

        # edit_file is offered in both phases: calling it is what moves the
        # model from explore to edit.
        offered = sorted(set(available_tools + ["edit_file"]))
        # Text-protocol calls come from the model's own text; native calls come
        # from out.tool_calls. Parsing `raw` here instead would double-count
        # every native call, because `raw` already contains their rendering.
        found = _parse_tool_calls(text_out, None) + list(out.tool_calls)
        tool_calls = [c for c in found if c["name"] in offered]
        # A call to a tool this phase does not offer must be ANSWERED, not
        # dropped. Silently discarding it looked identical to "the model made
        # no tool calls", so the model was told to continue with no hint that
        # its request had been refused, and could repeat it until max_turns.
        rejected = [c["name"] for c in found if c["name"] not in offered]
        if not tool_calls and not rejected:
            # No tool calls and not done -- ask the LLM to continue
            history += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": "Continue. Use your tools to explore and fix the defect. When done, emit <<<DONE>>>."},
            ]
            continue

        # Execute tool calls
        tool_results = []
        for name in rejected:
            print(f"           [{name}] REFUSED (not available in {phase} phase)")
            tool_results.append(
                f"[{name} result]: ERROR: {name} is not available in the {phase} phase. "
                f"Available now: {', '.join(offered)}."
            )
        for tc in tool_calls:
            print(f"           [{tc['name']}] {tc.get('args', {})}")
            # Check file scope for edit_file BEFORE executing (the edit is
            # irreversible once applied; the scope check must prevent it).
            if tc["name"] == "edit_file":
                path = tc.get("args", {}).get("path", "")
                if not _check_file_scope(path, profile):
                    tc_result = f"ERROR: file {path} is outside the allowed scope ({', '.join(profile.file_scope_whitelist) or 'all'})"
                    print(f"           [scope] REJECTED: {path}")
                    tool_results.append(f"[{tc['name']} result]: {tc_result[:2000]}")
                    # Transition phase even on rejection (the LLM tried to edit)
                    if phase == "explore":
                        phase = "edit"
                        available_tools = list(EDIT_TOOLS)
                        print(f"           [phase] explore -> edit")
                    continue
            tc_result = execute_tool(tc["name"], tc.get("args", {}), repo, profile, edited)
            tool_results.append(f"[{tc['name']} result]: {tc_result[:2000]}")

            if tc["name"] == "edit_file":
                if tc_result.startswith("OK"):
                    path = tc.get("args", {}).get("path", "")
                    if path and path not in edited:
                        edited.append(path)
                # Transition from explore to edit phase on the first edit call
                if phase == "explore":
                    phase = "edit"
                    available_tools = list(EDIT_TOOLS)
                    print(f"           [phase] explore -> edit")

        history += [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": "\n\n".join(tool_results)},
        ]

    # After the loop: run gate ladder on the diff
    if result["verdict"] == "DONE":
        diff = ws.diff()
        result["edited"] = list(edited)
        if diff.strip():
            patch_path = art / "final.patch"
            patch_path.write_text(diff, encoding="utf-8")
            result["patch"] = str(patch_path)
            print(f"  patch: {patch_path}")

            # Run the gate ladder
            if profile.build_cmd:
                gc = gates.check_compile(profile.build_cmd, repo, files=edited)
                print(f"  [compile] {'ok' if gc.ok else 'FAIL'} - {gc.summary}")
                if not gc.ok:
                    result["verdict"] = "BUILD_FAILED"
            if profile.test_cmd and result["verdict"] == "DONE":
                # Against the FROZEN baseline captured before any edit, not an
                # empty set: with set() every test that was already red counted
                # as a regression this patch caused.
                gt, outcome = gates.check_tests(
                    profile.test_cmd, repo, ws.baseline,
                )
                print(f"  [test] {'ok' if gt.ok else 'FAIL'} - {gt.summary}")
                if not gt.ok:
                    result["verdict"] = "TEST_FAILED"

            # Run the panel + arbiter on the diff (same moat as patch mode)
            if result["verdict"] == "DONE" and reviewers:
                from ..loop import review_panel, PanelResult
                review_prompt = (
                    f"# Developer mode review\n\n"
                    f"## Defect\n{defect_description[:500]}\n\n"
                    f"## Diff\n```diff\n{diff[:60000]}\n```\n\n"
                    f"Review this diff: does it close the defect? Does it introduce new issues?\n"
                )
                panel = review_panel(
                    reviewers, review_prompt, profile.reviewer_system, art, 1,
                    deadline_secs=config.get().loop.panel_deadline_secs,
                )
                desc = ", ".join(f"{v.model.split(':')[0]}={v.status}({v.blockers})" for v in panel.votes)
                print(f"  [panel] {panel.verdict or 'INVALID'}  [{desc}]")

                if not panel.valid:
                    result["verdict"] = "PANEL_UNREACHABLE"
                elif panel.unanimous_approve:
                    result["verdict"] = "APPROVE"
                    print("  panel unanimously approved")
                elif arbiter_model and panel.votes:
                    all_findings = [f for v in panel.votes if v.counted for f in v.finding_list]
                    if all_findings:
                        adj = arbiter.adjudicate(
                            arbiter_model, {"id": tid, "title": defect_description[:100],
                                           "defect": defect_description, "spec": ""},
                            all_findings, "developer mode", diff,
                            settled=profile.settled,
                            rules=profile.arbiter_rules,
                        )
                        (art / "arbiter.txt").write_text(adj.raw or adj.error, encoding="utf-8")
                        if adj.ok:
                            print(f"  [arbiter] {adj.summary()}")
                            if adj.recommendation == arbiter.SHIP:
                                result["verdict"] = "ARBITER_SHIP"
                            elif adj.recommendation == arbiter.ESCALATE:
                                result["verdict"] = "ESCALATED"
                        else:
                            result["verdict"] = "ARBITER_DEADLOCK"
                    else:
                        result["verdict"] = "ARBITER_SHIP"
                else:
                    result["verdict"] = "NEEDS_REVISION"

            # --apply was accepted and then ignored, so a caller who asked for
            # promotion silently got none -- while the edits happened to be live
            # anyway, in the tree they had not asked the loop to touch.
            if apply and result["verdict"] in ("APPROVE", "ARBITER_SHIP", "DONE"):
                moved = ws.promote(edited)
                result["applied"] = True
                result["applied_approved"] = result["verdict"] == "APPROVE"
                result["touched"] = moved
                print(f"  APPLIED -> {', '.join(moved)}")
                print("  review with `git diff` and commit explicit paths; nothing is staged.")
            elif apply:
                print(f"  NOT APPLIED: verdict={result['verdict']}")
            elif result["patch"]:
                print(f"  not applied (no --apply). Patch: {result['patch']}")

    (art / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def _render_tool_calls(calls: List[Dict[str, Any]]) -> str:
    """Render normalised tool calls into the text protocol `_parse_tool_calls`
    reads, so a native tool call and a text one are indistinguishable
    downstream. Inverse of `_parse_tool_calls`."""
    return "\n".join(
        f'<<<TOOL name="{c["name"]}">>>\n'
        f'{json.dumps(c.get("args", {}))}\n'
        f"<<<END TOOL>>>"
        for c in calls
    )


def _parse_tool_calls(
    raw: str, available_tools: Optional[List[str]]
) -> List[Dict[str, Any]]:
    """Parse tool calls from the LLM response.

    The LLM emits tool calls in a simple format:
    <<<TOOL name="tool_name">>>
    {"arg1": "value1", "arg2": "value2"}
    <<<END TOOL>>>

    `available_tools=None` returns every call found, so the caller can tell
    "asked for a tool it may not use here" apart from "made no tool call".
    """
    calls = []
    for m in re.finditer(
        r'<<<TOOL\s+name="(?P<name>\w+)">>>\r?\n(?P<args>.*?)<<<END\s*TOOL>>>',
        raw, re.DOTALL,
    ):
        name = m.group("name")
        if available_tools is not None and name not in available_tools:
            continue
        try:
            args = json.loads(m.group("args"))
        except json.JSONDecodeError:
            args = {}
        calls.append({"name": name, "args": args})
    return calls


def _check_file_scope(path: str, profile: profiles.Profile) -> bool:
    """Check if a file is within the profile's allowed scope."""
    if not profile.file_scope_whitelist:
        return True  # no restriction
    norm = path.replace("\\", "/")
    for allowed in profile.file_scope_whitelist:
        if norm.startswith(allowed):
            return True
    return False