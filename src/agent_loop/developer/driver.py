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

from .. import arbiter, gates, profiles, regions, workspace
from ..providers import Completion, ProviderError, chat
from .tools import TOOL_SCHEMAS, execute_tool


DEVELOPER_SYSTEM = """You are an autonomous software engineer fixing a defect in a codebase.
You have tools to read files, search code, trace call paths, edit files, and run builds/tests.

WORK IN TWO PHASES:
1. EXPLORE: Use read_file, search_code, and trace_call_path to understand the defect.
   Find the files and functions that need to change. Do NOT edit yet.
2. EDIT: Use edit_file to make changes. Use run_build and run_tests to verify.
   If the build or tests fail, fix the issue and try again.

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
EDIT_TOOLS = ["edit_file", "run_build", "run_tests"]


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

    history = [
        {"role": "system", "content": DEVELOPER_SYSTEM},
        {"role": "user", "content": f"# Defect to fix\n\n{defect_description}\n\n"
         f"Language: {profile.language}\n"
         f"File scope: {', '.join(profile.file_scope_whitelist) or '(all)'}\n"
         f"Build: {profile.build_cmd or '(none)'}\n"
         f"Test: {profile.test_cmd or '(none)'}\n\n"
         f"Start by exploring the codebase to find the code that needs to change."},
    ]

    # The tool-calling loop
    phase = "explore"  # "explore" or "edit"
    available_tools = EXPLORE_TOOLS if phase == "explore" else EDIT_TOOLS

    for turn in range(1, max_turns + 1):
        try:
            out = chat(implementer, history, max_tokens=16000)
        except ProviderError as exc:
            result["verdict"] = "IMPLEMENTER_UNREACHABLE"
            result["error"] = str(exc)
            break

        raw = out.text
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

        # Check for tool calls in the response
        tool_calls = _parse_tool_calls(raw, available_tools)
        if not tool_calls:
            # No tool calls and not done -- ask the LLM to continue
            history += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": "Continue. Use your tools to explore and fix the defect. When done, emit <<<DONE>>>."},
            ]
            continue

        # Execute tool calls
        tool_results = []
        for tc in tool_calls:
            print(f"           [{tc['name']}] {tc.get('args', {})}")
            tc_result = execute_tool(tc["name"], tc.get("args", {}), repo, profile)
            # Check file scope for edit_file
            if tc["name"] == "edit_file":
                path = tc.get("args", {}).get("path", "")
                if not _check_file_scope(path, profile):
                    tc_result = f"ERROR: file {path} is outside the allowed scope ({', '.join(profile.file_scope_whitelist) or 'all'})"
                    print(f"           [scope] REJECTED: {path}")
            tool_results.append(f"[{tc['name']} result]: {tc_result[:2000]}")

            # Transition from explore to edit phase when edit_file is first called
            if tc["name"] == "edit_file" and phase == "explore":
                phase = "edit"
                available_tools = EDIT_TOOLS
                print(f"           [phase] explore -> edit")

        history += [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": "\n\n".join(tool_results)},
        ]

    # After the loop: run gate ladder on the diff
    if result["verdict"] == "DONE":
        # Build the diff
        import subprocess
        diff_proc = subprocess.run(
            ["git", "diff"], cwd=str(repo), capture_output=True, text=True
        )
        if diff_proc.stdout.strip():
            patch_path = art / "final.patch"
            patch_path.write_text(diff_proc.stdout, encoding="utf-8")
            result["patch"] = str(patch_path)
            print(f"  patch: {patch_path}")

            # Run the gate ladder
            if profile.build_cmd:
                gc = gates.check_compile(profile.build_cmd, repo)
                print(f"  [compile] {'ok' if gc.ok else 'FAIL'} - {gc.summary}")
                if not gc.ok:
                    result["verdict"] = "BUILD_FAILED"
            if profile.test_cmd and result["verdict"] == "DONE":
                gt, outcome = gates.check_tests(
                    profile.test_cmd, repo, set(),  # no baseline in developer mode
                )
                print(f"  [test] {'ok' if gt.ok else 'FAIL'} - {gt.summary}")
                if not gt.ok:
                    result["verdict"] = "TEST_FAILED"

    (art / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def _parse_tool_calls(raw: str, available_tools: List[str]) -> List[Dict[str, Any]]:
    """Parse tool calls from the LLM response.

    The LLM emits tool calls in a simple format:
    <<<TOOL name="tool_name">>>
    {"arg1": "value1", "arg2": "value2"}
    <<<END TOOL>>>
    """
    calls = []
    for m in re.finditer(
        r'<<<TOOL\s+name="(?P<name>\w+)">>>\r?\n(?P<args>.*?)<<<END\s*TOOL>>>',
        raw, re.DOTALL,
    ):
        name = m.group("name")
        if name not in available_tools:
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