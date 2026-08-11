"""
Acceptance tests: a model that answers with a NATIVE tool_calls array.

Developer mode was dead on arrival against its own configured implementer
(kimi-k2.7-code:cloud) and nothing in 217 tests noticed, because every test
handed the driver a Completion built by hand with the text protocol already in
`text`. No test ever exercised a provider response shape, so the fact that
`_call_ollama` read only `content` and `thinking` -- and dropped `tool_calls`
on the floor -- was invisible.

The observed failure, reproduced end to end here:

    message keys: ['role', 'content', 'tool_calls']
    content: ''
    tool_calls: [read_file(path="src/agent_loop/report.py")]
    done_reason: stop, eval_count: 21

With think=True that empty content raised out of the provider as "exhausted its
output budget on reasoning ... Raise max_tokens above 48000" -- for a response
that generated 21 tokens and stopped voluntarily. With think=False it returned
an empty string and the driver told a model that had already answered to
"Continue", until it ran out of turns.
"""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from _interp import PY_EXE

from agent_loop import providers
from agent_loop.providers import Completion, ProviderError, _normalise_tool_calls
from agent_loop.profiles import Profile, register
from agent_loop.developer.driver import run_developer, _render_tool_calls, _parse_tool_calls


# ---------------------------------------------------------------------------
# _normalise_tool_calls -- the shapes real servers actually send
# ---------------------------------------------------------------------------
def test_normalise_ollama_shape():
    """Ollama nests under `function` and sends `arguments` already decoded."""
    calls = _normalise_tool_calls([
        {"id": "functions.read_file:0",
         "function": {"index": 0, "name": "read_file",
                      "arguments": {"path": "src/agent_loop/report.py"}}}
    ])
    assert calls == [{"name": "read_file", "args": {"path": "src/agent_loop/report.py"}}]


def test_normalise_openai_shape_decodes_string_arguments():
    """OpenAI-compatible servers send `arguments` as a JSON string."""
    calls = _normalise_tool_calls([
        {"type": "function",
         "function": {"name": "edit_file",
                      "arguments": '{"path": "a.py", "old_str": "x", "new_str": "y"}'}}
    ])
    assert calls == [
        {"name": "edit_file", "args": {"path": "a.py", "old_str": "x", "new_str": "y"}}
    ]


def test_normalise_tolerates_junk():
    """Malformed argument JSON degrades to empty args rather than raising, and
    an entry with no usable name is skipped -- a call the loop cannot name is a
    call it cannot execute, and an invented placeholder would be dispatched as
    a real request."""
    calls = _normalise_tool_calls([
        {"function": {"name": "read_file", "arguments": "{not json"}},
        {"function": {"arguments": {"path": "x"}}},   # no name -> skipped
        "garbage",                                      # not a dict -> skipped
        {"function": {"name": "run_tests", "arguments": None}},
    ])
    assert calls == [
        {"name": "read_file", "args": {}},
        {"name": "run_tests", "args": {}},
    ]


def test_normalise_handles_empty_and_none():
    assert _normalise_tool_calls(None) == []
    assert _normalise_tool_calls([]) == []


# ---------------------------------------------------------------------------
# _call_ollama -- the guard must not mistake a tool call for an empty answer
# ---------------------------------------------------------------------------
def _ollama_response(**overrides):
    data = {
        "message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "src/x.py"}}}
        ]},
        "done_reason": "stop",
        "eval_count": 21,
        "prompt_eval_count": 900,
    }
    data.update(overrides)
    return data


def _call(data, max_tokens=48000):
    with patch.object(providers, "_post", return_value=data):
        return providers._call_ollama(
            "kimi-k2.7-code:cloud", [{"role": "user", "content": "hi"}],
            0.1, max_tokens, 900, 32768, True, False,
        )


def test_tool_call_survives_empty_content():
    """The regression. Empty content plus a tool call is a complete answer."""
    out = _call(_ollama_response())
    assert out.tool_calls == [{"name": "read_file", "args": {"path": "src/x.py"}}]


def test_tool_call_with_thinking_does_not_raise():
    """think=True adds a `thinking` field. The old guard fired on
    (empty content AND any thinking) and never looked for the tool call."""
    data = _ollama_response()
    data["message"]["thinking"] = "The user wants me to read report.py first."
    out = _call(data)
    assert out.tool_calls, "a tool call must not be reported as a dead model"
    assert out.text == ""


def test_empty_answer_that_stopped_early_is_not_blamed_on_the_budget():
    """21 tokens against a 48000-token budget with done_reason=stop is not
    truncation. Telling the user to raise max_tokens sent a real debugging
    session looking at budgets for the wrong reason."""
    data = _ollama_response()
    data["message"]["tool_calls"] = []
    data["message"]["thinking"] = "hmm"
    with pytest.raises(ProviderError) as exc:
        _call(data)
    msg = str(exc.value)
    assert "neither content nor a tool call" in msg
    assert "raising max_tokens will not help" in msg
    assert "exhausted its output budget" not in msg


def test_genuine_truncation_still_reports_the_budget():
    """The original diagnosis was right for the case it was written for:
    deepseek-v4-pro burning its whole budget on chain of thought.

    The exact sentence "Raise max_tokens above 48000" was asserted here until
    O60 changed the advice -- reasoning expands to fill the budget, so raising
    it is no longer the FIRST thing this message recommends. What the test is
    for is that a genuine truncation still names the budget it died on, and
    that is asserted directly rather than through one phrasing of it."""
    data = _ollama_response(done_reason="length", eval_count=48000)
    data["message"]["tool_calls"] = []
    data["message"]["thinking"] = "x" * 40000
    with pytest.raises(ProviderError) as exc:
        _call(data)
    assert "exhausted its output budget" in str(exc.value)
    assert "48000" in str(exc.value), "the budget it died on must be named"
    assert "max_tokens" in str(exc.value), "and named as the budget knob"


def test_openai_backend_captures_tool_calls_too():
    data = {
        "choices": [{"message": {"content": None, "tool_calls": [
            {"function": {"name": "run_tests", "arguments": "{}"}}
        ]}, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    with patch.object(providers, "_post", return_value=data):
        out = providers._call_openai(
            "gpt-oss-120b", [{"role": "user", "content": "hi"}],
            0.1, 1024, 900, 32768, None, False,
        )
    assert out.tool_calls == [{"name": "run_tests", "args": {}}]
    assert out.text == ""


# ---------------------------------------------------------------------------
# The text-protocol round trip
# ---------------------------------------------------------------------------
def test_render_tool_calls_round_trips():
    calls = [{"name": "read_file", "args": {"path": "src/x.py"}}]
    assert _parse_tool_calls(_render_tool_calls(calls), ["read_file"]) == calls


def test_render_is_for_the_record_not_for_dispatch():
    """A namespaced name cannot survive the text protocol (its name regex is
    \\w+). Dispatch therefore reads out.tool_calls directly; this test pins the
    limitation so nobody 'simplifies' dispatch back through the rendering."""
    calls = [{"name": "functions.read_file", "args": {}}]
    assert _parse_tool_calls(_render_tool_calls(calls), None) == []


# ---------------------------------------------------------------------------
# End to end: the test that would have caught this
# ---------------------------------------------------------------------------
def _make_repo(tmpdir):
    repo = tmpdir / "repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    (src / "target.py").write_text(
        "class TargetClass:\n    def method(self):\n        return 42\n", encoding="utf-8"
    )
    os.system(f'cd /d "{repo}" && git init && git add -A && git commit -m init')
    return repo


def test_driver_dispatches_native_tool_calls(tmp_path):
    """Same shape as test_phase8_driver_completes, except the model answers the
    way kimi-k2.7-code actually does: empty text, native tool_calls."""
    repo = _make_repo(tmp_path)
    dev_profile = Profile(
        name="test-native-tool-calls",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        build_cmd=PY_EXE + " -m py_compile src/target.py",
        test_cmd="",
        file_scope_whitelist=("src/",),
        protected=("test_*.py", "tests/*"),
        implementer_rules="test", reviewer_priorities="test",
    )
    register(dev_profile)

    calls = [0]

    def mock_impl(model, messages, **kw):
        calls[0] += 1
        if calls[0] == 1:
            return Completion(text="", model=model, tool_calls=[
                {"name": "read_file", "args": {"path": "src/target.py"}}])
        if calls[0] == 2:
            return Completion(text="", model=model, tool_calls=[
                {"name": "edit_file", "args": {
                    "path": "src/target.py", "old_str": "return 42",
                    "new_str": "return 43"}}])
        return Completion(
            text="<<<DONE>>>\nChanged 42 to 43\n<<<END DONE>>>", model=model)

    with patch("agent_loop.developer.driver.chat", side_effect=mock_impl):
        with patch("agent_loop.loop.review_panel") as mock_panel:
            from agent_loop.loop import PanelResult, Vote
            mock_panel.return_value = PanelResult(
                votes=[Vote("r1", "APPROVE")], verdict="APPROVE", valid=True,
            )
            result = run_developer(
                repo, "The return value is wrong", dev_profile,
                "test-impl", ["r1"], arbiter_model="", max_turns=10, apply=False,
            )

    assert result["verdict"] in ("DONE", "APPROVE"), result.get("error")
    assert result["patch"], "a patch must be exported"
    assert "return 43" in Path(result["patch"]).read_text(encoding="utf-8")
    # Three turns: read, edit, done. Before the fix the driver saw three blank
    # turns and spent every one of max_turns saying "Continue".
    assert calls[0] == 3, f"expected 3 turns, took {calls[0]}"


def test_driver_does_not_double_count_a_native_call(tmp_path):
    """`raw` contains the rendering of the native calls for the artifact and the
    history turn. Dispatching by re-parsing `raw` would execute every native
    call twice."""
    repo = _make_repo(tmp_path)
    dev_profile = Profile(
        name="test-native-no-double",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        build_cmd=PY_EXE + " -m py_compile src/target.py", test_cmd="",
        file_scope_whitelist=("src/",), protected=("test_*.py", "tests/*"),
        implementer_rules="test", reviewer_priorities="test",
    )
    register(dev_profile)

    seen = []
    calls = [0]

    def mock_impl(model, messages, **kw):
        calls[0] += 1
        if calls[0] == 1:
            return Completion(text="", model=model, tool_calls=[
                {"name": "read_file", "args": {"path": "src/target.py"}}])
        return Completion(text="<<<DONE>>>\nnothing\n<<<END DONE>>>", model=model)

    real_execute = None
    from agent_loop.developer import driver as drv
    real_execute = drv.execute_tool

    def spy(name, args, repo_, profile_, edited_):
        seen.append(name)
        return real_execute(name, args, repo_, profile_, edited_)

    with patch("agent_loop.developer.driver.chat", side_effect=mock_impl):
        with patch("agent_loop.developer.driver.execute_tool", side_effect=spy):
            with patch("agent_loop.loop.review_panel") as mock_panel:
                from agent_loop.loop import PanelResult, Vote
                mock_panel.return_value = PanelResult(
                    votes=[Vote("r1", "APPROVE")], verdict="APPROVE", valid=True,
                )
                run_developer(
                    repo, "defect", dev_profile, "test-impl", ["r1"],
                    arbiter_model="", max_turns=6, apply=False,
                )

    assert seen.count("read_file") == 1, f"read_file executed {seen.count('read_file')}x: {seen}"


# ---------------------------------------------------------------------------
# Turn exhaustion must name itself
# ---------------------------------------------------------------------------
def test_max_turns_exhausted_is_named_not_blank(tmp_path):
    """A run that spends every turn without <<<DONE>>> used to fall out of the
    loop with verdict still "" -- fifteen turns of real work reported as no
    state at all, and indistinguishable downstream from a run that did
    nothing, because every branch after the loop keys off verdict == "DONE"."""
    repo = _make_repo(tmp_path)
    dev_profile = Profile(
        name="test-max-turns",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        build_cmd=PY_EXE + " -m py_compile src/target.py", test_cmd="",
        file_scope_whitelist=("src/",), protected=("test_*.py", "tests/*"),
        implementer_rules="test", reviewer_priorities="test",
    )
    register(dev_profile)

    def never_done(model, messages, **kw):
        return Completion(text="", model=model, output_tokens=7, tool_calls=[
            {"name": "read_file", "args": {"path": "src/target.py"}}])

    with patch("agent_loop.developer.driver.chat", side_effect=never_done):
        result = run_developer(
            repo, "defect", dev_profile, "test-impl", ["r1"],
            arbiter_model="", max_turns=3, apply=False,
        )

    assert result["verdict"] == "MAX_TURNS_EXHAUSTED", result["verdict"]
    assert "3 turns" in result.get("error", "")


def test_turns_are_recorded(tmp_path):
    """result["turns"] shipped as a permanently empty list."""
    repo = _make_repo(tmp_path)
    dev_profile = Profile(
        name="test-turns-recorded",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        build_cmd=PY_EXE + " -m py_compile src/target.py", test_cmd="",
        file_scope_whitelist=("src/",), protected=("test_*.py", "tests/*"),
        implementer_rules="test", reviewer_priorities="test",
    )
    register(dev_profile)

    def two_turns(model, messages, **kw):
        return Completion(text="", model=model, output_tokens=11, tool_calls=[
            {"name": "read_file", "args": {"path": "src/target.py"}}])

    with patch("agent_loop.developer.driver.chat", side_effect=two_turns):
        result = run_developer(
            repo, "defect", dev_profile, "test-impl", ["r1"],
            arbiter_model="", max_turns=2, apply=False,
        )

    assert len(result["turns"]) == 2, result["turns"]
    assert result["turns"][0]["tools"] == ["read_file"]
    assert result["turns"][0]["phase"] == "explore"
    assert result["turns"][0]["output_tokens"] == 11


def test_a_budget_exhausted_on_reasoning_does_not_only_advise_raising_it():
    """O60. "Raise max_tokens" is the advice this message has always given, and
    it failed twice in a row on the same ticket:

        64000 budget -> 282935 chars of thinking, empty content
        96000 budget -> 435641 chars of thinking, empty content

    4.42 and 4.54 characters per token. The reasoning expanded to fill whatever
    it was given; the budget is not a control on it, it is only where the model
    gets cut off. The message must not send the next person up the same ladder
    -- especially since the measured fix for the identical failure in the
    reviewer role was `think=False`, not a bigger number.
    """
    data = _ollama_response()
    data["message"]["content"] = ""
    data["message"]["tool_calls"] = []
    data["message"]["thinking"] = "x" * 435641
    data["done_reason"] = "length"
    data["eval_count"] = 96000

    with pytest.raises(providers.ProviderError) as exc:
        _call(data, max_tokens=96000)

    msg = str(exc.value)
    assert "96000" in msg, "the budget it died on has to be in the message"
    assert "think=False" in msg, (
        "the message must name the lever that actually bounds reasoning. "
        "Asserting on the word 'think' passes on the existing text, which says "
        "'chars of thinking' -- an assertion the unfixed message also satisfies. "
        f"got: {msg}"
    )
