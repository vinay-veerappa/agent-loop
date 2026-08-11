"""
O31: the modes that reason about a codebase had never been shown one.

`run_brainstorm` and `run_plan` both built their entire prompt from the request
text plus four profile fields (language, suffixes, build cmd, test cmd). Measured
on live runs: `in=264` tokens for brainstorm, `in=319` for plan. Plan mode's whole
job is to LOCALISE a defect and emit regions that resolve against the tree, and it
was doing that having never seen the tree.

`plan_mode.py` imports `build_context_slice` and never calls it, which looks like
an oversight and is not: that function takes `regions`, and regions are precisely
what plan mode exists to PRODUCE. There was nothing to pass. The missing piece is
a context builder keyed on the REQUEST rather than on a known location.

Why not reuse docs mode's `_build_graph_context`: it returns "" unless
`codebase-memory-mcp` is live, so on any machine without the graph running the
modes would still be blind. The graph is an enrichment here, not the mechanism --
the filesystem is always available and is what makes this work by default.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop import context
from agent_loop.profiles import Profile, register


PROFILE = Profile(
    name="test-o31",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    context_token_budget=3000,
    implementer_rules="t", reviewer_priorities="t",
)
register(PROFILE)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "review_mode.py").write_text(
        "import os\n"
        "\n"
        "\n"
        "def run_review(repo, base, head):\n"
        "    '''Adversarial review.'''\n"
        "    return collect_diff(repo, base, head)\n"
        "\n"
        "\n"
        "def collect_diff(repo, base, head):\n"
        "    return ''\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "gates.py").write_text(
        "class TestOutcome:\n"
        "    pass\n"
        "\n"
        "\n"
        "def run_tests(cmd, repo):\n"
        "    return TestOutcome()\n",
        encoding="utf-8",
    )
    # Must be ignored by the walk.
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "junk.py").write_text("def run_review(): pass\n", encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------
# symbol extraction
# --------------------------------------------------------------------------
def test_backticked_names_are_taken_as_symbols():
    got = context.extract_intent_symbols("`run_review` prints the wrong path")
    assert "run_review" in got


def test_snake_case_and_camel_case_are_taken_without_backticks():
    got = context.extract_intent_symbols(
        "run_review calls collect_diff and returns a TestOutcome"
    )
    assert {"run_review", "collect_diff", "TestOutcome"} <= set(got)


def test_dotted_and_qualified_names_are_split():
    got = context.extract_intent_symbols("gates.run_tests is never called")
    assert "run_tests" in got


def test_a_file_path_in_the_request_is_recognised():
    files, _ = context.split_paths_and_symbols(
        "the bug is in src/review_mode.py:243 and nowhere else"
    )
    assert "src/review_mode.py" in files


def test_ordinary_prose_yields_no_symbols():
    """A sentence with no code-shaped token must not send the whole dictionary
    to the searcher."""
    got = context.extract_intent_symbols(
        "The output is confusing and the user cannot tell what happened."
    )
    assert got == [], got


# --------------------------------------------------------------------------
# the context itself -- WITHOUT a graph, which is the point
# --------------------------------------------------------------------------
def test_a_symbol_is_located_in_the_tree(repo):
    out = context.build_intent_context(repo, PROFILE, "`run_review` is wrong")
    assert "review_mode.py" in out
    assert "run_review" in out
    assert ":4" in out, f"the definition line must be named: {out}"


def test_it_works_with_no_graph_project_configured(repo):
    """The mechanism is the filesystem. `build_context_slice` returns "" when
    graph_project is empty, and copying that behaviour here would have left both
    modes blind on every machine without the MCP server running."""
    assert not PROFILE.graph_project
    out = context.build_intent_context(repo, PROFILE, "`run_review` is wrong")
    assert out.strip(), "no context produced without a graph"


def test_a_file_named_in_the_request_is_reported_even_with_no_symbols(repo):
    out = context.build_intent_context(repo, PROFILE, "something in src/gates.py is off")
    assert "src/gates.py" in out


def test_pycache_is_not_searched(repo):
    out = context.build_intent_context(repo, PROFILE, "`run_review` is wrong")
    assert "__pycache__" not in out


def test_nothing_found_yields_an_empty_string(repo):
    """An empty section is worse than none: it costs tokens and reads as
    'the codebase has nothing to say about this'."""
    out = context.build_intent_context(repo, PROFILE, "make the colours nicer")
    assert out == ""


def test_the_budget_is_respected(repo):
    tight = Profile(
        name="test-o31-tight",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        context_token_budget=20,  # 80 chars
        implementer_rules="t", reviewer_priorities="t",
    )
    register(tight)
    out = context.build_intent_context(
        repo, tight, "`run_review` and `collect_diff` and `run_tests`"
    )
    # 200 is the floor the builder applies: below it nothing useful fits, and a
    # section that says only "## Codebase context" is worse than none. The
    # truncation notice counts against the budget rather than being added to it.
    assert len(out) <= 200, f"budget blown: {len(out)} chars"
    assert "truncated" in out, "silently dropped content without saying so"


def test_a_missing_repo_does_not_raise(tmp_path):
    out = context.build_intent_context(tmp_path / "nope", PROFILE, "`run_review`")
    assert out == ""


# --------------------------------------------------------------------------
# wired into the two blind modes
# --------------------------------------------------------------------------
def _capture_prompt(mod, run, repo):
    seen = {}

    def fake_chat(model, messages, **kw):
        seen["prompt"] = "\n".join(m["content"] for m in messages)
        from agent_loop.providers import Completion
        return Completion(text="nothing parseable", model=model)

    with patch.object(mod, "chat", side_effect=fake_chat):
        run()
    return seen.get("prompt", "")


def test_plan_mode_is_shown_the_codebase(repo):
    from agent_loop import plan_mode

    prompt = _capture_prompt(
        plan_mode, lambda: plan_mode.run_plan(
            repo, "`run_review` prints the wrong path", PROFILE, "impl", [], max_rounds=1,
        ), repo,
    )
    assert "review_mode.py" in prompt, "plan mode still cannot see the code it must localise in"


def test_brainstorm_mode_is_shown_the_codebase(repo):
    from agent_loop import brainstorm_mode

    prompt = _capture_prompt(
        brainstorm_mode, lambda: brainstorm_mode.run_brainstorm(
            repo, "`run_review` prints the wrong path", PROFILE, "impl",
        ), repo,
    )
    assert "review_mode.py" in prompt, "brainstorm still recommends approaches sight-unseen"


def test_the_graph_enriches_but_is_not_required(repo):
    """When the MCP graph IS up, its trace is added on top of the filesystem
    findings rather than replacing them."""
    graphed = Profile(
        name="test-o31-graph",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        graph_project="proj", context_token_budget=3000,
        implementer_rules="t", reviewer_priorities="t",
    )
    register(graphed)

    class _Client:
        def call_tool(self, name, args):
            return "run_review -> collect_diff -> git"

    with patch("agent_loop.mcp_client.get_mcp_client", return_value=_Client()):
        out = context.build_intent_context(repo, graphed, "`run_review` is wrong")

    assert "review_mode.py" in out, "the filesystem findings were dropped"
    assert "collect_diff -> git" in out, "the graph trace was not included"


def test_a_broken_graph_does_not_lose_the_filesystem_findings(repo):
    graphed = Profile(
        name="test-o31-graph-broken",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        graph_project="proj", context_token_budget=3000,
        implementer_rules="t", reviewer_priorities="t",
    )
    register(graphed)

    def boom():
        raise RuntimeError("mcp is down")

    with patch("agent_loop.mcp_client.get_mcp_client", side_effect=boom):
        out = context.build_intent_context(repo, graphed, "`run_review` is wrong")

    assert "review_mode.py" in out, "a graph failure took the whole context down with it"


# --------------------------------------------------------------------------
# recall: the filesystem is the filter, so extraction must not be strict
# --------------------------------------------------------------------------
def test_a_single_word_class_name_is_a_symbol():
    """`Config` has no underscore and no lower-to-upper transition. The first
    version of `_looks_like_code` rejected it, and on a live brainstorm run about
    `Config.roles` the context found nothing and added 25 tokens to a 264-token
    prompt. Same for `Vote`, `Finding`, `Profile`."""
    got = context.extract_intent_symbols("Config.roles is keyed by role name")
    assert "Config" in got, got


def test_an_all_caps_constant_is_a_symbol():
    got = context.extract_intent_symbols("PROMOTABLE is missing DONE")
    assert "PROMOTABLE" in got, got


def test_a_capitalised_class_is_located_in_the_tree(repo):
    out = context.build_intent_context(repo, PROFILE, "TestOutcome is the wrong shape")
    assert "gates.py:1" in out, out


def test_the_tree_is_walked_once_for_all_symbols(repo, monkeypatch):
    """Loosening extraction multiplied the candidates, so the per-symbol walk had
    to go: it re-read every file once per name."""
    calls = []
    real = context._iter_sources

    def counting(r, p):
        calls.append(1)
        return real(r, p)

    monkeypatch.setattr(context, "_iter_sources", counting)
    context.build_intent_context(
        repo, PROFILE, "`run_review` `collect_diff` `run_tests` `TestOutcome`"
    )
    # One walk for the symbol search. (A path-shaped token would add one more,
    # and this intent deliberately has none.)
    assert len(calls) == 1, f"walked the tree {len(calls)} times"


# --------------------------------------------------------------------------
# the graph must not inject its own failures as findings
# --------------------------------------------------------------------------
def test_a_graph_error_payload_is_not_presented_as_a_call_path(repo):
    """Observed live against the real MCP server: `trace_call_path` answers
    `{"error":"function not found"}` for a name it does not know, which is a
    200-OK JSON body, not a string starting with "ERROR". The first version
    injected three of those into the prompt under the heading "Call paths",
    which does not merely waste tokens -- it tells the model those symbols do
    not exist."""
    graphed = Profile(
        name="test-o31-graph-errors",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        graph_project="proj", context_token_budget=3000,
        implementer_rules="t", reviewer_priorities="t",
    )
    register(graphed)

    class _Client:
        def call_tool(self, name, args):
            return '{"error":"function not found"}'

    with patch("agent_loop.mcp_client.get_mcp_client", return_value=_Client()):
        out = context.build_intent_context(repo, graphed, "`run_review` is wrong")

    assert "Call paths" not in out, out
    assert "function not found" not in out, out
    assert "review_mode.py" in out, "the filesystem findings must survive"


def test_an_indented_field_definition_is_found(tmp_path):
    """`Config.roles` is a dataclass field, indented inside the class, so a
    column-0 assignment pattern misses it -- and it was the name the request
    actually pointed at."""
    (tmp_path / "config.py").write_text(
        "from dataclasses import dataclass\n"
        "\n"
        "\n"
        "@dataclass\n"
        "class Config:\n"
        "    roles: dict\n"
        "    modes: dict\n",
        encoding="utf-8",
    )
    out = context.build_intent_context(tmp_path, PROFILE, "Config.roles is keyed by name")
    assert "config.py:6" in out, out


def test_source_definitions_outrank_test_files(tmp_path):
    """Observed on a live run: `roles` matched `roles = dict(base.roles)` in two
    test files and pushed the real declaration out of the two-hit budget, because
    the walk was alphabetical and `tests/` sorts before `src/`. For localisation
    the source tree is the answer and a test is at best a corroboration."""
    # The test directory is deliberately named so that it sorts BEFORE the source
    # directory. The first version of this test used tests/ and src/, where `s`
    # already follows `t`... no: `src` sorts before `tests`, so it passed against
    # the unranked implementation and could never have failed.
    (tmp_path / "src").mkdir()
    (tmp_path / "checks").mkdir()
    (tmp_path / "checks" / "test_thing.py").write_text(
        "def test_it():\n    widget_count = 1\n", encoding="utf-8")
    (tmp_path / "src" / "thing.py").write_text(
        "widget_count = 0\n", encoding="utf-8")

    prof = Profile(
        name="test-o31-ranking",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        test_sources=("checks/*.py",), context_token_budget=3000,
        implementer_rules="t", reviewer_priorities="t",
    )
    register(prof)

    out = context.build_intent_context(tmp_path, prof, "`widget_count` is wrong")
    src_at = out.find("src/thing.py")
    test_at = out.find("checks/test_thing.py")
    assert src_at != -1, out
    assert test_at == -1 or src_at < test_at, f"a test file outranked the source: {out}"


def test_a_named_symbol_that_does_not_exist_yields_an_empty_string(repo):
    """Distinct from the prose case, which returns early because NOTHING was
    extracted. Here a real-looking symbol IS extracted and then found nowhere, so
    the later `if not parts` guard is the one doing the work -- and replacing it
    with `pass` left the prose test green, because that test never reaches it.

    Third instance this session of a guard shadowed by an earlier guard (see the
    O33 `id` validation). Worth checking for by reflex whenever two guards can
    return the same value.
    """
    out = context.build_intent_context(repo, PROFILE, "`no_such_function` is broken")
    assert out == "", f"emitted a context section with nothing in it: {out!r}"
