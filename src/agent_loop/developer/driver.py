"""
developer/driver.py
===================
Developer mode: the autonomous localization+edit path (Phase 8).

Input: a defect description (no pre-declared regions).
The LLM localizes by querying the graph + tools, then edits, then
the same gate ladder + panel + arbiter reviews the result.

Control flow (AutoCodeRover pattern, test-first):
  defect -> red phase (read-only tools + write_test)
        -> explore phase (read-only tools) -> edit phase (edit + build + test)
        -> gate ladder -> panel -> arbiter

The red phase is where the run earns the right to edit: the model must write a
test that FAILS against the current code, which is what gives the gate ladder
something it can refuse with. Without it the ladder can only ask "does this
compile and break nothing?", which a patch that fixes nothing also satisfies.
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

WORK IN THREE PHASES, IN ORDER:
1. RED: Use read_file, search_code and trace_call_path to understand the defect,
   then use write_test to write a test that FAILS against the current code.
   The test is run immediately. If it passes, it does not test the defect --
   you will be told so, and you must write a better one. You CANNOT edit source
   until a test fails. Assert on the defect's observable effect, not on the
   implementation you are about to write.
2. EXPLORE: Once your test is red, use read_file, search_code and
   trace_call_path to find what must change. Do NOT edit yet.
3. EDIT: Use edit_file to make changes. Use run_build and run_tests to verify.
   Calling edit_file moves you into the EDIT phase; search_code and
   trace_call_path are no longer available after that, so finish exploring first.

Your test becomes read-only the moment it goes red. You may not edit it, any
other test, or the build files that grade your change. If you believe a test is
wrong, say so in your summary rather than editing it. Your change is accepted
only if your failing test passes AND nothing that passed before now fails.

run_build and run_tests take NO arguments -- they run the commands this project
is configured with. If you want to check something the suite does not cover,
that is a test, not a command.

When you are done, emit:
<<<DONE>>>
summary of what you changed and why
<<<END DONE>>>

If you cannot fix the defect, emit:
<<<ESCALATE>>>
why you could not fix it
<<<END ESCALATE>>>
"""

# How much of a tool's output the model gets back. 2000 chars for everything
# was too tight in both directions: a 100-line read_file window is routinely
# longer than that, so the model silently received about half of what it asked
# for and re-read overlapping ranges to compensate; and the test/build
# diagnostics that decide the next edit were cut off entirely.
TOOL_RESULT_LIMIT = 4000
DIAGNOSTIC_RESULT_LIMIT = 8000
_DIAGNOSTIC_TOOLS = {"run_tests", "run_build", "write_test"}


def _result_limit(tool_name: str) -> int:
    return DIAGNOSTIC_RESULT_LIMIT if tool_name in _DIAGNOSTIC_TOOLS else TOOL_RESULT_LIMIT


# The red phase can look around, so the test is written against real code rather
# than a guess, but it cannot edit: that is the whole point of the phase.
RED_TOOLS = ["read_file", "search_code", "trace_call_path", "write_test"]
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

    # TDD is the default. Without a test that fails FIRST, the gate ladder has
    # nothing to refuse with: it can only check that the patch compiles and
    # breaks nothing, which a patch that fixes nothing also satisfies.
    tdd = config.get().mode("developer").require_failing_test and bool(profile.test_cmd)
    # "red" -> "explore" -> "edit". Without TDD the run starts where it always
    # did, so an unconfigured profile behaves as before.
    phase = "red" if tdd else "explore"
    available_tools = list(RED_TOOLS if tdd else EXPLORE_TOOLS)
    # Test names that were red at the end of the red phase. They are added to
    # the frozen baseline (so they are not counted as regressions) and required
    # green at the end (so the patch cannot pass without closing the defect).
    acceptance: List[str] = []
    locked_tests: set = set()
    result["tdd"] = tdd

    offerable = sorted(set(RED_TOOLS + EXPLORE_TOOLS + EDIT_TOOLS)) if tdd else \
        sorted(set(EXPLORE_TOOLS + ["edit_file"]))
    history = [
        {"role": "system", "content": DEVELOPER_SYSTEM + "\n" + render_tool_docs(offerable)},
        {"role": "user", "content": f"# Defect to fix\n\n{defect_description}\n\n"
         f"Language: {profile.language}\n"
         f"File scope: {', '.join(profile.file_scope_whitelist) or '(all)'}\n"
         f"Test location: {', '.join(profile.test_sources) or '(none declared)'}\n"
         f"Build: {profile.build_cmd or '(none)'}\n"
         f"Test: {profile.test_cmd or '(none)'}\n\n"
         + ("Start by finding the defect, then write a test that fails because of "
            "it. You cannot edit source until a test fails."
            if tdd else
            "Start by exploring the codebase to find the code that needs to change.")},
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
            # Done in the red phase means the model decided it was finished
            # without ever proving the defect exists. Accepting that would make
            # TDD advisory, which is the state this phase was added to end.
            if phase == "red":
                print("           [red] DONE refused: no failing test yet")
                history += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content":
                        "You cannot finish in the RED phase. Nothing has been proven "
                        "yet: no test fails because of this defect, so there is no "
                        "way to tell a real fix from a no-op. Use write_test to write "
                        "a test that FAILS against the current code, then continue."},
                ]
                continue
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

        # edit_file is offered in both the explore and edit phases: calling it
        # is what moves the model from one to the other. It is NOT offered in
        # the red phase -- offering it there would make the whole phase
        # advisory, since the model could simply skip to editing.
        offered = sorted(set(available_tools + ([] if phase == "red" else ["edit_file"])))
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
        # Record every turn, including one that made no call at all. `turns`
        # shipped as a permanently empty list, so result.json described a
        # fifteen-turn run as though nothing had happened.
        result["turns"].append({
            "turn": turn,
            "phase": phase,
            "tools": [c["name"] for c in tool_calls],
            "rejected": rejected,
            "output_tokens": out.output_tokens,
        })
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
                # The acceptance test is read-only once it is red. `protected`
                # already covers the profile's declared test globs, but a test
                # written to a path those globs do not cover would otherwise be
                # editable by the model that has to pass it.
                if path.replace("\\", "/") in locked_tests:
                    print(f"           [locked] REJECTED: {path}")
                    tool_results.append(
                        f"[edit_file result]: ERROR: {path} is the failing test that "
                        f"defines this change's acceptance criteria. It is read-only. "
                        f"Fix the source instead."
                    )
                    continue
                if not _check_file_scope(path, profile):
                    tc_result = f"ERROR: file {path} is outside the allowed scope ({', '.join(profile.file_scope_whitelist) or 'all'})"
                    print(f"           [scope] REJECTED: {path}")
                    tool_results.append(f"[{tc['name']} result]: {tc_result[:_result_limit(tc['name'])]}")
                    # Transition phase even on rejection (the LLM tried to edit)
                    if phase == "explore":
                        phase = "edit"
                        available_tools = list(EDIT_TOOLS)
                        print(f"           [phase] explore -> edit")
                    continue
            tc_result = execute_tool(tc["name"], tc.get("args", {}), repo, profile, edited)

            # A written test is only worth something if it is RED. Run the
            # suite now and check that this file actually produced a NEW
            # failure: a test that passes against unfixed code tests something
            # other than the defect, and a gate that cannot fail is worse than
            # no gate at all.
            if tc["name"] == "write_test" and tc_result.startswith("OK"):
                test_path = tc.get("args", {}).get("path", "").replace("\\", "/")
                gt, outcome = gates.check_tests(profile.test_cmd, repo, ws.baseline)
                new_red = sorted(outcome.failures - ws.baseline)
                if not outcome.counted:
                    tc_result += (
                        "\n\nERROR: the suite did not finish, so the test could not be "
                        "confirmed red. Check the file for a syntax or import error -- "
                        "an import of a name that does not exist yet is a COLLECTION "
                        "error, not a failure, and proves nothing. Import inside the "
                        "test body instead."
                    )
                elif not new_red:
                    tc_result += (
                        f"\n\nERROR: this test PASSES against the unfixed code "
                        f"({outcome.passed} passed, {outcome.failed} failed, no new "
                        f"failures). It therefore does not test the defect. Assert on "
                        f"the wrong behaviour the defect actually produces, then call "
                        f"write_test again."
                    )
                else:
                    acceptance = new_red
                    locked_tests.add(test_path)
                    if test_path not in edited:
                        edited.append(test_path)  # ships with the fix; build it too
                    # A new file is untracked, and `git diff` does not show
                    # untracked files -- so the exported patch carried the fix
                    # WITHOUT the test that proves it. Promoting that would land
                    # a change whose evidence had been silently dropped, and the
                    # next run's baseline would not contain the test at all.
                    # Intent-to-add puts it in the diff without committing it.
                    rc, out = ws.run(f'git add -N "{test_path}"')
                    if rc != 0:
                        print(f"           [red] WARNING: could not stage {test_path}: {out.strip()[:200]}")
                    # Fold the new failures into the frozen baseline so they are
                    # not later counted as regressions this patch caused, and
                    # require them green at the end.
                    ws.baseline |= set(new_red)
                    result["acceptance"] = list(acceptance)
                    phase = "explore"
                    available_tools = list(EXPLORE_TOOLS)
                    print(f"           [red] {len(new_red)} test(s) failing as required")
                    for t in new_red:
                        print(f"                 - {t}")
                    # Classify the redness mechanically. The prose below already
                    # asked the model to check this, and on the O34 run it did
                    # not: the test died in its own stub and sixty turns went
                    # into satisfying something that could never pass. A WARNING
                    # rather than a refusal, unlike test mode -- see the note in
                    # tests/acceptance/test_o34_red_for_the_right_reason.py. A
                    # crash-defect's test legitimately fails with the exception
                    # the defect raises, and a gate the model cannot override
                    # would strand the run in the red phase.
                    kinds = gates.failure_kinds(outcome.raw)
                    reached = gates.reached_an_assertion(kinds)
                    why = ", ".join(sorted(kinds)) if kinds else "(no exception identified)"
                    print(f"           [red] failed with: {why}")
                    red_warning = ""
                    if reached is False:
                        red_warning = (
                            f"WARNING: every failure is {why} and none is an assertion, so "
                            "these tests never reached an assertion. That is what a BROKEN "
                            "test looks like, not a test that caught the defect. Unless the "
                            "defect itself raises this, rewrite the test now -- it can never "
                            "pass, and every remaining turn will be spent against it."
                        )
                        print(f"           [red] {red_warning}")
                    print(f"           [phase] red -> explore")
                    # Show WHY it is red, not just that it is. A test can fail
                    # because the defect exists (what this phase is for) or
                    # because the test itself is broken -- a bad import, a
                    # command-line flag that does not exist. Both look identical
                    # as "a new failure appeared", and the second kind can never
                    # pass, so the run burns every remaining turn against it.
                    # This is the last moment the model can still notice.
                    tc_result += (
                        "\n\nCONFIRMED RED: " + ", ".join(new_red) +
                        "\nThis file is now read-only. Fix the source so these pass."
                        + (f"\n\n{red_warning}" if red_warning else "") +
                        "\n\nRead the failure output below and satisfy yourself that "
                        "these fail BECAUSE OF THE DEFECT. If they fail for any other "
                        "reason -- a bad import, a flag or path that does not exist -- "
                        "the test is wrong, it can never pass, and you must say so with "
                        "<<<ESCALATE>>> rather than edit source against it."
                        f"\n\n--- failure output (tail) ---\n{outcome.raw[-2500:]}"
                    )

            tool_results.append(f"[{tc['name']} result]: {tc_result[:_result_limit(tc['name'])]}")

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
    else:
        # Falling out of the for loop means max_turns was spent without the
        # model ever emitting <<<DONE>>>. That used to leave verdict at its ""
        # initial value: the run reported no state at all, and every branch
        # below keys off verdict == "DONE", so a run that did fifteen turns of
        # real work was indistinguishable from one that did nothing. Name it.
        if not result["verdict"]:
            result["verdict"] = "MAX_TURNS_EXHAUSTED"
            result["error"] = (
                f"spent all {max_turns} turns without emitting <<<DONE>>>; "
                f"edited {len(edited)} file(s). Raise --max-rounds (max_turns "
                f"is 5x it) or narrow the defect."
            )

    # TDD is not advisory. Reaching DONE without a red test means the phase
    # machine let something through, and shipping it would be shipping a patch
    # nothing can refuse.
    if tdd and result["verdict"] == "DONE" and not acceptance:
        result["verdict"] = "NO_FAILING_TEST"
        result["error"] = (
            "finished without a test that failed first, so there is no evidence "
            "the defect existed or that it is now fixed."
        )

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
                # expect_green is what makes the gate able to REFUSE rather than
                # merely observe. Without it this branch only asks "does the
                # suite still pass?", which a patch that changes nothing also
                # satisfies -- O3's first developer-mode patch was green here
                # and fixed nothing.
                gt, outcome = gates.check_tests(
                    profile.test_cmd, repo, ws.baseline, expect_green=acceptance,
                )
                print(f"  [test] {'ok' if gt.ok else 'FAIL'} - {gt.summary}")
                if acceptance:
                    print(f"  [test-first] {len(acceptance)} acceptance test(s) required green")
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