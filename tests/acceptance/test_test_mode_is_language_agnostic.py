"""Test mode must write tests in the project's language, at the project's path.

The live failure, on the first `--mode test` run this package has ever had against
a non-Python profile. Against a C# NinjaTrader profile whose `test_sources` is
`scripts/ninjatrader/addons/*Tests.cs`, it wrote
`tests/acceptance/test_generated.py` containing:

    import pytest
    from TradeCopierEngine import CopierRelationship, CopierSizingMode

-- Python, importing a `.cs` file as a module, calling `CalculateCopyQuantity`
(the real method is `CalculateFollowerQuantity`), and passing a C# `out` parameter
by value.

Four causes, all fixed here:

1. `test_file` defaulted to a hardcoded Python path in BOTH `run_test`'s signature
   and the `--test-file` CLI flag, and the flag was passed unconditionally.
2. `TEST_SYSTEM`'s output example was literally ```python / import pytest.
3. "Code under test" was `src.splitlines()[:100]` -- the head of a 2,700-line
   file whose regions are at 382-534, so the model never saw the method and
   invented a name for it.
4. Tests that PASS at baseline printed a WARNING and exited 0, so a gate that
   verified nothing looked like a success.
"""
from __future__ import annotations

import json

import pytest

from agent_loop import test_mode
from agent_loop.profiles import Profile

CSHARP = Profile(
    name="cs", language="csharp", file_suffixes=(".cs",), line_comment="//",
    block_comment=(), block_kind="decl", preprocessor_directives=(),
    build_cmd="dotnet build", test_cmd="dotnet run",
    lock_name="_lock", risk_calls=(),
    test_sources=("scripts/ninjatrader/addons/*Tests.cs",),
)

PYTHON = Profile(
    name="py", language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent", preprocessor_directives=(),
    build_cmd="python -m py_compile", test_cmd="python -m pytest",
    lock_name="", risk_calls=(),
    test_sources=("tests/test_*.py",),
)

NO_SOURCES = Profile(
    name="bare", language="go", file_suffixes=(".go",), line_comment="//",
    block_comment=(), block_kind="decl", preprocessor_directives=(),
    build_cmd="go build", test_cmd="go test", lock_name="", risk_calls=(),
)


def test_csharp_profile_gets_a_cs_path_inside_its_test_sources():
    path = test_mode.default_test_path(CSHARP, "CM1")
    assert path.endswith(".cs"), path
    assert path.startswith("scripts/ninjatrader/addons/"), path
    assert "test_generated.py" not in path


def test_the_generated_path_still_matches_the_test_sources_glob():
    """It must stay a TEST file, which is also what keeps it protected."""
    import fnmatch

    path = test_mode.default_test_path(CSHARP, "CM1")
    assert fnmatch.fnmatch(path, CSHARP.test_sources[0]), (
        f"{path} no longer matches {CSHARP.test_sources[0]}, so the runner will not "
        f"compile it and the implementer is free to edit it"
    )


def test_the_ticket_id_appears_so_two_tickets_do_not_collide():
    assert "CM1" in test_mode.default_test_path(CSHARP, "CM1")
    assert "CM2" in test_mode.default_test_path(CSHARP, "CM2")
    assert test_mode.default_test_path(CSHARP, "CM1") != test_mode.default_test_path(CSHARP, "CM2")


def test_python_profile_still_gets_a_python_path():
    path = test_mode.default_test_path(PYTHON, "T1")
    assert path.endswith(".py")
    assert path.startswith("tests/")


def test_a_profile_with_no_test_sources_falls_back_to_its_own_suffix():
    path = test_mode.default_test_path(NO_SOURCES, "T1")
    assert path.endswith(".go"), path


def test_the_system_prompt_does_not_hardcode_python():
    assert "import pytest" not in test_mode.TEST_SYSTEM
    assert "```python" not in test_mode.TEST_SYSTEM
    assert "LANGUAGE" in test_mode.TEST_SYSTEM


# --- the three prompt/verification behaviours ------------------------------


ENGINE = "\n".join(
    ["namespace X", "{", "    public class Engine", "    {"]
    + [f"        // filler line {i}" for i in range(120)]
    + [
        "        public int TheMethodUnderTest(int a, out bool flag)",
        "        {",
        "            flag = false;",
        "            return a;",
        "        }",
        "    }",
        "}",
    ]
)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "scripts" / "ninjatrader" / "addons").mkdir(parents=True)
    (tmp_path / "scripts" / "ninjatrader" / "addons" / "Engine.cs").write_text(
        ENGINE, encoding="utf-8"
    )
    (tmp_path / "scripts" / "ninjatrader" / "addons" / "ExistingTests.cs").write_text(
        "// EXISTING_HARNESS_MARKER\nprivate static void Assert(bool c, string m) { }\n",
        encoding="utf-8",
    )
    return tmp_path


TICKET = {
    "id": "CM1",
    "title": "t",
    "expect_green": ["the matrix entry was sized from the table"],
    "regions": [
        {
            "id": "R1",
            "file": "scripts/ninjatrader/addons/Engine.cs",
            "anchor": "public int TheMethodUnderTest(int a, out bool flag)",
        }
    ],
}


def _capture_prompt(monkeypatch, repo, profile=CSHARP):
    seen = {}

    class _Out:
        text = "<<<TESTS>>>\n```csharp\n// tests\n```\n<<<END TESTS>>>"

        def usage_line(self):
            return "stub 0.0s"

    def _chat(model, history, **kw):
        seen["prompt"] = history[-1]["content"]
        return _Out()

    monkeypatch.setattr(test_mode, "chat", _chat)
    # Skip the baseline run; the prompt is what these tests are about.
    monkeypatch.setattr(profile.__class__, "test_cmd", "", raising=False)
    test_mode.run_test(repo, "a defect", TICKET, profile, "stub-model")
    return seen["prompt"]


def test_the_prompt_shows_the_method_under_test_not_the_head_of_the_file(monkeypatch, repo):
    prompt = _capture_prompt(monkeypatch, repo)

    # Assert on the BODY, not the signature. The ticket JSON is also pasted into
    # the prompt and its region anchor IS the signature, so `"TheMethodUnderTest"
    # in prompt` stayed true with the region text removed entirely -- it survived
    # two mutations before this comment existed.
    assert "flag = false;" in prompt, "the method BODY was never shown"
    assert "## Code under test:" in prompt
    assert "lines " in prompt, "the resolved line range is missing"
    assert "filler line 3" not in prompt, "the head of the file was pasted instead"


def test_the_prompt_shows_an_existing_test_source_as_the_style_reference(monkeypatch, repo):
    prompt = _capture_prompt(monkeypatch, repo)
    assert "EXISTING_HARNESS_MARKER" in prompt


def test_the_prompt_names_the_language_and_the_real_target_path(monkeypatch, repo):
    prompt = _capture_prompt(monkeypatch, repo)
    assert "csharp" in prompt
    assert "ExistingTests.cs" in prompt or "CM1Generated" in prompt
    assert "test_generated.py" not in prompt


def test_expect_green_is_described_as_failure_output_not_test_names(monkeypatch, repo):
    prompt = _capture_prompt(monkeypatch, repo)
    assert "failing assertion" in prompt
    assert "Test names to use" not in prompt, (
        "calling these 'test names' is what produced method names the gate could "
        "never match against a [FAIL] <message> line"
    )


def test_tests_green_at_baseline_is_an_error_not_a_warning(monkeypatch, repo):
    """A green-before-the-fix suite gates nothing and must not exit 0."""
    from agent_loop import gates, workspace

    class _Out:
        text = "<<<TESTS>>>\n```csharp\n// tests\n```\n<<<END TESTS>>>"

        def usage_line(self):
            return "stub 0.0s"

    class _Outcome:
        ran = True
        counted = True
        failures = set()
        raw = "OK"
        passed = 5
        failed = 0

    class _WS:
        def __init__(self, root):
            self.root = root

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(test_mode, "chat", lambda *a, **k: _Out())
    monkeypatch.setattr(workspace, "open_workspace", lambda repo, name, **kw: _WS(repo))
    monkeypatch.setattr(gates, "run_tests", lambda *a, **k: _Outcome())

    result = test_mode.run_test(repo, "a defect", TICKET, CSHARP, "stub-model")

    assert result["tests_pass_baseline"] is True
    assert result.get("error"), "a vacuous gate was reported as success"
    assert "gate nothing" in result["error"]


# --- the fence info string is metadata, not code (O49) ----------------------


@pytest.mark.parametrize("fence", ["python", "csharp", "c#", "cs", "go", "rust",
                                   "javascript title=x", ""])
def test_any_fence_language_is_stripped(fence):
    r"""`(?:python)?` did not just fail to strip other languages -- it made them code.

    With ```csharp the optional group matched empty, `\s*` matched nothing
    because `c` is not whitespace, and the capture started at `csharp`. The
    generated file's first line was the word `csharp`, so the C# build failed on
    line 1 and no test in the project could run.
    """
    raw = f"<<<TESTS>>>\n```{fence}\nTHE_REAL_CODE = 1\n```\n<<<END TESTS>>>"
    got = test_mode._parse_tests(raw)
    assert got == "THE_REAL_CODE = 1", got
    assert fence not in got or fence == ""


def test_an_unfenced_block_is_still_accepted():
    raw = "<<<TESTS>>>\nTHE_REAL_CODE = 1\n<<<END TESTS>>>"
    assert test_mode._parse_tests(raw) == "THE_REAL_CODE = 1"


def test_backticks_inside_the_code_survive():
    raw = "<<<TESTS>>>\n```csharp\nvar s = \"a ``` b\";\n```\n<<<END TESTS>>>"
    got = test_mode._parse_tests(raw)
    assert "a ``` b" in got


def test_a_missing_block_is_none():
    assert test_mode._parse_tests("no block here") is None
